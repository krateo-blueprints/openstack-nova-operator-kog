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

# Stable markers, so a disposition is never read out of prose. The first version
# keyed on the word "survives" appearing in a message -- which works until
# someone rewords the message, and then every sample check silently flips.
MARK_SURVIVES = "SPEC-PROPERTY SURVIVES"
MARK_OMITTED = "SPEC-PROPERTY OMITTED"


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

    # Every parameter the author can set: body properties, plus the path and
    # query parameters any verb declares. A path param mapped FROM spec is
    # author-supplied, so it is a spec field even though no body mentions it.
    author_params = set()
    for v in verbs:
        author_params |= declared_params(oas, v.get("path"), v.get("method"))

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

    # D -- a findby that can never match is worse than no findby: it looks like
    # a lookup and silently returns nothing. Two cases on purpose, because
    # "absent" and "absent but a transform supplies it" are a defect and its fix.
    if findby:
        wrapper, item = collection_item_schema(oas, findby["path"], findby.get("method") or "get")
        for ident in res.get("identifiers") or []:
            leaf = ident.split(".")[-1]
            if resolves(item, oas, ident) or resolves(item, oas, leaf):
                continue
            if transform_supplies(ident):
                out.append(("D", ident,
                            "absent from the collection item (%s); the responseTransform "
                            "mentions it (matched on the leaf -- a heuristic, so confirm "
                            "it really manufactures it)" % wrapper, False))
            else:
                out.append(("D", ident,
                            "absent from the collection item (%s) and no responseTransform "
                            "supplies it -- findby can NEVER match" % wrapper, True))

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
                src = mapped[pp]
                root = src.partition(".")[0]
                ver = parse_version(oasgen_version)
                if ver is None:
                    raise Void("cannot parse the oasgen version %r, and what survives in the "
                               "spec depends on it (#421 landed in 0.52.0). Refusing to "
                               "classify rather than assume a version" % (oasgen_version,))
                body_declared = pp in props(create_body, oas)
                self_ref = (src == "spec." + pp)
                if body_declared:
                    out.append(("E", pp,
                                "%s: the create body declares %s at top level, so the property "
                                "exists regardless of the mapping" % (MARK_SURVIVES, pp), False))
                    continue
                if self_ref and ver >= (0, 52, 0):
                    out.append(("E", pp,
                                "%s: SELF-REFERENCING mapping from spec.%s, which oasgen#421 "
                                "(0.52.0, assuming %s) stopped deleting -- the author sets it"
                                % (MARK_SURVIVES, pp, oasgen_version), False))
                    continue
                if self_ref:
                    out.append(("E", pp,
                                "%s: SELF-REFERENCING mapping from spec.%s, and oasgen %s is "
                                "BELOW 0.52.0, where the mapping still deletes the property it "
                                "reads from -- so the verb reads a field that does not exist"
                                % (MARK_OMITTED, pp, oasgen_version), True))
                    continue
                if root == "spec":
                    out.append(("E", pp,
                                "%s: supplied from %s, so the top-level spec.%s is omitted -- "
                                "the author sets %s instead, which is a DIFFERENT field"
                                % (MARK_OMITTED, src, pp, src), False))
                    continue
                if root == "status":
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

    print("  classifying for oasgen %s -- what survives in the spec depends on it, "
          "because #421 landed in 0.52.0" % oasgen_version)
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


def _rd(verbs=None, identifiers=None, asf=None):
    if verbs is None:
        verbs = [
            {"action": "create", "method": "POST", "path": "/flavors"},
            {"action": "get", "method": "GET", "path": "/flavors/{id}",
             "requestFieldMapping": [{"inPath": "id", "inCustomResource": "status.flavor.id"}]},
        ]
    return {"spec": {"resource": {
        "kind": "ComputeFlavor",
        "identifiers": identifiers if identifiers is not None else ["flavor.id", "flavor.name"],
        "additionalStatusFields": asf or [],
        "verbsDescription": verbs,
    }}}


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

    # D -- the pair, deliberately two cases: a defect and its fix must not be
    # conflated, which a single case would have allowed.
    findby = [{"action": "create", "method": "POST", "path": "/flavors"},
              {"action": "findby", "method": "GET", "path": "/flavors"}]
    case("findby whose collection item lacks the identifier -> D, actionable",
         _rd(verbs=findby, identifiers=["flavor.name"]), _oas(), ["D"], ["D"])
    case("... same, but a responseTransform supplies it -> D, NOT actionable",
         _rd(verbs=[findby[0], dict(findby[1], responseTransform="{name: .flavors[0].name}")],
             identifiers=["flavor.name"]), _oas(), ["D"], [])

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
    if "--selftest" in sys.argv:
        return selftest()
    # The version is a PARAMETER with a stated default, not a silent assumption:
    # what survives in the spec differs across 0.52.0, so an answer that does not
    # say which version it is for is not an answer.
    ver = OASGEN_ASSUMED
    for i, a in enumerate(sys.argv):
        if a == "--oasgen-version" and i + 1 < len(sys.argv):
            ver = sys.argv[i + 1]
        elif a.startswith("--oasgen-version="):
            ver = a.split("=", 1)[1]
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
