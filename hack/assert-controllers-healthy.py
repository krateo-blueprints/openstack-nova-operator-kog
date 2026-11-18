#!/usr/bin/env python3
"""Every generated controller must have COME UP and STAYED UP. Two questions.

WHY THIS EXISTS, and it is a gap in this repo's own instrument rather than a new
idea. `hack/assert-served-controlled.py` asserts that a Deployment NAMED
`<restdefinition>-controller` exists for every served version. That is the right
question for "did the provider render a controller for this version", and it is
silent about whether anything inside it runs: a Deployment whose every pod is
crashlooping satisfies it completely.

On 2026-11-18 I promised a peer "do all six controllers reach Ready" and ran a
check that could not answer it. The numbers I reported came from raw Deployment
JSON a run happened to leave behind, not from an arm designed to ask. The
prediction being tested -- a Lease `create` denied because RBAC cannot authorize
a create by `resourceNames`, so controller-runtime exits -- produces EXACTLY the
failure this gap hides: the pod passes its probe, reports available, and then
exits. `availableReplicas: 1` read once is true in that world too.

SO THERE ARE THREE QUESTIONS, NOT ONE, AND THE FIRST TWO ARE NOT ENOUGH:

    came up    availableReplicas >= 1          a pod passed its readiness probe
    stayed up  restartCount == 0 after settle  nothing exited and was restarted
    ACQUIRED   the lease line in its log       it is actually reconciling anything

THE THIRD ONE EXISTS BECAUSE THE FIRST TWO BOTH PASS ON A BROKEN CLUSTER, and
this script shipped with only the first two while claiming to close the gap.
oasgen-provider measured the real behaviour against 0.83.0-rc1's own SHA:

  * the generated Role pairs `create` with `resourceNames`, which RBAC cannot
    authorize -- the object's name is in the request body, not in the
    authorization attributes -- so the Lease create is denied
  * a denied create does NOT exit. unstructured-runtime's ownership select{}
    waits on `acquired` or `ctx.Done()` with no timeout and no os.Exit, and
    client-go retries a denied create forever. The process stays up and starts
    NO WORKERS.
  * `values.yaml` ships `readinessProbe: null`, so a pod is Ready as soon as the
    container is running. `ownershipReadyzError()` exists and would surface it;
    nothing calls it, because there is no probe.

So: available, zero restarts, Ready, reconciling nothing, and every health check
in the platform agreeing. I had tuned this script to a predicted CRASH LOOP --
a symptom relayed to me and later retracted -- which is building a detector for
the failure you expect instead of for the question you have.

A LOG STRING THAT NO LONGER MATCHES MUST NOT READ AS HEALTHY. If neither the
waiting line nor the holding line is found, the acquisition question was not
answered and the verdict is VOID, not OK. That is the whole reason this arm can
be trusted in a way the first two could not.

A restart is not a warning here. A controller that exits and is restarted by the
kubelet is the signature of the denied-create failure, and a run that reports
"6 of 6 available" over six pods on their third restart is the kind of
reassuring reading this repo exists to stop producing.

VOID VS FAIL, kept distinct as everywhere else here:

    VOID (3)  no controller Deployments at all, or no pods to match them --
              nothing was measured, and a pass would be a statement about the
              query rather than about the controllers
    FAIL (1)  measured, and a controller is down or has restarted
    OK   (0)  every controller available with zero restarts after the settle

The settle interval is the caller's business -- this script judges whatever
snapshot it is handed. A caller that reads immediately and calls the result
"stayed up" has made the same mistake one level up, which is why the harness
passes SETTLE_SECONDS in and prints it beside the verdict.
"""
import json
import sys

VOID = 3
FAIL = 1
OK = 0

SUFFIX = "-controller"

