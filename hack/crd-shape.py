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

# The keywords that can REJECT a CR -- or SILENTLY CHANGE ONE. Purely
# descriptive fields (description, title, example) stay excluded: they move with
# docs edits and would bury a real change in noise.
#
# `default` USED TO BE EXCLUDED HERE AS "DESCRIPTIVE". THAT WAS WRONG, and it is
# the one exclusion that could have cost something irreversible. A `default` does
# not merely describe: the API server MATERIALISES it onto existing objects at
# the next re-render. The chain, as the oasgen-provider session reported it after
# a plumbing bump added `default: {}` to an optional object with a defaulted
# child:
#
#   the CRD gains `default: {}` on an optional object
#     -> the API server materialises it on EXISTING CRs at the next re-render
#     -> rest-dynamic-controller sends spec values WHOLE, so it gets sent
#     -> one unrelated write persists it upstream
#     -> reverting the CRD does NOT remove it, and it is UNOWNED, so
#        `kubectl apply` will not prune it either
#
# So a `default` appearing is not a cosmetic diff, it is the only change in this
# family that is unrecoverable by the time anyone notices. A fingerprint of
# `type` and `enum` cannot see it at all. IF A DIFF SHOWS A NEW `default`, STOP.
#
# `nullable` is here for a related reason: it governs whether the API server
# PRUNES an explicit null, so a change to it alters stored content rather than
# just validation.
#
# `additionalProperties` is handled separately below, because it is a bool in one
# form and a schema in the other, and the converter treats those on different
# branches -- a change that moved between them would be invisible to a keyword
# dump.
CONSTRAINTS = (
    "enum", "pattern", "format", "minLength", "maxLength",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "multipleOf", "minItems", "maxItems", "uniqueItems",
    "minProperties", "maxProperties", "required",
    "default", "nullable",
)


def _fmt(value):
    """Render a constraint value stably, so a dict ordering cannot fake a diff.

    `default` can be an arbitrary JSON value including `{}` and nested objects,
    so dicts are rendered with sorted keys. Without that, two identical defaults
    could differ textually by key order and manufacture a diff -- which in this
    script is worse than missing one, because a false `default` diff is exactly
    the signal that is supposed to stop an upgrade.
    """
    if isinstance(value, list):
        return "[" + ",".join(repr(v) for v in sorted(value, key=repr)) + "]"
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
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

    # additionalProperties: record the FORM, not the contents. `false`, `true`
    # and a schema are three different behaviours, and the converter reaches them
    # on different branches, so collapsing them would hide a move between forms.
    # The schema itself is walked below so its own constraints still show up.
    if "additionalProperties" in schema:
        ap = schema["additionalProperties"]
        if isinstance(ap, bool):
            bits.append(f"additionalProperties={ap}")
        elif isinstance(ap, dict):
            bits.append("additionalProperties=schema")
        else:
            bits.append(f"additionalProperties=?{type(ap).__name__}")

    if bits and path:
        out.append(f"{kind} {version} {path} " + " ".join(bits))

    for name, sub in sorted((schema.get("properties") or {}).items()):
        walk(sub, f"{path}.{name}" if path else name, out, kind, version)
    items = schema.get("items")
    if isinstance(items, dict):
        walk(items, f"{path}[]", out, kind, version)
    ap = schema.get("additionalProperties")
    if isinstance(ap, dict):
        walk(ap, f"{path}{{}}", out, kind, version)


