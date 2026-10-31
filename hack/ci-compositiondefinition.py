#!/usr/bin/env python3
"""Emit a CI variant of compositiondefinition.yaml pointing at a local tgz.

Usage:  ci-compositiondefinition.py <tgz-url>   > /tmp/cd.yaml

Takes the SHIPPED compositiondefinition.yaml and rewrites only `spec.chart`:

    url     -> the tgz URL given on the command line
    version -> REMOVED

`spec.chart.version` is documented as needed for oci and helm-repo urls and is
ignored for a tgz, because the archive carries its own version. Leaving the
release-time `CHART_VERSION` placeholder in place would be worse than removing
it: it is not valid semver, so anything that did read it would fail confusingly.

WHAT THIS DOES AND DOES NOT TEST. It does not exercise the shipped OCI url --
that is a release-time concern and cannot be checked before a release exists.
What it exercises is the part nobody had ever run: that a CompositionDefinition
built from this repo reconciles, and that the hand-written example in
examples/nova-composition/ actually applies against the CRD it generates. Three
other hand-written sample sets in this repo turned out never to have been
applied, each with a real defect, so that is the gap worth closing.

Everything else -- name, namespace, apiVersion, kind -- is preserved verbatim, so
a defect in those IS caught.
"""
import sys

import yaml


def main():
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 64
    url = sys.argv[1]
    if not (url.endswith(".tgz") or url.endswith(".tar.gz")):
        print(f"refusing: {url!r} is not a .tgz/.tar.gz, and plumbing's tgz "
              f"getter keys on exactly that suffix -- a url without it would be "
              f"routed to a different getter and fail for an unrelated reason",
              file=sys.stderr)
        return 64

    with open("compositiondefinition.yaml", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)

    chart = (doc.get("spec") or {}).get("chart")
    if not isinstance(chart, dict) or "url" not in chart:
        print("compositiondefinition.yaml has no spec.chart.url to rewrite -- "
              "its shape changed and this script must be updated rather than "
              "silently emitting something that looks right", file=sys.stderr)
        return 1

    before = chart.get("url")
    chart["url"] = url
    chart.pop("version", None)
    print(f"# CI variant. spec.chart.url rewritten for this run only:\n"
          f"#   from: {before}\n"
          f"#     to: {url}\n"
          f"# spec.chart.version removed (ignored for tgz; the archive carries it).",
          file=sys.stderr)
    yaml.safe_dump(doc, sys.stdout, sort_keys=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
