#!/usr/bin/env python3
"""The Nova create-window arm's instruments and its verdict.

QUESTION (it is the openstack-ironic-operator-kog session's, not ours: Ironic's
POST /v1/nodes is immediately consistent, so their lab cannot produce the window;
Nova's POST /servers returns 202 with the server in BUILD, so ours can):

  Q1  does a freshly-created Nova server ever answer a GET with 404 before it
      answers with BUILD (and does it ever vanish after being seen)?
  Q2  does the Krateo CR's Ready condition go True -> not-True around a create?

VERDICT VOCABULARY, the repo's own, and a question not asked is never a pass:
  measured  read, and the answer is in the record
  UNREAD    the instrument did not produce a reading; says nothing about the world
  ABSENT    read successfully and the thing is not there
  VOID      a precondition failed, so nothing was measured; NEVER a pass

SUBCOMMANDS
  draw-check <run-dir> <n>    classify ONE draw now, print one line (the live
                              per-draw read; the verdict is still `analyse`)
  rules                       print the discard rules (also the JSON stored in
                              run.json BEFORE draw 1, which is the point)
  poll      <args>            vendor-side poller for ONE draw -> poll.jsonl
  control404 <args>           GET a random uuid; Nova MUST answer 404 or the
                              instrument cannot see a 404 and the run is VOID
  teardown  --run-dir D --prefix P   delete, then PROVE by reading the vendor
  count     --prefix P        servers under the prefix; UNREAD (rc 2) is not zero
  run-json  FILE k=v ...      merge keys into run.json
  analyse   <run-dir>         the verdict (exit codes below)
  --self-test                 offline, against a stub Nova on 127.0.0.1

PARKED, and a DESIGN NOTE. The arm is not runnable today (no cloud credential is
settled in CI; see docs/e2e.md). It classifies ONE controller pass from the
controller's own log (classify_pass) instead of building a latency distribution:
one pass is decisive, so min_usable_draws is 1, not the 3 an earlier design
needed. The ceiling of 5 draws stays, for replicates, and a caller can set
min_usable_draws higher in the rules recorded before the draws.

EXIT CODES of `analyse` (usage failures are 64, never 2, because 2 is VOID and an
argparse-style usage error sharing it makes a mistyped flag read as "nothing was
measured"):
  0  no finding in the usable draw(s) (read the PASS classification: a no-flap
     means something only where a hint pass was seen)
  1  a FINDING in a usable draw (Q2 Ready left True by either instrument, Q1 404
     before BUILD, or a BURST of hint passes)
  2  VOID: a precondition failed (control, rules, Synced, credential...)
  3  REFUSED: fewer usable draws than min_usable_draws (1 by default), and every
     discard was contamination
  4  TEARDOWN FAILED: the run cannot prove it deleted what it created.
     Highest precedence. A run that cannot prove the bill stopped FAILED.
  64 usage

THE DISCARD RULES ARE FIXED BEFORE THE DRAWS (see RULES, stored in run.json by the
orchestrator before draw 1, and read back by `analyse` from there; a run.json
without them is VOID). They are:
  host-load          max 1-minute load during the draw > load_limit
  host-state-unread  fewer than 3 host samples (unread is not clean)
  cloud-degraded     any 5xx/429/transport error, or any probe slower than
                     cloud_latency_limit_s, from the poller during the draw
  cloud-state-unread no poll record at all
  window-incomplete  less than window_s - slack of watch after Synced=True
  missed-own-window  THE PER-DRAW NULL. Before the create is spent, meta.json
                     states this draw's expected window: the controller's OWN
                     resyncInterval (read from its startup line, e.g.
                     "resyncInterval":180000000000 ns) plus ready_tolerance_s.
                     The draw is contaminated on the spot if Ready first became
                     True later than external-create-succeeded + that window.
                     Both ends are apiserver-STORED times, so no clock mixing.
                     (If the interval could not be read the draw is NOT
                     discarded for it; it is flagged resync-unread and the
                     host-load rule is all that is left.)
  reader-unclean     the watch reader (hack/capture-watch-stream.py, vendored)
                     did not end on a clean EOF, has no summary, or left bytes
  relist             `probable_relists` > 0: the record cannot support an
                     ABSENCE claim (a flap already seen still counts
                     in `discarded_findings`, reported, not used for the exit)
  never-ready        Ready was never True: there is nothing to leave
VOID-class reasons (also make the draw unusable): synced-false, synced-never.
`load_limit` is a number CHOSEN for a 2-4 vCPU runner and NOT MEASURED; the first
dispatch records the loads so it can be set from data.
"""
import datetime
import http.server
import importlib.util
import json
import os
import random
import re
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

EXIT_OK, EXIT_FINDING, EXIT_VOID, EXIT_REFUSED, EXIT_TEARDOWN, EXIT_USAGE = 0, 1, 2, 3, 4, 64