def _self_test():
    """Assert that EVERY default renders visibly, including the falsy ones.

    WHY THIS IS A TEST AND NOT A COMMENT. `default: {}` on an optional object is
    the one change in this family that is irreversible -- the API server
    materialises it onto existing CRs, rest-dynamic-controller sends spec values
    whole so it reaches the vendor, and reverting the CRD does not remove it
    because the value is unowned. If a future tidy-up of `_fmt` made an empty
    value render as nothing, this script would emit a line with no `default=` in
    it and a version diff would report "no new defaults" -- which is the
    reassuring answer, and indistinguishable from the truth.

    The oasgen-provider session raised exactly that risk. It does not currently
    apply: `_fmt` routes dicts through `json.dumps` and lists through a sorted
    repr, so `{}` renders as `default={}`. **But it holds by accident** -- the
    dict branch exists to make key ordering stable, not because anyone foresaw
    the empty-object case. An accident that the next edit can undo needs a test.

    Run with `--self-test`; also runs before any real fingerprint.
    """
    cases = {
        "emptyObject":  ({"type": "object",  "default": {}},            "default={}"),
        "emptyString":  ({"type": "string",  "default": ""},            "default=''"),
        "zero":         ({"type": "integer", "default": 0},             "default=0"),
        "false":        ({"type": "boolean", "default": False},         "default=False"),
        "nullDefault":  ({"type": "string",  "default": None},          "default=None"),
        "nested":       ({"type": "object",  "default": {"b": 2, "a": 1}},
                                                     'default={"a":1,"b":2}'),
        "normal":       ({"type": "string",  "default": "X-Auth-Token"},
                                                     "default='X-Auth-Token'"),
    }
    problems = []
    for name, (schema, want) in cases.items():
        out = []
        walk({"type": "object", "properties": {name: schema}}, "", out, "T", "v1")
        line = next((l for l in out if l.endswith(want) or want in l), None)
        if line is None:
            problems.append(f"{name}: expected {want!r}, got {out!r}")
    # And the control: with the default removed, nothing may claim one.
    out = []
    walk({"type": "object", "properties": {"x": {"type": "object"}}}, "", out, "T", "v1")
    if any("default=" in l for l in out):
        problems.append(f"control failed: a schema with NO default emitted one: {out!r}")

    # A PROPERTY NAMED `default` IS NOT A `default` KEYWORD. The aruba-provider-kog
    # session hit this for real: their vendor schemas have boolean properties
    # literally named `default` (Vpc, Subnet, SecurityGroup, Project), and an
    # all-defaults walker counted 4 of them as defaults. This script distinguishes
    # them structurally -- a property becomes a PATH SEGMENT, a keyword becomes a
    # `default=` on the owning line -- but only because the two are read from
    # different places. An edit that scanned for the string "default" would
    # conflate them and inflate every count, so it is pinned here. The third case
    # is the hard one: a property named `default` that also HAS one.
    out = []
    # NESTED UNDER `spec` ON PURPOSE. walk() suppresses a line when `path` is
    # empty, so a fixture whose TOP level holds the property named `default`
    # hides any defect that attaches to that level -- the containing object is
    # never emitted. The first version of this arm was shaped that way and a
    # working conflation mutant passed it: the fixture, not the check, was blind.
    walk({"type": "object", "properties": {"spec": {
            "type": "object",
            "properties": {
                "default":   {"type": "boolean"},
                "isDefault": {"type": "boolean", "default": False},
                "both":      {"type": "object", "default": {},
                              "properties": {"default": {"type": "boolean",
                                                         "default": True}}},
            }}}}, "", out, "T", "v1")
    named = [l for l in out if l.endswith("default type=boolean")]
    if not named:
        problems.append(f"a property NAMED `default` and carrying none should be "
                        f"emitted as a bare path, got {out!r}")
    if any("default=" in l for l in named):
        problems.append(f"a property NAMED `default` was reported as HAVING one: "
                        f"{named!r}")
    if sum(1 for l in out if "default=" in l) != 3:
        problems.append(f"expected exactly 3 default KEYWORDS (isDefault, both, "
                        f"both.default), got {[l for l in out if 'default=' in l]!r}")
    if problems:
        print("SELF-TEST FAILED:", file=sys.stderr)
        for p_ in problems:
            print("  " + p_, file=sys.stderr)
        return False
    return True


def main():
    if "--self-test" in sys.argv[1:]:
        ok = _self_test()
        print("self-test: %s" % ("all default shapes render visibly" if ok else "FAILED"))
        return 0 if ok else 4

    # The self-test runs before every real fingerprint: it is microseconds, and a
    # fingerprint produced by a build that cannot render an empty default is worse
    # than no fingerprint, because it diffs clean.
    if not _self_test():
        print("refusing to fingerprint: the self-test above failed, so a clean "
              "diff from this build would mean nothing.", file=sys.stderr)
        return 4

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

    # STAMP THE INSTRUMENT INTO THE ARTIFACT. Two fingerprints are only
    # comparable if the same keyword set produced them, and a file on disk
    # carried no record of that -- so a baseline taken with this script and a
    # comparison taken with an older one diffed as though the VERSIONS
    # differed. That happened: a run on the wrong git branch used a build
    # without `default`, and the missing lines would have read as 0.56.0
    # REMOVING defaults. A different check voided the run first, which is
    # luck, not design. `diff` now shows the mismatch on line 1.
    # additionalProperties is handled outside CONSTRAINTS, so name it here too
    # or the stamp would understate what this build can see.
    kws = ",".join(sorted(CONSTRAINTS + ("additionalProperties",)))
    print(f"# crd-shape keywords: {kws}")
    print(f"# {matched} CRDs in {GROUP}, {len(out)} constrained fields")
    for line in sorted(out):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
