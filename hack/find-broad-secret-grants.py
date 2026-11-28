#!/usr/bin/env python3
"""List every grant of `get secrets` to a controller's ServiceAccount that is NOT
the per-resource `*-authsecrets` Role.

WHY. apikey-secret-read-arm.yaml's worst outcome is CONTROL PASSED: the version
known to be unable to read an apiKey Secret reads CLEAN. The first suspect is
that the Secret was reachable for an UNRELATED reason -- a grant wider than the
per-resource Role -- which is the shape that kept 0.54.0's original defect
invisible across 25 kinds: the grant was there, just not from the per-resource
Role. This names it, so a reader learns which of two cases it is without a second
investigation:

  BROAD grant found  the control is explained by a grant wider than the Role; the
                     arm cannot discriminate in this cluster, and the cause is
                     IDENTIFIED rather than unknown
  NONE               clean with no wider grant visible: the genuinely alarming
                     case, and it means the instrument's premise about the control
                     version is wrong

Half the hypothesis is excluded by construction (each version gets its own fresh
cluster, so a grant left from an earlier install cannot exist). What remains is a
grant created within the same run by the chart or by oasgen itself, and this is
what finds it.

Input is a directory of `kubectl get ... -o json` dumps: rolebindings.json,
clusterrolebindings.json, roles.json, clusterroles.json. A file that cannot be
read is UNREAD (exit 3), never "no grants": an unread dump and an empty one are
different facts.

usage: find-broad-secret-grants.py --dir D --secret NAME --sa ns/name [--sa ...]
       find-broad-secret-grants.py --self-test
exit: 0 read (prints BROAD ... lines or NONE), 3 UNREAD
"""
import argparse
import json
import os
import sys

NAMES = ("rolebindings", "clusterrolebindings", "roles", "clusterroles")


def rule_grants(rule, secret):
    if not ({"get", "*"} & set(rule.get("verbs") or [])):
        return False
    if not ({"secrets", "*"} & set(rule.get("resources") or [])):
        return False
    if not ({"", "*"} & set(rule.get("apiGroups") or [""])):
        return False
    names = rule.get("resourceNames") or []
    return (not names) or secret in names


def subject_matches(subj, sa_ns, sa_name):
    k = subj.get("kind")
    if k == "ServiceAccount":
        return subj.get("name") == sa_name and (subj.get("namespace") or "") == sa_ns
    if k == "Group":
        return subj.get("name") in ("system:serviceaccounts", "system:serviceaccounts:" + sa_ns,
                                    "system:authenticated")
    return False


def find(data, sas, secret):
    """data: name -> parsed list. Returns a list of description strings."""
    roles = {("Role", r["metadata"].get("namespace", ""), r["metadata"]["name"]): r
             for r in data["roles"]["items"]}
    croles = {r["metadata"]["name"]: r for r in data["clusterroles"]["items"]}
    out = []
    for kind, key in (("RoleBinding", "rolebindings"), ("ClusterRoleBinding", "clusterrolebindings")):
        for b in data[key]["items"]:
            subs = b.get("subjects") or []
            hit = [(ns, n) for ns, n in sas if any(subject_matches(s, ns, n) for s in subs)]
            if not hit:
                continue
            ref = b.get("roleRef") or {}
            rname = ref.get("name", "")
            # THE per-resource Role is the expected path; everything else is the finding.
            if ref.get("kind") == "Role" and rname.endswith("-authsecrets"):
                continue
            if ref.get("kind") == "ClusterRole":
                role = croles.get(rname)
            else:
                role = roles.get(("Role", b["metadata"].get("namespace", ""), rname))
            if role is None:
                continue
            if any(rule_grants(r, secret) for r in role.get("rules") or []):
                out.append("BROAD %s %s/%s -> %s %s grants get on secrets (subject SA %s)" % (
                    kind, b["metadata"].get("namespace", "") or "-", b["metadata"]["name"],
                    ref.get("kind"), rname, ", ".join("%s/%s" % h for h in hit)))
    return out


def load(d):
    data = {}
    for n in NAMES:
        try:
            with open(os.path.join(d, n + ".json")) as f:
                data[n] = json.load(f)
            if not isinstance(data[n].get("items"), list):
                return None
        except (OSError, ValueError, AttributeError):
            return None
    return data


