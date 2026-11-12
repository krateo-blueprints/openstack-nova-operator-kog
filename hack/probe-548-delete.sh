#!/usr/bin/env bash
# Does deleting a CR remove the VENDOR resource and release the finalizer?
#
# WHY THIS EXISTS. Every probe in hack/ measured creation, observation or
# restart behaviour. Checked rather than assumed: all seven read every 'delete'
# line, and each one is either teardown of the probe's own objects or ironic's
# GC control. NOT ONE deletes a managed CR and asserts the vendor resource is
# gone and the finalizer released -- on any kind, let alone on one of this
# repo's six apiKey documents. oasgen-provider's #548/#593 is about exactly that
# ordering: re-deriving the identifier through findby BEFORE releasing the
# finalizer.
#
# WHAT IT MEASURES, AND WHAT IT DOES NOT. The vendor is an in-cluster mock, so
# this measures the PROVIDER's finalizer behaviour against a vendor that
# behaves as instructed. It is not an acceptance row for "works against OVH",
# and must not be cited as one. Live OpenStack verification is parked by
# decision.
#
# THREE ARMS, and the middle one is the only one that can find a real defect:
#   happy  DELETE answers 204 and the resource goes            -> finalizer RELEASED
#   fail   DELETE answers 500 and the resource STAYS           -> finalizer RETAINED.
#          Releasing it here orphans a live vendor resource, which no amount of
#          happy-path testing can detect: a controller that releases
#          unconditionally passes the happy arm perfectly.
#   gone   DELETE answers 404 and the resource is already away -> finalizer RELEASED
#          (an idempotent delete must not wedge).
#
# A HAPPY PATH ALONE CANNOT DISTINGUISH correct ordering from no ordering at
# all, which is why there are three.
#
# WHAT THIS PROBE DOES NOT EXERCISE, confirmed by oasgen-provider from their own
# branch rather than inferred: the findby that #548 adds runs ONLY when the
# identifier is absent from status, and only when the create is known to have
# succeeded. Every arm here waits for 'status.<kind>.id' before deleting, so in
# every one of them the identifier IS present and the DELETE goes straight out
# with no findby at all. Asserting a 'findby -> DELETE -> release' sequence here
# would fail for the right reason: the sequence never happens. The window where
# it does -- a delete issued after the create landed at the vendor but before
# status was written -- is covered by keycloak-provider-kog's r1 row, on a
# bearer document.
#
# MODES:
#   --self-test       drive every outcome of the verdict table. No cluster, no docker.
#   --validate-mock   run the mock's SHIPPED BYTES in Docker and assert it really
#                     is stateful. Needs docker, not a cluster.
#   --dry-run         traverse every line of the cluster arm with the cluster
#                     stubbed. Exits 0 on purpose.
#   <kubeconfig> <context> [ns]   the full arm. Needs a cluster, creates none,
#                     deletes none.
set -uo pipefail
export LC_ALL=C

# SCRIPT-SCOPED so the EXIT trap can still reference them when it fires: these
# were 'local' in an earlier probe and the trap died under 'set -u' and LEFT THE
# CONTAINER RUNNING. Removal is BY ID, never by image filter -- other sessions
# on this machine run containers from the same images.
MOCK_CID=""
MOCK_TMP=""

ok()   { printf '\033[32m  OK: %s\033[0m\n' "$1"; }
bad()  { printf '\033[31m  FAIL: %s\033[0m\n' "$1"; FAILS=$((FAILS+1)); }
say()  { printf '  %s\n' "$1"; }
bold() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
void() { printf '\n\033[33m  VOID: %s\033[0m\n' "$1"; exit 9; }
FAILS=0
PROBE_UA=probe-548-control

