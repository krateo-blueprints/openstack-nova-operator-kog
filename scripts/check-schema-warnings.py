#!/usr/bin/env python3
"""Fail if any RestDefinition carries a schemaWarning -- and VOID if it cannot tell.

WHY GATE ON THIS AT ALL. `status.schemaWarnings` is a real shipped field on
RestDefinitionStatus, and the oasgen-provider maintainers treat it as a
contract: it is in the CRD, so it is API surface, and it will not be removed
without a deprecation. The openstack-ironic-operator-kog session found NINE
warnings on their own definitions that nothing had ever read. One said a kind's
`get` verb addressed a path absent from its own OAS, which made the verb inert,
which in turn disabled the repair path for a second defect where `findby` could
never match. Three failures deep, all of it sitting in a field nobody looked at.

WHY GATING ON IT IS SAFE, which is the part that needed checking rather than
assuming: the field is assigned UNCONDITIONALLY on every render, so **it clears
when the cause is fixed** rather than latching. A field that only ever got set
would make this gate permanently red after one transient and would have to be
deleted a week later. (The oasgen maintainers pointed at
`restdefinition.go:1306` for the assignment; the line number will rot, the
property will not, which is why the property is what is written here.)

WHY THE VOID CASES ARE MOST OF THIS FILE. An empty warning list is the SAME
OUTPUT as every way of failing to look:

  * no cluster, or the API not answering          -> cannot look
  * zero RestDefinitions                          -> nothing to look at
  * the field absent from the CRD schema          -> an OLDER oasgen; the
    field simply does not exist, and `kubectl` then returns nothing for it,
    which is indistinguishable from "no warnings". This is the case that bites
    on a DOWNGRADE: pin 0.52.0 and this gate would print a clean sheet forever.
  * the field present but never populated         -> a definition that has not
    re-rendered since install. Its empty list means UNKNOWN, not CLEAN.

THE LAST ONE IS NOT A GUESS, and the field's own documentation is what settles
it. `SchemaWarnings` is `omitempty`, so an empty result is omitted from the wire
rather than written as `[]` -- deliberately, to keep `schemaWarnings: []` out of
every healthy `kubectl get -o yaml`. The consequence is stated in the source
doc: "AN ABSENT FIELD MEANS EITHER 'this render produced no warnings' OR 'this
RestDefinition has not been rendered by a version that writes the field', and
the two cannot be told apart from this field alone. Read status.generatorBuild
alongside it when that distinction matters." So `generatorBuild` is the
prescribed witness rather than one this repo invented, and it is `omitempty`
too, which is exactly what makes its absence meaningful.

Both field names were read from
`apis/restdefinitions/v1alpha1/types.go` at oasgen `origin/main`
(`generatorBuild` :1282, `schemaWarnings` :1307) rather than taken from a
message, because a gate keyed on a field name that does not exist is
permanently VOID and the output would look like diligence.

The warnings are deliberately non-fatal upstream -- a RestDefinition carrying
them is still Ready and still Synced -- because failing generation over a
declaration naming a missing field would break working installs. That is
upstream's call and correct for them; it is also precisely why a consumer has
to read the field, since nothing in the conditions will ever say it. They also
go to a Warning Event, and the apiserver's default --event-ttl is one hour, so
a definition installed yesterday with broken declarations was invisible through
every other cluster channel (upstream #462, and #492 for the fleet sweep this
gate is a small instance of).

So each of those exits 4 (VOID) and says which one it was. Only a measurement
that could have shown a warning is allowed to exit 0.
"""
import json
import os
import subprocess
import sys

RD_RESOURCE = "restdefinitions.ogen.krateo.io"
VOID = 4
FOUND = 1
CLEAN = 0
# A warning this chart EXPECTED is gone. Non-zero because it demands a human
# edit to this file, but it must not share a code with FOUND: one says "a new
# defect may have appeared", the other says "something you were waiting for
# happened". A reader who cannot tell them apart will treat good news as a
# regression, or worse, the reverse.
STALE = 5