def selftest():
    fails = 0

    def t(name, want, got):
        nonlocal fails
        print("  %-76s %s" % (name, "ok" if want == got else "*** WRONG: want %r got %r ***" % (want, got)))
        fails += want != got
    sa = [("krateo-system", "ctl")]
    S = "tok"
    subj = [{"kind": "ServiceAccount", "name": "ctl", "namespace": "krateo-system"}]

    def rb(name, rkind, rname, subjects=subj, ns="krateo-system"):
        return {"metadata": {"name": name, "namespace": ns}, "roleRef": {"kind": rkind, "name": rname}, "subjects": subjects}

    def crb(name, rname, subjects=subj):
        return {"metadata": {"name": name}, "roleRef": {"kind": "ClusterRole", "name": rname}, "subjects": subjects}

    def role(name, rules, ns="krateo-system"):
        return {"metadata": {"name": name, "namespace": ns}, "rules": rules}
    sec = {"apiGroups": [""], "resources": ["secrets"], "verbs": ["get"]}
    scoped = {"apiGroups": [""], "resources": ["secrets"], "verbs": ["get"], "resourceNames": [S]}

    def d(rbs=(), crbs=(), roles=(), croles=()):
        return {"rolebindings": {"items": list(rbs)}, "clusterrolebindings": {"items": list(crbs)},
                "roles": {"items": list(roles)}, "clusterroles": {"items": list(croles)}}
    t("no grants at all -> empty", [], find(d(), sa, S))
    t("the per-resource authsecrets Role is the EXPECTED path, not reported", [],
      find(d([rb("a", "Role", "x-g-v1-authsecrets")], roles=[role("x-g-v1-authsecrets", [scoped])]), sa, S))
    t("a ClusterRoleBinding to a secrets-reading ClusterRole IS reported", 1,
      len(find(d(crbs=[crb("c", "wide")], croles=[{"metadata": {"name": "wide"}, "rules": [sec]}]), sa, S)))
    t("a wildcard rule IS reported", 1,
      len(find(d(crbs=[crb("c", "adm")], croles=[{"metadata": {"name": "adm"},
          "rules": [{"apiGroups": ["*"], "resources": ["*"], "verbs": ["*"]}]}]), sa, S)))
    t("a non-authsecrets namespaced Role granting it IS reported", 1,
      len(find(d([rb("r", "Role", "reader")], roles=[role("reader", [sec])]), sa, S)))
    t("a grant scoped to a DIFFERENT secret name is not reported", [],
      find(d([rb("r", "Role", "reader")], roles=[role("reader", [dict(scoped, resourceNames=["other"])])]), sa, S))
    t("a role that cannot read secrets is not reported", [],
      find(d(crbs=[crb("c", "cm")], croles=[{"metadata": {"name": "cm"},
          "rules": [{"apiGroups": [""], "resources": ["configmaps"], "verbs": ["get"]}]}]), sa, S))
    t("a binding for another SA is not reported", [],
      find(d(crbs=[crb("c", "wide", [{"kind": "ServiceAccount", "name": "other", "namespace": "krateo-system"}])],
             croles=[{"metadata": {"name": "wide"}, "rules": [sec]}]), sa, S))
    t("a system:serviceaccounts group binding IS reported", 1,
      len(find(d(crbs=[crb("c", "wide", [{"kind": "Group", "name": "system:serviceaccounts"}])],
                croles=[{"metadata": {"name": "wide"}, "rules": [sec]}]), sa, S)))
    t("a binding whose role is missing is skipped, not a crash", [],
      find(d(crbs=[crb("c", "gone")]), sa, S))
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        t("an empty directory is UNREAD (None), not 'no grants'", None, load(td))
        for n in NAMES:
            json.dump({"items": []}, open(os.path.join(td, n + ".json"), "w"))
        t("four empty dumps load", True, load(td) is not None)
        open(os.path.join(td, "roles.json"), "w").write("garbage")
        t("an unparseable dump is UNREAD", None, load(td))
    print("\n  --- %s ---" % ("FAIL: %d arm(s)" % fails if fails else "all arms pass"))
    return 1 if fails else 0


def main(argv):
    if argv[:1] == ["--self-test"]:
        return selftest()
    p = argparse.ArgumentParser()
    p.add_argument("--dir", required=True)
    p.add_argument("--secret", required=True)
    p.add_argument("--sa", action="append", default=[])
    a = p.parse_args(argv)
    data = load(a.dir)
    if data is None:
        print("UNREAD: could not read the four RBAC dumps in %s" % a.dir)
        return 3
    if not a.sa:
        print("UNREAD: no controller ServiceAccount was identified, so nothing was checked")
        return 3
    sas = [tuple(x.split("/", 1)) for x in a.sa]
    found = find(data, sas, a.secret)
    print("\n".join(found) if found else "NONE")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