# ---- the mock vendor, in ONE place ----------------------------------------
# nginx cannot do this. The claim under test is that after the CR is deleted
# the VENDOR RESOURCE IS GONE, and a static mock answers the same thing
# before and after -- so it could never tell a provider that deletes from one
# that only pretends to. This mock keeps state and EXPOSES it at /__state,
# which turns "a DELETE was issued" into "the resource is no longer there".
#
# QUOTED heredoc: the Python ships verbatim, and hack/probe-548-delete.sh
# --validate-mock extracts these exact bytes rather than a retyped copy.
mock_server() {
  cat <<'MOCKPY'
#!/usr/bin/env python3
# A STATEFUL mock Nova flavors endpoint. nginx cannot do this: the claim under
# test is that after a CR is deleted the VENDOR RESOURCE IS GONE, and a static
# mock can only ever answer the same thing before and after. So this one keeps
# state and exposes it, which turns "the provider issued a DELETE" into "the
# resource is no longer there".
#
# THE BEHAVIOUR IS KEYED BY THE REQUESTED FLAVOR NAME, so one mock serves all
# three arms and the arms differ only in the CR they apply:
#   *-fail  -> DELETE answers 500 and the resource STAYS. The finalizer must be
#              RETAINED: releasing it would orphan a live vendor resource, which
#              is the defect this arm exists to catch.
#   *-gone  -> DELETE answers 404 and the resource is removed. An idempotent
#              delete must still release the finalizer.
#   anything else -> DELETE answers 204 and the resource is removed.
#
# THE TOKEN VALUE IS NEVER LOGGED, only PRESENT/ABSENT, for the same reason the
# other mocks in this repo do it that way: a verdict must not be able to print a
# credential-shaped string.
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {}
SEQ = [0]


def behaviour(name):
    if name.endswith("-fail"):
        return "fail"
    if name.endswith("-gone"):
        return "gone"
    return "happy"


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass  # the default logger writes to stderr unstructured; ours is below

    def _log(self, status):
        tok = "PRESENT" if self.headers.get("X-Auth-Token") else "ABSENT"
        ua = self.headers.get("User-Agent") or "-"
        ua = ua.replace(" ", "+")
        print('%s "%s" %s tok=%s ua="%s"' % (self.client_address[0], self.command + " " + self.path, status, tok, ua), flush=True)

    def _send(self, status, body=None):
        payload = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)
        self._log(status)

    def _id_from_path(self):
        m = re.match(r"^/v2\.1/flavors/([^/?]+)$", self.path)
        return m.group(1) if m else None

    def do_GET(self):
        # The probe's own groundtruth read. Distinguishable in the log by its UA.
        if self.path == "/__state":
            return self._send(200, {"ids": sorted(STATE.keys())})
        fid = self._id_from_path()
        if fid is not None:
            if fid in STATE:
                return self._send(200, {"flavor": STATE[fid]})
            return self._send(404, {"itemNotFound": {"message": "not found", "code": 404}})
        # findby / list, which the provider may use to re-derive an identifier
        if self.path.startswith("/v2.1/flavors"):
            want = None
            m = re.search(r"[?&]name=([^&]+)", self.path)
            if m:
                want = m.group(1)
            items = [v for v in STATE.values() if want is None or v.get("name") == want]
            return self._send(200, {"flavors": items})
        return self._send(404, {"itemNotFound": {"message": "not found", "code": 404}})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw.decode() or "{}")
        except ValueError:
            body = {}
        flavor = body.get("flavor") or {}
        name = flavor.get("name") or "unnamed"
        SEQ[0] += 1
        fid = "%s-%d" % (behaviour(name), SEQ[0])
        STATE[fid] = {"id": fid, "name": name,
                      "ram": flavor.get("ram", 2048),
                      "vcpus": flavor.get("vcpus", 1),
                      "disk": flavor.get("disk", 20)}
        return self._send(200, {"flavor": STATE[fid]})

    def do_DELETE(self):
        fid = self._id_from_path()
        if fid is None:
            return self._send(404, {"itemNotFound": {"message": "not found", "code": 404}})
        kind = fid.split("-")[0]
        if kind == "fail":
            # The resource STAYS. This is the arm that can find a real defect.
            return self._send(500, {"computeFault": {"message": "simulated vendor failure", "code": 500}})
        STATE.pop(fid, None)
        if kind == "gone":
            return self._send(404, {"itemNotFound": {"message": "not found", "code": 404}})
        return self._send(204)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    print("mock-548 listening on %d" % port, flush=True)
    # THREADING, NOT HTTPServer, and this cost a cluster window.
    #
    # MEASURED: with 'protocol_version = "HTTP/1.1"' the server keeps
    # connections alive, and a single-threaded HTTPServer serves ONE connection
    # at a time -- so a client holding a persistent connection blocks every
    # other caller. The controller's Go HTTP client does exactly that. Reproduced
    # in Docker: hold one keep-alive connection open, and a second caller gets
    # HTTP 000, which is the identical symptom the cluster run reported
    # ("the observe verb answered '000'"). 200 again the moment it is released.
    #
    # The Docker validator could not catch it either: every one of its requests
    # was sequential and short-lived, so no two callers ever overlapped. The
    # fixture could not express concurrency, so it could not test for it.
    ThreadingHTTPServer(("", port), H).serve_forever()
MOCKPY
}