# The tag that first declares `json:"schemaWarnings,omitempty"` on
# RestDefinitionStatus. Measured by sweeping oasgen's tags rather than asked
# for: 0.50.0 and 0.52.0 do not have it, 0.54.0 through 0.57.0 do, with a
# control asserting the sweep could read RestDefinitionStatus at every tag so
# that "absent" was a reading and not a failure to look.
#
# This number is why the script needs the version at all. Below it the field
# CANNOT exist, so its absence is not a defect and VOIDing on it would make CI
# permanently red for a correct installation. At or above it, an absent field
# means something is wrong. Those two must not share an exit code -- which is
# the same rule this whole script is built on, one level up.
FIELD_SINCE = (0, 54, 0)

# ---------------------------------------------------------------------------
# THE SEVEN WARNINGS THIS CHART IS EXPECTED TO CARRY.
#
# WHY AN ALLOWLIST AND NOT A ZERO. Until 2026-11-18 this gate demanded zero
# warnings and was only ever reached at a pin where the field does not exist,
# so it had never once looked at a warning. The moment a second CI arm runs at
# the platform pin (0.74.0, where the field DOES exist) the gate goes live and
# finds seven. Six of them are this chart's design showing through, and a gate
# that is permanently red over correct behaviour gets deleted within a week --
# which would lose the one warning that is a real defect along with the six
# that are not.
#
# So the gate's question changes from "are there warnings" to "are these the
# warnings we decided to accept". That makes it sensitive to the thing worth
# being sensitive to: a SEVENTH warning appearing, or one of these SEVEN
# disappearing.
#
# A DISAPPEARED WARNING FAILS TOO, and that is the point rather than a quirk.
# Manually watching for a warning to vanish is exactly the kind of thing nobody
# does; a red gate saying "the thing you were waiting for happened" is.
#
# BUT IT IS NOT A SIGNAL FROM UPSTREAM, and an earlier version of this comment
# said it was. It claimed the create-at-202 entry would clear when oasgen started
# honouring `successCodes`, making it the trigger for a pin move. MEASURED
# 2026-11-18 against 0.83.0-rc1, which carries that fix: the warning is
# UNCHANGED, and it could not have been otherwise. The fix makes a DECLARED
# `successCodes` reach the generator, and this chart declares none -- so nothing
# upstream can clear it at any release.
#
# What a cleared 202 entry will actually mean is that OUR OWN declaration took
# effect, which is a confirmation of a change made here and not news from
# elsewhere. Useful, and a different thing. The remaining six entries are where
# this arm earns its keep: those clear when an upstream fix lands, with nothing
# of ours changing.
#
# `key` is a SUBSTRING chosen to survive rewording but to break if the
# warning's SUBJECT changes: the parameter name, the field name, the action.
# Matching on the whole sentence would make every upstream copy-edit a CI
# failure, and matching on one word would accept a different defect.
EXPECTED = {
    "nova-aggregate": {
        'parameter "id" is omitted':
            "BY DESIGN. The get verb supplies {id} from status.aggregate.id "
            "with a request-direction fieldMapping, so spec.id is not the "
            "author's to set. See the banner in chart/samples/.",
    },
    "nova-flavor": {
        'parameter "id" is omitted':
            "BY DESIGN, same mechanism, from status.flavor.id.",
    },
    "nova-keypair": {
        'parameter "name" is omitted':
            "BY DESIGN, same mechanism, from spec.keypair.name. Note this one "
            "reads from SPEC rather than status, which is why the parameter "
            "name differs from the other four.",
    },
    "nova-quota": {
        "status field 'tenant_id' not found":
            "DORMANT, documented in chart/templates/rd-quota.yaml. Real Nova "
            "returns `id` rather than `tenant_id` from quota_set. It is inert "
            "only because nothing in this chart declares a findby; it becomes "
            "a real defect the moment this kind gains one.",
    },
    "nova-server": {
        'parameter "id" is omitted':
            "BY DESIGN, same mechanism, from status.server.id.",
        "no suitable response schema found for action 'create'":
            "THE ONE REAL DEFECT HERE. Nova answers POST /servers with "
            "202, the generator defaulted to {200, 201} and nothing could "
            "widen it. Fixed upstream as oasgen#805 (a verb's SuccessCodes "
            "now reach the generator, UNIONed with the default rather than "
            "replacing it, so declaring 202 cannot cost 200/201). "
            "THIS ENTRY WILL NOT CLEAR ON ITS OWN AND MUST NOT BE WAITED "
            "FOR: measured against 0.83.0-rc1, which carries #805, and the "
            "warning is unchanged -- correctly, because the fix reads a "
            "DECLARED successCodes and this chart declares none. The order "
            "is: move the pin to a release carrying #805, declare "
            "`successCodes: [202]` on the server create verb, and THEN this "
            "entry clears and gets removed from here. Never declare it "
            "first: at the 0.52.0 pin the field is present in the CRD and "
            "honoured at runtime while the generator ignores it, which reads "
            "as configured and is half-working.",
    },
    "nova-server-group": {
        'parameter "id" is omitted':
            "BY DESIGN, same mechanism, from status.server_group.id.",
    },
}


