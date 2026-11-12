#!/usr/bin/env python3
r"""Do this chart's RestDefinitions and its OAS documents actually meet?

A STATIC SCREEN, ported from openstack-ironic-operator-kog's
scripts/check-restdefinition-shape.py (their #47) with their permission and at
their suggestion. Two documents ship side by side in this chart -- the
RestDefinition that declares verbs, identifiers and field mappings, and the
OpenAPI document those verbs are supposed to address -- and NOTHING in CI
checked that they agree. A verb naming a path the OAS does not declare renders
a CRD and a controller that can never succeed, and it does so silently.

WHY IT IS WORTH HAVING WHEN A CLUSTER PROBE EXISTS. The probes in hack/ measure
a running controller: they need a cluster, they cost money, and they answer
"did this work". This answers "could it ever work", from the shipped bytes, in
under a second, with no cluster and no network. In their repo it re-derived a
CRD-level defect they had previously measured from live cluster dumps -- two
methods sharing no step, same answer. That is an independent channel rather
than a replication, which is a thing this repo has been short of.

THE ONE REAL PORTING PROBLEM, and a naive port would have been worse than
nothing. Nova wraps every payload in an envelope, so this chart's identifiers
and mapping sources are DOTTED -- `flavor.id`, `status.server_group.id` -- while
theirs are flat. A screen that compared dotted names against top-level property
names would report a finding on all six documents on its first run, be correct
about none of them, and be muted by the second person who read it. So path
resolution walks the schema segment by segment, through $ref, allOf/anyOf/oneOf
and array items.

WHAT A GREEN RESULT DOES NOT MEAN, demonstrated the hard way by the session
this was ported from. They installed three kinds this screen had passed and the
kinds did not work: the vendor requires an API microversion their endpoints
arrive at (1.23 / 1.52 / 1.55 against a 1.1 default), so the CRs got 405, 404
and 405 on first contact. The screen passed them CORRECTLY -- a microversion
floor appears in neither the RestDefinition nor the OAS paths.

So: this proves TWO DOCUMENTS AGREE WITH EACH OTHER. It does not prove the
vendor will accept the result. For this chart, measured 2026-11-12: 0 of the 6
documents declare a microversion header, and the 11 endpoints they address are
all core Nova v2.1. Endpoint availability is therefore not the exposure here --
but FIELD-level microversion requirements (Nova has moved several, such as the
server-group policy field) are invisible to this screen and to the documents,
and are not verified anywhere in this repo. A kind can pass this screen and
still be refused on first contact.

  hack/check-restdefinition-shape.py              # screen the shipped chart
  hack/check-restdefinition-shape.py --selftest   # drive every row type
"""
from __future__ import annotations

import argparse
import copy
import glob
import os
import re
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError:  # pragma: no cover
    print("check-restdefinition-shape: PyYAML missing -- VOID, not a pass", file=sys.stderr)
    sys.exit(9)


class Void(Exception):
    """The screen could not form an opinion. Never a pass."""


# WHAT SURVIVES IN THE SPEC IS VERSION-DEPENDENT, and getting this wrong in
# either direction mislabels every document. The rule, assembled from two
# measurements that initially looked contradictory:
#
#   spec.<param> survives  IFF  the create body declares <param> at top level
#                          OR   the mapping is SELF-REFERENCING (param <- spec.param)
#                               AND oasgen >= 0.52.0
#
# The second clause is oasgen-provider#421, first released in 0.52.0 (0.51.0 was
# tagged 90 minutes before it merged). This repo measured both sides on its own
# quota document: at 0.50.0 `spec.tenant_id` was ABSENT, at 0.52.0 PRESENT and
# required -- see the 0.52.0 row in docs/configuration.md.
#
# The first clause came from openstack-ironic-operator-kog, who measured four
# live kinds at oasgen 0.50.0 and read it as a CONTRADICTION of the second: their
# self-referencing rows were absent and their body-declared row was present. It
# is not a contradiction -- their measurement predates #421, where absence is
# exactly what this rule predicts. But their body-declared row is a factor THIS
# chart cannot see: all six of its create bodies carry a single envelope key, so
# no mapped param is ever body-declared and that factor is CONSTANT here. A rule
# derived only from these six documents could not have found it.
#
# Not a default to leave implicit: the answer differs by version, so the version
# is a parameter and the output states which one it is for.
OASGEN_ASSUMED = "0.52.0"


def ci_oasgen_version(repo_root):
    """The oasgen version CI actually runs, read from the workflow that runs it.

    openstack-ironic-operator-kog found their screen reading a repo pin that said
    0.54.1 while their lab ran 0.59.0 -- five minors, and `make` would have
    DOWNGRADED a working cluster. Their lesson is the one worth copying: **a pin
    that disagrees with reality gives a wrong verdict rather than no verdict**,
    and they only found it because something finally read it for a decision.

    So this screen does not keep its own second declaration of the version. It
    reads the value out of .github/workflows/validate.yaml -- the file that
    installs the provider CI tests against -- and says which source it used.
    OASGEN_ASSUMED remains only as the fallback when that file cannot be read,
    and the output names the fallback as such rather than presenting it as the
    pin. Checked 2026-11-12: both say 0.52.0, so they agree today; nothing but
    this function would keep them agreeing.
    """
    wf = os.path.join(repo_root, ".github", "workflows", "validate.yaml")
    try:
        with open(wf) as fh:
            for line in fh:
                m = re.match(r'^\s*OASGEN_VERSION:\s*"?([0-9]+\.[0-9]+\.[0-9]+)"?\s*$', line)
                if m:
                    return m.group(1), "the OASGEN_VERSION in .github/workflows/validate.yaml"
    except OSError:
        pass
    return None, None

# Stable markers, so a disposition is never read out of prose. The first version
# keyed on the word "survives" appearing in a message -- which works until
# someone rewords the message, and then every sample check silently flips.
MARK_SURVIVES = "SPEC-PROPERTY SURVIVES"
MARK_OMITTED = "SPEC-PROPERTY OMITTED"
# F's stable marker. Same reason as the two above: an arm that asserts a letter
# and a severity is not asserting a message, and F has two messages -- one
# resting on a field the vendor returns, one resting on the transform heuristic.
MARK_UNREACHABLE_CREATE = "UNREACHABLE CREATE"
# G's stable marker. G is about a policy DECLARATION that is not in force --
# the key placed where the apiserver prunes it, placed where a CEL rule refuses
# the manifest, or carrying a value the enum refuses. Never about matching.
MARK_POLICY_NOT_IN_FORCE = "POLICY NOT IN FORCE"


def parse_version(text):
    """"0.52.0" -> (0, 52, 0); None if unparseable, which VOIDs rather than guesses."""
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", str(text or "").strip())
    return tuple(int(g) for g in m.groups()) if m else None


# Conditions that are REAL, MEASURED, upstream's, and already documented here.
# Keyed exactly, so a NEW instance of the same shape stays actionable. Each entry
# must name where the evidence lives -- an unexplained exemption is how a screen
# stops screening.
KNOWN = {
    # EMPTY, AND THAT IS THE CORRECT STATE TODAY. My first draft shipped four
    # entries exempting `status.<envelope>.id` mappings, written from the
    # assumption that they would not resolve. They do: the create response
    # declares the id, so no C row is produced and the exemptions matched
    # nothing. The screen's own stale-exemption NOTE caught them on the first
    # real run, and the honest response to "this exemption matches nothing" is
    # to delete it, not to keep a comment that looks like coverage.
    #
    # To add one: key it (slug, check, subject) exactly, and say where the
    # evidence lives. An exemption without a citation is how a screen stops
    # screening. The mechanism is proven by the selftest, which injects an
    # entry of its own rather than relying on a live one existing.
}

# The documents this chart actually ships. Asserted to still be screened, because
# a refactor that stopped reading them would otherwise report a clean sheet --
# the single most valuable line in the original, and the one I would have
# forgotten.
# THE SLUG IS THE ConfigMap KEY's STEM, not the resource name, and they differ:
# the server-group RestDefinition is named `<release>-server-group` while its OAS
# key is `servergroup.yaml`, and chart/assets/ calls the same file
# `server-group.yaml`. Three spellings of one document. This set caught my own
# wrong guess on its first real run -- which is what the assertion is for, and
# it is also why the OAS is read from the rendered ConfigMap rather than by
# guessing a filename in chart/assets/.
LIVE = {"flavor", "server", "servergroup", "keypair", "quota", "aggregate"}


# --------------------------------------------------------------------------
# schema resolution -- PURE, no IO, no cluster
# --------------------------------------------------------------------------
def _deref(schema, oas, depth=0):
    """Follow $ref once, returning the pointed-at schema."""
    if not isinstance(schema, dict) or depth > 8:
        return schema if isinstance(schema, dict) else {}
    if "$ref" in schema:
        name = str(schema["$ref"]).split("/")[-1]
        target = ((oas.get("components") or {}).get("schemas") or {}).get(name)
        return _deref(target, oas, depth + 1)
    return schema


def props(schema, oas, depth=0):
    """Property NAMES of a schema, following $ref and the composition keywords."""
    if schema is None or depth > 8:
        return set()
    schema = _deref(schema, oas, depth)
    out = set(schema.get("properties") or {})
    for key in ("allOf", "anyOf", "oneOf"):
        for sub in schema.get(key) or []:
            out |= props(sub, oas, depth + 1)
    if schema.get("type") == "array":
        out |= props(schema.get("items"), oas, depth + 1)
    return out


def resolves(schema, oas, dotted, depth=0):
    """Does a DOTTED path resolve through this schema?

    THE PORT'S WHOLE POINT. `flavor.id` must resolve against a create body whose
    only top-level property is `flavor`. A set-membership test on top-level names
    answers no, correctly for the wrong question, and that answer would be a
    finding on every document this chart ships.
    """
    if not dotted:
        return False
    schema = _deref(schema, oas, depth)
    if not isinstance(schema, dict):
        return False
    head, _, rest = dotted.partition(".")
    if schema.get("type") == "array":
        return resolves(schema.get("items"), oas, dotted, depth + 1)
    here = schema.get("properties") or {}
    if head in here:
        return True if not rest else resolves(here[head], oas, rest, depth + 1)
    for key in ("allOf", "anyOf", "oneOf"):
        for sub in schema.get(key) or []:
            if resolves(sub, oas, dotted, depth + 1):
                return True
    return False