# ---- the verdict, as a FUNCTION so every reading is testable ----------------
# Inputs are OBSERVATIONS, not conclusions: whether the controller issued a
# DELETE at all, what the vendor state says afterwards, and whether the
# finalizer was released. The expected combination differs per arm, which is
# the whole point -- "finalizer released" is correct for two arms and a DEFECT
# for the third.
classify_delete() { # classify_delete <arm> <delete_issued> <vendor> <finalizer>
  local arm="${1:-}" del="${2:-}" vend="${3:-}" fin="${4:-}"
  # No DELETE reached the vendor: nothing downstream is a statement about
  # ordering, whatever the finalizer did. Checked FIRST, because a released
  # finalizer with no DELETE issued is the worst case and would otherwise read
  # as the happy arm passing.
  if [ "$del" != "yes" ]; then echo no-delete-issued; return 0; fi
  case "$arm" in
    happy)
      case "$vend/$fin" in
        gone/released)   echo correct ;;
        present/*)       echo vendor-kept-it ;;
        gone/retained)   echo finalizer-stuck ;;
        *)               echo unclassified ;;
      esac ;;
    fail)
      case "$vend/$fin" in
        # The resource is still there and the finalizer is still held: correct,
        # and the ONLY correct reading for this arm.
        present/retained) echo correct ;;
        # Released while the vendor still has it. This is the defect: a live
        # vendor resource with nothing in the cluster referring to it.
        present/released) echo orphaned ;;
        gone/*)           echo mock-did-not-fail ;;
        *)                echo unclassified ;;
      esac ;;
    gone)
      case "$vend/$fin" in
        gone/released)   echo correct ;;
        gone/retained)   echo stuck-on-404 ;;
        present/*)       echo mock-did-not-remove ;;
        *)               echo unclassified ;;
      esac ;;
    *) echo unclassified ;;
  esac
}

delete_self_test() {
  local pass=0 fail=0
  t() {
    if [ "$2" = "$3" ]; then printf '  %-60s PASS\n' "$1"; pass=$((pass+1))
    else printf '  %-60s FAIL want=%s got=%s\n' "$1" "$2" "$3"; fail=$((fail+1)); fi
  }
  bold "verdict self-test (no cluster, no docker)"
  t "happy: vendor gone + finalizer released -> correct" \
      correct            "$(classify_delete happy yes gone released)"
  t "happy: finalizer still held -> finalizer-stuck" \
      finalizer-stuck    "$(classify_delete happy yes gone retained)"
  t "happy: vendor kept it -> vendor-kept-it, not correct" \
      vendor-kept-it     "$(classify_delete happy yes present released)"
  # THE ARM THE WHOLE PROBE EXISTS FOR. A controller that releases the
  # finalizer unconditionally passes the happy arm perfectly and fails here.
  t "fail: vendor still has it + finalizer held -> correct" \
      correct            "$(classify_delete fail yes present retained)"
  t "fail: finalizer RELEASED while vendor still has it -> orphaned" \
      orphaned           "$(classify_delete fail yes present released)"
  t "fail: the mock did not actually fail -> says so, not 'correct'" \
      mock-did-not-fail  "$(classify_delete fail yes gone released)"
  t "gone: 404 delete still releases -> correct" \
      correct            "$(classify_delete gone yes gone released)"
  t "gone: wedged on a 404 -> stuck-on-404" \
      stuck-on-404       "$(classify_delete gone yes gone retained)"
  t "gone: the mock did not remove it -> says so" \
      mock-did-not-remove "$(classify_delete gone yes present released)"
  # NO DELETE ISSUED OUTRANKS EVERYTHING. Without this, an arm where the
  # controller never contacted the vendor but released the finalizer would read
  # as the happy arm passing -- the reassuring answer from a probe that measured
  # nothing.
  t "no DELETE issued -> outranks a released finalizer" \
      no-delete-issued   "$(classify_delete happy no gone released)"
  t "no DELETE issued -> outranks the fail arm too" \
      no-delete-issued   "$(classify_delete fail no present retained)"
  t "an unknown arm -> unclassified, not the nearest match" \
      unclassified       "$(classify_delete weird yes gone released)"
  t "unknown observations -> unclassified" \
      unclassified       "$(classify_delete happy yes '' '')"
  # CONTROL: the readings must be DISTINCT, or the table above could pass with a
  # function that returned one string.
  local n
  n=$(printf '%s\n' \
       "$(classify_delete happy yes gone released)" \
       "$(classify_delete happy yes gone retained)" \
       "$(classify_delete happy yes present released)" \
       "$(classify_delete fail  yes present released)" \
       "$(classify_delete fail  yes gone released)" \
       "$(classify_delete gone  yes gone retained)" \
       "$(classify_delete gone  yes present released)" \
       "$(classify_delete happy no  gone released)" \
       "$(classify_delete weird yes gone released)" | sort -u | grep -c .)
  t "the nine readings are distinct (control)" 9 "$n"
  echo
  echo "  $pass passed, $fail failed"
  [ "$fail" -eq 0 ] || return 1
  return 0
}

# ---- the log reader ---------------------------------------------------------
# ---8<--- DELETE_AWK
# One record per request: METHOD PATH STATUS TOKENSTATE UA. UA spaces are
# squeezed to '+' so callers select on $5 with awk's default splitting rather
# than matching a prefix of it.
# shellcheck disable=SC2016  # single quotes are required: $0 and the field
# references are AWK's. Double quotes would have the shell substitute its own
# positional parameters -- empty here -- leaving a program that parses nothing.
DELETE_AWK='
{
  st = ""; tok = ""; ua = ""; meth = ""; path = ""
  if (match($0, /"[A-Z]+ [^"]+"/)) {
    req = substr($0, RSTART+1, RLENGTH-2); split(req, r, " "); meth = r[1]; path = r[2]
  }
  # THE LENGTH ARITHMETIC WAS WRONG AND NOTHING CAUGHT IT. The match is
  # The matched text is  " 500 tok=  -- 2 characters of prefix (quote, space)
  # (space, t, o, k, =), so the status is RLENGTH-7 long, not RLENGTH-8. The
  # first version printed "50" for 500 and "40" for 404. --validate-mock
  # asserted the method, the caller and the token state, and never the status,
  # so the one field nobody asserted is the one that was broken. Found by the
  # dry run printing a table of two-digit HTTP codes.
  if (match($0, /" [0-9]+ tok=/))      { st  = substr($0, RSTART+2, RLENGTH-7) }
  if (match($0, /tok=[A-Z]+/))         { tok = substr($0, RSTART+4, RLENGTH-4) }
  if (match($0, /ua="[^"]*"/))         { ua  = substr($0, RSTART+4, RLENGTH-5) }
  if (meth == "" || path == "" || st == "" || tok == "") next
  if (ua == "") ua = "-"
  gsub(/ /, "+", ua)
  print meth, path, st, tok, ua
}
'
# ---8<--- END DELETE_AWK

validate_mock() {
  command -v docker >/dev/null 2>&1 || void "docker is not available; --validate-mock runs the mock."
  docker info >/dev/null 2>&1 || void "docker is installed but not responding, so the mock was never started. Not a pass."
  bold "--validate-mock: the measuring device, from the bytes this probe ships"
  local port base
  MOCK_TMP=$(mktemp -d)
  mock_server > "$MOCK_TMP/mock.py"
  # THE EXTRACTION IS ASSERTED. A validator that silently got an empty file
  # would start nothing and report whatever the absence of a server looks like.
  python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$MOCK_TMP/mock.py" 2>/dev/null \
    || void "the extracted mock is not valid Python, so nothing was validated."
  grep -q 'def do_DELETE' "$MOCK_TMP/mock.py" \
    || void "the extracted mock has no do_DELETE, so it cannot be the delete-path mock."
  ok "extracted $(wc -l < "$MOCK_TMP/mock.py" | tr -d ' ') lines of valid Python with a do_DELETE"

  MOCK_CID=$(docker run -d -p 0:8080 -v "$MOCK_TMP/mock.py:/mock.py:ro" \
             python:3.12-alpine python3 /mock.py 8080 2>/dev/null)
  [ -n "$MOCK_CID" ] || void "could not start the mock container; nothing was validated."
  port=$(docker port "$MOCK_CID" 8080/tcp 2>/dev/null | head -1 | sed 's/.*://')
  [ -n "$port" ] || void "port 8080 is not published, so the mock is unreachable."
  base="http://127.0.0.1:$port"
  local up=no
  for _ in $(seq 1 30); do
    if curl -s -o /dev/null -m 2 "$base/__state"; then up=yes; break; fi
    sleep 1
  done
  [ "$up" = yes ] || { say "logs: $(docker logs "$MOCK_CID" 2>&1 | tail -3 | tr '\n' '|')"
                       void "the mock never answered /__state, so it is not running the shipped bytes."; }
  say "mock listening on $base"

  _mk() { curl -s -m 5 -A "$PROBE_UA" -H 'X-Auth-Token: dummy-not-a-credential' \
            -X POST -H 'Content-Type: application/json' \
            -d "{\"flavor\":{\"name\":\"$1\",\"ram\":2048,\"vcpus\":1,\"disk\":20}}" \
            "$base/v2.1/flavors"; }
  _id()    { printf '%s' "$1" | sed -n 's/.*"id": *"\([^"]*\)".*/\1/p'; }
  _code()  { curl -s -o /dev/null -w '%{http_code}' -m 5 -A "$PROBE_UA" "$@"; }
  _state() { curl -s -m 5 -A "$PROBE_UA" "$base/__state"; }

  bold "ASSERTION 1 -- the behaviour is keyed by the requested NAME"
  local ih if_ ig
  ih=$(_id "$(_mk probe-val-ok)"); if_=$(_id "$(_mk probe-val-x-fail)"); ig=$(_id "$(_mk probe-val-x-gone)")
  if [ "${ih#happy-}" != "$ih" ] && [ "${if_#fail-}" != "$if_" ] && [ "${ig#gone-}" != "$ig" ]; then
    ok "ids keyed by name: $ih / $if_ / $ig"
  else
    bad "create did not key the id by the requested name (got '$ih' '$if_' '$ig'). The three
    arms would then all measure the same behaviour."
  fi

  bold "ASSERTION 2 -- the mock is STATEFUL, which nginx could not be"
  local s0 s1
  s0=$(_state)
  if printf '%s' "$s0" | grep -q "$ih" && printf '%s' "$s0" | grep -q "$if_"; then
    ok "all three creates are visible in /__state"
  else bad "a created id is missing from /__state ($s0); the vendor-side assertion would be blind."; fi

  bold "ASSERTION 3 -- happy: 204, then GONE from state and 404 on re-read"
  local dh gh
  dh=$(_code -X DELETE "$base/v2.1/flavors/$ih"); gh=$(_code "$base/v2.1/flavors/$ih")
  s1=$(_state)
  if [ "$dh" = "204" ] && [ "$gh" = "404" ] && ! printf '%s' "$s1" | grep -q "$ih"; then
    ok "DELETE $ih -> 204, re-read 404, removed from state"
  else bad "happy arm's mock misbehaved: DELETE=$dh re-read=$gh state=$s1"; fi

  bold "ASSERTION 4 -- fail: 500, and the resource MUST STAY"
  # The arm that gives the probe its teeth. If the mock quietly removed the
  # resource on a 500, the fail arm would measure the happy path and a
  # controller that releases the finalizer unconditionally would pass it.
  local df gf
  df=$(_code -X DELETE "$base/v2.1/flavors/$if_"); gf=$(_code "$base/v2.1/flavors/$if_")
  s1=$(_state)
  if [ "$df" = "500" ] && [ "$gf" = "200" ] && printf '%s' "$s1" | grep -q "$if_"; then
    ok "DELETE $if_ -> 500, re-read 200, STILL in state"
  else bad "the fail arm's mock did not keep the resource: DELETE=$df re-read=$gf state=$s1.
    Without this the fail arm cannot detect an orphaned vendor resource."; fi

  bold "ASSERTION 5 -- gone: 404, and removed"
  local dg gg
  dg=$(_code -X DELETE "$base/v2.1/flavors/$ig"); gg=$(_code "$base/v2.1/flavors/$ig")
  if [ "$dg" = "404" ] && [ "$gg" = "404" ]; then ok "DELETE $ig -> 404, re-read 404"
  else bad "the gone arm's mock answered DELETE=$dg re-read=$gg, not 404/404."; fi

  bold "ASSERTION 6 -- the three DELETE outcomes must DIFFER"
  # Three arms that all received the same status would be one arm run three
  # times, and every per-arm verdict below would be the same measurement.
  if [ "$(printf '%s\n%s\n%s\n' "$dh" "$df" "$dg" | sort -u | grep -c .)" = "3" ]; then
    ok "204 / 500 / 404 are three distinct outcomes"
  else bad "the DELETE statuses are not distinct ($dh/$df/$dg); the arms do not differ."; fi

  bold "ASSERTION 7 -- findby can re-derive an id from a name"
  # #593 re-derives the identifier through findby before releasing the
  # finalizer, so the mock must be able to answer that query at all.
  if curl -s -m 5 -A "$PROBE_UA" "$base/v2.1/flavors?name=probe-val-x-fail" | grep -q "$if_"; then
    ok "findby by name returns the created flavor"
  else bad "findby by name did not return the flavor; a provider that re-derives the id would fail for a reason that is the mock's."; fi

  bold "ASSERTION 8 -- the token is recorded as PRESENT/ABSENT and never as a value"
  local logs nP nA
  logs=$(docker logs "$MOCK_CID" 2>&1)
  nP=$(printf '%s\n' "$logs" | awk "$DELETE_AWK" | awk '$4=="PRESENT"' | grep -c .)
  nA=$(printf '%s\n' "$logs" | awk "$DELETE_AWK" | awk '$4=="ABSENT"'  | grep -c .)
  if [ "$nP" -ge 1 ] && [ "$nA" -ge 1 ]; then
    ok "both token states observed (PRESENT=$nP ABSENT=$nA), so the field discriminates"
  else bad "only one token state observed (PRESENT=$nP ABSENT=$nA). A constant field cannot
    distinguish a credentialled request from an uncredentialled one."; fi
  if printf '%s\n' "$logs" | grep -qF 'dummy-not-a-credential'; then
    bad "the access log contains the token VALUE."
  else ok "only PRESENT/ABSENT reached the log"; fi

  bold "ASSERTION 9 -- the SHIPPED parser, on real mock output"
  local recs own oth noise badm
  curl -s -o /dev/null -m 5 -A 'Go-http-client/1.1' "$base/v2.1/flavors" >/dev/null 2>&1
  sleep 1
  recs=$(docker logs "$MOCK_CID" 2>&1 | awk "$DELETE_AWK")
  own=$(printf '%s\n' "$recs" | awk -v u="$PROBE_UA" '$5==u' | grep -c .)
  oth=$(printf '%s\n' "$recs" | awk '$5=="Go-http-client/1.1"' | grep -c .)
  if [ "$own" -ge 1 ] && [ "$oth" -ge 1 ]; then
    ok "separation works on real bytes: $own the probe's, $oth another client's"
  else bad "the parser did not separate the two User-Agents (own=$own other=$oth). The cluster
    arm's 'the controller never issued a DELETE' verdict depends on it."; fi
  # Every record must carry a real HTTP method, or non-request lines are being
  # parsed -- the mock prints a startup line too.
  badm=$(printf '%s\n' "$recs" | awk '$1!="GET" && $1!="POST" && $1!="DELETE" && $1!="PUT" && $1!="PATCH"' | grep -c .)
  if [ "$badm" -eq 0 ]; then ok "every record carries a real HTTP method"
  else bad "$badm record(s) carry something that is not an HTTP method; non-request lines are becoming records."
       say "offending: $(printf '%s\n' "$recs" | awk '$1!="GET" && $1!="POST" && $1!="DELETE"' | head -3 | tr '\n' '|')"; fi
  # THE STATUS FIELD, which nothing asserted until it was broken. The parser's
  # length arithmetic was one character short, so 500 parsed as "50" and 404 as
  # "40" -- and every other arm here passed, because they check the method, the
  # caller and the token state. The field nobody asserts is the field that
  # breaks. Checked against the three codes this mock is known to have served.
  local codes
  codes=$(printf '%s\n' "$recs" | awk '{print $3}' | sort -u | tr '\n' ' ')
  if printf '%s\n' "$recs" | awk '$3=="204"' | grep -q . \
     && printf '%s\n' "$recs" | awk '$3=="500"' | grep -q . \
     && printf '%s\n' "$recs" | awk '$3=="404"' | grep -q .; then
    ok "the three-digit statuses survive parsing (saw: $codes)"
  else
    bad "the parsed statuses are missing one of 204/500/404, which this mock is known to
    have served (saw: $codes). A status truncated to two digits still sorts and
    prints, so nothing downstream would look wrong."
  fi
  noise=$(printf '%s\n' 'mock-548 listening on 8080' 'Traceback (most recent call last):' | awk "$DELETE_AWK" | grep -c .)
  if [ "$noise" -eq 0 ]; then ok "the mock's own startup and traceback lines yield zero records"
  else bad "non-request lines produced $noise record(s)."; fi

  bold "ASSERTION 10 -- one slow client must not block every other caller"
  # THE ARM THAT WOULD HAVE SAVED A CLUSTER WINDOW. The cluster arm VOIDed with
  # "the observe verb answered '000'": the mock was a single-threaded HTTPServer
  # with protocol_version HTTP/1.1, so the controller's Go client held a
  # keep-alive connection and every other caller was refused. Reproduced here --
  # hold one connection, second caller gets 000; release it, 200 again.
  #
  # NINE ASSERTIONS PASSED OVER IT, because every request they make is
  # sequential and short-lived: the fixture could not express concurrency, so it
  # could not test for it. This arm makes two callers overlap, which is the
  # discriminating input.
  local held_rc held_code
  python3 - "$port" <<'HOLDER' &