def _version_tuple(v):
    """0.57.0 -> (0,57,0). Returns None if it cannot be parsed, which VOIDs."""
    core = (v or "").strip().lstrip("vV").split("-")[0].split("+")[0]
    parts = core.split(".")
    if not 2 <= len(parts) <= 4:
        return None
    try:
        return tuple(int(x) for x in parts[:3])
    except ValueError:
        return None


def _field_in_crd_schema(crd):
    """Is status.schemaWarnings declared in ANY served version of the CRD?"""
    for ver in (crd.get("spec", {}) or {}).get("versions", []) or []:
        props = (((ver.get("schema") or {}).get("openAPIV3Schema") or {})
                 .get("properties") or {})
        status = (props.get("status") or {}).get("properties") or {}
        if "schemaWarnings" in status:
            return True
    return False


def classify(crd, rd_items, oasgen_version=None):
    """Pure verdict over the two documents. Returns (exit_code, [lines])."""
    out = []
    if crd is None:
        return VOID, ["VOID: could not read the RestDefinition CRD, so it is "
                      "unknown whether this oasgen even serves schemaWarnings."]
    if not _field_in_crd_schema(crd):
        ver = _version_tuple(oasgen_version)
        if oasgen_version is not None and ver is None:
            return VOID, [
                "VOID: could not parse the oasgen version {!r}, so it is "
                "unknown whether".format(oasgen_version),
                "  this installation is old enough to lack the field "
                "legitimately.",
            ]
        if ver is not None and ver < FIELD_SINCE:
            return CLEAN, [
                "SKIPPED, and this is not a pass: oasgen {} predates "
                "status.schemaWarnings, which".format(oasgen_version),
                "  first appears in {}. The field cannot exist here, so there "
                "is nothing to measure and".format(
                    ".".join(str(x) for x in FIELD_SINCE)),
                "  nothing is wrong. This gate starts testing something the "
                "moment the pin reaches",
                "  {} or above -- and note {} is in this repo's UNSAFE set "
                "(it broke apiKey RBAC),".format(
                    ".".join(str(x) for x in FIELD_SINCE),
                    ".".join(str(x) for x in FIELD_SINCE)),
                "  so the first pin that makes this meaningful is 0.54.1 or "
                "later.",
            ]
        return VOID, [
            "VOID: status.schemaWarnings is NOT in the RestDefinition CRD "
            "schema, but oasgen {} should".format(oasgen_version or "<unknown>"),
            "  have it ({} introduced it). An absent field reads as an empty "
            "list through kubectl, so".format(
                ".".join(str(x) for x in FIELD_SINCE)),
            "  a clean result would be a statement about the CRD and not "
            "about the definitions.",
        ]
    if not rd_items:
        return VOID, ["VOID: zero RestDefinitions found. A gate over an empty "
                      "set passes on any corpus, including a broken one."]

    unrendered, seen, examined = [], [], 0
    for rd in rd_items:
        name = (rd.get("metadata") or {}).get("name", "<unnamed>")
        status = rd.get("status") or {}
        # The render-completed witness. Without it an empty warning list means
        # "this definition has not re-rendered since install", i.e. unknown.
        if not status.get("generatorBuild"):
            unrendered.append(name)
            continue
        examined += 1
        seen.append((name, status.get("schemaWarnings") or []))

    if unrendered:
        out.append("VOID: {} definition(s) carry no status.generatorBuild, so "
                   "their render never completed and an".format(len(unrendered)))
        out.append("  empty warning list means UNKNOWN, not clean: {}"
                   .format(", ".join(sorted(unrendered))))
        return VOID, out

    # Three outcomes over the allowlist, and they do not share an exit code.
    unexpected, cleared, accepted = [], [], 0
    present = {name for name, _ in seen}
    for name, warns in seen:
        exp = EXPECTED.get(name, {})
        for w in warns:
            if any(k in w for k in exp):
                accepted += 1
            else:
                unexpected.append((name, w))
        for k, why in exp.items():
            if not any(k in w for w in warns):
                cleared.append((name, k, why))

    # An EXPECTED definition that is not in the corpus at all is NOT a cleared
    # warning: `restdefinitions.<name>.enabled` legitimately toggles kinds off,
    # and reporting a disabled kind as "the upstream fix landed" would send a
    # reader to move a pin for no reason. Said out loud rather than skipped
    # silently, because a shrinking corpus is also how an allowlist rots.
    absent = sorted(set(EXPECTED) - present)

    if unexpected:
        out.append("FOUND: {} schemaWarning(s) that this chart has NOT accepted."
                   .format(len(unexpected)))
        for name, w in sorted(unexpected):
            out.append("  {}:".format(name))
            out.append("    - {}".format(w))
        out.append("")
        out.append("An unexpected warning is the case this gate exists for. "
                   "Either it is a new defect,")
        out.append("or it is behaviour this chart accepts -- in which case add "
                   "it to EXPECTED in this")
        out.append("file WITH THE REASON, so the next reader inherits the "
                   "judgement and not just the pass.")
        if cleared:
            out.append("")
            out.append("Also, {} expected warning(s) have CLEARED -- see below; "
                       "both need attention.".format(len(cleared)))
            for name, k, why in sorted(cleared):
                out.append("  {}: {!r} no longer appears".format(name, k))
        return FOUND, out

    if cleared:
        out.append("STALE: {} warning(s) this chart expected are GONE. That is "
                   "news, not a failure.".format(len(cleared)))
        for name, k, why in sorted(cleared):
            out.append("  {}: {!r} no longer appears".format(name, k))
            out.append("      {}".format(why))
        out.append("")
        out.append("Remove the entry from EXPECTED in this file, and do what "
                   "its note says to do. This")
        out.append("gate is red on purpose: a warning disappearing is the "
                   "trigger for work that nobody")
        out.append("remembers to check for by hand.")
        if absent:
            out.append("")
            out.append("(Not counted above, and not a defect: {} expected "
                       "definition(s) are not in the".format(len(absent)))
            out.append(" corpus at all, so their kinds are presumably "
                       "disabled: {})".format(", ".join(absent)))
        return STALE, out

    out.append("clean: {} RestDefinition(s) examined, every one with a "
               "generatorBuild witness,".format(examined))
    out.append("  {} accepted warning(s) matched, zero unexpected, zero "
               "cleared.".format(accepted))
    if absent:
        out.append("  {} expected definition(s) absent from the corpus "
                   "(kind disabled?): {}".format(len(absent), ", ".join(absent)))
    return CLEAN, out