def declared_params(oas, path, method=None, where=("path", "query")):
    """Parameter names the OAS declares for a path, path-level and operation-level.

    THE SCREEN'S FIRST REAL RUN WAS WRONG WITHOUT THIS, and the way it was wrong
    is the reason this function exists rather than a wider `in_spec`. Nova's
    quota set is addressed by `tenant_id`, which the OAS declares as a
    PATH-LEVEL parameter -- not as a property of any request body. A resolver
    that looked only at request bodies reported that `tenant_id` "can never be
    populated" and that all three of its mappings were unresolvable: four
    actionable rows about a resource this repo has MEASURED working on a real
    cluster. The contradiction with that measurement is what surfaced it; a
    screen whose first run disagrees with a settled measurement is wrong until
    proven otherwise.
    """
    node = (oas.get("paths") or {}).get(path) or {}
    out = set()
    for pr in node.get("parameters") or []:
        if isinstance(pr, dict) and pr.get("in") in where and pr.get("name"):
            out.add(pr["name"])
    if method:
        for pr in (_op(oas, path, method).get("parameters") or []):
            if isinstance(pr, dict) and pr.get("in") in where and pr.get("name"):
                out.add(pr["name"])
    return out


def _op(oas, path, method):
    return ((oas.get("paths") or {}).get(path) or {}).get((method or "").lower()) or {}


def request_schema(oas, path, method):
    op = _op(oas, path, method)
    for _ct, media in ((op.get("requestBody") or {}).get("content") or {}).items():
        return media.get("schema")
    return None


def response_schema(oas, path, method):
    op = _op(oas, path, method)
    for code, resp in (op.get("responses") or {}).items():
        if not str(code).startswith("2"):
            continue
        for _ct, media in ((resp or {}).get("content") or {}).items():
            return media.get("schema")
    return None


def collection_item_schema(oas, path, method):
    """For a collection endpoint, the schema of ONE item, plus the wrapper name."""
    schema = _deref(response_schema(oas, path, method), oas)
    if not isinstance(schema, dict):
        return None, None
    for wrapper, sub in (schema.get("properties") or {}).items():
        sub = _deref(sub, oas)
        if isinstance(sub, dict) and sub.get("type") == "array":
            return wrapper, sub.get("items")
    return "(unwrapped)", schema