import socket, sys, time
s = socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=5)
s.sendall(b"GET /__state HTTP/1.1\r\nHost: x\r\nConnection: keep-alive\r\n\r\n")
s.recv(65536)
time.sleep(8)
s.close()
HOLDER
  held_rc=$!
  sleep 2
  held_code=$(curl -s -o /dev/null -w '%{http_code}' -m 6 "$base/__state" 2>/dev/null)
  wait "$held_rc" 2>/dev/null
  if [ "$held_code" = "200" ]; then
    ok "a second caller got 200 while a keep-alive connection was held open"
  else
    bad "a second caller got '${held_code:-nothing}' while one keep-alive connection was
    held. The server serialises callers, so the controller's persistent connection
    will block this probe's own reads -- which is exactly how the cluster arm
    VOIDed with 000. Use ThreadingHTTPServer."
  fi
  # GREPPED IN THE EXTRACTED MOCK, NOT IN "$0", and the difference is the whole
  # assertion. My first version searched this script for the server-class string
  # -- which the grep's OWN LINE contains, so it always matched and the arm was
  # vacuous. Proven by mutation: reverting the mock to HTTPServer produced a
  # VOID from Python (the name was undefined), never a FAIL from this check,
  # because this check could not fail. A self-matching detector is a defect this
  # repo has shipped before.
  if grep -q 'ThreadingHTTPServer' "$MOCK_TMP/mock.py"; then
    ok "the EXTRACTED mock serves with ThreadingHTTPServer"
  else
    bad "the extracted mock is not using ThreadingHTTPServer, so one persistent client
    blocks all others -- which is how the cluster arm VOIDed with 000."
  fi

  bold "RESULT"
  if [ "$FAILS" -ne 0 ]; then
    printf '\033[31m  %s assertion(s) failed: the delete arms would measure with a broken instrument.\033[0m\n' "$FAILS"
    return 1
  fi
  ok "the shipped mock is stateful, keys behaviour by name, gives three distinct DELETE outcomes, and leaks no token."
  say "It could have reported a broken instrument and did not."
  return 0
}