RULES = {
    "min_usable_draws": 1,
    "hint_s": 15.0,
    "grid_tolerance_s": 2.0,
    "enqueue_tolerance_s": 1.0,
    "max_draws": 5,
    "load_limit": 8.0,
    "cloud_latency_limit_s": 5.0,
    "window_slack_s": 5.0,
    "resync_ceiling_s": 180.0,
    "span_flag_s": 30.0,
    "ready_tolerance_s": 5.0,
    "min_host_samples": 3,
}
CREATE_PREFIX = "krateo.io/external-create-"
PREFIX_RE = re.compile(r"^nova-window-[a-z0-9][a-z0-9-]*-$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def now_iso():
    n = datetime.datetime.now(datetime.timezone.utc)
    return n.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (n.microsecond // 1000)


def load_jsonl(path):
    out = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        out.append({"record": "unparseable", "raw": line[:200]})
    except OSError:
        return None
    return out


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# HTTP. NO REDIRECTS FOLLOWED: this carries the Keystone token, and urllib's
# default handler re-sends custom headers to the redirect target. A 3xx is an
# error here, exactly as scripts/get-token.sh refuses `curl -L`.
# ---------------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class Nova:
    def __init__(self, base, token, timeout=15.0):
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.opener = urllib.request.build_opener(_NoRedirect)

    def req(self, method, path, auth=True, root=False):
        """-> dict(http, latency_s, vendor_date, body|None, err|None). Never raises.
        root=True addresses the ORIGIN's `/` (the version document), not the
        project-scoped base."""
        base = self.base
        if root:
            u = urllib.parse.urlsplit(self.base)
            base = "%s://%s" % (u.scheme, u.netloc)
        r = urllib.request.Request(base + path, method=method)
        if auth:
            r.add_header("X-Auth-Token", self.token)
        r.add_header("Accept", "application/json")
        t0 = time.monotonic()
        out = {"http": None, "latency_s": None, "vendor_date": None, "body": None, "err": None}
        try:
            with self.opener.open(r, timeout=self.timeout) as resp:
                raw = resp.read()
                out["http"] = resp.status
                out["vendor_date"] = resp.headers.get("Date")
        except urllib.error.HTTPError as e:
            out["http"] = e.code
            out["vendor_date"] = e.headers.get("Date") if e.headers else None
            try:
                raw = e.read()
            except Exception:  # noqa: BLE001
                raw = b""
        except Exception as e:  # noqa: BLE001 -- transport error is data
            out["err"] = "%s: %s" % (type(e).__name__, e)
            out["latency_s"] = round(time.monotonic() - t0, 3)
            return out
        out["latency_s"] = round(time.monotonic() - t0, 3)
        try:
            out["body"] = json.loads(raw) if raw else None
        except ValueError:
            out["err"] = "unparseable body"
        return out


def list_servers(nova):
    """-> (ok, servers, why). ok is False for ANYTHING short of a complete read,
    so 'empty' can never be an artefact of a failed or truncated read."""
    r = nova.req("GET", "/servers/detail?limit=1000")
    if r["http"] != 200 or not isinstance(r["body"], dict) or "servers" not in r["body"]:
        return False, [], "list read failed: http=%s err=%s" % (r["http"], r["err"])
    if r["body"].get("servers_links"):
        return False, [], "list is PAGINATED (servers_links present): not a complete read"
    return True, r["body"]["servers"], ""


# ---------------------------------------------------------------------------
# poll: the vendor-side instrument for ONE draw.
# ---------------------------------------------------------------------------
def poll(nova, name, out_path, id_file=None, interval=0.5, deadline_s=300.0,
         stop_file=None, sleep=time.sleep):
    t0 = time.monotonic()
    stop = {"flag": None}

    def on_sig(signum, _f):
        stop["flag"] = signal.Signals(signum).name
    signal.signal(signal.SIGTERM, on_sig)
    signal.signal(signal.SIGINT, on_sig)

    n = 0
    rec_all = []

    def emit(rec):
        nonlocal n
        n += 1
        rec = dict(rec, n=n, client_wall=now_iso(), client_t=round(time.monotonic() - t0, 3))
        rec_all.append(rec)
        with open(out_path, "a") as f:
            f.write(json.dumps(rec) + "\n")

    emit({"record": "header",
          "bases": {"client_wall": "poller client clock", "client_t": "poller client "
                    "monotonic seconds since poller start (same instant)",
                    "vendor_date": "the vendor's own Date header, 1s resolution; a "
                    "THIRD clock, not comparable to the other two"},
          "name": name, "interval_s": interval})
    root = nova.req("GET", "/", auth=False, root=True)
    emit({"record": "cloud", "phase": "start", "probe": "GET /", "http": root["http"],
          "latency_s": root["latency_s"], "vendor_date": root["vendor_date"], "err": root["err"]})

    sid, id_source, last_status, terminal_hits = None, None, None, 0
    while time.monotonic() - t0 < deadline_s and not stop["flag"]:
        if stop_file and os.path.exists(stop_file):
            stop["flag"] = "stop-file"
            break
        if sid is None and id_file and os.path.exists(id_file):
            try:
                cand = open(id_file).read().strip()
            except OSError:
                cand = ""
            if cand:
                sid, id_source = cand, "cr-status"
                emit({"record": "id", "id": sid, "id_source": id_source})
        if sid is None:
            ok, servers, why = list_servers(nova)
            hit = [s for s in servers if s.get("name") == name] if ok else []
            emit({"record": "probe", "kind": "list", "http": 200 if ok else None,
                  "matches": len(hit), "ok": ok, "why": why})
            if hit:
                sid, id_source = hit[0].get("id"), "list"
                emit({"record": "id", "id": sid, "id_source": id_source})
        else:
            r = nova.req("GET", "/servers/%s" % sid)
            srv = (r["body"] or {}).get("server") if isinstance(r["body"], dict) else None
            last_status = (srv or {}).get("status")
            emit({"record": "probe", "kind": "byid", "id": sid, "http": r["http"],
                  "status": last_status, "latency_s": r["latency_s"],
                  "vendor_date": r["vendor_date"], "err": r["err"]})
            if last_status in ("ACTIVE", "ERROR"):
                terminal_hits += 1
                if terminal_hits >= 3:
                    stop["flag"] = "terminal-status"
                    break
        sleep(interval)
    root = nova.req("GET", "/", auth=False, root=True)
    emit({"record": "cloud", "phase": "end", "probe": "GET /", "http": root["http"],
          "latency_s": root["latency_s"], "vendor_date": root["vendor_date"], "err": root["err"]})
    emit({"record": "summary", "probes": n, "id": sid, "id_source": id_source,
          "last_status": last_status, "exit_reason": stop["flag"] or "deadline"})
    return 0


def control404(nova, out_path):
    fake = str(uuid.uuid4())
    r = nova.req("GET", "/servers/%s" % fake)
    rec = {"record": "control404", "id": fake, "http": r["http"], "err": r["err"],
           "client_wall": now_iso(), "sees_a_404": r["http"] == 404}
    with open(out_path, "w") as f:
        json.dump(rec, f)
    print(json.dumps(rec))
    return 0 if rec["sees_a_404"] else EXIT_VOID


# ---------------------------------------------------------------------------
# teardown: delete, then PROVE by reading the vendor. A delete call returning is
# not the proof. The proof is: a COMPLETE list read (200, not paginated) holds no
# server under this run's prefix, AND each id the run ever saw answers 404.
# ---------------------------------------------------------------------------
def teardown(nova, prefix, seen_ids, creates_issued, out_path, deadline_s=300.0,
             interval=5.0, delete=True, sleep=time.sleep):
    if not PREFIX_RE.match(prefix or ""):
        # An EMPTY or short prefix matches every server in the project, and this
        # function deletes what matches. Refuse before any request is made.
        msg = ("REFUSED: prefix %r is not this arm's own nova-window-<id>- shape; "
               "deleting by it could delete servers that are not ours" % (prefix,))
        sys.stderr.write(msg + "\n")
        return EXIT_USAGE
    t0 = time.monotonic()
    deleted, why, remaining, byid = [], "", [], {}
    proven = False
    ever_seen = set(seen_ids)
    while True:
        ok, servers, lwhy = list_servers(nova)
        mine = [s for s in servers if str(s.get("name", "")).startswith(prefix)] if ok else []
        for s in mine:
            ever_seen.add(s.get("id"))
        if ok and delete:
            for s in mine:
                if not str(s.get("name", "")).startswith(prefix):
                    continue  # belt and braces: never DELETE what is not ours
                if s.get("id") not in deleted:
                    r = nova.req("DELETE", "/servers/%s" % s["id"])
                    deleted.append(s["id"])
                    if r["http"] not in (200, 202, 204, 404):
                        why = "DELETE %s answered http=%s err=%s" % (s["id"], r["http"], r["err"])
        byid = {}
        for i in sorted(x for x in ever_seen if x):
            byid[i] = nova.req("GET", "/servers/%s" % i)["http"]
        remaining = [s.get("id") for s in mine]
        gone = all(code == 404 for code in byid.values())
        if ok and not mine and gone:
            proven = True
            why = ""
            break
        if not ok:
            why = lwhy
        elif mine:
            why = "still listed under prefix: %s" % ",".join(str(x) for x in remaining)
        elif not gone:
            why = "id(s) not yet 404: %s" % {k: v for k, v in byid.items() if v != 404}
        if time.monotonic() - t0 >= deadline_s:
            break
        sleep(interval)
    blind = False
    if proven and creates_issued > 0 and not ever_seen:
        # A create was issued and this run never saw ONE server under the prefix.
        # An empty list from an instrument that never saw anything proves nothing.
        proven, blind = False, True
        why = ("%d create(s) were issued but the instrument never saw a server "
               "under the prefix, so an empty list is NOT proof of deletion" % creates_issued)
    rec = {"record": "teardown", "proven": proven, "why": why, "prefix": prefix,
           "creates_issued": creates_issued, "ids_ever_seen": sorted(x for x in ever_seen if x),
           "deleted_by_me": deleted, "still_listed": remaining, "byid_http": byid,
           "blind_instrument": blind, "client_wall": now_iso()}
    with open(out_path, "w") as f:
        json.dump(rec, f, indent=2)
    print(json.dumps(rec))
    return 0 if proven else EXIT_TEARDOWN


# ---------------------------------------------------------------------------
# analysis. PURE functions below; each is mutation-tested in BOTH directions.
# ---------------------------------------------------------------------------
def cond(obj, ctype):
    for c in ((obj or {}).get("status") or {}).get("conditions") or []:
        if isinstance(c, dict) and c.get("type") == ctype:
            return c
    return None


def events_of(recs):
    return [r for r in (recs or []) if isinstance(r, dict) and r.get("record") == "event"]


def synced_state(evs):
    """-> (state, t_synced, detail). state: ok | false | never.
    A transient False before the first True is NOT a void (reported by the
    caller); False is void only if True is never reached."""
    first_ok, last = None, None
    false_before = False
    for r in evs:
        c = cond((r.get("event") or {}).get("object"), "Synced")
        if not c:
            continue
        last = c
        if c.get("status") == "True" and c.get("reason") == "ReconcileSuccess":
            first_ok = r
            break
        if c.get("status") == "False":
            false_before = True
    if first_ok is not None:
        return "ok", first_ok.get("delivered_t"), {"synced_false_seen_before_true": false_before}
    if last and last.get("status") == "False":
        return "false", None, {"reason": last.get("reason"), "message": last.get("message")}
    return "never", None, {"last": (last or {}).get("status")}


def ready_sequence(evs, t_from):
    """Ready statuses from t_from on, the status IN FORCE at t_from first.
    -> list of (delivered_t, status), one entry per CHANGE."""
    state, seq = None, []
    for r in evs:
        c = cond((r.get("event") or {}).get("object"), "Ready")
        t = r.get("delivered_t")
        if c is None or t is None:
            continue
        st = c.get("status")
        if t < t_from:
            state = st
            continue
        if not seq:
            seq.append((t_from, state if state is not None else st))
            state = seq[0][1]
        if st != state:
            seq.append((t, st))
            state = st
    if state is not None and not seq:
        seq.append((t_from, state))
    return seq


def left_true(seq):
    """Transitions from True to anything else. THE flap observable (Q2)."""
    out = []
    for (t0, a), (t1, b) in zip(seq, seq[1:]):
        if a == "True" and b != "True":
            out.append({"from": a, "to": b, "at_delivered_t": t1})
    return out


def create_annotations(final_cr, evs):
    """-> (dict name->value | None, source). None means UNREAD, {} means ABSENT."""
    if isinstance(final_cr, dict):
        ann = ((final_cr.get("metadata") or {}).get("annotations")) or {}
        return {k: v for k, v in ann.items() if k.startswith(CREATE_PREFIX)}, "final-cr.json"
    best = None
    for r in evs:
        ann = ((((r.get("event") or {}).get("object") or {}).get("metadata") or {})
               .get("annotations")) or {}
        got = {k: v for k, v in ann.items() if k.startswith(CREATE_PREFIX)}
        if got:
            best = got
    if best is not None:
        return best, "event-stream"
    return None, None


def _parse_z(s):
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def create_span(anns):
    """The SPAN = succeeded - pending, in apiserver-stored seconds. Not the marker,
    not the interval. -> (seconds | None, note)."""
    if anns is None:
        return None, "UNREAD: no object was read"
    if not anns:
        return None, "ABSENT: the object was read and has no external-create-* annotation"
    p = _parse_z(anns.get(CREATE_PREFIX + "pending"))
    s = _parse_z(anns.get(CREATE_PREFIX + "succeeded"))
    if p is None or s is None:
        miss = [k for k in ("pending", "succeeded") if _parse_z(anns.get(CREATE_PREFIX + k)) is None]
        return None, "UNREAD: %s absent or unparseable among %s" % (",".join(miss), sorted(anns))
    return int((s - p).total_seconds()), "measured"


def q1_reading(poll_recs):
    """-> dict(state, ...). Reads ONLY the poller's record."""
    if poll_recs is None:
        return {"state": "UNREAD", "why": "no poll record"}
    probes = [r for r in poll_recs if r.get("record") == "probe"]
    byid = [r for r in probes if r.get("kind") == "byid"]
    lists = [r for r in probes if r.get("kind") == "list"]
    ids = [r for r in poll_recs if r.get("record") == "id"]
    if not byid:
        return {"state": "UNREAD", "why": "the poller never learned a server id, so no by-id GET "
                "was ever issued", "list_probes": len(lists)}
    first200 = next((i for i, r in enumerate(byid) if r.get("http") == 200), None)
    before = [r for r in (byid if first200 is None else byid[:first200]) if r.get("http") == 404]
    after = [] if first200 is None else [r for r in byid[first200:] if r.get("http") == 404]
    seq, last = [], None
    for r in byid:
        if r.get("status") != last and r.get("status") is not None:
            seq.append(r["status"])
            last = r["status"]
    state = ("SEEN-404-BEFORE-BUILD" if before else
             "SEEN-VANISHED-AFTER-VISIBLE" if after else
             "NOT-SEEN" if first200 is not None else "UNREAD")
    return {"state": state, "id_source": (ids[0].get("id_source") if ids else None),
            "byid_probes": len(byid), "first_200_at_probe": first200,
            "404_before_first_200": len(before), "404_after_first_200": len(after),
            "status_sequence": seq,
            "why": "" if state != "UNREAD" else "no by-id probe ever answered 200"}


def ready_true_stored(evs):
    """First Ready=True lastTransitionTime found in the stream: an APISERVER-stored
    time, comparable with the external-create-* annotations and with nothing else."""
    for r in evs:
        c = cond((r.get("event") or {}).get("object"), "Ready")
        if c and c.get("status") == "True":
            z = _parse_z(c.get("lastTransitionTime"))
            if z is not None:
                return z
    return None


def own_window(meta, anns, evs, rules):
    """THE PER-DRAW NULL. -> dict(state, ...). Stated from meta.json, which the
    orchestrator wrote BEFORE the create was issued."""
    r = meta.get("resync_s")
    if not isinstance(r, (int, float)) or r <= 0:
        return {"state": "resync-unread",
                "why": "the controller's resyncInterval was not read, so no per-draw window "
                       "could be stated; the host-load rule is all that is left"}
    expected = r + rules["ready_tolerance_s"]
    succ = _parse_z((anns or {}).get(CREATE_PREFIX + "succeeded"))
    ready = ready_true_stored(evs)
    if succ is None or ready is None:
        return {"state": "unread", "expected_by_s": expected, "resync_s": r,
                "why": "succeeded=%s ready_true=%s" % (succ, ready)}
    delta = int((ready - succ).total_seconds())
    return {"state": "inside" if delta <= expected else "MISSED", "resync_s": r,
            "expected_by_s": expected, "ready_true_minus_succeeded_s": delta,
            "bases": "apiserver-stored minus apiserver-stored (1s resolution)"}


def classify_draw(d, rules):
    """-> dict. d keys: meta, events (list|None), final_cr, host (list|None),
    poll (list|None). Never raises on missing input: absence is a reading."""
    discards, voids = [], []
    meta = d.get("meta") or {}
    recs = d.get("events")
    evs = events_of(recs)
    summ = next((r for r in reversed(recs or []) if r.get("record") == "summary"), None)
    out = {"draw": meta.get("draw"), "name": meta.get("name")}

    # --- the reader's own account of itself (requirement 5) ----------------
    if recs is None or summ is None:
        discards.append(("reader-unclean", "no summary record: the reader's file simply ended"))
    else:
        if not summ.get("stdin_clean_eof") or summ.get("leftover_bytes"):
            discards.append(("reader-unclean", "stdin_clean_eof=%s leftover_bytes=%s exit_reason=%s"
                             % (summ.get("stdin_clean_eof"), summ.get("leftover_bytes"),
                                summ.get("exit_cause"))))
        if summ.get("probable_relists"):
            discards.append(("relist", "%d second ADDED for an already-seen uid"
                             % summ["probable_relists"]))
    out["reader"] = None if summ is None else {k: summ.get(k) for k in (
        "events", "by_type", "probable_relists", "stdin_clean_eof", "leftover_bytes", "exit_cause")}

    # --- precondition (requirement 1): the create SUCCEEDED ----------------
    st, t_synced, detail = synced_state(evs)
    out["synced"] = {"state": st, **detail}
    if st == "false":
        voids.append(("synced-false", "Synced=False reason=%s message=%s"
                      % (detail.get("reason"), detail.get("message"))))
    elif st == "never":
        voids.append(("synced-never", "Synced never reached True/ReconcileSuccess (last=%s)"
                      % detail.get("last")))

    # --- Q2 -----------------------------------------------------------------
    flap, seq = [], []
    if st == "ok":
        seq = ready_sequence(evs, t_synced)
        flap = left_true(seq)
        if not any(s == "True" for _, s in seq):
            discards.append(("never-ready", "Ready was never True after Synced; sequence=%s"
                             % [s for _, s in seq]))
        end_t = (summ or {}).get("window_s")   # reader-monotonic, same base as delivered_t
        need = (meta.get("window_s") or 0) - rules["window_slack_s"]
        covered = None if end_t is None else round(end_t - t_synced, 1)
        out["window"] = {"needed_s": need, "covered_s": covered,
                         "bases": "delivered_t, reader-client-monotonic; both ends"}
        if covered is None or covered < need:
            discards.append(("window-incomplete", "watched %ss after Synced, needed >= %ss"
                             % (covered, need)))
    out["ready_sequence"] = [{"delivered_t": t, "status": s} for t, s in seq]
    out["ready_status_changes"] = max(len(seq) - 1, 0)
    out["left_true"] = flap

    # --- the create annotations and their SPAN (requirement 2) --------------
    anns, src = create_annotations(d.get("final_cr"), evs)
    span, note = create_span(anns)
    out["create_annotations"] = {"source": src, "values": anns,
                                 "bases": "apiserver-stored; compared with each other only",
                                 "span_s": span, "span_note": note}
    out["flags"] = []
    if span is not None and span > rules["span_flag_s"]:
        out["flags"].append("create-span-%ds-exceeds-%ds" % (span, rules["span_flag_s"]))
    if detail.get("synced_false_seen_before_true"):
        out["flags"].append("synced-false-seen-before-true")

    # --- the per-draw window, stated before the create (requirement 6) ------
    ow = own_window(meta, anns, evs, rules)
    out["own_window"] = ow
    if ow["state"] == "MISSED":
        discards.append(("missed-own-window", "Ready first True %ss after create-succeeded; this draw's "
                         "window was resync %ss + %ss tolerance = %ss"
                         % (ow["ready_true_minus_succeeded_s"], ow["resync_s"],
                            rules["ready_tolerance_s"], ow["expected_by_s"])))
    elif ow["state"] in ("resync-unread", "unread"):
        out["flags"].append("own-window-" + ow["state"])
    # --- the PASS CLASSIFICATION: was the mechanism under test exercised? ----
    pc = classify_pass(d.get("controller_log"), meta.get("name"), rules)
    out["pass"] = pc
    if pc["state"] in ("UNREAD", "UNREAD-GRID"):
        voids.append(("pass-unread", "the controller's pass could not be classified (%s): whether the "
                      "re-observe mechanism fired is unknown, so 'no flap' would say nothing: %s"
                      % (pc["state"], pc.get("why"))))
    elif pc["state"] == "STARVED":
        discards.append(("starved-pass", pc["why"]))
    elif pc["state"] == "BURST":
        out["flags"].append("hint-burst")
    # --- the second, independent flap instrument (vendored watch-condition-flap)
    fw = d.get("flapwatch")
    if fw is None or fw.get("rc") is None:
        out["flapwatch"] = {"state": "UNREAD"}
        out["flags"].append("flapwatch-unread")
    else:
        names = {0: "NO FLAP", 1: "FLAP", 2: "VOID"}
        out["flapwatch"] = {"state": names.get(fw["rc"], "unexpected-rc-%s" % fw["rc"]), "rc": fw["rc"]}
        if fw["rc"] == 2:
            out["flags"].append("flapwatch-void")
        if (fw["rc"] == 1) != bool(flap):
            out["flags"].append("instruments-disagree(capture-left-true=%s flapwatch=%s)"
                                % (bool(flap), out["flapwatch"]["state"]))
    out["flap"] = bool(flap) or out["flapwatch"]["state"] == "FLAP"

    # --- host and cloud state beside the draw (requirement 6) ---------------
    host = d.get("host")
    if host is None or len([h for h in host if "load1" in h]) < rules["min_host_samples"]:
        discards.append(("host-state-unread", "%s host samples, need %d"
                         % (0 if host is None else len(host), rules["min_host_samples"])))
        out["host"] = {"state": "UNREAD"}
    else:
        loads = [h["load1"] for h in host if "load1" in h]
        out["host"] = {"state": "measured", "samples": len(loads), "load1_max": max(loads),
                       "load1_min": min(loads), "nproc": host[0].get("nproc"),
                       "load_limit": rules["load_limit"]}
        if max(loads) > rules["load_limit"]:
            discards.append(("host-load", "1-min load peaked at %.2f > limit %.2f"
                             % (max(loads), rules["load_limit"])))
    pol = d.get("poll")
    if pol is None:
        discards.append(("cloud-state-unread", "no poll record"))
        out["cloud"] = {"state": "UNREAD"}
    else:
        bad = [r for r in pol if r.get("record") in ("probe", "cloud") and (
            r.get("err") or (isinstance(r.get("http"), int) and (r["http"] >= 500 or r["http"] == 429))
            or (r.get("record") == "cloud" and r.get("http") is None)
            or (r.get("record") == "probe" and r.get("kind") == "list" and r.get("ok") is False))]
        slow = [r for r in pol if r.get("latency_s") is not None
                and r["latency_s"] > rules["cloud_latency_limit_s"]]
        lat = [r["latency_s"] for r in pol if r.get("latency_s") is not None]
        out["cloud"] = {"state": "measured", "probes": len(lat),
                        "latency_max_s": max(lat) if lat else None, "errors_or_5xx_429": len(bad),
                        "slow_probes": len(slow), "latency_limit_s": rules["cloud_latency_limit_s"]}
        if bad or slow:
            discards.append(("cloud-degraded", "%d error/5xx/429 probe(s), %d slower than %ss"
                             % (len(bad), len(slow), rules["cloud_latency_limit_s"])))
    out["q1"] = q1_reading(pol)

    out["void"] = [{"code": c, "detail": x} for c, x in voids]
    out["discards"] = [{"code": c, "detail": x} for c, x in discards]
    out["usable"] = not voids and not discards
    return out


def _ver(v):
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (0, 0, 0)


def read_text(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def read_flapwatch(p):
    try:
        rc = int(open(os.path.join(p, "flapwatch.rc")).read().strip())
    except (OSError, ValueError):
        return None
    return {"rc": rc}


def analyse_run(run_dir):
    """-> (exit_code, report dict). All reading of files happens here."""
    rep = {"run_dir": run_dir, "draws": [], "notes": []}
    run = load_json(os.path.join(run_dir, "run.json"))
    td = load_json(os.path.join(run_dir, "teardown.json"))
    ctl = load_json(os.path.join(run_dir, "control404.json"))
    rules = (run or {}).get("rules")
    rep["rules"] = rules
    rep["run"] = {k: (run or {}).get(k) for k in ("oasgen", "draws_requested", "draws_started",
                                                    "run_id", "cap_breach")}
    # 1. teardown outranks everything
    rep["teardown"] = td
    if td is None:
        rep["verdict"] = "TEARDOWN UNPROVEN: no teardown.json -- the run cannot prove it deleted anything"
        return EXIT_TEARDOWN, rep
    if not td.get("proven"):
        rep["verdict"] = "TEARDOWN FAILED: %s" % td.get("why")
        return EXIT_TEARDOWN, rep
    if (run or {}).get("cap_breach"):
        rep["verdict"] = "CAP BREACH: %s" % run["cap_breach"]
        return EXIT_TEARDOWN, rep
    # 2. run-level VOID
    if run is None or not isinstance(rules, dict) or not all(k in rules for k in RULES):
        rep["verdict"] = ("VOID: the discard rules were not recorded in run.json BEFORE the draws, "
                          "so they cannot be shown not to have been chosen afterwards")
        return EXIT_VOID, rep
    if ctl is None or ctl.get("sees_a_404") is not True:
        rep["verdict"] = ("VOID: the 404 control did not see a 404 (%s) -- an instrument that cannot "
                          "see a 404 cannot report its absence" % (ctl,))
        return EXIT_VOID, rep
    draws = sorted(d for d in os.listdir(run_dir) if d.startswith("draw-")) if os.path.isdir(run_dir) else []
    for dn in draws:
        p = os.path.join(run_dir, dn)
        d = {"meta": load_json(os.path.join(p, "meta.json")) or {"draw": dn},
             "events": load_jsonl(os.path.join(p, "events.jsonl")),
             "final_cr": load_json(os.path.join(p, "final-cr.json")),
             "host": load_jsonl(os.path.join(p, "host.jsonl")),
             "poll": load_jsonl(os.path.join(p, "poll.jsonl")),
             "flapwatch": read_flapwatch(p), "controller_log": read_text(os.path.join(p, "controller.log"))}
        d["meta"].setdefault("draw", dn)
        rep["draws"].append(classify_draw(d, rules))
    usable = [x for x in rep["draws"] if x["usable"]]
    rep["usable"], rep["discarded"] = len(usable), len(rep["draws"]) - len(usable)
    if run.get("draws_started") is not None and run["draws_started"] != len(rep["draws"]):
        rep["notes"].append("draws_started=%s but %d draw directories exist"
                            % (run["draws_started"], len(rep["draws"])))
    rep["discarded_findings"] = [
        {"draw": x["draw"], "left_true": x["left_true"], "flapwatch": x["flapwatch"]["state"],
         "q1": x["q1"]["state"],
         "why_discarded": [e["code"] for e in x["discards"] + x["void"]]}
        for x in rep["draws"] if not x["usable"]
        and (x["flap"] or x["q1"]["state"].startswith("SEEN"))]
    if len(usable) < rules["min_usable_draws"]:
        any_void = any(x["void"] for x in rep["draws"])
        rep["verdict"] = ("%s: %d usable draw(s) of %d, need >= %d. NOT REPORTING a result."
                          % ("VOID" if any_void else "REFUSED", len(usable), len(rep["draws"]),
                             rules["min_usable_draws"]))
        return (EXIT_VOID if any_void else EXIT_REFUSED), rep
    q2 = [x for x in usable if x["flap"]]
    burst = [x for x in usable if x["pass"]["state"] == "BURST"]
    q1 = [x for x in usable if x["q1"]["state"].startswith("SEEN")]
    q1_unread = [x for x in usable if x["q1"]["state"] == "UNREAD"]
    n = len(usable)
    states = sorted({x["pass"]["state"] for x in usable})
    rep["pass"] = "pass classification: %s" % ", ".join(states)
    if (run.get("oasgen") and _ver(run["oasgen"]) >= (0, 99, 0)
            and all(x["pass"]["state"] == "NO-HINT-PASS" for x in usable)):
        rep["notes"].append("oasgen %s is at or above 0.99.0, where a hint pass is expected, and none was "
                            "seen: ABSENT at a version that should carry it" % run["oasgen"])
    if q2:
        rep["q2"] = "FLAP in %d of %d usable draws" % (len(q2), n)
    elif n == 1:
        rep["q2"] = ("NO FLAP in the single classified pass (%s). One draw is a per-pass classification, "
                     "not a rate" % states[0])
    else:
        rep["q2"] = ("NO FLAP in %d usable draws (bounds the per-create rate near 3/%d at 95%%; "
                     "does not prove impossibility)" % (n, n))
    rep["q1"] = ("404 before BUILD / vanish in %d of %d usable draws" % (len(q1), n) if q1 else
                 ("NOT SEEN in %d of %d usable draws; UNREAD in %d" % (n - len(q1_unread), n, len(q1_unread))
                  if q1_unread else "NOT SEEN in %d usable draws" % n))
    if burst and not (q2 or q1):
        rep["verdict"] = "FINDING: %d hint passes for one object in one window (BURST); see the pass record" % len(burst)
        return EXIT_FINDING, rep
    if q2 or q1:
        rep["verdict"] = "FINDING: Q2 %s | Q1 %s" % (rep["q2"], rep["q1"])
        return EXIT_FINDING, rep
    rep["verdict"] = "NO FLAP: Q2 %s | Q1 %s | %s" % (rep["q2"], rep["q1"], rep["pass"])
    return EXIT_OK, rep


def render(rep, code):
    L = []
    L.append("== nova create-window arm ==")
    r = rep.get("rules")
    L.append("DISCARD RULES (recorded in run.json before draw 1): %s" % json.dumps(r, sort_keys=True))
    L.append("run: %s" % json.dumps(rep.get("run"), sort_keys=True))
    L.append("TIME BASES: delivered_wall/delivered_t = the reader's own clock when it printed "
             "the event; stored_times/annotations = the apiserver's clock. Spans below are "
             "stored-vs-stored only. No normalised timeline is built.")
    for x in rep.get("draws", []):
        L.append("")
        L.append("draw %s  name=%s  %s" % (x["draw"], x["name"],
                 "USABLE" if x["usable"] else "DISCARDED"))
        for e in x["void"]:
            L.append("  VOID-class   %s: %s" % (e["code"], e["detail"]))
        for e in x["discards"]:
            L.append("  discarded    %s: %s" % (e["code"], e["detail"]))
        L.append("  synced       %s" % json.dumps(x["synced"]))
        L.append("  own window   %s" % json.dumps(x["own_window"]))
        pz = dict(x["pass"])
        pz["passes"] = ["%s objs=%s gap=%ss on_grid=%s" % (r["kind"], r["objects"], r["gap_s"], r["on_grid"])
                        for r in pz.get("passes", [])]
        L.append("  PASS         %s" % json.dumps(pz))
        L.append("  flapwatch    %s  (vendored watch-condition-flap.py, independent of the capture)"
                 % json.dumps(x["flapwatch"]))
        L.append("  ready        sequence=%s status_changes=%d left_true=%s" % (
            [s["status"] for s in x["ready_sequence"]], x["ready_status_changes"],
            json.dumps(x["left_true"])))
        ca = x["create_annotations"]
        L.append("  create ann   source=%s values=%s" % (ca["source"], json.dumps(ca["values"], sort_keys=True)))
        L.append("  create SPAN  %s s (%s)  [apiserver-stored, succeeded minus pending]" % (
            ca["span_s"], ca["span_note"]))
        L.append("  host         %s" % json.dumps(x["host"]))
        L.append("  cloud        %s" % json.dumps(x["cloud"]))
        L.append("  Q1           %s" % json.dumps(x["q1"]))
        if x["flags"]:
            L.append("  flags        %s" % ", ".join(x["flags"]))
        if x.get("reader"):
            L.append("  reader       %s" % json.dumps(x["reader"]))
    if rep.get("discarded_findings"):
        L.append("")
        L.append("FINDINGS IN DISCARDED DRAWS (reported, NOT counted toward the exit code):")
        for f in rep["discarded_findings"]:
            L.append("  %s" % json.dumps(f))
    for n in rep.get("notes", []):
        L.append("note: %s" % n)
    L.append("")
    L.append("teardown: %s" % json.dumps(rep.get("teardown")))
    L.append("VERDICT (exit %d): %s" % (code, rep["verdict"]))
    L.append("NOT ESTABLISHED, whatever the verdict: this is a handful of draws on one cloud at "
             "one oasgen version. Absence bounds a rate; it does not prove impossibility.")
    return "\n".join(L)


def _cadence():
    """The vendored hack/resync-cadence.py as a library (its log parsing, not a copy)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resync-cadence.py")
    if not os.path.exists(path):
        return None
    spec = importlib.util.spec_from_file_location("resync_cadence", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def classify_pass(text, name, rules):
    """CLASSIFY THE PASS (one pass is decisive; there is no sampling here).

    Reads the controller's own log. Every pass line carries `queuedAt`, the
    ENQUEUE clock. Per pass that touches our CR:
      gap     = first line - queuedAt            (queue latency; hint passes carry
                                                   hint PLUS queue delay, so the test
                                                   is gap >= hint, never a band)
      objects = distinct CRs sharing one queuedAt cluster
      on_grid = queuedAt == startup + k * resyncInterval, k >= 1 (a resync enqueue)
    A pass with gap >= hint_s is
      STARVED  if it covers several objects on one queuedAt (starvation delays a
               WHOLE pass), or is a single-object pass that sits ON the resync grid
               (a resync pass that waited); neither can be a hint
      HINT     only if it covers exactly ONE object AND its queuedAt is OFF the grid,
               i.e. between two resync enqueues
    The grid is anchored on the startup line and is only trusted once a pass of
    ours is seen ON it (grid_validated); a large-gap off-grid pass with an unvalidated
    grid is UNREAD-GRID, not HINT.
    At most one hint is expected per object per window (core-provider drops a
    hint inside a pending one), so more than one is BURST: something else.
    -> dict(state in HINT|NO-HINT-PASS|STARVED|BURST|UNREAD|UNREAD-GRID, ...)."""
    C = _cadence()
    if C is None:
        return {"state": "UNREAD", "why": "hack/resync-cadence.py is not present to parse the log"}
    if not text or not text.strip():
        return {"state": "UNREAD", "why": "no controller log"}
    recs, counts = C.read_records(text)
    starts = C.startup_info(recs)
    den = {"total_lines": counts["total_lines"], "unparsed_lines": counts["unparsed_lines"]}
    if not starts:
        return {"state": "UNREAD", "denominator": den,
                "why": "no startup line with resyncInterval in the log window, so the grid, and "
                       "with it the difference between a hint and a starved pass, is unknown"}
    st = starts[-1]
    S = C.parse_ts_ns(st.get("timestamp"))
    R = st["resync_s"]
    if S is None or not R:
        return {"state": "UNREAD", "denominator": den, "why": "startup line has no usable timestamp/resync"}
    items, no_q = [], 0
    for r in recs:
        if "queuedAt" not in r:
            no_q += 1
            continue
        q = C.parse_ts_ns(r.get("queuedAt"))
        if q is not None:
            items.append((q, r))
    den.update({"lines_without_queuedAt": no_q, "lines_with_queuedAt": len(items)})
    NS = C.NS
    rows, hint_ns = [], rules["hint_s"] * NS
    for c in C.cluster_on_enqueue(items, int(rules["enqueue_tolerance_s"] * NS)):
        if not any(r.get("name") == name for _, r in c):
            continue
        objs = len({C.cr_key(r) for _, r in c})
        # One ROW per distinct queuedAt of OURS: a hint enqueued a moment after an
        # event-driven pass can share its tolerance cluster, and a cluster-level gap
        # (min over the cluster) would then hide it behind the earlier pass.
        mine = {}
        for q, r in c:
            if r.get("name") == name:
                mine.setdefault(q, []).append(r)
        for q0, lines in sorted(mine.items()):
            ts = [t for t in (C.parse_ts_ns(r.get("timestamp")) for r in lines) if t is not None]
            gap = (min(ts) - q0) / NS if ts else None
            k = round((q0 - S) / (R * NS))
            on_grid = k >= 1 and abs(q0 - S - k * R * NS) <= rules["grid_tolerance_s"] * NS
            if gap is None:
                kind = "unread"
            elif gap * NS < hint_ns:
                kind = "normal"
            elif objs > 1:
                kind = "starved-shared-queuedAt"
            elif on_grid:
                kind = "starved-resync-pass"
            else:
                kind = "hint"
            rows.append({"queuedAt": C.fmt_ns(q0), "objects": objs, "gap_s": gap, "on_grid": on_grid,
                         "kind": kind})
    hints = [r for r in rows if r["kind"] == "hint"]
    starved = [r for r in rows if r["kind"].startswith("starved")]
    validated = any(r["on_grid"] and r["kind"] == "normal" for r in rows)
    out = {"resync_s": R, "hint_s": rules["hint_s"], "build": st.get("build"), "passes": rows,
           "grid_validated": validated, "denominator": den}
    if hints and not validated:
        out.update(state="UNREAD-GRID", why="a large-gap off-grid pass exists but no pass of ours was "
                   "ever seen ON the resync grid, so the grid anchor is unvalidated and a hint cannot "
                   "be told from a starved pass")
    elif len(hints) > 1:
        out.update(state="BURST", why="%d hint passes for one object in one window; one pending "
                   "recheck per object is expected, so this is something else" % len(hints))
    elif hints:
        out.update(state="HINT", why="")
    elif starved:
        out.update(state="STARVED", why="large-gap pass(es) that cannot be hints: %s"
                   % [r["kind"] for r in starved])
    else:
        out.update(state="NO-HINT-PASS", why="ABSENT: the log was read and no pass waited >= the hint")
    return out


def count_prefix(nova, prefix):
    """-> (count | None, why). None is UNREAD: never read as zero."""
    ok, servers, why = list_servers(nova)
    if not ok:
        return None, why
    return len([s for s in servers if str(s.get("name", "")).startswith(prefix)]), ""


def run_json_set(path, pairs):
    """Merge key=value pairs into a JSON file. Values parse as JSON, else string."""
    cur = load_json(path) if os.path.exists(path) else {}
    cur = cur if isinstance(cur, dict) else {}
    for p in pairs:
        k, _, v = p.partition("=")
        try:
            cur[k] = json.loads(v)
        except ValueError:
            cur[k] = v
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cur, f, indent=2)
    os.replace(tmp, path)


def ids_seen_in_run(run_dir):
    out = set()
    if os.path.isdir(run_dir):
        for d in sorted(os.listdir(run_dir)):
            for r in load_jsonl(os.path.join(run_dir, d, "poll.jsonl")) or []:
                if r.get("record") == "id" and r.get("id"):
                    out.add(r["id"])
    return sorted(out)


# ---------------------------------------------------------------------------
# SELF-TEST. A stub Nova on 127.0.0.1 for the instruments; synthetic run
# directories for the verdict. Mutation results are reported by the PR, not here.
# ---------------------------------------------------------------------------
class StubNova:
    """Behaviours: byid_404_first (N by-id 404s before 200), delete_works,
    list_status (HTTP code of the list), paginate, root_status."""

    def __init__(self, **kw):
        self.kw = dict(byid_404_first=0, delete_works=True, list_status=200, paginate=False,
                       build_polls=2, control_answers_404=True)
        self.kw.update(kw)
        self.servers = {}      # id -> {name, polls, deleted}
        self.byid_calls = {}
        self.lock = threading.Lock()
        stub = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=None):
                raw = json.dumps(body).encode() if body is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                with stub.lock:
                    if self.path == "/":
                        return self._send(200, {"versions": []})
                    if self.path.startswith("/servers/detail"):
                        if stub.kw["list_status"] != 200:
                            return self._send(stub.kw["list_status"], {})
                        live = [{"id": i, "name": s["name"]} for i, s in stub.servers.items()
                                if not s["deleted"] and not s.get("hidden")]
                        body = {"servers": live}
                        if stub.kw["paginate"]:
                            body["servers_links"] = [{"rel": "next", "href": "x"}]
                        return self._send(200, body)
                    m = re.match(r"^/servers/([0-9a-f-]+)$", self.path)
                    if m:
                        i = m.group(1)
                        s = stub.servers.get(i)
                        if s is None:
                            if not stub.kw["control_answers_404"]:
                                return self._send(200, {"server": {"id": i, "status": "ACTIVE"}})
                            return self._send(404, {"itemNotFound": {}})
                        if s["deleted"]:
                            return self._send(404, {})
                        c = stub.byid_calls[i] = stub.byid_calls.get(i, 0) + 1
                        if c <= stub.kw["byid_404_first"]:
                            return self._send(404, {"itemNotFound": {}})
                        s["polls"] += 1
                        st = "BUILD" if s["polls"] <= stub.kw["build_polls"] else "ACTIVE"
                        return self._send(200, {"server": {"id": i, "name": s["name"], "status": st}})
                self._send(404, {})

            def do_DELETE(self):
                with stub.lock:
                    m = re.match(r"^/servers/([0-9a-f-]+)$", self.path)
                    if m and m.group(1) in stub.servers:
                        if stub.kw["delete_works"]:
                            stub.servers[m.group(1)]["deleted"] = True
                        return self._send(204)
                self._send(404, {})

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def add(self, name):
        i = str(uuid.uuid4())
        with self.lock:
            self.servers[i] = {"name": name, "polls": 0, "deleted": False}
        return i

    def close(self):
        self.httpd.shutdown()


def _mk_events(ready_seq, synced="ok", relist=False, end_t=300.0, clean=True, ann=True,
               ready_delay=20):
    """Synthetic output of the VENDORED reader (hack/capture-watch-stream.py):
    header, event lines, summary. ready_seq: [(delivered_t, 'True'/'False'/...)].
    ready_delay: seconds after external-create-succeeded (06Z) that Ready=True was
    STORED by the apiserver (lastTransitionTime), a different base from delivered_t."""
    recs, seq = [{"record": "header", "schema": 1}], 0
    ltt = "2026-01-01T00:%02d:%02dZ" % divmod(6 + ready_delay, 60)

    def ev(t, rdy, syn):
        nonlocal seq
        seq += 1
        conds = [{"type": "Ready", "status": rdy, "reason": "Available",
                  "lastTransitionTime": ltt if rdy == "True" else "2026-01-01T00:00:01Z"}]
        if syn == "ok":
            conds.append({"type": "Synced", "status": "True", "reason": "ReconcileSuccess"})
        elif syn == "false":
            conds.append({"type": "Synced", "status": "False", "reason": "ReconcileError",
                          "message": "vendor said 400: bad flavor"})
        md = {"uid": "u1", "name": "x"}
        if ann:
            md["annotations"] = {CREATE_PREFIX + "pending": "2026-01-01T00:00:05Z",
                                 CREATE_PREFIX + "succeeded": "2026-01-01T00:00:06Z"}
        return {"record": "event", "seq": seq, "delivered_t": t,
                "delivered_wall": "2026-01-01T01:39:%02d.000+00:00" % (int(t) % 60),
                "probable_relist": False,
                "event": {"type": "MODIFIED", "object": {"metadata": md, "status": {"conditions": conds}}}}
    for t, s in ready_seq:
        recs.append(ev(t, s, synced))
    if relist:
        r = ev(end_t - 1, ready_seq[-1][1], synced)
        r["event"]["type"], r["probable_relist"] = "ADDED", True
        recs.append(r)
    recs.append({"record": "summary", "exit_cause": "eof" if clean else "SIGTERM",
                 "stdin_clean_eof": clean, "events": seq + (1 if relist else 0),
                 "by_type": {"MODIFIED": seq}, "probable_relists": 1 if relist else 0,
                 "leftover_bytes": 0 if clean else 40, "window_s": end_t})
    return recs


_S0 = 1795888800  # 2026-11-28T18:00:00Z, the fixtures' startup time


def _z(sec):
    return datetime.datetime.fromtimestamp(sec, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "000Z"


def _ctl_log(name, kind, resync=180):
    """Synthetic controller log in the shape resync-cadence.py reads: a startup line,
    then pass lines carrying queuedAt. Times are seconds after the startup line.
    kinds: hint | nohint | starved_grid | starved_shared | burst | nogrid | nostart | empty"""
    if kind == "empty":
        return ""
    lines = []
    if kind != "nostart":
        lines.append({"msg": "Starting.", "build": "0.99.0-x", "resyncInterval": resync * 1_000_000_000,
                      "timestamp": _z(_S0)})

    def item(q, gap, who=name, n=0):
        lines.append({"msg": "External resource is up to date", "kind": "Instance", "namespace": "krateo-system",
                      "name": who, "queuedAt": _z(_S0 + q + n * 1e-5), "timestamp": _z(_S0 + q + gap)})
    item(30.0, 0.4)                                    # the event-driven pass at create
    if kind != "nogrid":
        item(resync, 0.4)                              # a resync pass ON the grid (validates it)
    if kind == "hint":
        item(31.0, 16.0)                               # enqueued 1s later, waited 16s: hint + queue
    elif kind == "burst":
        item(31.0, 16.0)
        item(60.0, 17.0)
    elif kind == "nogrid":
        item(31.0, 16.0)
    elif kind == "starved_grid":
        item(2 * resync, 30.0)                         # ON the grid, waited 30s: a starved resync pass
    elif kind == "starved_shared":
        for i in range(6):                             # six objects share one enqueue, all waited
            item(100.0, 25.0, who="other-%d" % i, n=i)
        item(100.0, 25.0, n=9)
    return "\n".join(json.dumps(x) for x in lines) + "\n"


def _write_run(root, draws, rules=None, td=None, ctl=True, cap_breach=None, oasgen="0.74.0"):
    """draw dict keys: events host poll window_s resync_s flapwatch_rc"""
    os.makedirs(root, exist_ok=True)
    rules = RULES if rules is None else rules
    json.dump({"oasgen": oasgen, "draws_requested": 5, "draws_started": len(draws),
               "run_id": "t", "rules": rules, "cap_breach": cap_breach},
              open(os.path.join(root, "run.json"), "w"))
    json.dump(td if td is not None else {"proven": True, "why": ""},
              open(os.path.join(root, "teardown.json"), "w"))
    if ctl is not None:
        json.dump({"sees_a_404": ctl, "http": 404 if ctl else 200},
                  open(os.path.join(root, "control404.json"), "w"))
    for i, d in enumerate(draws, 1):
        p = os.path.join(root, "draw-%d" % i)
        os.makedirs(p, exist_ok=True)
        json.dump({"draw": i, "name": "n%d" % i, "window_s": d.get("window_s", 240),
                   "resync_s": d.get("resync_s", 180)}, open(os.path.join(p, "meta.json"), "w"))
        cl = d.get("controller_log", "hint")
        if cl is not None:
            with open(os.path.join(p, "controller.log"), "w") as f:
                f.write(_ctl_log("n%d" % i, cl) if cl in ("hint", "nohint", "starved_grid", "starved_shared",
                                                           "burst", "nogrid", "nostart", "empty") else cl)
        if d.get("flapwatch_rc") is not None:
            open(os.path.join(p, "flapwatch.rc"), "w").write(str(d["flapwatch_rc"]))
        if d.get("events") is not None:
            with open(os.path.join(p, "events.jsonl"), "w") as f:
                for r in d["events"]:
                    f.write(json.dumps(r) + "\n")
        if d.get("host") is not None:
            with open(os.path.join(p, "host.jsonl"), "w") as f:
                for h in d["host"]:
                    f.write(json.dumps(h) + "\n")
        if d.get("poll") is not None:
            with open(os.path.join(p, "poll.jsonl"), "w") as f:
                for r in d["poll"]:
                    f.write(json.dumps(r) + "\n")


def _good_poll(extra_404=0, vanish=False, status_5xx=False):
    r = [{"record": "header"}, {"record": "cloud", "phase": "start", "http": 200, "latency_s": 0.1},
         {"record": "id", "id": "i1", "id_source": "cr-status"}]
    r += [{"record": "probe", "kind": "byid", "http": 404, "status": None, "latency_s": 0.1}
          for _ in range(extra_404)]
    r += [{"record": "probe", "kind": "byid", "http": 200, "status": s, "latency_s": 0.1}
          for s in ("BUILD", "BUILD", "ACTIVE")]
    if vanish:
        r.append({"record": "probe", "kind": "byid", "http": 404, "status": None, "latency_s": 0.1})
    if status_5xx:
        r.append({"record": "probe", "kind": "byid", "http": 503, "latency_s": 0.1})
    r.append({"record": "cloud", "phase": "end", "http": 200, "latency_s": 0.1})
    return r


GOOD_HOST = [{"load1": 0.5, "nproc": 4}] * 5
STEADY = [(10.0, "False"), (30.0, "True"), (100.0, "True"), (280.0, "True")]
FLAPPY = [(10.0, "False"), (30.0, "True"), (120.0, "False"), (150.0, "True")]


def _draw(events=None, host=GOOD_HOST, poll=None, **kw):
    d = {"events": events if events is not None else _mk_events(STEADY),
         "host": host, "poll": poll if poll is not None else _good_poll(), "flapwatch_rc": 0}
    d.update(kw)
    return d


def _self_test():
    import shutil
    import tempfile
    fails = []

    def t(name, want, got):
        if want == got:
            print("  ok   %s" % name)
        else:
            print("  *** WRONG: %s (want %r got %r)" % (name, want, got))
            fails.append(name)

    tmp = tempfile.mkdtemp()

    def run_case(name, draws, **kw):
        root = os.path.join(tmp, name)
        _write_run(root, draws, **kw)
        return analyse_run(root)

    five = [_draw() for _ in range(5)]
    # ---- verdict branches, both directions ----------------------------------
    code, rep = run_case("clean", five)
    t("5 clean draws -> NO FLAP, exit 0", 0, code)
    t("  the verdict says NO FLAP", True, rep["verdict"].startswith("NO FLAP"))
    t("  and says it bounds a rate", True, "3/5" in rep["q2"] and "does not prove" in rep["q2"])
    code, rep = run_case("flap", [_draw(events=_mk_events(FLAPPY))] + five[:4])
    t("one usable draw leaving True -> FINDING, exit 1", 1, code)
    t("  the flap is located", 1, len(rep["draws"][0]["left_true"]))
    code, _ = run_case("q1", [_draw(poll=_good_poll(extra_404=2))] + five[:4])
    t("a by-id 404 before the first 200 -> FINDING, exit 1", 1, code)
    code, rep = run_case("vanish", [_draw(poll=_good_poll(vanish=True))] + five[:4])
    t("a 404 AFTER being seen -> FINDING, exit 1", 1, code)
    t("  labelled as a vanish, not as a before-BUILD", "SEEN-VANISHED-AFTER-VISIBLE",
      rep["draws"][0]["q1"]["state"])

    # ---- VOID is reachable ---------------------------------------------------
    code, rep = run_case("synced_false", [_draw(events=_mk_events(STEADY, synced="false"))] + five[:4])
    t("Synced=False in one draw: that draw is VOID-class and unusable", False, rep["draws"][0]["usable"])
    t("  with the vendor's reason attached", True,
      "vendor said 400" in rep["draws"][0]["void"][0]["detail"])
    t("  4 usable remain, so the run still reports", 0, code)
    code, rep = run_case("all_synced_false", [_draw(events=_mk_events(STEADY, synced="false"))] * 5)
    t("every draw Synced=False -> VOID, exit 2 (not REFUSED)", 2, code)
    t("  a run where Ready never happened is NOT NO FLAP", False, rep["verdict"].startswith("NO FLAP"))
    code, rep = run_case("no_control", five, ctl=False)
    t("the 404 control not seeing a 404 -> VOID, exit 2", 2, code)
    code, rep = run_case("no_control_file", five, ctl=None)
    t("a MISSING control file -> VOID, exit 2", 2, code)
    code, rep = run_case("no_rules", five, rules={})
    t("rules not recorded before the draws -> VOID, exit 2", 2, code)

    # ---- contamination is discarded, identifiably, with the rule that did it --
    hot = [dict(h, load1=50.0) for h in GOOD_HOST]
    code, rep = run_case("hot", [_draw(host=hot)] + five[:4])
    t("a draw at load 50 is discarded", False, rep["draws"][0]["usable"])
    t("  by the host-load rule, naming the number", True,
      rep["draws"][0]["discards"][0]["code"] == "host-load" and "50.00" in rep["draws"][0]["discards"][0]["detail"])
    t("  the other four draws are untouched", [True] * 4, [x["usable"] for x in rep["draws"][1:]])
    t("  and the report says WHICH draws it discarded", 1, rep["discarded"])
    code, rep = run_case("onehost", [_draw(host=GOOD_HOST[:1])] + five[:4])
    t("ONE host sample is not enough to call a draw clean", "host-state-unread",
      rep["draws"][0]["discards"][0]["code"])
    code, rep = run_case("nohost", [_draw(host=None)] + five[:4])
    t("an UNREAD host is not 'clean': discarded host-state-unread", "host-state-unread",
      rep["draws"][0]["discards"][0]["code"])
    code, rep = run_case("slow", [_draw(poll=_good_poll(status_5xx=True))] + five[:4])
    t("a 503 from the cloud during the draw -> cloud-degraded", "cloud-degraded",
      rep["draws"][0]["discards"][0]["code"])
    code, rep = run_case("nopoll", [dict(_draw(), poll=None)] + five[:4])
    t("no poll record at all -> cloud-state-unread (unread is not clean)", "cloud-state-unread",
      rep["draws"][0]["discards"][0]["code"])
    code, rep = run_case("short", [_draw(events=_mk_events(STEADY, end_t=100.0))] + five[:4])
    t("a window cut short is discarded", "window-incomplete", rep["draws"][0]["discards"][0]["code"])
    code, rep = run_case("unclean", [_draw(events=_mk_events(STEADY, clean=False))] + five[:4])
    t("a reader that did not reach clean EOF is discarded", "reader-unclean",
      rep["draws"][0]["discards"][0]["code"])
    ev_nosum = [r for r in _mk_events(STEADY) if r["record"] != "summary"]
    code, rep = run_case("nosummary", [_draw(events=ev_nosum)] + five[:4])
    t("NO summary record (the file just ended) is discarded, not read as 'nothing happened'",
      "reader-unclean", rep["draws"][0]["discards"][0]["code"])
    # ---- the PASS CLASSIFIER ------------------------------------------------
    def pc(kind, **kw):
        return classify_pass(_ctl_log("n1", kind), "n1", dict(RULES, **kw))
    t("a 16s single-object pass OFF the grid, grid validated -> HINT", "HINT", pc("hint")["state"])
    t("  the validated grid is what licenses it", True, pc("hint")["grid_validated"])
    t("control (no hint, as at 0.98.0): NO-HINT-PASS, an ABSENT", "NO-HINT-PASS", pc("nohint")["state"])
    t("a large-gap pass ON the resync grid is a starved resync pass, not a hint", "STARVED",
      pc("starved_grid")["state"])
    t("a large-gap pass whose items all share one queuedAt is STARVED", "STARVED",
      pc("starved_shared")["state"])
    t("two hint passes for one object in one window is BURST, not HINT", "BURST", pc("burst")["state"])
    t("a hint with NO pass of ours ever seen on the grid is UNREAD-GRID", "UNREAD-GRID", pc("nogrid")["state"])
    t("no startup line is UNREAD, never defaulted to 180s", "UNREAD", pc("nostart")["state"])
    t("an empty log is UNREAD", "UNREAD", pc("empty")["state"])
    t("gap exactly the hint (15s) counts: >= not >", "HINT",
      classify_pass(_ctl_log("n1", "hint").replace('"timestamp": "%s"' % _z(_S0 + 31.0 + 16.0),
                                                    '"timestamp": "%s"' % _z(_S0 + 31.0 + 15.0)), "n1",
                    dict(RULES))["state"])
    t("a gap just under the hint (14.9s) is not a hint", "NO-HINT-PASS",
      classify_pass(_ctl_log("n1", "hint").replace('"timestamp": "%s"' % _z(_S0 + 31.0 + 16.0),
                                                    '"timestamp": "%s"' % _z(_S0 + 31.0 + 14.9)), "n1",
                    dict(RULES))["state"])
    t("hint PLUS queue delay (gap 16 against hint 15) is read as >= the hint, not a band", "HINT",
      pc("hint")["state"])
    t("a hint that shares a tolerance cluster with the create pass is still found (row per queuedAt)",
      "hint", [r for r in pc("hint")["passes"] if r["gap_s"] and r["gap_s"] >= 15][0]["kind"])
    t("the denominator is printed (lines without queuedAt are counted, not dropped)", True,
      pc("hint")["denominator"]["lines_without_queuedAt"] >= 1)
    code, rep = run_case("p_hint", [_draw()] * 1)
    t("a draw with a HINT pass is usable", True, rep["draws"][0]["usable"])
    code, rep = run_case("p_unread", [_draw(controller_log="nostart")])
    t("pass UNREAD is VOID-class: 'no flap' without knowing the mechanism ran says nothing", True,
      code == 2 and rep["draws"][0]["void"][0]["code"] == "pass-unread")
    code, rep = run_case("p_nolog", [_draw(controller_log=None)])
    t("no controller log at all is VOID too", 2, code)
    code, rep = run_case("p_starved", [_draw(controller_log="starved_grid")])
    t("a STARVED pass discards the draw (load, not the vendor)", "starved-pass",
      rep["draws"][0]["discards"][0]["code"])
    code, rep = run_case("p_burst", [_draw(controller_log="burst")])
    t("a BURST is a FINDING (exit 1), not a pass", 1, code)
    code, rep = run_case("p_ctl", [_draw(controller_log="nohint")])
    t("the 0.98.0-style control: no hint pass, no flap -> exit 0, said plainly", True,
      code == 0 and "NO-HINT-PASS" in rep["verdict"])

    root = os.path.join(tmp, "p_expected")
    _write_run(root, [_draw(controller_log="nohint")], oasgen="0.99.0")
    code, rep = analyse_run(root)
    t("at oasgen 0.99.0 an absent hint pass is called out, not passed quietly", True,
      code == 0 and any("ABSENT at a version that should carry it" in n for n in rep["notes"]))
    root = os.path.join(tmp, "p_expected2")
    _write_run(root, [_draw(controller_log="nohint")], oasgen="0.98.0")
    t("at 0.98.0 (the control) the same absence carries no such note", [], analyse_run(root)[1]["notes"])

    # ---- the per-draw null: stated before the create, checked per draw -------
    code, rep = run_case("ow_in", [_draw(events=_mk_events(STEADY, ready_delay=20))] + five[:4])
    t("Ready 20s after create-succeeded, resync 180: inside its own window", "inside",
      rep["draws"][0]["own_window"]["state"])
    code, rep = run_case("ow_edge", [_draw(events=_mk_events(STEADY, ready_delay=185))] + five[:4])
    t("exactly resync+tolerance (185s) is still inside", "inside", rep["draws"][0]["own_window"]["state"])
    code, rep = run_case("ow_miss", [_draw(events=_mk_events(STEADY, ready_delay=186))] + five[:4])
    t("186s (ironic's 'just past its own pass') is MISSED and the draw discarded", "missed-own-window",
      rep["draws"][0]["discards"][0]["code"])
    t("  the discard says which window it missed, with numbers", True,
      "180" in rep["draws"][0]["discards"][0]["detail"] and "186" in rep["draws"][0]["discards"][0]["detail"])
    code, rep = run_case("ow_other", [_draw(events=_mk_events(STEADY, ready_delay=186), resync_s=300)] + five[:4])
    t("the SAME 186s under a controller whose resync is 300s is inside (the window is per controller)",
      "inside", rep["draws"][0]["own_window"]["state"])
    code, rep = run_case("ow_unread", [_draw(resync_s=None)] + five[:4])
    t("resync not read: flagged, NOT discarded (host-load is all that is left)", True,
      rep["draws"][0]["usable"] and "own-window-resync-unread" in rep["draws"][0]["flags"])
    code, rep = run_case("ow_noann", [_draw(events=_mk_events(STEADY, ann=False))] + five[:4])
    t("no create annotation: own window is UNREAD, flagged, not 'inside'", "unread",
      rep["draws"][0]["own_window"]["state"])

    # ---- the vendored flap watcher as an independent second instrument ----
    code, rep = run_case("fw_only", [_draw(flapwatch_rc=1)] + five[:4])
    t("flapwatch says FLAP, capture saw none -> FINDING exit 1", 1, code)
    t("  and the disagreement is flagged", True,
      any(f.startswith("instruments-disagree") for f in rep["draws"][0]["flags"]))
    code, rep = run_case("cap_only", [_draw(events=_mk_events(FLAPPY), flapwatch_rc=0)] + five[:4])
    t("capture saw a flap, flapwatch said NO FLAP -> still FINDING, flagged", True,
      code == 1 and any(f.startswith("instruments-disagree") for f in rep["draws"][0]["flags"]))
    code, rep = run_case("fw_void", [_draw(flapwatch_rc=2)] + five[:4])
    t("flapwatch VOID is flagged, never read as 'no flap'", True,
      "flapwatch-void" in rep["draws"][0]["flags"] and rep["draws"][0]["flapwatch"]["state"] == "VOID")
    code, rep = run_case("fw_none", [_draw(flapwatch_rc=None)] + five[:4])
    t("no flapwatch result at all is UNREAD", "UNREAD", rep["draws"][0]["flapwatch"]["state"])
    code, rep = run_case("fw_hot", [_draw(flapwatch_rc=1, host=hot)] + five[:4])
    t("a flapwatch FLAP inside a discarded draw is reported, not counted", True,
      code == 0 and len(rep["discarded_findings"]) == 1)

    code, rep = run_case("relist", [_draw(events=_mk_events(STEADY, relist=True))] + five[:4])
    t("a probable re-list is flagged and the draw discarded", "relist",
      rep["draws"][0]["discards"][0]["code"])
    no_sync = [r for r in _mk_events(STEADY) if r["record"] != "event"] + [
        {"record": "event", "seq": 1, "delivered_t": 5.0, "probable_relist": False,
         "event": {"type": "ADDED", "object": {"metadata": {"uid": "u1"}, "status": {}}}}]
    no_sync = [no_sync[0], no_sync[2], no_sync[1]]
    code, rep = run_case("never_synced", [_draw(events=no_sync)] * 5)
    t("a CR that never reports Synced at all is VOID-class, not NO FLAP", True,
      code == 2 and rep["draws"][0]["void"][0]["code"] == "synced-never")
    code, rep = run_case("never_ready", [_draw(events=_mk_events([(10.0, "False"), (280.0, "False")]))]
                         + five[:4])
    t("Ready never True is discarded, never a NO FLAP draw", "never-ready",
      rep["draws"][0]["discards"][0]["code"])
    # a flap in a contaminated draw is REPORTED and NOT COUNTED
    code, rep = run_case("flap_hot", [_draw(events=_mk_events(FLAPPY), host=hot)] + five[:4])
    t("a flap inside a discarded draw does not set the exit code", 0, code)
    t("  but it is reported under discarded_findings", 1, len(rep["discarded_findings"]))

    # ---- fewer than three usable draws refuses ------------------------------
    R3 = dict(RULES, min_usable_draws=3)   # the replicate rule, for someone who asks for replicates
    code, rep = run_case("two", [_draw(), _draw()], rules=R3)
    t("with the replicate rule (3), 2 usable draws -> REFUSED, exit 3", 3, code)
    t("  no NO-FLAP language anywhere in the verdict", False, "NO FLAP" in rep["verdict"])
    code, rep = run_case("three", [_draw()] * 3, rules=R3)
    t("exactly 3 usable draws under the replicate rule -> reports (the boundary)", 0, code)
    code, rep = run_case("five_three_dead", [_draw()] * 2 + [_draw(host=hot)] * 3, rules=R3)
    t("5 drawn, 3 contaminated, replicate rule -> REFUSED, exit 3", 3, code)
    code, rep = run_case("one_clean", [_draw()])
    t("ONE clean draw reports under the per-pass design (min_usable_draws is 1)", 0, code)
    t("  and says it is a classification, not a rate", True, "per-pass classification" in rep["q2"])
    code, rep = run_case("one_hot", [_draw(host=hot)])
    t("ONE contaminated draw -> REFUSED, exit 3 (one is the floor, not zero)", 3, code)
    code, rep = run_case("one_hot_replicates", [_draw(host=hot)] + five[:1], rules=R3)
    t("replicate rule: 1 usable of 2 -> REFUSED", 3, code)
    code, rep = run_case("zero", [])
    t("ZERO draws -> REFUSED, never NO FLAP", 3, code)

    # ---- teardown outranks everything ---------------------------------------
    code, rep = run_case("td_fail", [_draw(events=_mk_events(FLAPPY))] * 5,
                         td={"proven": False, "why": "still listed"})
    t("teardown unproven -> exit 4 even over a FINDING", 4, code)
    code, rep = run_case("td_ok_void", [_draw(events=_mk_events(STEADY, synced="false"))] * 5,
                         td={"proven": False, "why": "x"})
    t("teardown unproven -> exit 4 even over VOID", 4, code)
    os.remove(os.path.join(tmp, "clean", "teardown.json"))
    code, rep = analyse_run(os.path.join(tmp, "clean"))
    t("NO teardown.json -> exit 4, not a pass", 4, code)
    code, rep = run_case("breach", five, cap_breach="6 servers under prefix, cap 5")
    t("a cap breach is a FAILED run, exit 4", 4, code)

    # ---- the span, the annotations ------------------------------------------
    code, rep = run_case("span", five)
    ca = rep["draws"][0]["create_annotations"]
    t("annotations come from the object, source named", "event-stream",
      ca["source"])
    t("the SPAN is 1s (succeeded minus pending), not an interval", 1, ca["span_s"])
    t("both annotation values are captured", 2, len(ca["values"]))
    t("the span is labelled as apiserver-stored", True, "apiserver-stored" in ca["bases"])
    t("ABSENT is distinguished from UNREAD (object read, no annotation)", True,
      create_span({})[1].startswith("ABSENT") and create_span(None)[1].startswith("UNREAD"))
    t("only 'pending' present -> UNREAD span, not 0", None,
      create_span({CREATE_PREFIX + "pending": "2026-01-01T00:00:05Z"})[0])
    t("a 94.7s DELIVERY gap does not change a 1s stored span", 1,
      create_span({CREATE_PREFIX + "pending": "2026-01-01T18:09:18Z",
                   CREATE_PREFIX + "succeeded": "2026-01-01T18:09:19Z"})[0])
    out = render(rep, code)
    t("the rendered report names both time bases", True,
      "reader's own clock" in out and "apiserver's clock" in out)
    t("it prints the discard rules before the draws", True,
      out.index("DISCARD RULES") < out.index("draw 1"))
    t("it states what is NOT established", True, "does not prove impossibility" in out)

    # ---- the ready-sequence logic on its own ---------------------------------
    t("True,True,True -> no leaving", [], left_true([(1, "True"), (2, "True")]))
    t("True -> Unknown counts as leaving True", 1, len(left_true([(1, "True"), (2, "Unknown")])))
    t("False -> True is NOT a flap (creation settling)", [], left_true([(1, "False"), (2, "True")]))
    t("True -> False -> True is one departure", 1,
      len(left_true([(1, "True"), (2, "False"), (3, "True")])))

    t("analyse with the wrong arity is a usage error 64, never VOID's 2", 64, main(["x", "analyse"]))
    t("an unknown subcommand is 64", 64, main(["x", "bogus"]))

    # ---- the instruments, against a stub Nova -------------------------------
    stub = StubNova(byid_404_first=0)
    nova = Nova(stub.url, "tok")
    sid = stub.add("nw-1")
    out_p = os.path.join(tmp, "poll_ok.jsonl")
    poll(nova, "nw-1", out_p, interval=0.01, deadline_s=20, sleep=lambda s: time.sleep(0.01))
    recs = load_jsonl(out_p)
    q = q1_reading(recs)
    t("poller finds a server by name and reaches ACTIVE", ["BUILD", "ACTIVE"], q["status_sequence"])
    t("  a healthy stub reads NOT-SEEN", "NOT-SEEN", q["state"])
    t("  the poll header names its clocks", True, "vendor_date" in recs[0]["bases"])
    t("  the poll ends on a summary record", "summary", recs[-1]["record"])
    t("  with a cloud state before and after", ["start", "end"],
      [r["phase"] for r in recs if r.get("record") == "cloud"])
    stub.close()

    stub = StubNova(byid_404_first=3)
    nova = Nova(stub.url, "tok")
    stub.add("nw-2")
    idf = os.path.join(tmp, "idfile")
    out_p = os.path.join(tmp, "poll_404.jsonl")
    # the id comes from the CR (as in the real run), so by-id probing starts
    # BEFORE the server is guaranteed listable
    open(idf, "w").write(list(stub.servers)[0])
    poll(nova, "nw-2", out_p, id_file=idf, interval=0.01, deadline_s=20, sleep=lambda s: time.sleep(0.01))
    q = q1_reading(load_jsonl(out_p))
    t("poller + stub that 404s first 3 GETs -> SEEN-404-BEFORE-BUILD", "SEEN-404-BEFORE-BUILD", q["state"])
    t("  counting exactly 3", 3, q["404_before_first_200"])
    t("  id_source says the id came from the CR", "cr-status", q["id_source"])
    stub.close()

    # redirects must not be followed with the token attached
    class Redir(http.server.BaseHTTPRequestHandler):
        seen_token = []

        def log_message(self, *a):
            pass

        def do_GET(self):
            Redir.seen_token.append(self.headers.get("X-Auth-Token"))
            if self.path.startswith("/hop"):
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
            else:
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:%d/hop" % self.server.server_address[1])
                self.end_headers()
    rs = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redir)
    threading.Thread(target=rs.serve_forever, daemon=True).start()
    r = Nova("http://127.0.0.1:%d" % rs.server_address[1], "SECRET").req("GET", "/servers/x")
    t("a 302 is an error, not a hop (the token is not re-sent)", True,
      r["http"] == 302 and "SECRET" not in [x for x in Redir.seen_token[1:]])
    rs.shutdown()

    # the 404 control
    stub = StubNova()
    t("control404: a vendor that 404s an unknown id passes", 0,
      control404(Nova(stub.url, "t"), os.path.join(tmp, "c1.json")))
    stub.kw["control_answers_404"] = False
    t("control404: a vendor that answers 200 for a random id is VOID", 2,
      control404(Nova(stub.url, "t"), os.path.join(tmp, "c2.json")))
    stub.close()

    # teardown, proven by reading the vendor
    stub = StubNova()
    nova = Nova(stub.url, "t")
    a, b = stub.add("nova-window-r1-1"), stub.add("nova-window-r1-2")
    stub.add("someone-elses")
    tdp = os.path.join(tmp, "td.json")
    rc = teardown(nova, "nova-window-r1-", [a], 2, tdp, deadline_s=10, interval=0.01, sleep=lambda s: None)
    td = load_json(tdp)
    t("teardown deletes and PROVES: exit 0", 0, rc)
    t("  proof is by vendor read: both ids 404", {a: 404, b: 404}, td["byid_http"])
    t("  it did not touch a server outside the prefix", False,
      any(s["deleted"] for s in stub.servers.values() if s["name"] == "someone-elses"))
    stub.close()

    stub = StubNova(delete_works=False)
    nova = Nova(stub.url, "t")
    stub.add("nova-window-r1-1")
    rc = teardown(nova, "nova-window-r1-", [], 1, tdp, deadline_s=0.2, interval=0.01)
    t("a DELETE that returns 204 but changes nothing -> exit 4 (call returning is not proof)", 4, rc)
    t("  and it names what is still listed", True, "still listed" in load_json(tdp)["why"])
    stub.close()

    stub = StubNova(list_status=500)
    rc = teardown(Nova(stub.url, "t"), "nova-window-r1-", [], 1, tdp, deadline_s=0.2, interval=0.01)
    t("a FAILED list read is not 'empty': exit 4", 4, rc)
    stub.close()

    stub = StubNova(paginate=True)
    rc = teardown(Nova(stub.url, "t"), "nova-window-r1-", [], 1, tdp, deadline_s=0.2, interval=0.01)
    t("a PAGINATED list is not a complete read: exit 4", 4, rc)
    stub.close()

    stub = StubNova()
    rc = teardown(Nova(stub.url, "t"), "nova-window-r1-", [], 3, tdp, deadline_s=0.2, interval=0.01)
    t("3 creates issued, NOTHING ever seen: an empty list proves nothing -> exit 4", 4, rc)
    t("  flagged as a blind instrument", True, load_json(tdp)["blind_instrument"])
    rc = teardown(Nova(stub.url, "t"), "nova-window-r1-", [], 0, tdp, deadline_s=0.2, interval=0.01)
    t("0 creates issued and an empty vendor -> proven (the control for the case above)", 0, rc)
    stub.close()

    # a server the LIST no longer shows but that still answers by id is not deleted
    stub = StubNova(delete_works=False)
    gid = stub.add("nova-window-r1-ghost")
    stub.servers[gid]["hidden"] = True
    rc = teardown(Nova(stub.url, "t"), "nova-window-r1-", [gid], 1, tdp, deadline_s=0.2, interval=0.01)
    t("empty list but the id still answers 200 -> NOT proven, exit 4", 4, rc)
    stub.close()
    stub = StubNova(paginate=True)
    stub.add("nova-window-r1-a")
    rc = teardown(Nova(stub.url, "t"), "nova-window-r1-", [], 1, tdp, deadline_s=0.2, interval=0.01)
    t("a paginated list holding our server is not a proof either: exit 4", 4, rc)
    t("  and it says why", True, "PAGINATED" in load_json(tdp)["why"])
    stub.close()

    # the prefix guard: an EMPTY prefix matches every server in the project
    stub = StubNova()
    keep = stub.add("someone-elses-prod-server")
    for bad in ("", "nova-", "nova-window-", "prod", "nova-window-12"):
        rc = teardown(Nova(stub.url, "t"), bad, [], 0, tdp, deadline_s=0.1, interval=0.01)
        t("teardown REFUSES prefix %r (64) and deletes nothing" % bad, (64, False),
          (rc, stub.servers[keep]["deleted"]))
    rc = teardown(Nova(stub.url, "t"), "nova-window-9-", [], 0, tdp, deadline_s=1, interval=0.01)
    t("a well-formed prefix is accepted and leaves a foreign server alone", (0, False),
      (rc, stub.servers[keep]["deleted"]))
    stub.close()

    # count / run-json / ids_seen_in_run
    stub = StubNova()
    stub.add("nw-x-1")
    stub.add("nw-x-2")
    stub.add("nw-xy-1")
    t("count_prefix counts the prefix and nothing else", (2, ""), count_prefix(Nova(stub.url, "t"), "nw-x-"))
    stub.kw["list_status"] = 500
    t("count_prefix on a failed read is None (UNREAD), not 0", None, count_prefix(Nova(stub.url, "t"), "nw-x-")[0])
    stub.close()
    rj = os.path.join(tmp, "rj.json")
    run_json_set(rj, ["a=1", "b=hello", 'c={"x":2}'])
    run_json_set(rj, ["a=2"])
    t("run-json merges and parses", {"a": 2, "b": "hello", "c": {"x": 2}}, load_json(rj))
    os.makedirs(os.path.join(tmp, "rd", "draw-1"))
    with open(os.path.join(tmp, "rd", "draw-1", "poll.jsonl"), "w") as f:
        f.write(json.dumps({"record": "id", "id": "abc"}) + "\n")
    t("ids are collected from every draw's poll record", ["abc"], ids_seen_in_run(os.path.join(tmp, "rd")))

    shutil.rmtree(tmp, ignore_errors=True)
    if fails:
        print("--- FAIL: %d arm(s): %s ---" % (len(fails), ", ".join(fails)))
        return 1
    print("--- all arms pass ---")
    return 0


def _arg(argv, flag, default=None, required=False):
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    if required:
        sys.stderr.write("usage: missing %s\n" % flag)
        sys.exit(EXIT_USAGE)
    return default


def main(argv):
    if len(argv) < 2:
        sys.stderr.write(__doc__.split("SUBCOMMANDS")[0] + "\nusage: see SUBCOMMANDS\n")
        return EXIT_USAGE
    cmd = argv[1]
    if cmd == "--self-test":
        return _self_test()
    if cmd == "rules":
        print(json.dumps(RULES, sort_keys=True))
        return 0
    if cmd == "draw-check":
        if len(argv) != 4:
            return EXIT_USAGE
        run = load_json(os.path.join(argv[2], "run.json")) or {}
        rules = run.get("rules") or RULES
        dn = os.path.join(argv[2], "draw-%s" % argv[3])
        d = {"meta": load_json(os.path.join(dn, "meta.json")) or {},
             "events": load_jsonl(os.path.join(dn, "events.jsonl")),
             "final_cr": load_json(os.path.join(dn, "final-cr.json")),
             "host": load_jsonl(os.path.join(dn, "host.jsonl")),
             "poll": load_jsonl(os.path.join(dn, "poll.jsonl")),
             "flapwatch": read_flapwatch(dn),
             "controller_log": read_text(os.path.join(dn, "controller.log"))}
        x = classify_draw(d, rules)
        print("draw %s: %s%s" % (argv[3], "USABLE" if x["usable"] else "DISCARDED",
              "".join(" [%s: %s]" % (e["code"], e["detail"]) for e in x["void"] + x["discards"])))
        return 0
    if cmd == "run-json":
        if len(argv) < 4:
            return EXIT_USAGE
        run_json_set(argv[2], argv[3:])
        return 0
    if cmd in ("poll", "control404", "teardown", "count"):
        tok = os.environ.get("NOVA_TOKEN", "")
        if not tok:
            sys.stderr.write("NOVA_TOKEN is not set (the token is read from the environment, "
                             "never from argv)\n")
            return EXIT_VOID
        nova = Nova(_arg(argv, "--nova", required=True), tok)
        if cmd == "poll":
            return poll(nova, _arg(argv, "--name", required=True), _arg(argv, "--out", required=True),
                        id_file=_arg(argv, "--id-file"), stop_file=_arg(argv, "--stop-file"),
                        interval=float(_arg(argv, "--interval", "0.5")),
                        deadline_s=float(_arg(argv, "--deadline", "300")))
        if cmd == "control404":
            return control404(nova, _arg(argv, "--out", required=True))
        if cmd == "count":
            n, why = count_prefix(nova, _arg(argv, "--prefix", required=True))
            print("UNREAD" if n is None else n)
            if n is None:
                sys.stderr.write("count UNREAD: %s\n" % why)
            return 0 if n is not None else EXIT_VOID
        rd = _arg(argv, "--run-dir", required=True)
        run = load_json(os.path.join(rd, "run.json")) or {}
        return teardown(nova, _arg(argv, "--prefix", required=True), ids_seen_in_run(rd),
                        int(run.get("creates_issued") or 0), os.path.join(rd, "teardown.json"),
                        deadline_s=float(_arg(argv, "--deadline", "300")))
    if cmd == "analyse":
        if len(argv) != 3:
            sys.stderr.write("usage: nova-create-window.py analyse <run-dir>\n")
            return EXIT_USAGE
        code, rep = analyse_run(argv[2])
        print(render(rep, code))
        try:
            with open(os.path.join(argv[2], "report.json"), "w") as f:
                json.dump(rep, f, indent=2, default=str)
        except OSError:
            pass
        return code
    sys.stderr.write("unknown subcommand %r\n" % cmd)
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main(sys.argv))