# --------------------------------------------------------------------------
# evaluate -- PURE: a parsed RestDefinition plus a parsed OAS in, rows out
# --------------------------------------------------------------------------
# Rows are (check, subject, message, actionable). `actionable` is the whole
# point of the shape: a row that is real, understood and documented is still
# printed -- suppressing it would hide a NEW instance of the same shape -- but
# it does not fail the screen.
def evaluate(rd, oas, slug, oasgen_version=OASGEN_ASSUMED):
    res = ((rd or {}).get("spec") or {}).get("resource") or {}
    verbs = res.get("verbsDescription") or []
    if not verbs:
        raise Void("%s declares no verbsDescription, so there is nothing to screen" % slug)
    paths = oas.get("paths") or {}
    if not paths:
        raise Void("%s: its OAS declares no paths at all, so every verb would read as inert "
                   "and the screen would report six findings about a missing document" % slug)

    by_action = {v.get("action"): v for v in verbs}
    create = by_action.get("create")
    reader = by_action.get("get") or by_action.get("findby")
    findby = by_action.get("findby")

    create_body = request_schema(oas, create["path"], create["method"]) if create else None
    create_resp = response_schema(oas, create["path"], create["method"]) if create else None
    read_resp = response_schema(oas, reader["path"], reader["method"]) if reader else None
    asf = set(res.get("additionalStatusFields") or [])
    xf_text = str((findby or {}).get("responseTransform") or "")

    # An ACTION-ONLY kind: create, with no `get` and no `update`. It matters
    # because such a kind reaches its create exactly ONE way -- Observe reporting
    # the external resource ABSENT.
    #
    # The mechanism is one line, in unstructured-runtime's
    # pkg/controller/worker.go (read on main, at 1113; ironic cited 964 from
    # another ref, same structure):
    #
    #     if !observation.ResourceExists {          // -> queues ctrlevent.Create
    #     } else if !observation.ResourceUpToDate { // -> queues ctrlevent.Update
    #
    # A Create is queued ONLY on !ResourceExists. Nothing about up-to-dateness
    # can produce one. So once findby matches and Observe answers exists=true,
    # the create is unreachable for good -- and with no update verb the Update
    # that drift would queue cannot act either (restResources.go logs
    # "API action update not found").
    #
    # Deliberately NOT qualified by compareScope. An earlier version of this
    # comment leaned on the `compareScope: updatable` branch, where scopeFields
    # is empty when no update verb exists; that is real but narrower than the
    # truth, and it does not apply to a manifest that never sets compareScope.
    # The two scopes differ only in appearance: on the default scope drift IS
    # compared, it agrees (status was just populated from the same response body
    # being compared), and the result is the same silent no-op.
    #
    # Measured consequence: every bare-metal transition in
    # openstack-ironic-operator-kog -- deploy, undeploy, manage, provide,
    # inspect, clean -- became a silent no-op for a day, every CR reading Ready
    # and nothing erroring (their #65, merged).
    action_only = bool(create) and not by_action.get("get") and not by_action.get("update")

    # identifiersMatchPolicy is a PER-VERB field, and #148 read it from the
    # RESOURCE. Measured on the generated CRD, ogen.krateo.io_restdefinitions.yaml,
    # at the version this repo PINS (0.52.0) and again at 0.61.0, 0.62.0 and main
    # -- identical at all four:
    #
    #   spec.resource.verbsDescription[].identifiersMatchPolicy   enum [AND, OR]
    #   x-kubernetes-validations on the same verb item:
    #     rule:    self.action == 'findby' || !has(self.identifiersMatchPolicy)
    #     message: identifiersMatchPolicy can only be set for 'findby' actions
    #
    # The RESOURCE level has no such property. Its 13 are additionalStatusFields,
    # compareScope, configurationFields, createApiRef, deleteApiRef,
    # dependentLookups, excludedSpecFields, identifiers, includedSpecFields, kind,
    # observeApiRef, updateApiRef, verbsDescription -- and the resource object sets
    # no x-kubernetes-preserve-unknown-fields (the CRD's five are all on
    # *ApiRef.extras and fieldMapping.defaultIfAbsent), so a key placed there is an
    # unknown field in a structural schema and the apiserver PRUNES it: the apply
    # succeeds and the policy silently does not exist.
    #
    # What the old read cost: it could never see a declared AND, so every manifest
    # screened as OR, and D printed its NON-actionable "findby can still match via
    # <other identifier>" over manifests where findby could never match at all --
    # the same fail-quiet direction as the bug #148 was written to fix. The fixture
    # put the key in the same wrong place, so the arm that asserted AND asserted
    # nothing: test and code shared one error, which is this file's own README rule.
    #
    # OR remains the default when the key is ABSENT, and it "adopts on the FIRST
    # declared identifier whose value agrees and never looks at the rest". So one
    # unreadable identifier does not stop findby matching unless the policy is AND
    # or every identifier is unreadable -- "findby can NEVER match" is a claim
    # about the POLICY, not about one identifier.
    policy_declared = (findby or {}).get("identifiersMatchPolicy")

    # Where ELSE the key appears is a finding, not something to normalise away.
    policy_misplaced = []
    if "identifiersMatchPolicy" in res:
        policy_misplaced.append(("spec.resource", res["identifiersMatchPolicy"], "pruned"))
    for v in verbs:
        if v.get("action") != "findby" and "identifiersMatchPolicy" in v:
            policy_misplaced.append(
                ("spec.resource.verbsDescription[action=%s]" % (v.get("action") or "?"),
                 v["identifiersMatchPolicy"], "refused"))

    # The enum is case-SENSITIVE and spelled AND;OR. rest-dynamic-controller
    # lowercases and normalises anything unrecognised to "or", and its own comment
    # says that branch is UNREACHABLE: the enum reaches the generated CRD and the
    # chart's copy, so the apiserver refuses the manifest at admission. The
    # previous `.strip().upper()` modelled a path nobody can drive, and in doing so
    # read `and` -- a value the enum refuses -- as a working AND policy.
    policy_valid = policy_declared is None or policy_declared in ("AND", "OR")
    match_policy = policy_declared if policy_declared in ("AND", "OR") else "OR"

    def transform_supplies(ident):
        """Does the findby responseTransform plausibly supply this identifier?

        A HEURISTIC, AND IT DOWNGRADES, so it is stated rather than hidden. The
        original matched the identifier name in the transform text, which works
        for flat identifiers. Every identifier in this chart is DOTTED
        (`flavor.name`), and a transform naturally references the LEAF
        (`{name: .flavors[0].name}`), so the exact-name test never matched and
        the "a transform supplies it" case was unreachable -- the self-test arm
        for it failed, which is how this was found. Matching the leaf on a word
        boundary keeps `name` from matching `names`, but it cannot prove the
        transform really populates the field. A row downgraded on this basis
        says so.
        """
        if not ident or not xf_text:
            return False
        if ident in xf_text:
            return True
        leaf = ident.split(".")[-1]
        return bool(leaf) and re.search(r"\b%s\b" % re.escape(leaf), xf_text) is not None

    ver = parse_version(oasgen_version)
    if ver is None:
        raise Void("cannot parse the oasgen version %r, and what survives in the spec "
                   "depends on it (#421 landed in 0.52.0). Refusing to classify rather "
                   "than assume a version" % (oasgen_version,))

    # WHICH PATH PARAMS SURVIVE AS SPEC PROPERTIES, COMPUTED ONCE AND CONSUMED BY
    # EVERY CHECK. The first version computed it only inside the E rows and let
    # check C treat every declared path param as resolvable, so at an oasgen
    # below 0.52.0 the screen said the property was deleted (E, actionable) AND
    # that the mapping reading it resolved fine (C, silent) -- two checks
    # disagreeing about one fact, in the same run.
    #
    # openstack-ironic-operator-kog hit the mirror image: they special-cased
    # check C's actionability and turned four healthy kinds red. Their diagnosis
    # is the one to keep -- A SURVIVING PATH PARAM *IS* A SPEC FIELD, so it
    # belongs in the spec set before anything reads it, and the version rule
    # belongs there and nowhere else.
    survives_reason = {}
    for v in verbs:
        for m in v.get("requestFieldMapping") or []:
            pp = m.get("inPath")
            if not pp:
                continue
            src = str(m.get("inCustomResource") or "")
            if pp in props(create_body, oas):
                survives_reason[pp] = (True, "body", src)
            elif src == "spec." + pp and ver >= (0, 52, 0):
                survives_reason[pp] = (True, "selfref", src)
            elif src == "spec." + pp:
                survives_reason[pp] = (False, "selfref-below-0.52.0", src)
            elif src.partition(".")[0] == "spec":
                survives_reason[pp] = (False, "spec-elsewhere", src)
            else:
                survives_reason[pp] = (False, src.partition(".")[0] or "unknown", src)
    # An UNMAPPED declared path param is kept as spec.<param>: nothing supplies
    # it, so the author must.
    for v in verbs:
        mapped = {m.get("inPath") for m in (v.get("requestFieldMapping") or [])}
        for pp in re.findall(r"\{(\w+)\}", v.get("path") or ""):
            if pp not in mapped:
                survives_reason.setdefault(pp, (True, "unmapped", ""))

    # Every parameter the author can set: body properties, plus the declared path
    # and query parameters THAT SURVIVE at this version.
    author_params = set()
    for v in verbs:
        for pp in declared_params(oas, v.get("path"), v.get("method")):
            ok, _why, _src = survives_reason.get(pp, (True, "undeclared-mapping", ""))
            if ok:
                author_params.add(pp)

    def in_spec(dotted):
        return resolves(create_body, oas, dotted) or (dotted in author_params)

    def in_status(dotted):
        # additionalStatusFields are declared verbatim, so an exact match counts:
        # the author has asserted the field will be there.
        if dotted in asf:
            return True
        return resolves(create_resp, oas, dotted) or resolves(read_resp, oas, dotted)

    out = []

    # A -- the path and the method must both be declared, or the verb is inert.
    for v in verbs:
        p = v.get("path")
        m = (v.get("method") or "").lower()
        a = v.get("action")
        if p not in paths:
            out.append(("A", "%s %s" % (a, p),
                        "path is not declared in the OAS -- the verb is INERT", True))
        elif m not in {str(k).lower() for k in (paths.get(p) or {})}:
            out.append(("A", "%s %s %s" % (a, m.upper(), p),
                        "path is declared but method %s is not -- the verb is INERT" % m.upper(), True))

    # B -- an identifier must be readable from SOMEWHERE, or it can never be
    # populated. Unlike the original, the create RESPONSE counts: Nova assigns
    # `id` server-side, so requiring it in the request body would report a
    # finding on every document here and be wrong about all of them.
    for ident in res.get("identifiers") or []:
        if in_spec(ident) or in_status(ident):
            continue
        if transform_supplies(ident):
            out.append(("B", ident,
                        "not in the create body or any 2xx response; the findby "
                        "responseTransform mentions it (matched on the leaf -- a "
                        "heuristic, so confirm it really populates the field)", False))
        else:
            out.append(("B", ident,
                        "identifier resolves in neither the create body, the create "
                        "response, the read response, nor additionalStatusFields -- "
                        "it can never be populated", True))

    # C -- a mapping's source must resolve where it says it lives.
    for v in verbs:
        for m in v.get("requestFieldMapping") or []:
            src = str(m.get("inCustomResource") or "")
            root, _, rest = src.partition(".")
            if root == "spec":
                ok, pool = in_spec(rest), "the create body"
            elif root == "status":
                ok, pool = in_status(rest), "any 2xx response or additionalStatusFields"
            else:
                out.append(("C", src, "mapping source names neither spec nor status", True))
                continue
            if not ok:
                known = (slug, "C", src) in KNOWN
                out.append(("C", "%s %s<-%s" % (v.get("action"), m.get("inPath"), src),
                            "source does not resolve in %s%s" % (
                                pool, (" -- KNOWN: " + KNOWN[(slug, "C", src)]) if known else ""),
                            not known))

    # D -- an identifier the findby collection item cannot carry.
    #
    # FOUR outcomes, not two. The row always stays -- the identifier really is
    # unreadable, and additionalStatusFields under it really will not populate --
    # but `actionable` is the whole product of this screen, and "fix this" is the
    # WRONG instruction in three of the four cases:
    #
    #   action-only kind      -> informational. Supplying the identifier makes
    #                            findby match, which makes the create UNREACHABLE.
    #                            Reported as F instead.
    #   transform supplies it -> informational (unchanged; a heuristic).
    #   OR policy, another
    #   identifier readable   -> informational. findby still matches on that one,
    #                            so this costs DISCRIMINATION, not function: this
    #                            identifier can never be the matching one, which
    #                            is the precondition for adopting a different
    #                            object than the CR named (rdc #444).
    #   otherwise             -> ACTIONABLE. findby genuinely cannot match.
    # G -- a policy DECLARATION that never reaches the runtime. Separate from D
    # and F, which ask whether findby can match; G asks whether the author's
    # declaration is in force at all. Both branches are invisible in review, and
    # the first is the mistake #148 itself made.
    for where, value, fate in policy_misplaced:
        if fate == "pruned":
            out.append(("G", "%s.identifiersMatchPolicy" % where,
                        "%s: declared %r at %s, which the CRD does not define. The resource "
                        "object is a structural schema with no "
                        "x-kubernetes-preserve-unknown-fields, so the apiserver PRUNES this "
                        "key: the apply succeeds, the policy does not exist, and findby runs "
                        "under the OR default. It belongs on the findby verb -- "
                        "spec.resource.verbsDescription[action=findby].identifiersMatchPolicy"
                        % (MARK_POLICY_NOT_IN_FORCE, value, where), True))
        else:
            out.append(("G", "%s.identifiersMatchPolicy" % where,
                        "%s: declared %r on a non-findby verb (%s). The CRD carries a CEL rule "
                        "on the verb item -- self.action == 'findby' || "
                        "!has(self.identifiersMatchPolicy), message \"identifiersMatchPolicy "
                        "can only be set for 'findby' actions\" -- so the apiserver REFUSES "
                        "this manifest at admission. It cannot apply as written"
                        % (MARK_POLICY_NOT_IN_FORCE, value,
                           where.split("=")[-1].rstrip("]")), True))
    if findby and not policy_valid:
        out.append(("G", "identifiersMatchPolicy=%r" % (policy_declared,),
                    "%s: the findby verb declares %r, and the generated CRD's enum is exactly "
                    "[AND, OR], case-sensitive, so the apiserver REFUSES this manifest at "
                    "admission. Do not read the runtime's normalise-to-OR branch as a fallback: "
                    "it cannot be reached from a cluster. Checks D and F are NOT evaluated for "
                    "this kind, because every conclusion they would draw rests on a policy the "
                    "cluster will never accept -- fix the value and re-run"
                    % (MARK_POLICY_NOT_IN_FORCE, policy_declared), True))

    if findby and policy_valid:
        wrapper, item = collection_item_schema(oas, findby["path"], findby.get("method") or "get")
        idents = list(res.get("identifiers") or [])

        def in_item(ident):
            return bool(resolves(item, oas, ident)
                        or resolves(item, oas, ident.split(".")[-1]))

        # A responseTransform that manufactures an identifier makes findby able to
        # match on it. That is not a side note: ADDING one is what made ironic's
        # create unreachable, so F has to count it exactly as it counts a field
        # the vendor really returns.
        readable = [i for i in idents if in_item(i)]
        matchable = [i for i in idents if in_item(i) or transform_supplies(i)]
        if match_policy == "AND":
            can_match = bool(idents) and len(matchable) == len(idents)
        else:
            can_match = bool(matchable)

        for ident in idents:
            if in_item(ident):
                continue
            if transform_supplies(ident):
                out.append(("D", ident,
                            "absent from the collection item (%s); the responseTransform "
                            "mentions it (matched on the leaf -- a heuristic, so confirm "
                            "it really manufactures it)" % wrapper, False))
            elif action_only:
                out.append(("D", ident,
                            "absent from the collection item (%s) and no responseTransform "
                            "supplies it. NOT a defect to fix on this kind: it declares "
                            "create with no get and no update, so Observe reaches the create "
                            "ONLY by reporting the resource absent -- supplying this "
                            "identifier would make findby match and the create would never be "
                            "dispatched. The real cost is observability: this identifier, and "
                            "any additionalStatusFields under it, stay unpopulated" % wrapper,
                            False))
            elif can_match:
                out.append(("D", ident,
                            "absent from the collection item (%s) and no responseTransform "
                            "supplies it. findby can still match under identifiersMatchPolicy "
                            "%s via %s, so this costs discrimination rather than function: this "
                            "identifier can never be the matching one, which is the "
                            "precondition for adopting an object the CR did not name. Declare "
                            "identifiersMatchPolicy: AND to require all of them to agree"
                            % (wrapper, match_policy, ", ".join(readable) or "a transform"),
                            False))
            else:
                out.append(("D", ident,
                            "absent from the collection item (%s) and no responseTransform "
                            "supplies it -- findby can NEVER match%s" % (
                                wrapper,
                                " (identifiersMatchPolicy: AND requires every declared "
                                "identifier to agree, so one unreadable identifier is fatal)"
                                if match_policy == "AND" else ""),
                            True))

        # F -- the inverse of D, and the one that actually broke a cluster. An
        # action-only kind whose findby CAN match has an unreachable create: the
        # CR reports Ready and performs nothing, with no condition and no error.
        # Note what this does NOT flag: the same findby, the same transform, on a
        # kind that also declares get or update. Without that exclusion F
        # degenerates into "no responseTransform allowed on a findby", which
        # would be a worse screen than no screen.
        if action_only and can_match:
            # Does F rest on a field the vendor really returns, or only on the
            # transform heuristic? transform_supplies matches the identifier's
            # LEAF on a word boundary, so it can read "supplied" for a transform
            # that merely mentions the name. F fires either way, and the
            # asymmetry is deliberate: over-reporting costs a review,
            # under-reporting ships the silent no-op. But a row resting on the
            # heuristic has to SAY so, exactly as D's transform branch does --
            # otherwise this check quietly inherits every false positive of a
            # heuristic written for a different purpose, which is the coupling
            # ironic flagged between their F and our #137.
            heuristic_only = not any(in_item(i) for i in matchable)
            out.append(("F", ",".join(matchable) or "(none)",
                        "%s: this kind declares create with no get and no update, and its "
                        "findby CAN match (%s, under identifiersMatchPolicy %s). A Create is "
                        "queued only on !ResourceExists, so once findby matches the create is "
                        "NEVER dispatched: the CR reports Ready and performs nothing%s"
                        % (MARK_UNREACHABLE_CREATE, ", ".join(matchable), match_policy,
                           (" -- NOTE: this rests only on the responseTransform heuristic "
                            "(leaf match), not on a field the collection item declares, so "
                            "confirm the transform really manufactures it; the check errs "
                            "toward reporting because the opposite error is silent")
                           if heuristic_only else ""),
                        True))

    # E -- what happens to path params, which is where this chart's live
    # schemaWarnings come from. Informational by construction: the generator
    # says so itself, and the row exists so a reader can see the shape rather
    # than rediscover it from a cluster.
    seen = set()
    for v in verbs:
        mapped = {m.get("inPath"): str(m.get("inCustomResource") or "")
                  for m in (v.get("requestFieldMapping") or [])}
        for pp in re.findall(r"\{(\w+)\}", v.get("path") or ""):
            if pp in seen:
                continue
            if pp in mapped:
                seen.add(pp)
                ok, why, src = survives_reason.get(pp, (True, "unmapped", ""))
                if ok and why == "body":
                    out.append(("E", pp,
                                "%s: the create body declares %s at top level, so the property "
                                "exists regardless of the mapping" % (MARK_SURVIVES, pp), False))
                    continue
                if ok and why == "selfref":
                    out.append(("E", pp,
                                "%s: SELF-REFERENCING mapping from spec.%s, which oasgen#421 "
                                "(0.52.0; classifying for %s) stopped deleting -- the author "
                                "sets it" % (MARK_SURVIVES, pp, oasgen_version), False))
                    continue
                if why == "selfref-below-0.52.0":
                    out.append(("E", pp,
                                "%s: SELF-REFERENCING mapping from spec.%s, and oasgen %s is "
                                "BELOW 0.52.0, where the mapping still deletes the property it "
                                "reads from -- so the verb reads a field that does not exist"
                                % (MARK_OMITTED, pp, oasgen_version), True))
                    continue
                if why == "spec-elsewhere":
                    out.append(("E", pp,
                                "%s: supplied from %s, so the top-level spec.%s is omitted -- "
                                "the author sets %s instead, which is a DIFFERENT field"
                                % (MARK_OMITTED, src, pp, src), False))
                    continue
                if why == "status":
                    # The CONTROLLER supplies it, so oasgen omits spec.<param>.
                    # This is the same mechanism as the generation warnings this
                    # chart triages -- NOT a claim that each row here has a
                    # matching live warning: the live gate reports them for a
                    # SUBSET of these documents, and why that subset is not
                    # determinable from the OAS alone.
                    out.append(("E", pp,
                                "%s: supplied from status by a request-direction fieldMapping, "
                                "so oasgen omits spec.%s -- same mechanism as the generation "
                                "warnings this chart triages, re-derived from the OAS side"
                                % (MARK_OMITTED, pp), False))
                else:
                    out.append(("E", pp,
                                "%s: supplied from %r, which names neither spec nor status -- "
                                "check C above reports the source separately"
                                % (MARK_OMITTED, src), False))
            elif not in_spec(pp):
                seen.add(pp)
                out.append(("E", pp,
                            "%s: UNMAPPED and absent from the create body, so it is kept as "
                            "spec.%s -- a field the API never returns" % (MARK_SURVIVES, pp),
                            False))
    return out