def _kubectl(kubeconfig, context, args):
    cmd = ["kubectl", "--kubeconfig", kubeconfig, "--context", context] + args
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "could not run kubectl: {}".format(exc)
    if p.returncode != 0:
        return None, (p.stderr or p.stdout or "").strip()[:400]
    try:
        return json.loads(p.stdout), None
    except ValueError as exc:
        return None, "kubectl output was not JSON: {}".format(exc)


def selftest():
    """Every verdict must be reachable, and each VOID must be distinguishable."""
    good_crd = {"spec": {"versions": [{"schema": {"openAPIV3Schema": {
        "properties": {"status": {"properties": {"schemaWarnings": {}}}}}}}]}}
    bare_crd = {"spec": {"versions": [{"schema": {"openAPIV3Schema": {
        "properties": {"status": {"properties": {}}}}}}]}}
    rendered = {"metadata": {"name": "ok"},
                "status": {"generatorBuild": "0.57.0-abc", "schemaWarnings": []}}
    warned = {"metadata": {"name": "bad"},
              "status": {"generatorBuild": "0.57.0-abc",
                         "schemaWarnings": ["findby can never match"]}}
    unrendered = {"metadata": {"name": "stale"}, "status": {}}
    # Real names from EXPECTED, so these arms exercise the allowlist rather
    # than a parallel fixture that cannot express the defect.
    def _rd(name, warns):
        return {"metadata": {"name": name},
                "status": {"generatorBuild": "0.74.0-abc",
                           "schemaWarnings": list(warns)}}
    EXP_FLAVOR = ('generation error: parameter "id" is omitted from the '
                  'generated spec because verb "get" supplies it')
    assert any(k in EXP_FLAVOR for k in EXPECTED["nova-flavor"]), \
        "the fixture cannot match its own allowlist entry -- vacuous arm"
    exp_only = _rd("nova-flavor", [EXP_FLAVOR])
    exp_plus = _rd("nova-flavor", [EXP_FLAVOR, "maxLength dropped from a $ref"])
    exp_gone = _rd("nova-flavor", [])

    cases = [
        ("clean corpus",            good_crd, [rendered],            CLEAN, "0.57.0"),
        ("a warning present",       good_crd, [rendered, warned],    FOUND, "0.57.0"),
        # The field is absent AND the version explains why -> not a defect.
        ("field absent, old pin",   bare_crd, [rendered],            CLEAN, "0.52.0"),
        # The field is absent and the version does NOT explain it -> defect.
        ("field absent, new pin",   bare_crd, [rendered],            VOID,  "0.57.0"),
        # Absent field with no version at all: cannot excuse it, so VOID.
        ("field absent, no version", bare_crd, [rendered],           VOID,  None),
        ("unparseable version",     bare_crd, [rendered],            VOID,  "not-a-version"),
        ("CRD unreadable",          None,     [rendered],            VOID,  "0.57.0"),
        ("zero definitions",        good_crd, [],                    VOID,  "0.57.0"),
        ("never rendered",          good_crd, [rendered, unrendered], VOID, "0.57.0"),
        # The allowlist's three outcomes.
        ("only expected warnings",  good_crd, [exp_only],            CLEAN, "0.74.0"),
        ("an unexpected warning",   good_crd, [exp_plus],            FOUND, "0.74.0"),
        ("an expected one cleared", good_crd, [exp_gone],            STALE, "0.74.0"),
    ]
    bad = 0
    for label, crd, items, want, ver in cases:
        got, lines = classify(crd, items, ver)
        ok = got == want
        bad += not ok
        print("  {:24s} want={} got={}  {}".format(
            label, want, got, "ok" if ok else "*** WRONG ***"))
        if not ok:
            for l in lines:
                print("      {}".format(l))
    # The suite must contain at least one of each verdict, or it proves nothing
    # about a classifier that returns a constant.
    wants = [w for _, _, _, w, _ in cases]
    for v, nm in ((CLEAN, "CLEAN"), (FOUND, "FOUND"), (VOID, "VOID"),
                  (STALE, "STALE")):
        if v not in wants:
            print("  *** SUITE DEFECT: no arm expects {}".format(nm))
            bad += 1
    # And the three VOID arms must not be interchangeable: each must name its
    # own cause, or a VOID would send a reader to the wrong explanation.
    msgs = set()
    for label, crd, items, want, ver in cases:
        if want != VOID:
            continue
        _, lines = classify(crd, items, ver)
        msgs.add(lines[0])
    n_void = sum(1 for w in wants if w == VOID)
    if len(msgs) != n_void:
        print("  *** SUITE DEFECT: {} VOID arms share {} distinct messages -- "
              "a VOID would not say which.".format(n_void, len(msgs)))
        bad += 1
    else:
        print("  {:24s} {} VOID arms, {} distinct causes  ok"
              .format("void messages differ", n_void, len(msgs)))
    # A SKIP and a real CLEAN share an exit code, which is correct -- neither
    # is a defect -- so the TEXT is the only thing that separates them, and a
    # reader who cannot separate them will record "gate green" for a gate that
    # measured nothing. Assert the text does the work.
    _, skip_lines = classify(bare_crd, [rendered], "0.52.0")
    _, real_lines = classify(good_crd, [rendered], "0.57.0")
    if "SKIPPED" not in skip_lines[0] or "not a pass" not in skip_lines[0]:
        print("  *** SUITE DEFECT: the skipped case does not announce itself as "
              "skipped: {!r}".format(skip_lines[0]))
        bad += 1
    elif "SKIP" in real_lines[0]:
        print("  *** SUITE DEFECT: a real clean result claims to be skipped")
        bad += 1
    else:
        print("  {:24s} skip says SKIPPED+not-a-pass, clean does not  ok"
              .format("skip vs clean text"))
    # A kind toggled OFF must not read as a cleared warning. Without this the
    # allowlist would tell a reader to move a pin because they disabled quota.
    code, lines = classify(good_crd, [rendered], "0.74.0")
    if code != CLEAN:
        print("  *** SUITE DEFECT: a corpus with no EXPECTED definitions "
              "returned {} -- a disabled kind reads as a cleared warning"
              .format(code))
        bad += 1
    elif not any("disabled" in l for l in lines):
        print("  *** SUITE DEFECT: the absent-definition note is silent, so a "
              "shrinking corpus would rot the allowlist invisibly")
        bad += 1
    else:
        print("  {:24s} absent kinds -> CLEAN and said out loud  ok"
              .format("disabled kind"))
    print("  --- {} ---".format("FAIL" if bad else "all arms pass"))
    return 1 if bad else 0


