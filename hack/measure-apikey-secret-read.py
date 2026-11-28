#!/usr/bin/env python3
"""Classify what a LIVE generated controller did with an apiKey Secret.

WHY THIS EXISTS. Everything this repo knows about the apiKey grant is from
READING FILES: hack/measure-apikey-grant.py reads three source files per
version. Nobody here had ever watched a generated controller actually read an
apiKey Secret. This is the classifier for the arm that does
(.github/workflows/apikey-secret-read-arm.yaml): it takes what the cluster said
and decides, in pure functions an offline --self-test can drive.

TWO WITNESSES, because one is not enough.

  WITNESS A -- the MECHANISM, read from the cluster. oasgen generates a Role per
  resource named `<resource>-<apiGroup>-<rbacVersion>-authsecrets` whose
  resourceNames list only its own Secrets. States:

      contains   a matching Role exists AND a rule grants `secrets` by that name
      lacks      a matching Role exists and does NOT grant that Secret
      ABSENT     no Role for THIS resource, though authsecrets Roles for other
                 resources exist. Reported distinctly from `lacks`: "there is no
                 Role" and "the Role omits the Secret" are different facts.
      ABSENT-ANY no authsecrets Role for ANY resource. For the control this is a
                 SETUP failure (VOID), not evidence about apiKey: a control that
                 holds because nothing was ever granted anywhere holds vacuously.
      UNREAD     the Role list could not be read at all

  THE ROLE NAME IS MATCHED BY SHAPE, NOT BUILT. deploy/authsecretrbac.go at
  BOTH 0.54.0 and 0.74.0 carries two name functions: AuthSecretRoleName (group
  included, the one that CREATES) and legacyAuthSecretRoleName (group omitted,
  "MIGRATION ONLY", used only to DELETE pre-#440 pairs). 0.54.0 already carries
  #440; both forms exist in the versions this arm is pointed at, only one is
  ever created, and which one depends on upstream internals the arm should not
  have to track. So: prefix `<plural>-`, suffix `-authsecrets`, and the name
  that matched is PRINTED -- the arm reports what it found rather than asserting
  what it expected, so a future rename shows as a changed printed name rather
  than a false ABSENT (which the control row would count as HOLDING, for the
  wrong reason). Same reason the safe-set instrument pins a tag set, not a sha.

  WITNESS B -- the BEHAVIOUR, which does not need a reachable vendor. A safe
  version gets PAST the secret read and then fails on the network; 0.54.0 fails
  ON the read with a Forbidden. Different strings:

      reached-endpoint  a dial/lookup failure naming the unroutable host, and
                        NO secrets Forbidden
      secret-forbidden  a Forbidden / cannot-read on a Secret
      neither           the question was not asked (VOID)
      UNREAD            no evidence could be gathered at all

  `reached-endpoint` REQUIRES POSITIVE EVIDENCE: a line carrying both a
  dial-failure phrase AND the endpoint's host. The absence of a Forbidden is
  never enough, and a dial failure that does not name the host (the kube-apiserver
  blipping, say) is not evidence about the vendor endpoint.

THE VERDICT TABLE
  subject  : A contains  AND B reached-endpoint            -> PASS            rc 0
             A not-contains (lacks/ABSENT) AND B forbidden -> FAIL            rc 1
  control  : A not-contains AND B secret-forbidden         -> CONTROL HELD    rc 0
             A contains AND B reached-endpoint             -> CONTROL PASSED  rc 3
  anything else, either witness UNREAD, the witnesses disagreeing, `neither`
                                                           -> VOID            rc 3

A CONTROL THAT PASSES IS THE MOST IMPORTANT THING THIS CAN CATCH. If the version
known to be broken reads as working, the arm cannot tell a broken grant from a
working one and every future green is worthless, so it is reported as the
INSTRUMENT being in question, not the provider.

Exit codes: 0 pass / held, 1 the subject genuinely fails the secret read, 2
usage, 3 VOID (including a control that passed).

Usage:
  measure-apikey-secret-read.py verdict --role subject|control --version V
        --roles roles.json --evidence evidence.txt --plural P --secret S
        --host H --out result.json
  measure-apikey-secret-read.py probe-b --evidence evidence.txt --host H
        (prints the class; exit 0 once it is not `neither`, 1 while it is)
  measure-apikey-secret-read.py probe-a --roles roles.json --plural P --secret S
        (prints the state; exit 0 only when the Role contains the Secret)
  measure-apikey-secret-read.py combine subject.json control.json
  measure-apikey-secret-read.py --self-test
"""
import argparse
import json
import re
import sys

ROLE_SUFFIX = "-authsecrets"