# --------------------------------------------------------------------------
# sample screening -- also PURE
# --------------------------------------------------------------------------
# WHY THIS EXISTS. docs/architecture.md says the pruned-field hazard is not live
# here -- "5 sample documents of affected kinds, 0 setting a pruned field" --
# and ends with "Re-screen if a sample grows a top-level spec.id or spec.name."
# That re-screen was a sentence in a document and nothing in CI did it. A field
# the API server prunes is removed SILENTLY: the author gets no rejection, no
# event and no condition, so the only way to notice is to look. This looks.
#
# BOTH DIRECTIONS, because the three-case omission model makes them different
# checks rather than one:
#   a param oasgen OMITS must NOT be a top-level spec key -- it would be pruned
#   a param oasgen KEEPS (a self-referencing mapping, since oasgen#421) MUST be
#     set, or the verb's path placeholder never resolves and the request is
#     refused before it is sent
# A one-directional gate would have passed ComputeQuota with `tenant_id` missing,
# which is the broken case, while "0 samples set a pruned field" stayed true.
def param_dispositions(rd, oas, slug, oasgen_version=OASGEN_ASSUMED):
    """{param: "omitted"|"kept"} for one document, derived from its E rows."""
    out = {}
    for check, subject, message, _act in evaluate(rd, oas, slug, oasgen_version):
        if check != "E":
            continue
        # Keyed on a STABLE MARKER, never on prose. The first version looked for
        # the word "survives" in the message, so rewording a message would have
        # silently flipped every sample check that depends on this.
        if MARK_SURVIVES in message:
            out[subject] = "kept"
        elif MARK_OMITTED in message:
            out[subject] = "omitted"
        else:
            raise Void("an E row for %r carries neither disposition marker, so the sample "
                       "prune gate cannot tell whether the field survives: %s"
                       % (subject, message))
    return out


def screen_sample(kind, spec_keys, dispositions):
    """Rows for ONE sample CR. Pure: a kind, its top-level spec keys, a map in."""
    rows = []
    for param, how in sorted(dispositions.items()):
        if how == "omitted" and param in spec_keys:
            rows.append(("S", "%s spec.%s" % (kind, param),
                         "the sample sets a top-level field oasgen OMITS from the spec, so "
                         "the API server PRUNES it silently -- no rejection, no event, no "
                         "condition. The author's value is simply gone", True))
        elif how == "kept" and param not in spec_keys:
            rows.append(("S", "%s spec.%s" % (kind, param),
                         "the sample does NOT set a field this document's mapping reads "
                         "from, so the verb's path placeholder never resolves and the "
                         "request is refused before it is sent", True))
    return rows


# --------------------------------------------------------------------------
# run -- the only part that touches the filesystem or helm
# --------------------------------------------------------------------------
def render_chart(chart_dir):
    """helm template the chart, stamped, with the one required value supplied."""
    if not os.path.isdir(chart_dir):
        raise Void("no chart at %s, so there is nothing to screen" % chart_dir)
    tmp = tempfile.mkdtemp()
    staged = os.path.join(tmp, "chart")
    subprocess.run(["cp", "-R", chart_dir, staged], check=True)
    # CHART_VERSION is a placeholder in the repo; helm refuses the chart without
    # a real version, exactly as CI stamps it.
    for root, _dirs, files in os.walk(staged):
        for f in files:
            if f == "Chart.yaml":
                p = os.path.join(root, f)
                with open(p) as fh:
                    body = fh.read()
                with open(p, "w") as fh:
                    fh.write(body.replace("CHART_VERSION", "0.0.0-screen"))
    # nova.url is REQUIRED by the chart's own fail-fast. Supplying a dummy keeps
    # this a screen of shapes rather than of endpoints.
    proc = subprocess.run(
        ["helm", "template", "screen", staged, "--set", "nova.url=http://screen.invalid/v2.1"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise Void("helm template failed, so the shipped shapes were never read:\n%s"
                   % (proc.stderr or "").strip()[:600])
    return proc.stdout


def pair_documents(rendered):
    """Pair each RestDefinition with the OAS it names, the way the cluster will.

    The OAS is read out of the rendered ConfigMap rather than from chart/assets/
    on purpose: the pairing is what `oasPath` says, so a RestDefinition pointing
    at the wrong ConfigMap is caught here instead of being screened against the
    document its author meant.
    """
    docs = [d for d in yaml.safe_load_all(rendered) if d]
    rds = [d for d in docs if d.get("kind") == "RestDefinition"]
    cms = {d["metadata"]["name"]: (d.get("data") or {})
           for d in docs if d.get("kind") == "ConfigMap"}
    if not rds:
        raise Void("the rendered chart contains no RestDefinition at all")
    pairs = []
    for rd in rds:
        ref = str((rd.get("spec") or {}).get("oasPath") or "")
        m = re.match(r"^configmap://[^/]+/([^/]+)/(.+)$", ref)
        if not m:
            pairs.append((rd, None, ref, "oasPath is not a configmap:// reference: %r" % ref))
            continue
        cm_name, key = m.group(1), m.group(2)
        if cm_name not in cms:
            pairs.append((rd, None, ref,
                          "oasPath names ConfigMap %r, which this chart does not render" % cm_name))
            continue
        if key not in cms[cm_name]:
            pairs.append((rd, None, ref,
                          "ConfigMap %r has no key %r (it has %s)"
                          % (cm_name, key, sorted(cms[cm_name]))))
            continue
        try:
            oas = yaml.safe_load(cms[cm_name][key]) or {}
        except yaml.YAMLError as exc:
            pairs.append((rd, None, ref, "the OAS in %s/%s is not parseable YAML: %s"
                          % (cm_name, key, exc)))
            continue
        slug = re.sub(r"\.ya?ml$", "", key)
        pairs.append((rd, oas, slug, None))
    return pairs


def run(oasgen_version=OASGEN_ASSUMED):
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    # The version CI runs beats this file's own constant, so the two cannot drift.
    if oasgen_version is OASGEN_ASSUMED:
        found, where = ci_oasgen_version(root)
        if found:
            oasgen_version, ver_source = found, where
        else:
            ver_source = ("this script's FALLBACK constant -- "
                          ".github/workflows/validate.yaml could not be read for it")
    else:
        ver_source = "the --oasgen-version argument"
    rendered = render_chart(os.path.join(root, "chart"))
    pairs = pair_documents(rendered)

    actionable, informational, broken, screened = [], [], [], set()
    by_kind = {}
    for rd, oas, slug, err in pairs:
        name = (rd.get("metadata") or {}).get("name", "<unnamed>")
        if err:
            broken.append((name, err))
            continue
        screened.add(slug)
        try:
            rows = evaluate(rd, oas, slug, oasgen_version)
        except Void as v:
            broken.append((name, str(v)))
            continue
        for check, subject, message, act in rows:
            (actionable if act else informational).append((slug, check, subject, message))
        kind = (((rd.get("spec") or {}).get("resource") or {}).get("kind"))
        if kind:
            by_kind[kind] = (slug, param_dispositions(rd, oas, slug, oasgen_version))

    print("  classifying for oasgen %s, from %s" % (oasgen_version, ver_source))
    print("  (what survives in the spec depends on it: #421 landed in 0.52.0)")
    print("  screened %d document(s): %s" % (len(screened), ", ".join(sorted(screened)) or "none"))

    # The samples, against what each document says about its own path params.
    sample_dir = os.path.join(root, "chart", "samples")
    sample_files = sorted(glob.glob(os.path.join(sample_dir, "*.yaml")))
    if not sample_files:
        print("\nVOID -- no sample found under chart/samples/, so the prune check read nothing.")
        return 9
    checked_crs = 0
    for f in sample_files:
        try:
            docs = [d for d in yaml.safe_load_all(open(f)) if d]
        except yaml.YAMLError as exc:
            broken.append((os.path.basename(f), "sample is not parseable YAML: %s" % exc))
            continue
        for d in docs:
            kind = d.get("kind")
            if kind not in by_kind:
                continue  # Configuration halves and anything not generated here
            slug, disp = by_kind[kind]
            checked_crs += 1
            keys = set((d.get("spec") or {}).keys())
            for check, subject, message, act in screen_sample(kind, keys, disp):
                (actionable if act else informational).append(
                    (slug, check, "%s %s" % (os.path.basename(f), subject), message))
    # EVERY RESOURCE KIND MUST HAVE BEEN SEEN IN A SAMPLE. Without this, deleting
    # a sample silently removes its check and the gate reports a clean sheet --
    # the same shape as the LIVE assertion above, one level down.
    sampled = set()
    for f in sample_files:
        try:
            for d in (x for x in yaml.safe_load_all(open(f)) if x):
                if d.get("kind") in by_kind:
                    sampled.add(d["kind"])
        except yaml.YAMLError:
            pass
    missing_samples = set(by_kind) - sampled
    if missing_samples:
        print("\nVOID -- these resource kinds have NO sample, so nothing screened them: %s"
              % ", ".join(sorted(missing_samples)))
        print("  The prune check below would be clean by absence.")
        return 9
    print("  screened %d sample CR(s) across %d kind(s)" % (checked_crs, len(sampled)))

    # EVERY SHIPPED DOCUMENT MUST HAVE BEEN SCREENED. Without this, a refactor
    # that stopped reading them reports a clean sheet -- the most valuable line
    # in the original and the one easiest to leave out.
    missed = LIVE - screened
    if missed:
        print("\nVOID -- these documents ship but were NOT screened: %s" % ", ".join(sorted(missed)))
        print("  A clean result below would be a statement about this screen, not about them.")
        return 9

    if informational:
        print("\n  informational -- real, understood, and NOT failing the screen:")
        for slug, check, subject, message in informational:
            print("    [%s] %-14s %-28s %s" % (check, slug, subject, message))

    # A KNOWN entry that no longer matches anything is a stale exemption, and a
    # stale exemption is how a screen quietly stops screening.
    matched = {(s, c, subj.split("<-")[-1]) for s, c, subj, _m in informational + actionable}
    stale = [k for k in KNOWN if k not in matched]
    if stale:
        print("\n  NOTE: %d KNOWN exemption(s) matched nothing this run: %s" % (len(stale), stale))
        print("  Either the condition was fixed -- remove the entry -- or the screen stopped")
        print("  reading what it used to. Not failing on it, but it is never nothing.")

    if broken:
        print("\nVOID -- the screen could not form an opinion on these. NOT a pass:")
        for name, err in broken:
            print("    %s: %s" % (name, err))
        return 9

    if actionable:
        print("\nFAIL -- %d actionable row(s):" % len(actionable))
        for slug, check, subject, message in actionable:
            print("    [%s] %-14s %-28s %s" % (check, slug, subject, message))
        return 1

    print("\n  no actionable disagreement between any RestDefinition and its OAS.")
    return 0


# --------------------------------------------------------------------------
# selftest -- no chart, no helm, no cluster. evaluate() is pure, which is the
# only reason this costs under a second.
# --------------------------------------------------------------------------
def _oas(**overrides):
    """A minimal Nova-shaped OAS: an ENVELOPE, because that is the hard case."""
    base = {
        "paths": {
            "/flavors": {
                "post": {
                    "requestBody": {"content": {"application/json": {"schema": {
                        "type": "object",
                        "properties": {"flavor": {"type": "object", "properties": {
                            "name": {"type": "string"}, "ram": {"type": "integer"}}}},
                    }}}},
                    "responses": {"200": {"content": {"application/json": {"schema": {
                        "type": "object",
                        "properties": {"flavor": {"type": "object", "properties": {
                            "id": {"type": "string"}, "name": {"type": "string"}}}},
                    }}}}},
                },
                "get": {"responses": {"200": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {"flavors": {"type": "array", "items": {
                        "type": "object", "properties": {"id": {"type": "string"}}}}},
                }}}}}},
            },
            "/flavors/{id}": {"get": {"responses": {"200": {"content": {"application/json": {
                "schema": {"type": "object", "properties": {"flavor": {"type": "object",
                           "properties": {"id": {"type": "string"}, "name": {"type": "string"}}}}}}}}}}},
        }
    }
    base.update(overrides)
    return base