# shellcheck disable=SC2329  # invoked by the EXIT trap on the next line
mock_cleanup() {
  [ -n "$MOCK_CID" ] && docker rm -f "$MOCK_CID" >/dev/null 2>&1
  [ -n "$MOCK_TMP" ] && rm -rf "$MOCK_TMP"
  return 0
}
trap mock_cleanup EXIT

if [ "${1:-}" = "--self-test" ]; then
  delete_self_test || { printf '\n\033[31mFAIL: the verdict table does not behave as specified.\033[0m\n' >&2; exit 1; }
  ok "the verdict table can produce every one of its nine readings"
  exit 0
fi
if [ "${1:-}" = "--validate-mock" ]; then validate_mock; exit $?; fi

# ---- --dry-run: traverse every line of the cluster arm with no cluster -------
# hack/probe-590-restart.sh died on a BILLING GKE cluster with "N_CTRL: unbound
# variable" -- a name used one line above its assignment, unreachable by any
# check that stopped short of it. This traverses the whole main path under
# 'set -u'. It stubs the cluster, not the logic: every condition, count and
# classification runs for real. Exits 0 ON PURPOSE.
DRY=0
if [ "${1:-}" = "--dry-run" ]; then DRY=1; shift; fi
if [ "$DRY" = "1" ]; then
  set -- "${1:-/dev/null}" "${2:-dry-context}" "${3:-krateo-system}"
  # STUBS KEY ON THE ARGUMENTS, NEVER ON SHARED STATE. The first version kept a
  # "which arm are we in" file that the production code would have had to
  # maintain -- so it stayed at "none", the id lookup returned empty, and the
  # dry run hung for 240s in wait_for. A stub that needs the real code to help
  # it is not a stub. Everything below is derivable from the CR name in "$*",
  # which the real calls already carry.
  dry_log() {
    cat <<'DRYLOG'
10.0.0.5 "POST /v2.1/flavors" 200 tok=PRESENT ua="Go-http-client/1.1"
10.0.0.5 "DELETE /v2.1/flavors/happy-1" 204 tok=PRESENT ua="Go-http-client/1.1"
10.0.0.5 "DELETE /v2.1/flavors/fail-2" 500 tok=PRESENT ua="Go-http-client/1.1"
10.0.0.5 "DELETE /v2.1/flavors/gone-3" 404 tok=PRESENT ua="Go-http-client/1.1"
10.0.0.9 "GET /__state" 200 tok=ABSENT ua="probe-548-control"
mock-548 listening on 8080
DRYLOG
  }
  K() {
    case "$*" in
      version*)                     return 0 ;;
      *"get crd"*)                  echo crd-exists; return 0 ;;
      *"create configmap"*)         echo "apiVersion: v1"; return 0 ;;
      *"rollout status"*)           return 0 ;;
      *"logs deploy/mock-548"*)     dry_log; return 0 ;;
      # THE REAL _curl_in_cluster RUNS, and this answers the pod it creates.
      # An earlier version stubbed _curl_in_cluster itself -- dead code, because
      # the real definition appears later in the file and overrides it. The
      # symptom was arm fail reading "mock-did-not-fail": the fallback stub
      # returned nothing, so the vendor looked empty. Keying on the pod name
      # keeps every stub derivable from arguments AND exercises the real helper.
      *"run probe-548-state-fail"*) echo '{"ids": ["fail-2"]}'; return 0 ;;
      *"run probe-548-state-"*)     echo '{"ids": []}'; return 0 ;;
      # The OBSERVE verb, which must AGREE with /__state above or the arm VOIDs.
      # Keyed on the pod name like everything else, so the two stubs cannot
      # drift into the disagreement they exist to detect.
      *"run probe-548-obs-fail"*)   echo 200; return 0 ;;
      *"run probe-548-obs-"*)       echo 404; return 0 ;;
      # The vendor id the controller wrote into status, keyed by CR name.
      *jsonpath=\{.status.flavor.id\}*)
          case "$*" in
            *probe-548-x-fail*) echo fail-2  ;;
            *probe-548-x-gone*) echo gone-3  ;;
            *probe-548-ok*)     echo happy-1 ;;
            *)                  echo "" ;;
          esac; return 0 ;;
      *status.conditions*)          echo "type=Ready status=True reason=Available message="; return 0 ;;
      # Existence AFTER the delete: only the fail arm's object survives, because
      # its finalizer is correctly retained while the vendor still has it.
      *"get computeflavors"*)
          case "$*" in
            *probe-548-x-fail*) echo probe-548-x-fail; return 0 ;;
            *)                  return 1 ;;
          esac ;;
      *"patch "*)                   return 0 ;;
      *delete*)                     return 0 ;;
      *"apply -f -"*)               cat >/dev/null 2>&1; return 0 ;;
      *apply*)                      return 0 ;;
      *)                            return 0 ;;
    esac
  }
  H() { return 0; }
  # The fail arm deliberately never disappears, so at the production bound this
  # traversal would spend 90 real seconds proving a timeout it already knows
  # about. Shortened here and PRINTED by the arm, never silently.
  : "${PROBE_548_GONE_WAIT:=6}"
  export PROBE_548_GONE_WAIT
