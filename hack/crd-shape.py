#!/usr/bin/env python3
"""Emit a canonical shape fingerprint for this chart's generated CRDs.

Usage:
    kubectl get crd -o json | hack/crd-shape.py > /tmp/shape-0.48.0.txt
    # ... bump, regenerate ...
    kubectl get crd -o json | hack/crd-shape.py > /tmp/shape-0.49.0.txt
    diff /tmp/shape-0.48.0.txt /tmp/shape-0.49.0.txt

WHY THIS EXISTS. A provider bump can change the SHAPE of a generated CRD, not
just fix a bug, and a shape change is a different risk class: a constraint that
was not there before can reject a CR a user has already written and applied.

oasgen-provider 0.49.0 carries exactly such a change (#367): for a field
constrained in BOTH an OpenAPI parameter and the request body schema, a
constraint that generation previously dropped now reaches the CRD. Static
analysis of this chart's six assets says we are not exposed -- no parameter in
any of them carries a constraint, so neither merge path can fire. That analysis
is cross-checked and I believe it.

It is still an analysis OF THE DOCUMENTS, by me, and the first version of it
returned six clean zeros while never entering a single body schema, because it
did not follow `$ref` into `components/schemas`. A control caught that. This
script answers the same question FROM THE ARTIFACT instead, so it holds even if
my reading of the documents is wrong for a third reason nobody has thought of.

WHAT IT PRINTS. One sorted line per field per served version:

    ComputeFlavor v2-1 spec.flavor.disk type=integer
    ComputeFlavor v2-1 spec.flavor.name type=string
    ComputeKeypair v2-1 spec.keypair.type type=string enum=['ssh','x509']

Sorted and fully qualified so `diff` output is the answer rather than the start
of an investigation: a changed constraint is one line, and an added field is one
line, with no reordering noise.

WHAT IT REFUSES TO DO. Report a clean result it cannot support:

  * zero matching CRDs            -> exit 3, VOID. An empty fingerprint diffs
                                     equal against another empty fingerprint,
                                     which reads as "no shape change" and is
                                     the most dangerous output this could have.
  * a CRD with no schema          -> exit 3, named. A served version whose
                                     schema cannot be read is not a version
                                     with no constraints.
  * unreadable stdin              -> exit 2.

A zero from an unread source is exactly what a correct negative looks like, so
it is never printed as one.
"""
import json
import sys

GROUP = "nova.openstack.krateo.io"

# The keywords that can REJECT a CR. Descriptive fields (description, title,
# example, default) are deliberately excluded: they change with docs edits and
# would bury a real constraint change in noise.
CONSTRAINTS = (
    "enum", "pattern", "format", "minLength", "maxLength",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "multipleOf", "minItems", "maxItems", "uniqueItems",
    "minProperties", "maxProperties", "required",
)


def _fmt(value):
    """Render a constraint value stably, so a dict ordering cannot fake a diff."""
    if isinstance(value, list):
        return "[" + ",".join(repr(v) for v in sorted(value, key=repr)) + "]"
    return repr(value)


def walk(schema, path, out, kind, version):
    if not isinstance(schema, dict):
        return
    bits = []
    t = schema.get("type")
    if t:
        bits.append(f"type={t}")
    for kw in CONSTRAINTS:
        if kw in schema:
            bits.append(f"{kw}={_fmt(schema[kw])}")
    if bits and path:
        out.append(f"{kind} {version} {path} " + " ".join(bits))

    for name, sub in sorted((schema.get("properties") or {}).items()):
        walk(sub, f"{path}.{name}" if path else name, out, kind, version)
    items = schema.get("items")
    if isinstance(items, dict):
        walk(items, f"{path}[]", out, kind, version)


def main():
    try:
        doc = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"could not parse stdin as JSON ({e}). Expected "
              f"`kubectl get crd -o json`. This is VOID, not an empty shape.",
              file=sys.stderr)
        return 2

    items = doc.get("items")
    if items is None:
        # A single CRD object rather than a List.
        items = [doc] if doc.get("kind") == "CustomResourceDefinition" else []

    out, problems, matched = [], [], 0
    for crd in items:
        spec = crd.get("spec") or {}
        if spec.get("group") != GROUP:
            continue
        matched += 1
        kind = (spec.get("names") or {}).get("kind") or crd.get("metadata", {}).get("name", "?")
        versions = spec.get("versions") or []
        if not versions:
            problems.append(f"{kind}: no versions listed")
            continue
        for v in versions:
            vname = v.get("name", "?")
            if not v.get("served", False):
                continue
            schema = ((v.get("schema") or {}).get("openAPIV3Schema"))
            if not isinstance(schema, dict):
                problems.append(
                    f"{kind} {vname}: served, but its openAPIV3Schema is absent "
                    f"or unreadable -- that is NOT the same as a version with no "
                    f"constraints, so this run cannot be compared")
                continue
            walk(schema, "", out, kind, vname)

    if matched == 0:
        print(f"no CRDs in group {GROUP} found in the input. An empty "
              f"fingerprint compares EQUAL to another empty fingerprint, which "
              f"would read as 'no shape change'. Refusing: this is VOID.",
              file=sys.stderr)
        return 3
    if problems:
        print("\n".join(f"PROBLEM: {p}" for p in problems), file=sys.stderr)
        return 3

    print(f"# {matched} CRDs in {GROUP}, {len(out)} constrained fields")
    for line in sorted(out):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