def _rd(verbs=None, identifiers=None, asf=None, policy=None, policy_at="findby"):
    if verbs is None:
        verbs = [
            {"action": "create", "method": "POST", "path": "/flavors"},
            {"action": "get", "method": "GET", "path": "/flavors/{id}",
             "requestFieldMapping": [{"inPath": "id", "inCustomResource": "status.flavor.id"}]},
        ]
    # The arms share module-level verb lists, so writing a policy into one would
    # leak into every later arm -- a fixture quietly mutating a fixture.
    verbs = copy.deepcopy(verbs)
    res = {
        "kind": "ComputeFlavor",
        "identifiers": identifiers if identifiers is not None else ["flavor.id", "flavor.name"],
        "additionalStatusFields": asf or [],
        "verbsDescription": verbs,
    }
    # Omitted means OR, which is the runtime's own default -- so the absence of
    # this key has to be a tested state, not just an untested default.
    #
    # policy_at says WHERE the key goes, because the location is what #148 got
    # wrong: "findby" (the default, and the only place the CRD defines it),
    # "resource" (where #148 read it, and where the apiserver prunes it), or
    # another verb's action name (where the CEL rule refuses the manifest). A
    # fixture that can only express the correct location cannot test a screen
    # that reads the wrong one -- which is exactly how this went green before.
    if policy is not None:
        if policy_at == "resource":
            res["identifiersMatchPolicy"] = policy
        else:
            for v in verbs:
                if v.get("action") == policy_at:
                    v["identifiersMatchPolicy"] = policy
                    break
            else:
                raise AssertionError(
                    "fixture asks for a policy on a %r verb it does not declare" % policy_at)
    return {"spec": {"resource": res}}


def _voids(fn):
    """True iff fn() raises Void. Used so a VOID can be asserted as a behaviour."""
    try:
        fn()
    except Void:
        return True
    except Exception:
        return False
    return False