# The two lines that separate "alive" from "working". Supplied by the
# oasgen-provider session from unstructured-runtime's own source; neither was in
# any alternation this repo had, because every earlier attempt was looking for a
# crash.
# A ONE-SHOT LINE IS NOT A DETECTOR. Both of these are emitted exactly once, by
# acquireOwnership, so finding neither says only that the capture missed the
# window -- and the failure direction is the bad one: a missed HOLDING reads as
# "did not acquire" on a controller that acquired fine. A peer's run two minutes
# after startup got 0/0 on an install that was definitely broken; mine saw them
# only because the capture began at pod start. They are kept as CORROBORATION
# and are no longer the verdict.
HOLDING = "Holding the ownership lease; starting workers"
WAITING = "Waiting for the ownership lease before reconciling"
# THIS one repeats, per lease operation, and carries the apiserver's refusal
# verbatim -- so its ABSENCE is weak and its PRESENCE is conclusive.
REFUSED = "Cannot read or write the ownership lease"


def judge(deploys, pods, settle=None, logs=None, leases=None):
    """Pure verdict over the listings and logs. -> (exit_code, [lines]).

    `logs` maps controller name -> its log text. None means the acquisition
    question was not asked, which is VOID and never OK.
    """
    out = []
    ctl = [d for d in (deploys.get("items") or [])
           if (d.get("metadata") or {}).get("name", "").endswith(SUFFIX)]
    if not ctl:
        return VOID, ["VOID: no Deployment whose name ends in {!r} was found, so "
                      "there is nothing to judge. A clean result here would be a "
                      "statement about the query.".format(SUFFIX)]

    pod_items = pods.get("items") or []
    if not pod_items:
        return VOID, ["VOID: {} controller Deployment(s) but ZERO pods returned. "
                      "Restart counts live on pods, so 'stayed up' is "
                      "unmeasurable and must not read as true.".format(len(ctl))]

    down, restarted, rows = [], [], []
    for d in sorted(ctl, key=lambda x: x["metadata"]["name"]):
        name = d["metadata"]["name"]
        st = d.get("status") or {}
        avail = st.get("availableReplicas") or 0
        ready = st.get("readyReplicas") or 0
        # Pods are matched by name prefix rather than by label selector: the
        # generated Deployment's selector is the provider's business and has
        # changed before, while `<deployment>-<replicaset>-<pod>` is how the
        # kubelet names them and is stable.
        mine = [p for p in pod_items
                if (p.get("metadata") or {}).get("name", "").startswith(name + "-")]
        restarts = 0
        waiting = []
        for p in mine:
            for cs in ((p.get("status") or {}).get("containerStatuses") or []):
                restarts += cs.get("restartCount") or 0
                w = ((cs.get("state") or {}).get("waiting") or {}).get("reason")
                if w:
                    waiting.append(w)
        rows.append((name, avail, ready, len(mine), restarts,
                     ",".join(sorted(set(waiting))) or "-"))
        if avail < 1:
            down.append(name)
        if restarts:
            restarted.append((name, restarts))
        if not mine:
            # A Deployment with no pod of its own is not "available"; say so as
            # its own cause rather than letting it pass on the status field.
            down.append(name + " (no pods matched)")

    out.append("  {:<32} {:>5} {:>5} {:>5} {:>8}  {}".format(
        "CONTROLLER", "avail", "ready", "pods", "restarts", "waiting"))
    for r in rows:
        out.append("  {:<32} {:>5} {:>5} {:>5} {:>8}  {}".format(*r))
    if settle is not None:
        out.append("  (read {}s after the readiness waits)".format(settle))

    if down:
        out.append("")
        out.append("FAIL: {} controller(s) not available: {}".format(
            len(down), ", ".join(sorted(set(down)))))
        return FAIL, out
    if restarted:
        out.append("")
        out.append("FAIL: every controller is available AND {} has restarted -- "
                   "which is the shape".format(
                       ", ".join("{} x{}".format(n, c) for n, c in restarted)))
        out.append("of a controller that passes its probe and then exits. "
                   "'Available' read once is true in that")
        out.append("world too, so this is a failure and not a note.")
        return FAIL, out

    # THE THIRD QUESTION, ANSWERED FROM AN OBJECT. A Lease either exists with a
    # holder or it does not; that is not timing-dependent the way a single log
    # line is. The name is taken from the Deployment's OWN `-lease-name` arg
    # rather than derived from a convention, because the convention is the thing
    # oasgen#806 changed and a derivation would encode the version that broke.
    if leases is None:
        out.append("")
        out.append("VOID: {} controller(s) available with zero restarts, and THE "
                   "ACQUISITION QUESTION WAS NOT ASKED".format(len(rows)))
        out.append("  -- no Lease listing was supplied. Available means the "
                   "process is alive; with")
        out.append("  readinessProbe null it does not even mean it is serving. "
                   "Pass a leases JSON.")
        return VOID, out

    by_name = {(l.get("metadata") or {}).get("name"): l
               for l in (leases.get("items") or [])}
    unnamed, missing, unheld, held = [], [], [], []
    for name, _, _, _, _, _ in rows:
        want = lease_name_of(deploys, name)
        if not want:
            unnamed.append(name)
            continue
        lease = by_name.get(want)
        if lease is None:
            missing.append("{} (no Lease {!r})".format(name, want))
            continue
        holder = ((lease.get("spec") or {}).get("holderIdentity") or "").strip()
        if not holder:
            unheld.append("{} (Lease {!r} exists, holderIdentity empty)".format(
                name, want))
            continue
        held.append((name, want, holder))

    # The refusal line, which repeats and so is safe to rely on when present.
    refused = []
    if logs:
        for name, _, _, _, _, _ in rows:
            text = logs.get(name) or ""
            if REFUSED in text:
                refused.append(name)

    if unnamed:
        out.append("")
        out.append("VOID: {} controller(s) carry no `-lease-name` argument, so "
                   "there is no Lease to look".format(len(unnamed)))
        out.append("  for and the question cannot be asked: {}".format(
            ", ".join(sorted(unnamed))))
        out.append("  (oasgen#806 made the provider GIVE this name; a build that "
                   "derives it instead")
        out.append("   would need a different check, not a default guess here.)")
        return VOID, out

    if missing or unheld:
        bad = sorted(missing + unheld)
        out.append("")
        out.append("FAIL: {} controller(s) are ALIVE AND NOT HOLDING A LEASE:"
                   .format(len(bad)))
        for b in bad:
            out.append("  {}".format(b))
        if refused:
            out.append("")
            out.append("  and the REPEATING refusal is in the log of {}: {!r}"
                       .format(", ".join(sorted(refused)), REFUSED))
            out.append("  -- that line carries the apiserver's own words and is "
                       "the thing to read.")
        out.append("")
        out.append("  This is the silent failure: available, zero restarts, "
                   "Ready, no workers started.")
        out.append("  It is worse than a crash loop precisely because nothing "
                   "else reports it.")
        return FAIL, out

    if refused:
        out.append("")
        out.append("FAIL: every Lease is held, AND the repeating refusal appears "
                   "in the log of {}.".format(", ".join(sorted(refused))))
        out.append("  A held Lease with refusals logged means it is being lost "
                   "and retaken, or another")
        out.append("  object is being refused. Both are real; neither is "
                   "visible in the table above.")
        return FAIL, out

    out.append("")
    for name, want, holder in held:
        out.append("  {:<32} Lease {} held by {}".format(name, want, holder))
    out.append("")
    out.append("every one of {} controller(s) available, zero restarts, and "
               "HOLDING its own Lease".format(len(held)))
    if logs:
        seen = sum(1 for n, _, _, _, _, _ in rows if HOLDING in (logs.get(n) or ""))
        out.append("  (corroboration only, one-shot lines: {} of {} logs still "
                   "carried the acquisition message)".format(seen, len(rows)))
    return OK, out