# A secrets Forbidden: `secrets "nova-token" is forbidden: User "system:...
# cannot get resource "secrets" in API group "" in the namespace ...`. Either
# phrasing, and the line must mention a secret -- a Forbidden about something
# else is not the 0.54.0 shape.
_FORBIDDEN = re.compile(
    r'(secrets?\b.{0,80}\bis forbidden|forbidden.{0,200}\bresource "secrets"|'
    r'cannot (get|list|watch) resource "secrets")', re.IGNORECASE)

# What a connect failure to an unroutable name looks like in Go.
_DIAL = re.compile(
    r'(no such host|dial tcp|lookup\s+\S+|connection refused|i/o timeout|'
    r'server misbehaving|name resolution|temporary failure in name)',
    re.IGNORECASE)


def witness_a(roles, plural, secret):
    """(state, detail). `roles` is the parsed output of `kubectl get role -A -o
    json`, or None when it could not be read."""
    if not isinstance(roles, dict) or not isinstance(roles.get("items"), list):
        return "UNREAD", "the Role list could not be parsed"
    matched = []
    for r in roles["items"]:
        name = (r.get("metadata") or {}).get("name", "")
        if name.startswith(plural + "-") and name.endswith(ROLE_SUFFIX):
            matched.append(r)
    if not matched:
        anywhere = [r for r in roles["items"]
                    if (r.get("metadata") or {}).get("name", "").endswith(ROLE_SUFFIX)]
        if not anywhere:
            return "ABSENT-ANY", "no *%s Role exists for ANY resource" % ROLE_SUFFIX
        return "ABSENT", "no Role named %s-*%s exists (others do: %s)" % (
            plural, ROLE_SUFFIX, sorted(r["metadata"]["name"] for r in anywhere)[:5])
    for r in matched:
        for rule in r.get("rules") or []:
            if "secrets" in (rule.get("resources") or []) and \
                    secret in (rule.get("resourceNames") or []):
                return "contains", "Role %s grants secrets/%s (resourceNames=%s)" % (
                    r["metadata"]["name"], secret, rule.get("resourceNames"))
    return "lacks", "Role(s) %s exist but no rule grants secrets/%s" % (
        [m["metadata"]["name"] for m in matched], secret)


def witness_b(evidence, host):
    """(class, matched_lines). `evidence` is the gathered text, or None."""
    if evidence is None or not evidence.strip():
        return "UNREAD", []
    lines = evidence.splitlines()
    forbidden = [l.strip() for l in lines if _FORBIDDEN.search(l)]
    # Positive evidence only: the dial phrase AND the host on the SAME line.
    dial = [l.strip() for l in lines if _DIAL.search(l) and host in l]
    if forbidden:
        # A Forbidden on the read means it failed ON the read, whatever else the
        # log also says.
        return "secret-forbidden", forbidden[:5]
    if dial:
        return "reached-endpoint", dial[:5]
    return "neither", []


def _holds(a):
    return a == "contains"


def verdict(role, a, b):
    """(label, rc, why). Pure."""
    if a in ("UNREAD",):
        return "VOID", 3, "witness A (the Role) could not be read, so the mechanism was not observed"
    if b == "UNREAD":
        return "VOID", 3, "witness B (logs/conditions) could not be gathered, so the behaviour was not observed"
    if b == "neither":
        return "VOID", 3, ("witness B saw neither a dial failure to the endpoint nor a secrets Forbidden: "
                           "the controller never reached either, so the question was not asked")
    if role == "subject":
        if _holds(a) and b == "reached-endpoint":
            return "PASS", 0, "the Role grants the Secret and the controller got past the read to the network"
        if not _holds(a) and b == "secret-forbidden":
            return "FAIL", 1, "no Role grants the Secret and the controller was Forbidden reading it"
        return "VOID", 3, "the witnesses disagree (A=%s, B=%s): the mechanism and the behaviour tell different stories" % (a, b)
    if role == "control":
        if a == "ABSENT-ANY":
            return "VOID", 3, ("no authsecrets Role exists for ANY resource at the control version: that is a "
                               "SETUP failure, not evidence about apiKey -- the control would hold vacuously")
        if not _holds(a) and b == "secret-forbidden":
            return "CONTROL HELD", 0, "the known-broken version has no grant and is Forbidden on the read"
        if _holds(a) and b == "reached-endpoint":
            return "CONTROL PASSED", 3, ("the known-broken version reads as WORKING: the INSTRUMENT, not the "
                                         "provider, is in question")
        return "VOID", 3, "the control's witnesses disagree (A=%s, B=%s)" % (a, b)
    return "VOID", 3, "unknown role %r" % role