def selftest():
    passed = failed = 0

    def case(label, rd, oas, want_checks, want_actionable, slug="flavor"):
        nonlocal passed, failed
        try:
            rows = evaluate(rd, oas, slug)
            got_checks = sorted({r[0] for r in rows})
            got_act = sorted({r[0] for r in rows if r[3]})
        except Void:
            got_checks, got_act = ["VOID"], ["VOID"]
        ok = got_checks == sorted(want_checks) and got_act == sorted(want_actionable)
        if ok:
            print("  %-58s PASS" % label); passed += 1
        else:
            print("  %-58s FAIL rows=%s actionable=%s want rows=%s actionable=%s"
                  % (label, got_checks, got_act, sorted(want_checks), sorted(want_actionable)))
            failed += 1

    print("\n\033[1m== check-restdefinition-shape selftest (no chart, no helm, no cluster)\033[0m")

    # THE NEGATIVE CONTROL FIRST. A screen that cannot report a clean document
    # makes every finding below meaningless -- and this is the arm the naive
    # port would have failed, because `flavor.id` has to resolve THROUGH the
    # envelope rather than appear among the top-level property names.
    case("a clean, envelope-shaped document yields only informational E",
         _rd(), _oas(), ["E"], [])

    # A -- the verb is inert
    case("a path the OAS does not declare -> A, actionable",
         _rd(verbs=[{"action": "create", "method": "POST", "path": "/nope"}]),
         _oas(), ["A", "B"], ["A", "B"])
    case("a declared path with an undeclared method -> A, actionable",
         _rd(verbs=[{"action": "create", "method": "PATCH", "path": "/flavors"}]),
         _oas(), ["A", "B"], ["A", "B"])

    # A's TWO MESSAGES MUST DIFFER, and nothing checked that until a mutation
    # survived. Disabling the "path not declared" branch still produced an A row
    # -- the method branch catches it, because an absent path has no methods --
    # but with the WRONG message: "the path is declared but the method is not",
    # about a path that does not exist. The check letter and the actionability
    # were identical, so the arms above could not tell. Two different repairs
    # hang on which message a reader gets.
    def a_msg(rd, oas):
        rows = [r for r in evaluate(rd, oas, "flavor") if r[0] == "A"]
        return rows[0][2] if rows else "<no A row>"

    missing_path = a_msg(_rd(verbs=[{"action": "create", "method": "POST", "path": "/nope"}]), _oas())
    missing_meth = a_msg(_rd(verbs=[{"action": "create", "method": "PATCH", "path": "/flavors"}]), _oas())
    for label, got, want in (
        ("a missing PATH says the path is not declared", missing_path, "path is not declared"),
        ("a missing METHOD says the method is not", missing_meth, "method PATCH is not"),
    ):
        if want in got:
            print("  %-58s PASS" % label); passed += 1
        else:
            print("  %-58s FAIL want=%r in got=%r" % (label, want, got)); failed += 1
    if missing_path == missing_meth:
        print("  %-58s FAIL identical messages" % "the two A messages must differ"); failed += 1
    else:
        print("  %-58s PASS" % "the two A messages must differ"); passed += 1

    # B -- an identifier that can never be populated
    case("an identifier absent everywhere -> B, actionable",
         _rd(identifiers=["flavor.nope"]), _oas(), ["B", "E"], ["B"])
    case("an identifier only in additionalStatusFields -> no B",
         _rd(identifiers=["flavor.extra"], asf=["flavor.extra"]), _oas(), ["E"], [])

    # C -- a mapping source that does not resolve
    case("a mapping from spec that the create body lacks -> C, actionable",
         _rd(verbs=[{"action": "create", "method": "POST", "path": "/flavors"},
                    {"action": "get", "method": "GET", "path": "/flavors/{id}",
                     "requestFieldMapping": [{"inPath": "id", "inCustomResource": "spec.flavor.nope"}]}]),
         _oas(), ["C", "E"], ["C"])
    case("a mapping naming neither spec nor status -> C, actionable",
         _rd(verbs=[{"action": "create", "method": "POST", "path": "/flavors"},
                    {"action": "get", "method": "GET", "path": "/flavors/{id}",
                     "requestFieldMapping": [{"inPath": "id", "inCustomResource": "metadata.name"}]}]),
         _oas(), ["C", "E"], ["C"])
    # The KNOWN table must DOWNGRADE a documented row rather than hide it.
    # MY FIRST VERSION OF THIS CASE WAS WRONG, not the code: it blanked only the
    # GET response, and `status.flavor.id` still resolved through the CREATE
    # response, so there was no unresolvable source to downgrade and the arm
    # asserted nothing. Both responses have to be blank for the row to exist.
    _no_resp = {"paths": {
        "/flavors": {"post": {"requestBody": {"content": {"application/json": {"schema": {
            "type": "object", "properties": {"flavor": {"type": "object",
                              "properties": {"name": {"type": "string"}}}}}}}},
            "responses": {"204": {}}}},
        "/flavors/{id}": {"get": {"responses": {"204": {}}}}}}
    # The downgrade mechanism is proven with an entry INJECTED here, because
    # KNOWN ships empty and a self-test that depended on a live exemption would
    # start failing the day that exemption is correctly removed.
    case("an unknown unresolvable source -> C row, ACTIONABLE",
         _rd(verbs=[{"action": "create", "method": "POST", "path": "/flavors"},
                    {"action": "get", "method": "GET", "path": "/flavors/{id}",
                     "requestFieldMapping": [{"inPath": "id", "inCustomResource": "status.flavor.id"}]}],
             identifiers=["flavor.name"]),
         _no_resp, ["C", "E"], ["C"], slug="probe-slug")
    KNOWN[("probe-slug", "C", "status.flavor.id")] = "injected by the selftest"
    try:
        case("... the SAME row with a KNOWN entry -> NOT actionable",
             _rd(verbs=[{"action": "create", "method": "POST", "path": "/flavors"},
                        {"action": "get", "method": "GET", "path": "/flavors/{id}",
                         "requestFieldMapping": [{"inPath": "id", "inCustomResource": "status.flavor.id"}]}],
                 identifiers=["flavor.name"]),
             _no_resp, ["C", "E"], [], slug="probe-slug")
    finally:
        KNOWN.pop(("probe-slug", "C", "status.flavor.id"), None)

    # THE REGRESSION ARM FOR TODAY'S DEFECT. Nova's quota set is addressed by a
    # PATH-LEVEL parameter that no request body declares. The first version of
    # this screen looked only at request bodies and produced FOUR actionable
    # rows about a resource measured working on a real cluster. If
    # declared_params stops being consulted, these two arms fail.
    quota_oas = {"paths": {"/os-quota-sets/{tenant_id}": {
        "parameters": [{"name": "tenant_id", "in": "path", "required": True}],
        "put": {"requestBody": {"content": {"application/json": {"schema": {
            "type": "object", "properties": {"quota_set": {"type": "object",
                              "properties": {"cores": {"type": "integer"}}}}}}}},
                "responses": {"200": {"content": {"application/json": {"schema": {
                    "type": "object", "properties": {"quota_set": {"type": "object",
                                      "properties": {"cores": {"type": "integer"}}}}}}}}}},
        "get": {"responses": {"200": {"content": {"application/json": {"schema": {
            "type": "object", "properties": {"quota_set": {"type": "object"}}}}}}}}}}}
    quota_rd = {"spec": {"resource": {
        "kind": "ComputeQuota", "identifiers": ["tenant_id"], "additionalStatusFields": [],
        "verbsDescription": [
            {"action": "create", "method": "PUT", "path": "/os-quota-sets/{tenant_id}",
             "requestFieldMapping": [{"inPath": "tenant_id", "inCustomResource": "spec.tenant_id"}]},
            {"action": "get", "method": "GET", "path": "/os-quota-sets/{tenant_id}",
             "requestFieldMapping": [{"inPath": "tenant_id", "inCustomResource": "spec.tenant_id"}]}]}}}
    case("a PATH-LEVEL param identifier -> no B, no C (regression)",
         quota_rd, quota_oas, ["E"], [], slug="quota")
    case("... and its E row says the field is KEPT, not omitted",
         quota_rd, quota_oas, ["E"], [], slug="quota")

    # AN UNMAPPED PATH PARAM THAT IS ALSO AN IDENTIFIER. Nothing supplies it, so
    # the author must, so it IS a spec field and check B must not report it as
    # unpopulatable. This arm exists because a mutation marking unmapped params
    # as non-surviving survived every other arm: the entry is consumed only
    # through the spec set, and no other fixture has an identifier that is an
    # unmapped path param.
    unmapped_oas = {"paths": {"/widgets/{wid}": {
        "parameters": [{"name": "wid", "in": "path", "required": True}],
        "post": {"requestBody": {"content": {"application/json": {"schema": {
            "type": "object", "properties": {"widget": {"type": "object"}}}}}},
                 "responses": {"200": {"content": {"application/json": {"schema": {
                     "type": "object", "properties": {"widget": {"type": "object"}}}}}}}},
        "get": {"responses": {"200": {"content": {"application/json": {"schema": {
            "type": "object", "properties": {"widget": {"type": "object"}}}}}}}}}}}
    unmapped_rd = {"spec": {"resource": {
        "kind": "Widget", "identifiers": ["wid"], "additionalStatusFields": [],
        "verbsDescription": [
            {"action": "create", "method": "POST", "path": "/widgets/{wid}"},
            {"action": "get", "method": "GET", "path": "/widgets/{wid}"}]}}}
    # NO ROWS AT ALL is correct here, and my first expectation was wrong rather
    # than the code: an unmapped param the OAS DECLARES is an ordinary
    # author-set field -- nothing is omitted and nothing is unusual. The arm
    # still kills the mutation, because marking unmapped params as
    # non-surviving makes B fire (identifier unpopulatable) AND the E unmapped
    # branch fire, so [] becomes ["B", "E"].
    case("an UNMAPPED but DECLARED path param -> no rows at all",
         unmapped_rd, unmapped_oas, [], [], slug="widget")

    # AND THE UNMAPPED E BRANCH IS NOT DEAD CODE. Once a declared path param
    # counts as a spec field, that branch is reachable only for a placeholder
    # the OAS does not declare at all -- `{foo}` in a path with no matching
    # parameter. This arm proves it still fires, because a branch no fixture can
    # reach is a branch nobody will notice breaking.
    undeclared_oas = {"paths": {"/gadgets/{gid}": {
        "post": {"requestBody": {"content": {"application/json": {"schema": {
            "type": "object", "properties": {"gadget": {"type": "object"}}}}}},
                 "responses": {"200": {"content": {"application/json": {"schema": {
                     "type": "object", "properties": {"gadget": {"type": "object"}}}}}}}}}}}
    undeclared_rd = {"spec": {"resource": {
        "kind": "Gadget", "identifiers": [], "additionalStatusFields": [],
        "verbsDescription": [
            {"action": "create", "method": "POST", "path": "/gadgets/{gid}"}]}}}
    case("an UNDECLARED path placeholder -> E, informational",
         undeclared_rd, undeclared_oas, ["E"], [], slug="gadget")

    # E's TWO MESSAGES MUST DIFFER TOO, for the same reason A's must: removing
    # the branch on the mapping root left the row letter and the actionability
    # unchanged, so every arm above still passed while the screen told a reader
    # that an author-set field had been omitted from spec. That is the opposite
    # of the truth and it was the defect of this screen's first real run.
    def e_msg(rd, oas, slug="flavor"):
        rows = [r for r in evaluate(rd, oas, slug) if r[0] == "E"]
        return rows[0][3 - 1] if rows else "<no E row>"

    # THREE messages, because there are three cases. The two-way version of
    # this arm is what let the screen tell a reader that `spec.name` survived a
    # mapping from `spec.keypair.name`, when what survives is the SOURCE field
    # and `spec.name` is omitted exactly as in the status case.
    status_msg = e_msg(_rd(), _oas())
    selfref_msg = e_msg(quota_rd, quota_oas, "quota")
    keypair_oas = {"paths": {
        "/os-keypairs": {"post": {"requestBody": {"content": {"application/json": {"schema": {
            "type": "object", "properties": {"keypair": {"type": "object",
                              "properties": {"name": {"type": "string"}}}}}}}},
            "responses": {"200": {"content": {"application/json": {"schema": {
                "type": "object", "properties": {"keypair": {"type": "object",
                                  "properties": {"name": {"type": "string"}}}}}}}}}}},
        "/os-keypairs/{name}": {
            "parameters": [{"name": "name", "in": "path", "required": True}],
            "get": {"responses": {"200": {"content": {"application/json": {"schema": {
                "type": "object", "properties": {"keypair": {"type": "object"}}}}}}}}}}}
    keypair_rd = {"spec": {"resource": {
        "kind": "ComputeKeypair", "identifiers": ["keypair.name"], "additionalStatusFields": [],
        "verbsDescription": [
            {"action": "create", "method": "POST", "path": "/os-keypairs"},
            {"action": "get", "method": "GET", "path": "/os-keypairs/{name}",
             "requestFieldMapping": [{"inPath": "name", "inCustomResource": "spec.keypair.name"}]}]}}}
    other_msg = e_msg(keypair_rd, keypair_oas, "keypair")
    # Asserted on the MARKERS, not the prose, because the markers are what
    # param_dispositions and the sample gate read. An arm matching prose passes
    # while a reworded message silently flips every downstream check.
    body_oas = {"paths": {"/things": {
        "post": {"requestBody": {"content": {"application/json": {"schema": {
            "type": "object", "properties": {"id": {"type": "string"},
                              "name": {"type": "string"}}}}}},
                 "responses": {"200": {"content": {"application/json": {"schema": {
                     "type": "object", "properties": {"id": {"type": "string"}}}}}}}},
    }, "/things/{id}": {"get": {"responses": {"200": {"content": {"application/json": {
        "schema": {"type": "object", "properties": {"id": {"type": "string"}}}}}}}}}}}
    body_rd = {"spec": {"resource": {
        "kind": "Thing", "identifiers": ["id"], "additionalStatusFields": [],
        "verbsDescription": [
            {"action": "create", "method": "POST", "path": "/things"},
            {"action": "get", "method": "GET", "path": "/things/{id}",
             "requestFieldMapping": [{"inPath": "id", "inCustomResource": "spec.name"}]}]}}}
    body_msg = e_msg(body_rd, body_oas, "thing")
    # ironic's row: a BODY-DECLARED param survives whatever the mapping says, and
    # it is a factor this chart cannot see (all six bodies are a single envelope
    # key), so it is tested only here.
    old_ver_msg = e_msg(quota_rd, quota_oas, "quota")
    for label, got, want in (
        ("status-mapped carries the OMITTED marker", status_msg, MARK_OMITTED),
        ("self-referencing at 0.52.0 carries SURVIVES", selfref_msg, MARK_SURVIVES),
        ("spec-but-elsewhere carries OMITTED", other_msg, MARK_OMITTED),
        ("a BODY-DECLARED param carries SURVIVES", body_msg, MARK_SURVIVES),
    ):
        if want in got:
            print("  %-58s PASS" % label); passed += 1
        else:
            print("  %-58s FAIL want=%r in got=%r" % (label, want, got)); failed += 1
    if len({status_msg, selfref_msg, other_msg, body_msg}) == 4:
        print("  %-58s PASS" % "all four E messages differ"); passed += 1
    else:
        print("  %-58s FAIL only %d distinct" % ("all four E messages differ",
              len({status_msg, selfref_msg, other_msg, body_msg}))); failed += 1

    # THE VERSION ARM, and it is the one that reconciles two measurements that
    # looked contradictory. Below 0.52.0 a self-referencing mapping DELETES the
    # property it reads from, so the same document classifies differently -- and
    # it becomes ACTIONABLE, because the verb then reads a field that is gone.
    rows_050 = [r for r in evaluate(quota_rd, quota_oas, "quota", "0.50.0") if r[0] == "E"]
    msg_050 = rows_050[0][2] if rows_050 else "<none>"
    act_050 = bool(rows_050 and rows_050[0][3])
    for label, cond in (
        ("the SAME document at 0.50.0 carries OMITTED", MARK_OMITTED in msg_050),
        ("... and is ACTIONABLE at 0.50.0, not merely informational", act_050),
        ("... and is NOT actionable at 0.52.0",
         not any(r[3] for r in evaluate(quota_rd, quota_oas, "quota", "0.52.0") if r[0] == "E")),
        ("an unparseable version VOIDs rather than assuming one",
         _voids(lambda: evaluate(quota_rd, quota_oas, "quota", "not-a-version"))),
    ):
        if cond:
            print("  %-58s PASS" % label); passed += 1
        else:
            print("  %-58s FAIL" % label); failed += 1

    # D and F -- the action-only axis. SIX arms, because `actionable` now turns on
    # three independent conditions (is the kind action-only, what is the match
    # policy, does a transform supply the identifier), and one arm per check would
    # let any of them regress in silence.
    #
    # WHY THIS BLOCK IS SIX ARMS AND NOT TWO. The two arms this file used to ship
    # were `create` + `findby` and nothing else -- an ACTION-ONLY kind -- and they
    # asserted that D is actionable on it, and that adding a responseTransform is
    # the fix. Both assertions were backwards: on such a kind the create is
    # reachable only while findby does NOT match, so supplying the identifier
    # disables it. Following exactly that advice turned every bare-metal
    # transition in openstack-ironic-operator-kog into a silent no-op for a day,
    # every CR reading Ready (their #65). The fixture was minimal and the
    # assertions were green, which is what made a wrong screen look tested -- so
    # the arms below pin the *reason* for each verdict, not just its letter.
    aonly = [{"action": "create", "method": "POST", "path": "/flavors"},
             {"action": "findby", "method": "GET", "path": "/flavors"}]
    xf = {"responseTransform": "{name: .flavors[0].name}"}
    managed = [aonly[0],
               {"action": "get", "method": "GET", "path": "/flavors/{id}",
                "requestFieldMapping": [{"inPath": "id", "inCustomResource": "status.flavor.id"}]},
               aonly[1]]

    case("action-only kind, identifier unreadable -> D, NOT actionable",
         _rd(verbs=aonly, identifiers=["flavor.name"]), _oas(), ["D"], [])
    case("action-only + transform makes findby match -> F, actionable",
         _rd(verbs=[aonly[0], dict(aonly[1], **xf)], identifiers=["flavor.name"]),
         _oas(), ["D", "F"], ["F"])
    case("action-only + identifier readable in the item -> F, actionable",
         _rd(verbs=aonly, identifiers=["flavor.id"]), _oas(), ["F"], ["F"])
    # THE ARM THAT KEEPS F FROM BECOMING "no responseTransform allowed". Same
    # findby, same transform; the kind just also declares a get, so its create is
    # reachable and there is nothing to report. Without this arm F would pass its
    # other tests while banning a legitimate construct.
    case("SAME transform, but the kind has a get -> no F at all",
         _rd(verbs=[managed[0], managed[1], dict(aonly[1], **xf)],
             identifiers=["flavor.name"]), _oas(), ["D", "E"], [])
    case("managed kind, OR policy, another identifier readable -> D informational",
         _rd(verbs=managed, identifiers=["flavor.id", "flavor.name"]), _oas(), ["D", "E"], [])
    case("managed kind, AND on the FINDBY VERB -> D ACTIONABLE",
         _rd(verbs=managed, identifiers=["flavor.id", "flavor.name"], policy="AND"),
         _oas(), ["D", "E"], ["D"])

    # ---- G: is the policy DECLARATION even in force? ------------------------
    # THE ARM THAT WOULD HAVE CAUGHT #148, and the reason the arm above was
    # worthless until now. Same AND, placed where #148 read it. The screen must
    # NOT treat it as a policy -- so D stays informational, exactly as with no
    # policy at all -- and must report the key as pruned.
    case("AND at the RESOURCE level: not a policy, and reported -> D + G",
         _rd(verbs=managed, identifiers=["flavor.id", "flavor.name"],
             policy="AND", policy_at="resource"),
         _oas(), ["D", "E", "G"], ["G"])
    case("lowercase 'and', which the enum refuses -> G, D and F suppressed",
         _rd(verbs=aonly, identifiers=["flavor.id"], policy="and"),
         _oas(), ["G"], ["G"])
    case("policy on a NON-findby verb, which CEL refuses -> G",
         _rd(verbs=managed, identifiers=["flavor.id"], policy="AND", policy_at="create"),
         _oas(), ["E", "G"], ["G"])
    # And the control for the suppression above: the SAME kind with a valid
    # policy still produces its F. Without this, "G suppresses D and F" could be
    # "F stopped working".
    case("... the same kind with a VALID policy still reports F",
         _rd(verbs=aonly, identifiers=["flavor.id"], policy="OR"),
         _oas(), ["F"], ["F"])

    # F's TWO MESSAGES must differ, not just its letter and severity. One rests
    # on a field the collection item really declares; the other rests only on
    # transform_supplies, a leaf-match heuristic written for check D. Without this
    # pair, F silently inherits every false positive of that heuristic and says
    # nothing about it -- the coupling ironic flagged between their F and our
    # #137. Same rule as MARK_SURVIVES/MARK_OMITTED: where two cases share a row
    # type, assert their messages differ.
    _f_xf = [r for r in evaluate(
        _rd(verbs=[aonly[0], dict(aonly[1], **xf)], identifiers=["flavor.name"]),
        _oas(), "flavor") if r[0] == "F"]
    _f_real = [r for r in evaluate(
        _rd(verbs=aonly, identifiers=["flavor.id"]), _oas(), "flavor") if r[0] == "F"]
    _m_xf = _f_xf[0][2] if _f_xf else "<none>"
    _m_real = _f_real[0][2] if _f_real else "<none>"
    for label, cond in (
        ("F on a transform-only match says it is a heuristic",
         "heuristic" in _m_xf),
        ("F on a genuinely readable identifier does NOT",
         "heuristic" not in _m_real),
        ("both F messages still carry the stable marker",
         MARK_UNREACHABLE_CREATE in _m_xf and MARK_UNREACHABLE_CREATE in _m_real),
        ("... and the two F messages are not the same string",
         _m_xf != _m_real and _m_xf != "<none>" and _m_real != "<none>"),
    ):
        if cond:
            print("  %-58s PASS" % label); passed += 1
        else:
            print("  %-58s FAIL" % label); failed += 1

    # G's three messages must differ by CAUSE, not just share a letter: pruned
    # (silently dropped, policy never existed), enum-refused and CEL-refused
    # (manifest will not apply). A reader who cannot tell them apart cannot tell
    # "your policy is not doing anything" from "your manifest will be rejected".
    _g = {}
    for key, rd in (
        ("pruned", _rd(verbs=managed, identifiers=["flavor.id"],
                       policy="AND", policy_at="resource")),
        ("enum", _rd(verbs=aonly, identifiers=["flavor.id"], policy="and")),
        ("cel", _rd(verbs=managed, identifiers=["flavor.id"],
                    policy="AND", policy_at="create")),
    ):
        rows = [r for r in evaluate(rd, _oas(), "flavor") if r[0] == "G"]
        _g[key] = rows[0][2] if rows else "<none>"
    for label, cond in (
        ("G on a resource-level key says PRUNES", "PRUNES" in _g["pruned"]),
        ("G on a bad enum value says REFUSES at admission", "REFUSES" in _g["enum"]),
        ("... and says D and F were not evaluated", "NOT evaluated" in _g["enum"]),
        ("G on a non-findby verb quotes the CEL rule",
         "self.action == 'findby'" in _g["cel"]),
        ("all three G messages carry the stable marker",
         all(MARK_POLICY_NOT_IN_FORCE in m for m in _g.values())),
        ("... and no two G messages are the same string",
         len(set(_g.values())) == 3 and "<none>" not in set(_g.values())),
    ):
        if cond:
            print("  %-58s PASS" % label); passed += 1
        else:
            print("  %-58s FAIL" % label); failed += 1

    # E -- both path-param shapes, informational
    case("an UNMAPPED path param -> E, informational",
         _rd(verbs=[{"action": "create", "method": "POST", "path": "/flavors"},
                    {"action": "get", "method": "GET", "path": "/flavors/{id}"}]),
         _oas(), ["E"], [])

    # The two VOIDs, each proven to fire: a screen that cannot VOID reports a
    # pass when it has read nothing.
    case("VOID: no verbsDescription at all", _rd(verbs=[]), _oas(), ["VOID"], ["VOID"])
    case("VOID: an OAS with no paths", _rd(), {"paths": {}}, ["VOID"], ["VOID"])

    # ---- the SAMPLE prune gate, both directions ----------------------------
    print("\n  sample prune gate:")
    def srow(label, kind, keys, disp, want_subjects):
        nonlocal passed, failed
        rows = screen_sample(kind, set(keys), disp)
        got = sorted(r[1] for r in rows if r[3])
        if got == sorted(want_subjects):
            print("    %-56s PASS" % label); passed += 1
        else:
            print("    %-56s FAIL got=%s want=%s" % (label, got, sorted(want_subjects)))
            failed += 1

    OMIT = {"id": "omitted"}
    KEEP = {"tenant_id": "kept"}
    # An OMITTED field present is the hazard the doc described: pruned silently.
    srow("an omitted param SET by a sample -> actionable",
         "ComputeFlavor", ["configurationRef", "flavor", "id"], OMIT, ["ComputeFlavor spec.id"])
    srow("an omitted param absent -> no row",
         "ComputeFlavor", ["configurationRef", "flavor"], OMIT, [])
    # A KEPT field missing is the OTHER hazard, and a one-directional gate would
    # have called this clean while the CR could not resolve its path.
    srow("a KEPT param NOT set by a sample -> actionable",
         "ComputeQuota", ["configurationRef", "quota_set"], KEEP, ["ComputeQuota spec.tenant_id"])
    srow("a KEPT param set -> no row",
         "ComputeQuota", ["configurationRef", "quota_set", "tenant_id"], KEEP, [])
    srow("a document with no path params -> no rows ever",
         "ComputeFlavor", ["configurationRef", "flavor", "id"], {}, [])
    # The two messages must DIFFER: same row letter, opposite repair. This is the
    # arm that the A and E mutants taught me to write BEFORE the mutation.
    m_omit = screen_sample("K", {"id"}, OMIT)[0][2]
    m_keep = screen_sample("K", set(), KEEP)[0][2]
    for label, got, want in (
        ("an omitted-and-set row says it is PRUNED", m_omit, "PRUNES it silently"),
        ("a kept-and-missing row says the path never resolves", m_keep, "never resolves"),
    ):
        if want in got:
            print("    %-56s PASS" % label); passed += 1
        else:
            print("    %-56s FAIL want=%r in got=%r" % (label, want, got)); failed += 1
    if m_omit != m_keep:
        print("    %-56s PASS" % "the two S messages differ"); passed += 1
    else:
        print("    %-56s FAIL identical" % "the two S messages differ"); failed += 1

    # param_dispositions must read the E rows, not guess: a document whose E row
    # says "survives" is KEPT and every other E row is OMITTED.
    d_status = param_dispositions(_rd(), _oas(), "flavor")
    d_self = param_dispositions(quota_rd, quota_oas, "quota")
    for label, got, want in (
        ("a status-mapped param reads as omitted", d_status.get("id"), "omitted"),
        ("a self-referencing param reads as kept", d_self.get("tenant_id"), "kept"),
    ):
        if got == want:
            print("    %-56s PASS" % label); passed += 1
        else:
            print("    %-56s FAIL got=%r want=%r" % (label, got, want)); failed += 1

    # ---- E MUST NOT CLAIM A PROPERTY IS GONE WHILE C SAYS IT RESOLVES ------
    # The invariant that broke: survival was computed inside the E rows only, so
    # below 0.52.0 the screen said the property was deleted (E, actionable) AND
    # that the mapping reading it resolved fine (C, silent). Two checks, one
    # fact, opposite answers, in the same run. Asserted as a property across
    # both versions rather than as two separate expectations.
    # THE IMPLICATION IS ONE-DIRECTIONAL, and the symmetric version is a bug.
    # openstack-ironic-operator-kog wrote `C-rows subset-of E-suppressed` first,
    # measured agreement across 7 kinds x 3 versions, and it LOOKED like
    # confirmation -- but a C row also fires when an author names a field that
    # simply does not exist, a typo, which E has no reason to report. The
    # symmetric invariant would flag a REAL FINDING as an internal inconsistency:
    # it turns a defect into noise about the screen itself.
    #
    # My first version of the per-version check below asserted
    # `e_omitted == c_fired`, which is that same symmetric claim. It held on this
    # fixture only because the fixture has no typo; it would have broken the day
    # one was added, and it encoded the wrong rule for anyone who reused it.
    # These are now FIXTURE EXPECTATIONS (what this document does at this
    # version) and the invariant is stated once, below, in one direction only.
    print("\n  the quota fixture, per version (expectations, not an invariant):")
    for ver, want_e, want_c in (("0.52.0", False, False), ("0.50.0", True, True)):
        rows = evaluate(quota_rd, quota_oas, "quota", ver)
        e_omitted = any(r[0] == "E" and MARK_OMITTED in r[2] for r in rows)
        c_fired = any(r[0] == "C" for r in rows)
        label = "at %s: E omitted=%s, C fires=%s" % (ver, e_omitted, c_fired)
        if e_omitted == want_e and c_fired == want_c:
            print("    %-56s PASS" % label); passed += 1
        else:
            print("    %-56s FAIL want E=%s C=%s" % (label, want_e, want_c)); failed += 1

    # AND THE ARM THAT PROVES THE UNSOUND DIRECTION IS NOT IMPLEMENTED: a C row
    # with no suppressed-E row is LEGITIMATE -- an author naming a field that
    # does not exist. If the invariant were stated the other way round this
    # would be reported as the screen contradicting itself.
    typo_rd = _rd(verbs=[{"action": "create", "method": "POST", "path": "/flavors"},
                         {"action": "get", "method": "GET", "path": "/flavors/{id}",
                          "requestFieldMapping": [
                              {"inPath": "id", "inCustomResource": "spec.flavor.nope"}]}],
                  identifiers=["flavor.name"])
    typo_rows = evaluate(typo_rd, _oas(), "flavor")
    typo_c = any(r[0] == "C" for r in typo_rows)
    typo_selfref_e = any(r[0] == "E" and MARK_OMITTED in r[2] and "SELF-REFERENCING" in r[2]
                         for r in typo_rows)
    if typo_c and not typo_selfref_e:
        print("    %-56s PASS" % "a C row with no suppressed-E row is allowed (a typo)")
        passed += 1
    else:
        print("    %-56s FAIL C=%s selfref-E=%s"
              % ("a C row with no suppressed-E row is allowed", typo_c, typo_selfref_e))
        failed += 1
    # THE INVARIANT, stated once and in one direction: a SELF-REFERENCING param
    # that E reports suppressed MUST produce a C row. Never the converse.
    # THE CORPUS MUST CONTAIN THE INPUT THAT DISTINGUISHES THIS INVARIANT FROM
    # ITS OWN NEGATION, and mine did not. openstack-ironic-openstack-kog found it
    # in their screen first and I had the identical hole: the typo document was a
    # SEPARATE arm calling evaluate() directly, so it never passed through this
    # loop. Adding the symmetric clause -- also requiring that no C row exists
    # without a suppressed self-referencing E row -- then SURVIVED: every other
    # document names fields that exist, so over this corpus the sound and
    # symmetric forms are identical and the arm passed either way.
    #
    # Testing the RULE and testing the ARM THAT POLICES THE RULE are different
    # tests, and I had assumed one covered the other.
    #
    # Their question is cheaper than the mutation test and would have caught both
    # of ours before either was written: WHICH INPUT DISTINGUISHES THIS FROM ITS
    # NEGATION, AND IS IT IN THE CORPUS?
    bad = []
    discriminating = 0
    for nm, rd_, oas_, slug_ in (("quota", quota_rd, quota_oas, "quota"),
                                 ("keypair", keypair_rd, keypair_oas, "keypair"),
                                 ("body-declared", body_rd, body_oas, "thing"),
                                 ("status-mapped", _rd(), _oas(), "flavor"),
                                 # The discriminating input: a mapping naming a
                                 # field that does not exist. C fires, no
                                 # suppressed-E row -- legitimate, and the shape
                                 # the symmetric form would wrongly flag.
                                 ("typo-source", typo_rd, _oas(), "flavor")):
        for ver in ("0.50.0", "0.52.0", "0.60.0"):
            rows = evaluate(rd_, oas_, slug_, ver)
            # Only the SELF-REFERENCING shape can disagree, because only it has a
            # source whose resolution depends on survival. spec-elsewhere sources
            # resolve on their own field, which is a different question.
            for r in rows:
                if r[0] != "E" or MARK_OMITTED not in r[2]:
                    continue
                if "SELF-REFERENCING" not in r[2]:
                    continue
                if not any(x[0] == "C" for x in rows):
                    bad.append("%s@%s" % (nm, ver))
            # Count the shapes that would separate the sound invariant from the
            # symmetric one: a C row with NO suppressed self-referencing E row.
            if any(x[0] == "C" for x in rows) and not any(
                    r[0] == "E" and MARK_OMITTED in r[2] and "SELF-REFERENCING" in r[2]
                    for r in rows):
                discriminating += 1
    if not bad:
        print("    %-56s PASS" % "no shape has E-omitted-but-C-silent at any version")
        passed += 1
    else:
        print("    %-56s FAIL %s" % ("no shape has E-omitted-but-C-silent", bad)); failed += 1
    # AND THE DISCRIMINATING INPUT MUST STILL BE HERE. Without this the corpus
    # can lose it silently -- someone deletes the typo fixture, every case still
    # passes, and the arm goes back to passing whether the invariant is right or
    # wrong. A green arm over a corpus that cannot fail it is not evidence.
    if discriminating >= 1:
        print("    %-56s PASS" % ("the corpus contains %d C-without-E case(s), so the "
                                  "arm can tell the invariant from its negation"
                                  % discriminating)); passed += 1
    else:
        print("    %-56s FAIL" % "the corpus has NO C-without-E case -- arm is vacuous")
        print("      Over this corpus the sound and symmetric invariants are identical,")
        print("      so the arm above passes whether the rule is right or wrong.")
        failed += 1

    # ---- THE VERSION'S PROVENANCE ------------------------------------------
    # A mutation that makes run() ignore the workflow and fall back to the
    # constant is UNDETECTABLE while the two agree -- which is exactly the state
    # worth worrying about. openstack-ironic-operator-kog found their screen
    # reading a pin that said 0.54.1 while their lab ran 0.59.0, and a pin that
    # disagrees with reality gives a WRONG verdict rather than no verdict. So
    # rather than pretend that mutant is killed, the AGREEMENT is asserted: if
    # CI's pin moves and this file's fallback does not, this arm says so.
    print("\n  the version's provenance:")
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ci_ver, ci_where = ci_oasgen_version(_root)
    if ci_ver:
        print("    %-56s PASS" % ("CI's OASGEN_VERSION is readable (%s)" % ci_ver)); passed += 1
    else:
        print("    %-56s FAIL could not read it" % "CI's OASGEN_VERSION is readable"); failed += 1
    if ci_ver == OASGEN_ASSUMED:
        print("    %-56s PASS" % "the fallback constant AGREES with CI's pin"); passed += 1
    else:
        print("    %-56s FAIL CI=%s constant=%s -- update OASGEN_ASSUMED"
              % ("the fallback constant agrees with CI's pin", ci_ver, OASGEN_ASSUMED)); failed += 1
    if ci_where and "validate.yaml" in ci_where:
        print("    %-56s PASS" % "and it names where it read it from"); passed += 1
    else:
        print("    %-56s FAIL source not named" % "and it names where it read it from"); failed += 1
    # A tree with no workflow must report absence, not a guess.
    import tempfile as _tf
    _empty = _tf.mkdtemp()
    if ci_oasgen_version(_empty) == (None, None):
        print("    %-56s PASS" % "no workflow -> reports absence, not a guess"); passed += 1
    else:
        print("    %-56s FAIL invented a version" % "no workflow -> reports absence"); failed += 1

    # The resolver itself, because every check above rests on it.
    print("\n  dotted resolution through an envelope:")
    oas = _oas()
    body = request_schema(oas, "/flavors", "POST")
    resp = response_schema(oas, "/flavors", "POST")
    for label, schema, dotted, want in (
        ("flavor.name in the create body", body, "flavor.name", True),
        ("flavor.id NOT in the create body", body, "flavor.id", False),
        ("flavor.id in the create response", resp, "flavor.id", True),
        ("a bare leaf does NOT resolve at top level", body, "name", False),
        ("a missing leaf under a real wrapper", body, "flavor.nope", False),
        ("an empty path never resolves", body, "", False),
    ):
        got = resolves(schema, oas, dotted)
        if got == want:
            print("    %-52s PASS" % label); passed += 1
        else:
            print("    %-52s FAIL want=%s got=%s" % (label, want, got)); failed += 1

    print("\n  %d passed, %d failed" % (passed, failed))
    return 0 if failed == 0 else 1