fi

KUBECONFIG_PATH="${1:?usage: probe-548-delete.sh <kubeconfig> <context> [namespace] | --self-test | --validate-mock | --dry-run}"
CONTEXT="${2:?a context is required: never rely on the current kubectl context, because the shared kubeconfig reaches real remote clusters}"
NS="${3:-krateo-system}"
if [ "$DRY" = "0" ]; then
  K() { kubectl --kubeconfig "$KUBECONFIG_PATH" --context "$CONTEXT" "$@"; }
  H() { helm --kubeconfig "$KUBECONFIG_PATH" --kube-context "$CONTEXT" "$@"; }
fi

GROUP=nova.openstack.krateo.io
HOST="mock-548.${NS}.svc.cluster.local"
CFG=probe-548-cfg
SEC=probe-548-token
# How long a released finalizer is allowed to take. Stated, because "retained"
# and "slower than this" are the same observation and only the bound separates
# them from each other.
# Overridable so --dry-run does not pay 90 real seconds to traverse the fail
# arm, whose whole point is that the object does NOT disappear. The bound is
# printed with every arm's observations, so a run at a shortened bound says so
# in its own output rather than looking like a 90s result.
GONE_WAIT=${PROBE_548_GONE_WAIT:-90}

[ -f chart/Chart.yaml ] || void "run me from the repo root: chart/Chart.yaml is not here."
# shellcheck source=hack/lib/wait.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/wait.sh"

bold "preconditions -- each failure says what it means"
K version --request-timeout=10s >/dev/null 2>&1 \
  || void "the API server at context $CONTEXT is not reachable. Nothing was measured."
# WRITTEN AS AN EXPLICIT CONDITION, not 'A && B || void'. That shape means
# "void unless both" here, which is correct -- but it is one character away
# from the bug shellcheck warns about, and this repo has already shipped a
# 'A && ok || bad' that reported a finding because the printf returned
# non-zero. The clearer form costs two lines.
if ! K get crd "computeflavors.${GROUP}" >/dev/null 2>&1 \
   || ! K get crd "computeflavorconfigurations.${GROUP}" >/dev/null 2>&1; then
  void "both halves of the ComputeFlavor pair must be served. Install the chart first;
    this probe measures delete behaviour, not installation."
fi
ok "API reachable and both halves of the pair are served"

bold "mock-548: a STATEFUL vendor whose DELETE behaviour is keyed by flavor name"
MOCK_PY=$(mock_server)
printf '%s\n' "$MOCK_PY" | grep -q 'def do_DELETE' \
  || void "the embedded mock has no do_DELETE, so there is nothing to measure a delete against."
K -n "$NS" create configmap mock-548-src --from-literal=mock.py="$MOCK_PY" \
  --dry-run=client -o yaml 2>/dev/null | K -n "$NS" apply -f - >/dev/null 2>&1 \
  || void "could not apply the mock source ConfigMap."