def lease_name_of(deploys, controller):
    """The `-lease-name=<x>` this Deployment was told to use, or None.

    Read off the object rather than derived: oasgen#806 moved this name from
    controller-derived to provider-given, so any derivation here would encode
    the shape that broke.
    """
    for d in (deploys.get("items") or []):
        if (d.get("metadata") or {}).get("name") != controller:
            continue
        for c in ((d.get("spec") or {}).get("template") or {}).get(
                "spec", {}).get("containers") or []:
            for a in (c.get("args") or []):
                if a.startswith("-lease-name="):
                    return a.split("=", 1)[1]
    return None


def _dep(name, avail=1, ready=1, lease="l-nova-server"):
    args = ["-group=g", "-version=v2-1"]
    if lease is not None:
        args.append("-lease-name=" + lease)
    return {"metadata": {"name": name},
            "spec": {"template": {"spec": {"containers": [{"args": args}]}}},
            "status": {"availableReplicas": avail, "readyReplicas": ready}}


def _lease(name, holder="nova-server-controller-abc-1"):
    return {"metadata": {"name": name}, "spec": {"holderIdentity": holder}}


def _pod(name, restarts=0, waiting=None):
    cs = {"restartCount": restarts}
    if waiting:
        cs["state"] = {"waiting": {"reason": waiting}}
    return {"metadata": {"name": name}, "status": {"containerStatuses": [cs]}}