def combine(subject, control):
    """(label, rc) over the two per-version result dicts. The control gates first."""
    if control.get("label") != "CONTROL HELD":
        return "VOID", 3
    if subject.get("label") == "PASS":
        return "PASS", 0
    if subject.get("label") == "FAIL":
        return "FAIL", 1
    return "VOID", 3


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def cmd_verdict(a):
    raw = _read(a.roles)
    try:
        roles = json.loads(raw) if raw is not None else None
    except ValueError:
        roles = None
    sa, da = witness_a(roles, a.plural, a.secret)
    sb, lb = witness_b(_read(a.evidence), a.host)
    label, rc, why = verdict(a.role, sa, sb)
    print("version %s (%s)" % (a.version, a.role))
    print("  witness A (mechanism): %s -- %s" % (sa, da))
    print("  witness B (behaviour): %s" % sb)
    for l in lb:
        print("    matched: %s" % l[:300])
    print("  verdict: %s -- %s" % (label, why))
    with open(a.out, "w") as f:
        json.dump({"version": a.version, "role": a.role, "a": sa, "b": sb,
                   "label": label, "rc": rc, "why": why}, f)
    return rc


def cmd_probe_b(a):
    c, _ = witness_b(_read(a.evidence), a.host)
    print(c)
    return 0 if c in ("reached-endpoint", "secret-forbidden") else 1


def cmd_probe_a(a):
    raw = _read(a.roles)
    try:
        roles = json.loads(raw) if raw is not None else None
    except ValueError:
        roles = None
    st, detail = witness_a(roles, a.plural, a.secret)
    print("%s -- %s" % (st, detail))
    return 0 if st == "contains" else 1


def cmd_combine(a):
    s = json.load(open(a.subject))
    c = json.load(open(a.control))
    label, rc = combine(s, c)
    print("subject %s: %s | control %s: %s -> %s" % (
        s["version"], s["label"], c["version"], c["label"], label))
    return rc


