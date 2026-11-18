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

SO "CAME UP" AND "STAYED UP" MUST NOT SHARE AN ANSWER:

    came up    availableReplicas >= 1          a pod passed its readiness probe
    stayed up  restartCount == 0 after settle  nothing exited and was restarted

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


def judge(deploys, pods, settle=None):
    """Pure verdict over two listings. Returns (exit_code, [lines])."""
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

    out.append("")
    out.append("every one of {} controller(s) available with zero restarts"
               .format(len(rows)))
    return OK, out


def _dep(name, avail=1, ready=1):
    return {"metadata": {"name": name},
            "status": {"availableReplicas": avail, "readyReplicas": ready}}


def _pod(name, restarts=0, waiting=None):
    cs = {"restartCount": restarts}
    if waiting:
        cs["state"] = {"waiting": {"reason": waiting}}
    return {"metadata": {"name": name}, "status": {"containerStatuses": [cs]}}


def selftest():
    """Each verdict reachable, and the two questions must disagree somewhere."""
    cases = [
        ("healthy: up and stayed up",
         {"items": [_dep("nova-server-controller")]},
         {"items": [_pod("nova-server-controller-abc-1")]}, OK),
        # THE ARM THIS SCRIPT EXISTS FOR: available is 1 and it has restarted.
        # If this returned OK the script would be assert-served-controlled.py
        # with extra columns.
        ("available but RESTARTED",
         {"items": [_dep("nova-server-controller")]},
         {"items": [_pod("nova-server-controller-abc-1", restarts=3)]}, FAIL),
        ("not available",
         {"items": [_dep("nova-server-controller", avail=0, ready=0)]},
         {"items": [_pod("nova-server-controller-abc-1", waiting="CrashLoopBackOff")]},
         FAIL),
        ("available but NO pod of its own",
         {"items": [_dep("nova-server-controller")]},
         {"items": [_pod("something-else-xyz-1")]}, FAIL),
        ("no controller Deployments at all",
         {"items": [{"metadata": {"name": "oasgen-provider"}}]},
         {"items": [_pod("oasgen-provider-1")]}, VOID),
        ("Deployments but zero pods",
         {"items": [_dep("nova-server-controller")]},
         {"items": []}, VOID),
    ]
    bad = 0
    for label, dep, pod, want in cases:
        got, lines = judge(dep, pod, settle=0)
        ok = got == want
        bad += not ok
        print("  {:34s} want={} got={}  {}".format(
            label, want, got, "ok" if ok else "*** WRONG ***"))
        if not ok:
            for l in lines:
                print("      {}".format(l))
    wants = [w for _, _, _, w in cases]
    for v, nm in ((OK, "OK"), (FAIL, "FAIL"), (VOID, "VOID")):
        if v not in wants:
            print("  *** SUITE DEFECT: no arm expects {}".format(nm))
            bad += 1
    # The two VOID arms must name different causes, or a VOID sends a reader to
    # the wrong explanation.
    msgs = {judge(d, p, 0)[1][0] for _, d, p, w in cases if w == VOID}
    nvoid = sum(1 for w in wants if w == VOID)
    if len(msgs) != nvoid:
        print("  *** SUITE DEFECT: {} VOID arms share {} message(s)".format(
            nvoid, len(msgs)))
        bad += 1
    else:
        print("  {:34s} {} VOID arms, {} distinct causes  ok".format(
            "void messages differ", nvoid, len(msgs)))
    # And the restart arm must fail for the RESTART reason, not incidentally.
    _, lines = judge({"items": [_dep("nova-server-controller")]},
                     {"items": [_pod("nova-server-controller-a-1", restarts=1)]}, 0)
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
            "[settle-seconds]\n"
            "       assert-controllers-healthy.py --selftest\n")
        return 2
    try:
        deploys = json.load(open(argv[1], encoding="utf-8"))
        pods = json.load(open(argv[2], encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print("VOID: could not read a listing: {}".format(exc))
        return VOID
    settle = argv[3] if len(argv) > 3 else None
    code, lines = judge(deploys, pods, settle)
    for l in lines:
        print(l)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