K -n "$NS" apply -f - >/dev/null 2>&1 <<YAML || void "could not apply the mock Deployment/Service."
apiVersion: apps/v1
kind: Deployment
metadata: { name: mock-548, namespace: ${NS} }
spec:
  replicas: 1
  selector: { matchLabels: { app: mock-548 } }
  template:
    metadata: { labels: { app: mock-548 } }
    spec:
      containers:
        - name: mock
          image: python:3.12-alpine
          command: ["python3", "/src/mock.py", "8080"]
          ports: [ { containerPort: 8080 } ]
          volumeMounts: [ { name: src, mountPath: /src } ]
          # tcpSocket for the same two reasons as probe-portonly: it proves the
          # server is LISTENING without generating HTTP traffic, and an httpGet
          # on /__state would appear in the access log as a caller with UA
          # 'kube-probe/...' -- a second non-probe caller, which the attribution
          # check below would correctly report as mixing callers. The check was
          # written to notice foreign traffic; the readiness probe should not BE
          # the foreign traffic.
          readinessProbe:
            tcpSocket: { port: 8080 }
            initialDelaySeconds: 1
            periodSeconds: 2
      volumes:
        - name: src
          configMap: { name: mock-548-src }
---
apiVersion: v1
kind: Service
metadata: { name: mock-548, namespace: ${NS} }
spec:
  selector: { app: mock-548 }
  ports: [ { port: 8080, targetPort: 8080 } ]
YAML
K -n "$NS" rollout status deploy/mock-548 --timeout=240s >/dev/null 2>&1 \
  || void "the mock never became ready. Nothing was measured; check:
    kubectl -n $NS logs deploy/mock-548"
ok "stateful mock rolled out, readiness gated on /__state answering"

_curl_in_cluster() { # _curl_in_cluster <podname> <url>
  K -n "$NS" run "$1" --rm -i --quiet --restart=Never \
    --image=curlimages/curl:8.11.0 -- -s -m 10 -A "$PROBE_UA" "$2" 2>/dev/null
}
vendor_ids() { _curl_in_cluster "probe-548-state-$1" "http://${HOST}:8080/__state"; }
# The OBSERVE verb's answer, which is what the CONTROLLER actually decides on.
# oasgen-provider confirmed from their branch that a 500 on DELETE decides
# NOTHING: resolveDeleteOutcome asks the observe verb and applies one rule --
#   500 + observe PRESENT -> error returned, finalizer HELD, delete retried
#   500 + observe ABSENT  -> RELEASED, because the delete evidently worked
#   500 + no get/findby   -> "not verifiable", the delete result is trusted, held
# So the fail arm only measures what it claims if the mock KEEPS the resource
# AND reports it PRESENT on observe. If it 500s the DELETE while reporting the
# resource gone, the controller RELEASES -- correctly, on the evidence it was
# given -- and this probe would have called that 'orphaned': a false finding
# about someone else's code. That property is now asserted, not assumed.
observe_code() { # observe_code <arm> <vendor-id>
  K -n "$NS" run "probe-548-obs-$1" --rm -i --quiet --restart=Never \
    --image=curlimages/curl:8.11.0 -- \
    -s -o /dev/null -w '%{http_code}' -A "$PROBE_UA" \
    "http://${HOST}:8080/v2.1/flavors/$2" 2>/dev/null | grep -oE '[0-9]{3}' | head -1
}
read_records() { K -n "$NS" logs deploy/mock-548 --tail=1000 2>/dev/null | awk "$DELETE_AWK"; }

STAGE=$(mktemp -d)
cp -R chart "$STAGE/chart"
find "$STAGE/chart" -name Chart.yaml -exec sed -i.bak "s/CHART_VERSION/0.0.0-probe/g" {} + 2>/dev/null
find "$STAGE/chart" -name '*.bak' -delete 2>/dev/null
H upgrade --install nova "$STAGE/chart" -n "$NS" --set nova.url="http://${HOST}:8080/v2.1" \
  --wait --timeout 8m > /tmp/probe-548-helm.log 2>&1 \
  || { sed 's/^/    /' /tmp/probe-548-helm.log >&2; void "helm upgrade failed; nothing was measured."; }
ok "chart pointed at the stateful mock (one endpoint; behaviour is keyed by flavor NAME)"

K -n "$NS" apply -f - >/dev/null 2>&1 <<YAML || void "could not apply the Secret and Configuration."
apiVersion: v1
kind: Secret
metadata: { name: ${SEC}, namespace: ${NS} }
stringData: { token: dummy-not-a-credential }
---
apiVersion: ${GROUP}/v1alpha1
kind: ComputeFlavorConfiguration
metadata: { name: ${CFG}, namespace: ${NS} }
spec:
  authentication:
    apiKey:
      tokenRef: { name: ${SEC}, namespace: ${NS}, key: token }
      header: X-Auth-Token
YAML
ok "apiKey Configuration applied -- this is a measurement on an apiKey document"

