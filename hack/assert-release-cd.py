#!/usr/bin/env python3
"""Assert a release's CompositionDefinition asset is stamped and coherent.

Usage:  assert-release-cd.py <path-to-compositiondefinition.yaml> <version>

Checks four things, each of which has a concrete failure behind it:

1. `spec.chart.version` is the released version, not the `CHART_VERSION`
   placeholder. release-tag.yaml's own comment records that a hand-maintained
   CompositionDefinition once registered 0.1.0 -- a version that was never
   published -- and that core-provider accepts a stale registration SILENTLY.
   A placeholder would be worse still: it is not valid semver, so whatever read
   it would fail somewhere unrelated.

2. `spec.chart.url` is an `oci://` url. This asset is the one a user applies, so
   it must point at the registry rather than at a tgz left over from a CI run.

3. The url's chart NAME matches this repo's chart. A CD that reconciles against
   somebody else's chart would still reach Available.

4. apiVersion/kind are what core-provider expects. If those drift, the apply
   fails with `no matches for kind`, which is also what a missing CRD says, so
   the reader debugs the wrong layer.

Exits 0 only if all four hold; prints what it checked either way, because a
guard that passes silently teaches nobody what it covers.
"""
import sys

import yaml


def main():
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 64
    path, version = sys.argv[1], sys.argv[2]

    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    if not isinstance(doc, dict):
        print(f"{path}: not a YAML mapping", file=sys.stderr)
        return 1

    problems = []

    api = doc.get("apiVersion")
    kind = doc.get("kind")
    if api != "core.krateo.io/v1alpha1":
        problems.append(f"apiVersion is {api!r}, expected core.krateo.io/v1alpha1")
    if kind != "CompositionDefinition":
        problems.append(f"kind is {kind!r}, expected CompositionDefinition")

    chart = (doc.get("spec") or {}).get("chart") or {}
    got_version = chart.get("version")
    url = chart.get("url") or ""

    if got_version == "CHART_VERSION":
        problems.append("spec.chart.version is still the CHART_VERSION "
                        "placeholder -- the release did not stamp this asset")
    elif got_version != version:
        problems.append(f"spec.chart.version is {got_version!r} but the release "
                        f"is {version!r}; core-provider accepts a stale "
                        f"registration silently, so this would reconcile against "
                        f"the wrong chart")

    if not url.startswith("oci://"):
        problems.append(f"spec.chart.url is {url!r}; the asset a user applies "
                        f"must point at the registry, not at a tgz or a local "
                        f"path left over from CI")
    elif not url.rstrip("/").endswith("/openstack-nova-operator-kog"):
        problems.append(f"spec.chart.url is {url!r}, which does not name this "
                        f"repo's chart -- a CD pointing at another chart would "
                        f"still reach Available")

    print(f"  asset:        {path}")
    print(f"  apiVersion:   {api}")
    print(f"  kind:         {kind}")
    print(f"  chart.url:    {url}")
    print(f"  chart.version: {got_version}  (release: {version})")
    if problems:
        print("\n".join(f"  PROBLEM: {p}" for p in problems), file=sys.stderr)
        return 1
    print("  OK: stamped, oci, names this chart, and the GVK is as expected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