def main():
    # argparse, and parse_args() rather than parse_known_args(). The hand-rolled
    # argv scan accepted anything: `--self-test` -- the hyphenated spelling is the
    # natural guess -- matched no branch, fell through to the REAL screen, printed
    # a green result and exited 0. So a flag typo silently ran a different,
    # passing program, and I read it as six new self-test arms passing. Same
    # family as a mock that cannot disagree: the reassuring output and the
    # broken-instrument output were the same string. An unrecognised flag now
    # exits 2 before anything runs. parse_known_args() would reintroduce exactly
    # the fall-through.
    ap = argparse.ArgumentParser(
        prog="check-restdefinition-shape.py",
        description="Screen this chart's RestDefinitions against their OAS documents.")
    ap.add_argument("--selftest", action="store_true",
                    help="drive every row type; no chart, no helm, no cluster")
    ap.add_argument("--oasgen-version", default=OASGEN_ASSUMED,
                    help="the oasgen-provider version to screen for (default: %s)"
                         % OASGEN_ASSUMED)
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    # The version is a PARAMETER with a stated default, not a silent assumption:
    # what survives in the spec differs across 0.52.0, so an answer that does not
    # say which version it is for is not an answer.
    ver = args.oasgen_version
    if parse_version(ver) is None:
        print("VOID: --oasgen-version %r is not a version, and what survives in the spec "
              "depends on it. Refusing rather than assuming." % ver)
        return 9
    try:
        return run(ver)
    except Void as v:
        print("VOID: %s" % v)
        print("NOT a pass.")
        return 9


if __name__ == "__main__":
    sys.exit(main())