def main(argv):
    if "--self-test" in argv:
        return selftest()
    if len(argv) < 4:
        sys.stderr.write(
            "usage: check-schema-warnings.py <kubeconfig> <context> <namespace> "
            "[oasgen-version]\n"
            "       check-schema-warnings.py --self-test\n"
            "The context is REQUIRED and never defaulted: this runs on a shared\n"
            "machine whose ambient kubeconfig reaches real remote clusters.\n")
        return 2
    kubeconfig, context, ns = argv[1], argv[2], argv[3]
    oasgen_version = argv[4] if len(argv) > 4 else None
    if not os.path.exists(kubeconfig):
        print("VOID: kubeconfig {!r} does not exist.".format(kubeconfig))
        return VOID

    crd, err = _kubectl(kubeconfig, context, ["get", "crd", RD_RESOURCE, "-o", "json"])
    if crd is None:
        print("VOID: could not read the RestDefinition CRD. kubectl said:")
        print("  {}".format(err))
        return VOID

    listing, err = _kubectl(kubeconfig, context,
                            ["-n", ns, "get", RD_RESOURCE, "-o", "json"])
    if listing is None:
        print("VOID: could not list RestDefinitions in namespace {!r}. "
              "kubectl said:".format(ns))
        print("  {}".format(err))
        return VOID

    code, lines = classify(crd, listing.get("items") or [], oasgen_version)
    for l in lines:
        print(l)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
