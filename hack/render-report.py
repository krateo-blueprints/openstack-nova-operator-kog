#!/usr/bin/env python3
"""Report what one render says about itself: conditions, warnings, auth block.

Called by `hack/render-check.sh`. Split out because the three things it prints
are the three a reader of a render actually needs, and they come from two JSON
dumps that a shell script would have to re-parse per field.

WHAT IT REFUSES TO DO. Print a clean report it cannot support. Zero
RestDefinitions, or zero Configuration CRDs in the group, means the render
produced nothing and an empty report would read exactly like a healthy one, so
both exit 3 (VOID).

ON schemaWarnings. The field does not exist below oasgen 0.54.0, so an empty
list is NOT the same claim at every version: below 0.54.0 it means "unknown",
at and above it means "none". The report says which, because conflating them is
how a consumer concludes a render is clean when the field was simply absent.
"""
import json
import sys

VOID = 3


def main(argv):
    if len(argv) != 4:
        print("usage: render-report.py <rds.json> <crds.json> <group>", file=sys.stderr)
        return 2
    rds_path, crds_path, group = argv[1], argv[2], argv[3]
    rds = json.load(open(rds_path))["items"]
    crds = json.load(open(crds_path))["items"]

    if not rds:
        print("VOID: no RestDefinitions in the dump -- an empty report reads "
              "like a healthy one", file=sys.stderr)
        return VOID

    print("  RestDefinition conditions")
    warn_total = 0
    field_present = False
    for it in sorted(rds, key=lambda r: r["metadata"]["name"]):
        name = it["metadata"]["name"]
        st = it.get("status", {})
        conds = ";".join("%s=%s/%s" % (c["type"], c["status"], c.get("reason", ""))
                         for c in st.get("conditions", []))
        warns = st.get("schemaWarnings")
        if warns is not None:
            field_present = True
        warns = warns or []
        warn_total += len(warns)
        print("    %-26s %s  warnings=%d" % (name, conds, len(warns)))
        for w in warns:
            print("        WARN %s" % w)

    if field_present:
        print("  %d schemaWarning(s) total; the field is PRESENT, so 0 would mean none"
              % warn_total)
    else:
        print("  status.schemaWarnings is ABSENT on every definition. That is this "
              "provider predating 0.54.0, NOT a clean render -- the condition has "
              "nowhere to be reported here.")

    print()
    print("  Configuration auth blocks")
    seen = 0
    for crd in sorted(crds, key=lambda c: c["spec"]["names"]["kind"]):
        spec = crd["spec"]
        if spec["group"] != group or not spec["names"]["plural"].endswith("configurations"):
            continue
        for v in spec["versions"]:
            props = (v["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"])
            auth = props.get("authentication", {}).get("properties", {})
            line = "    %-34s %-9s authentication=%s" % (
                spec["names"]["kind"], v["name"], sorted(auth) or "NONE")
            print(line)
            api_key = auth.get("apiKey", {}).get("properties", {})
            if api_key:
                print("    %-34s %-9s   apiKey=%s" % ("", "", sorted(api_key)))
            seen += 1

    if seen == 0:
        print("VOID: no Configuration CRDs in group %s -- this chart's documents "
              "all declare a security scheme, so zero means the render did not "
              "happen rather than that there is nothing to show" % group,
              file=sys.stderr)
        return VOID
    print("  %d Configuration version(s) inspected" % seen)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