def selftest():
    """Each verdict reachable, and the two questions must disagree somewhere."""
    up = {"items": [_dep("nova-server-controller")]}
    pod1 = {"items": [_pod("nova-server-controller-abc-1")]}
    HELD = {"items": [_lease("l-nova-server")]}
    EMPTY_HOLDER = {"items": [_lease("l-nova-server", holder="")]}
    WRONG_LEASE = {"items": [_lease("some-other-lease")]}
    NO_LEASES = {"items": []}
    NO_ARG = {"items": [_dep("nova-server-controller", lease=None)]}
    LOG_OK = {"nova-server-controller": "...\n" + HOLDING + "\n..."}
    LOG_MUTE = {"nova-server-controller": "some other log line entirely"}
    LOG_REFUSED = {"nova-server-controller": "x\n" + REFUSED + ": forbidden\n" + REFUSED}
    # The fixtures must be able to express what the arms claim, asserted before
    # any arm relies on it.
    assert lease_name_of(up, "nova-server-controller") == "l-nova-server"
    assert lease_name_of(NO_ARG, "nova-server-controller") is None
    assert REFUSED in LOG_REFUSED["nova-server-controller"]
    assert REFUSED not in LOG_OK["nova-server-controller"]
    assert HOLDING not in LOG_MUTE["nova-server-controller"]

    cases = [
        # THE VERDICT IS NOW AN OBJECT: a held Lease passes even with no log
        # line at all, which is the whole point of the change.
        ("held Lease, no log line",   up, pod1, OK,   LOG_MUTE, HELD),
        ("held Lease, line present",  up, pod1, OK,   LOG_OK,   HELD),
        ("Lease MISSING",             up, pod1, FAIL, LOG_MUTE, WRONG_LEASE),
        ("Lease exists, holder EMPTY", up, pod1, FAIL, LOG_MUTE, EMPTY_HOLDER),
        ("held Lease + REPEATING refusal", up, pod1, FAIL, LOG_REFUSED, HELD),
        ("no -lease-name argument",   NO_ARG, pod1, VOID, LOG_OK, HELD),
        ("no Lease listing supplied", up, pod1, VOID, LOG_OK, None),
        ("no Leases at all",          up, pod1, FAIL, LOG_MUTE, NO_LEASES),
        # the first two questions still short-circuit, before any Lease is read
        ("available but RESTARTED",
         up, {"items": [_pod("nova-server-controller-abc-1", restarts=3)]},
         FAIL, LOG_OK, HELD),
        ("not available",
         {"items": [_dep("nova-server-controller", avail=0, ready=0)]},
         {"items": [_pod("nova-server-controller-abc-1", waiting="CrashLoopBackOff")]},
         FAIL, LOG_OK, HELD),
        ("available but NO pod of its own",
         up, {"items": [_pod("something-else-xyz-1")]}, FAIL, LOG_OK, HELD),
        ("no controller Deployments at all",
         {"items": [{"metadata": {"name": "oasgen-provider"}}]},
         {"items": [_pod("oasgen-provider-1")]}, VOID, LOG_OK, HELD),
        ("Deployments but zero pods", up, {"items": []}, VOID, LOG_OK, HELD),
    ]
    bad = 0
    for label, dep, pod, want, lg, lz in cases:
        got, lines = judge(dep, pod, settle=0, logs=lg, leases=lz)
        ok = got == want
        bad += not ok
        print("  {:34s} want={} got={}  {}".format(
            label, want, got, "ok" if ok else "*** WRONG ***"))
        if not ok:
            for l in lines:
                print("      {}".format(l))
    wants = [w for _, _, _, w, _, _ in cases]
    for v, nm in ((OK, "OK"), (FAIL, "FAIL"), (VOID, "VOID")):
        if v not in wants:
            print("  *** SUITE DEFECT: no arm expects {}".format(nm))
            bad += 1
    # The two VOID arms must name different causes, or a VOID sends a reader to
    # the wrong explanation.
    # THE CAUSE LINE, NOT lines[0]. This check used to read the first line and
    # was correct while every verdict started with its own message. Now the
    # arms that reach the table print the table FIRST, so lines[0] is the shared
    # column header and two genuinely distinct VOIDs compared equal -- the suite
    # reported a defect that was its own. Read the line that states the cause.
    def _cause(lines):
        for l in lines:
            if l.lstrip().startswith("VOID:"):
                return l.strip()
        return "<no VOID: line at all>"
    msgs = {_cause(judge(d, p, 0, lg, lz)[1])
            for _, d, p, w, lg, lz in cases if w == VOID}
    nvoid = sum(1 for w in wants if w == VOID)
    if len(msgs) != nvoid:
        print("  *** SUITE DEFECT: {} VOID arms share {} message(s)".format(
            nvoid, len(msgs)))
        bad += 1
    else:
        print("  {:34s} {} VOID arms, {} distinct causes  ok".format(
            "void messages differ", nvoid, len(msgs)))
    # And the restart arm must fail for the RESTART reason, not incidentally.
    _, lines = judge(up,
                     {"items": [_pod("nova-server-controller-a-1", restarts=1)]},
                     0, LOG_OK, HELD)
    if not any("has restarted" in l for l in lines):
        print("  *** SUITE DEFECT: the restart arm does not name restarts as "
              "the cause, so it could be failing for any reason")
        bad += 1
    else:
        print("  {:34s} names restarts as the cause  ok".format("restart arm"))
    print("  --- {} ---".format("FAIL" if bad else "all arms pass"))
    return 1 if bad else 0


