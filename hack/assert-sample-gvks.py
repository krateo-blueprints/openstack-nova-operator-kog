#!/usr/bin/env python3
"""Assert every shipped sample's apiVersion against the GVKs oasgen PUBLISHED.

Reads `kubectl get restdefinitions.ogen.krateo.io -o json` on stdin and compares
each CR in chart/samples/*.yaml against `.status.resource.apiVersion` (for a
resource kind) or `.status.configuration.apiVersion` (for a `*Configuration`).

WHY THIS EXISTS, AND WHY IT IS NOT A DERIVATION. The two halves of a generated
CRD pair are served at DIFFERENT apiVersions: the resource half takes
`crdgen.NormalizeVersionName(info.version)` and the Configuration half is a
fixed `v1alpha1`. Seven examples in this repo said `v1alpha1` for resource kinds
and could never be applied; the failure is `no matches for kind`, which is also
what a missing CRD says, so it misdirects the reader entirely.

`scripts/validate.py` used to catch that by PORTING oasgen's normalizer and
asserting it against known inputs -- a reimplementation of someone else's
internal rule, maintained in a repo that does not own it, able to drift from it
silently. oasgen publishes both values on the RestDefinition and always has, so
once this repo had a cluster in CI the honest check became a COMPARISON. This is
that comparison, and it is why the ported normalizer was deleted.

Deliberately does not map kind-by-kind. Every resource half in this chart shares
one apiVersion and every Configuration half shares another, so it collects the
two published sets, requires each to be a single value, and checks membership.
A per-kind mapping would need the plural, which is not derivable -- core-provider
resolves it through `plumbing/kubeutil/plurals`, and a naive `lower(kind)+"s"`
silently dropped 32 of 83 rows in a fleet join.
"""
import glob
import json
import re
import sys


def crs_in(path):
    """Yield (lineno, kind, apiVersion) for each CR in a YAML file.

    Textual rather than parsed: samples carry comment blocks and multiple
    documents, and a parse buys nothing when the two fields are adjacent keys.
    """
    lines = open(path, encoding="utf-8").read().splitlines()
    for i, ln in enumerate(lines):
        m = re.match(r"^apiVersion:\s*(\S+)\s*$", ln)
        if not m:
            continue
        for j in range(i + 1, min(i + 4, len(lines))):
            km = re.match(r"^kind:\s*([A-Za-z]+)\s*$", lines[j])
            if km:
                yield i + 1, km.group(1), m.group(1)
                break


def published_sets(rd_json):
    res, cfg = set(), set()
    for item in rd_json.get("items", []):
        st = item.get("status") or {}
        for key, sink in (("resource", res), ("configuration", cfg)):
            av = (st.get(key) or {}).get("apiVersion")
            if av:
                sink.add(av)
    return res, cfg


def main():
    try:
        rd = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"could not read RestDefinition JSON on stdin: {e}", file=sys.stderr)
        return 2

    res, cfg = published_sets(rd)
    print(f"  published resource apiVersion(s):      {sorted(res) or 'NONE'}")
    print(f"  published configuration apiVersion(s): {sorted(cfg) or 'NONE'}")

    # Precondition. An empty set is what you get when nothing reconciled, and
    # "no published GVKs" must not read as "nothing to disagree with". Every
    # sample would vacuously pass against an empty expectation.
    if not res:
        print("no .status.resource.apiVersion published by any RestDefinition -- "
              "nothing reconciled, so this check has no expectation to compare "
              "against and is VOID, not a pass", file=sys.stderr)
        return 3
    if len(res) != 1 or (cfg and len(cfg) != 1):
        print(f"expected one apiVersion per half; got resource={sorted(res)} "
              f"configuration={sorted(cfg)}. A per-kind mapping would be needed, "
              f"and the plural is not derivable -- widen this check deliberately.",
              file=sys.stderr)
        return 1

    bad, checked = [], 0
    for path in sorted(glob.glob("chart/samples/*.yaml")):
        for lineno, kind, av in crs_in(path):
            if not av.startswith("nova.openstack.krateo.io/"):
                continue
            checked += 1
            want = cfg if kind.endswith("Configuration") else res
            if av not in want:
                bad.append(f"{path}:{lineno}: {kind} declares {av} but oasgen "
                           f"published {sorted(want)}")
    if not checked:
        print("no sample CRs found to check -- that is a failure, not a pass",
              file=sys.stderr)
        return 3
    print(f"  checked {checked} sample CR(s) against the published GVKs")
    if bad:
        print("\n".join(bad), file=sys.stderr)
        return 1
    print("  OK: every sample's apiVersion matches what oasgen published")
    return 0


if __name__ == "__main__":
    sys.exit(main())