VERDICTS=""
run_arm() { # run_arm <arm> <cr-name>
  local arm="$1" cr="$2" vid fin vend del_issued v
  bold "ARM ${arm} -- CR ${cr}"
  K -n "$NS" apply -f - >/dev/null 2>&1 <<YAML || void "arm $arm: the CR did not apply, so the arm measured nothing."
apiVersion: ${GROUP}/v2-1
kind: ComputeFlavor
metadata: { name: ${cr}, namespace: ${NS} }
spec:
  configurationRef: { name: ${CFG}, namespace: ${NS} }
  flavor: { name: ${cr}, ram: 2048, vcpus: 1, disk: 20 }
YAML
  # THE IDENTIFIER MUST EXIST BEFORE A DELETE MEANS ANYTHING. Without it the
  # controller has nothing to delete at the vendor, and a released finalizer
  # would be correct for a reason that has nothing to do with #593.
  # shellcheck disable=SC2329  # invoked by NAME through wait_for
  _has_id() { [ -n "$(K -n "$NS" get "computeflavors.${GROUP}" "$cr" -o 'jsonpath={.status.flavor.id}' 2>/dev/null)" ]; }
  wait_for "arm $arm: the CR to carry a vendor id in status" 40 6 _has_id \
    || void "arm $arm: the CR never got a vendor id within 240s. That is a create-path
    TIMEOUT, not a finding about delete."
  vid=$(K -n "$NS" get "computeflavors.${GROUP}" "$cr" -o 'jsonpath={.status.flavor.id}' 2>/dev/null)
  say "vendor id: $vid"

  K -n "$NS" delete "computeflavors.${GROUP}" "$cr" --wait=false >/dev/null 2>&1
  # Bounded, and the bound is printed: "retained" and "slower than the bound"
  # are the same observation, and only the stated bound separates them.
  # shellcheck disable=SC2329  # invoked by NAME through wait_for
  _gone() { ! K -n "$NS" get "computeflavors.${GROUP}" "$cr" >/dev/null 2>&1; }
  if wait_for "arm $arm: the CR object to disappear" "$((GONE_WAIT / 5))" 5 _gone >/dev/null 2>&1; then
    fin=released
  else
    fin=retained
  fi
  sleep 3
  # VENDOR PRESENCE, READ TWO WAYS, and they must agree. /__state is the mock's
  # own truth; the observe verb is the input the controller decided on. If they
  # disagree the arm cannot say which the controller acted on, so it VOIDs
  # rather than guessing -- the same discipline as probe-401 cross-checking curl
  # against the mock's access log.
  local st_has obs
  if printf '%s' "$(vendor_ids "$arm")" | grep -qF "$vid"; then st_has=yes; else st_has=no; fi
  obs=$(observe_code "$arm" "$vid")
  case "${st_has}/${obs:-none}" in
    yes/200) vend=present ;;
    no/404)  vend=gone ;;
    *)       void "arm $arm: the two readings of vendor presence DISAGREE -- /__state says
    $(if [ "$st_has" = yes ]; then echo present; else echo absent; fi) and the observe verb
    answered '${obs:-no status at all}'. The controller decides on the OBSERVE answer, so
    this arm cannot say which input produced the finalizer outcome. Not a finding." ;;
  esac
  # Did the CONTROLLER issue the DELETE, as distinct from the probe? The probe
  # never issues a DELETE, but it does query /__state, so the marker is what
  # keeps the two apart -- and a DELETE attributed to the probe would make
  # every verdict below a measurement of this script.
  if read_records | awk -v u="$PROBE_UA" -v id="$vid" '$1=="DELETE" && $5!=u && index($2, id)' | grep -q .; then
    del_issued=yes
  else
    del_issued=no
  fi
  v=$(classify_delete "$arm" "$del_issued" "$vend" "$fin")
  say "delete_issued=$del_issued vendor=$vend finalizer=$fin (bound ${GONE_WAIT}s)"
  case "$v" in
    correct)             ok "arm $arm: $v" ;;
    orphaned)            bad "arm $arm: ORPHANED -- the finalizer was released while the vendor
    still has $vid. Nothing in the cluster now refers to a live vendor resource.
    This is the defect the fail arm exists to find, and no happy path can see it." ;;
    no-delete-issued)    bad "arm $arm: the controller never issued a DELETE for $vid, so the
    finalizer outcome says nothing about ordering. The arm measured something else." ;;
    mock-did-not-fail|mock-did-not-remove)
                         bad "arm $arm: $v -- the INSTRUMENT did not do what the arm requires, so
    this arm is void rather than a finding. --validate-mock asserts exactly this
    and passed in isolation, so look at the mock's logs in the cluster." ;;
    *)                   bad "arm $arm: $v" ;;
  esac
  VERDICTS="${VERDICTS}${arm}=${v} "
}

run_arm happy probe-548-ok
run_arm fail  probe-548-x-fail
run_arm gone  probe-548-x-gone

bold "CALLER COLUMN -- an unmarked request of the probe's own would read as the controller"
ALL=$(read_records)
printf '    %-7s %-34s %-5s %-9s %s\n' METHOD PATH CODE TOKEN CALLER
printf '%s\n' "$ALL" | awk -v u="$PROBE_UA" '$5!=u {print $1, $2, $3, $4, $5}' | sort -u | head -20 |
  while read -r m p c t cal; do printf '    %-7s %-34s %-5s %-9s %s\n' "$m" "$p" "$c" "$t" "$cal"; done
N_CALLERS=$(printf '%s\n' "$ALL" | awk -v u="$PROBE_UA" '$5!=u {print $5}' | sort -u | grep -c .)
if [ "${N_CALLERS:-0}" -eq 1 ]; then
  ok "exactly one non-probe caller, so every count above is attributable to it"
else
  bad "$N_CALLERS distinct non-probe callers reached the mock. An unmarked request of the
    probe's own appears here as the controller's; read the table before the verdicts."
fi

bold "SUMMARY"
say "$VERDICTS"
say "Measured against an in-cluster mock that behaves as instructed. This is the"
say "PROVIDER's finalizer behaviour, not an acceptance row for a real OpenStack."

bold "cleanup -- including any finalizer this probe's own arms left held"
# THE FAIL ARM LEAVES A STUCK OBJECT BY DESIGN: its finalizer is correctly
# retained because the vendor still has the resource. Left alone it would wedge
# the namespace for the next run and for anyone else using this cluster, so the
# finalizer is removed explicitly -- after the measurement, never before it.
for cr in probe-548-ok probe-548-x-fail probe-548-x-gone; do
  if K -n "$NS" get "computeflavors.${GROUP}" "$cr" >/dev/null 2>&1; then
    K -n "$NS" patch "computeflavors.${GROUP}" "$cr" --type=merge \
      -p '{"metadata":{"finalizers":[]}}' >/dev/null 2>&1
    K -n "$NS" delete "computeflavors.${GROUP}" "$cr" --ignore-not-found --timeout=30s >/dev/null 2>&1
  fi
done
K -n "$NS" delete "computeflavorconfigurations.${GROUP}" "$CFG" --ignore-not-found >/dev/null 2>&1
K -n "$NS" delete secret "$SEC" --ignore-not-found >/dev/null 2>&1
K -n "$NS" delete deploy/mock-548 svc/mock-548 cm/mock-548-src --ignore-not-found >/dev/null 2>&1
rm -rf "$STAGE"

if [ "$DRY" = "1" ]; then
  bold "DRY RUN COMPLETE"
  say "Every line of the cluster arm executed under set -u, all three arms, the"
  say "caller column, the verdict table and the finalizer cleanup. That is ALL it proves."
  say "It contacted no cluster and deleted nothing."
  exit 0
fi
[ "$FAILS" -eq 0 ] || exit 1
exit 0