def main(argv):
    if "--selftest" in argv or "--self-test" in argv:
        return selftest()
    if len(argv) < 3:
        sys.stderr.write(
            "usage: assert-controllers-healthy.py <deployments.json> <pods.json> "
            "[settle-seconds] [logs-dir] [leases.json]\n"
            "  leases.json is a `kubectl get lease -o json`. WITHOUT it the\n"
            "  acquisition question is not asked and the verdict is VOID.\n"
            "  logs-dir holds <controller-name>.log per controller; it corroborates\n"
            "  and carries the REPEATING refusal, which is conclusive when present.\n"
            "       assert-controllers-healthy.py --selftest\n")
        return 2
    try:
        deploys = json.load(open(argv[1], encoding="utf-8"))
        pods = json.load(open(argv[2], encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print("VOID: could not read a listing: {}".format(exc))
        return VOID
    settle = argv[3] if len(argv) > 3 else None
    logs = None
    if len(argv) > 4:
        import glob as _glob
        import os as _os
        logs = {}
        for f in sorted(_glob.glob(_os.path.join(argv[4], "*.log"))):
            logs[_os.path.basename(f)[:-4]] = open(
                f, encoding="utf-8", errors="replace").read()
    leases = None
    if len(argv) > 5:
        try:
            leases = json.load(open(argv[5], encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print("VOID: could not read the Lease listing: {}".format(exc))
            return VOID
    code, lines = judge(deploys, pods, settle, logs, leases)
    for l in lines:
        print(l)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