def selftest():
    fails = 0

    def t(name, want, got):
        nonlocal fails
        ok = want == got
        print("  %-78s %s" % (name, "ok" if ok else "*** WRONG: want %r got %r ***" % (want, got)))
        if not ok:
            fails += 1

    P, S, H = "computekeypairs", "nova-token", "nova.invalid"

    def role(name, names, resources=("secrets",)):
        return {"metadata": {"name": name},
                "rules": [{"apiGroups": [""], "resources": list(resources), "resourceNames": names,
                           "verbs": ["get"]}]}
    new = "computekeypairs-nova.openstack.krateo.io-v1-authsecrets"
    old = "computekeypairs-v1-authsecrets"
    print("witness A")
    t("A: new-style Role listing the Secret -> contains", "contains",
      witness_a({"items": [role(new, [S])]}, P, S)[0])
    t("A: pre-#440 group-less Role listing it -> contains (the control's own name)", "contains",
      witness_a({"items": [role(old, [S])]}, P, S)[0])
    t("A: Role exists but omits the Secret -> lacks", "lacks",
      witness_a({"items": [role(new, ["other"])]}, P, S)[0])
    t("A: Role names the Secret under a non-secrets resource -> lacks", "lacks",
      witness_a({"items": [role(new, [S], resources=("configmaps",))]}, P, S)[0])
    t("A: no authsecrets Role anywhere -> ABSENT-ANY, NOT ABSENT or lacks", "ABSENT-ANY",
      witness_a({"items": []}, P, S)[0])
    t("A: only another resource's Role -> ABSENT", "ABSENT",
      witness_a({"items": [role("computeflavors-g-v1-authsecrets", [S])]}, P, S)[0])
    t("A: a Role with the right prefix but no -authsecrets suffix is not one -> ABSENT-ANY", "ABSENT-ANY",
      witness_a({"items": [role("computekeypairs-reader", [S])]}, P, S)[0])
    t("A: unparseable list -> UNREAD, NOT ABSENT", "UNREAD", witness_a(None, P, S)[0])
    t("A: wrong shape -> UNREAD", "UNREAD", witness_a({"items": "x"}, P, S)[0])

    print("witness B")
    forb = ('secrets "nova-token" is forbidden: User "system:serviceaccount:krateo-system:x" '
            'cannot get resource "secrets" in API group "" in the namespace "krateo-system"')
    dial = 'Get "https://nova.invalid/v2.1/p/os-keypairs/k": dial tcp: lookup nova.invalid: no such host'
    t("B: a secrets Forbidden -> secret-forbidden", "secret-forbidden", witness_b(forb, H)[0])
    t("B: a dial failure naming the host -> reached-endpoint", "reached-endpoint", witness_b(dial, H)[0])
    t("B: BOTH present -> secret-forbidden (it failed on the read)", "secret-forbidden",
      witness_b(forb + "\n" + dial, H)[0])
    t("B: unrelated text -> neither, NOT reached-endpoint", "neither",
      witness_b("reconciling\nstarting watch\nok", H)[0])
    t("B: no Forbidden alone is NOT reached-endpoint", "neither",
      witness_b("controller started; nothing else logged", H)[0])
    t("B: a dial failure to ANOTHER host is not evidence about the endpoint", "neither",
      witness_b("dial tcp 10.96.0.1:443: connection refused", H)[0])
    t("B: a Forbidden about a non-secret is not the 0.54.0 shape", "neither",
      witness_b('pods "x" is forbidden: cannot list resource "pods"', H)[0])
    t("B: no evidence at all -> UNREAD", "UNREAD", witness_b(None, H)[0])
    t("B: empty evidence -> UNREAD", "UNREAD", witness_b("  \n", H)[0])

    print("verdict table")
    for role_, a_, b_, want in [
        ("subject", "contains", "reached-endpoint", ("PASS", 0)),
        ("subject", "ABSENT", "secret-forbidden", ("FAIL", 1)),
        ("subject", "lacks", "secret-forbidden", ("FAIL", 1)),
        ("subject", "contains", "secret-forbidden", ("VOID", 3)),
        ("subject", "ABSENT", "reached-endpoint", ("VOID", 3)),
        ("subject", "contains", "neither", ("VOID", 3)),
        ("subject", "UNREAD", "reached-endpoint", ("VOID", 3)),
        ("subject", "contains", "UNREAD", ("VOID", 3)),
        ("control", "ABSENT", "secret-forbidden", ("CONTROL HELD", 0)),
        ("control", "lacks", "secret-forbidden", ("CONTROL HELD", 0)),
        ("control", "contains", "reached-endpoint", ("CONTROL PASSED", 3)),
        ("control", "contains", "secret-forbidden", ("VOID", 3)),
        ("control", "ABSENT", "neither", ("VOID", 3)),
        ("control", "ABSENT-ANY", "secret-forbidden", ("VOID", 3)),
        ("subject", "ABSENT-ANY", "secret-forbidden", ("FAIL", 1)),
        ("control", "ABSENT", "reached-endpoint", ("VOID", 3)),
        ("control", "UNREAD", "secret-forbidden", ("VOID", 3)),
    ]:
        got = verdict(role_, a_, b_)
        t("%s A=%s B=%s" % (role_, a_, b_), want, (got[0], got[1]))
    t("a PASSED control is reported as the instrument in question", True,
      "INSTRUMENT" in verdict("control", "contains", "reached-endpoint")[2])
    t("the table reaches every label", True,
      {verdict("subject", "contains", "reached-endpoint")[0], verdict("subject", "ABSENT", "secret-forbidden")[0],
       verdict("control", "ABSENT", "secret-forbidden")[0], verdict("control", "contains", "reached-endpoint")[0],
       verdict("subject", "contains", "neither")[0]} ==
      {"PASS", "FAIL", "CONTROL HELD", "CONTROL PASSED", "VOID"})

    print("combine")
    t("pass + held -> PASS", ("PASS", 0), combine({"label": "PASS"}, {"label": "CONTROL HELD"}))
    t("fail + held -> FAIL", ("FAIL", 1), combine({"label": "FAIL"}, {"label": "CONTROL HELD"}))
    t("pass + control PASSED -> VOID, the green is worthless", ("VOID", 3),
      combine({"label": "PASS"}, {"label": "CONTROL PASSED"}))
    t("pass + control VOID -> VOID", ("VOID", 3), combine({"label": "PASS"}, {"label": "VOID"}))
    t("void subject + held -> VOID", ("VOID", 3), combine({"label": "VOID"}, {"label": "CONTROL HELD"}))

    if fails:
        print("\n  --- FAIL: %d arm(s) ---" % fails)
        return 1
    print("\n  --- all arms pass ---")
    return 0


def main(argv):
    if argv[:1] == ["--self-test"]:
        return selftest()
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")
    v = sub.add_parser("verdict")
    for n in ("version", "roles", "evidence", "plural", "secret", "host", "out"):
        v.add_argument("--" + n, required=True)
    v.add_argument("--role", required=True, choices=["subject", "control"])
    b = sub.add_parser("probe-b")
    b.add_argument("--evidence", required=True)
    b.add_argument("--host", required=True)
    pa = sub.add_parser("probe-a")
    pa.add_argument("--roles", required=True)
    pa.add_argument("--plural", required=True)
    pa.add_argument("--secret", required=True)
    c = sub.add_parser("combine")
    c.add_argument("subject")
    c.add_argument("control")
    a = p.parse_args(argv)
    if a.cmd == "verdict":
        return cmd_verdict(a)
    if a.cmd == "probe-b":
        return cmd_probe_b(a)
    if a.cmd == "probe-a":
        return cmd_probe_a(a)
    if a.cmd == "combine":
        return cmd_combine(a)
    p.print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
