#!/usr/bin/env python3
"""Repo guards that need no cluster, so they can run on every PR.

Each check here exists because the defect it screens for was SHIPPING in this
repository, not because it seemed plausible. The three were found on 2026-10-29
and reported across the Krateo blueprint repos; `aruba-provider-kog` hit all
three independently and contributed the original implementations.

Every check asserts it can detect a known-bad input AND that it does not
false-positive on a known-good one, before it trusts its own sweep. A screen
that reports "all clear" without proving it can catch a known-bad case is just
a function that returns false. That is not hypothetical here: a sweep for the
removed auth-bridge matched `auth-bridge` and `authBridge` but not "auth
bridge" with a space, reported clean, and had missed ten prose mentions
including the docs landing page. An INCOMPLETE pattern fails exactly like a
dead one.
"""
import glob
import json
import os
import re
import sys
import warnings

import yaml

# Docs that are dated evidence or a changelog: they record what was true at a
# past version ON PURPOSE, and rewriting them would destroy the record. The
# published articles additionally carry a superseded banner at the top, so a
# reader is told at the point of reading rather than having to notice a date.
HISTORICAL_DOCS = (
    "docs/medium-article.md",
    "docs/medium-openstack-compute-operator.md",
    "docs/log.md",
    "docs/release.md",
)


def _docs():
    for f in sorted(glob.glob("**/*.md", recursive=True)):
        if f.startswith(".git" + os.sep) or os.sep + "charts" + os.sep in f:
            continue
        yield f


def verify_composition_apiversion(errors):
    """No file may hardcode a `composition.krateo.io/<version>` apiVersion.

    core-provider derives the Composition CRD's served version from the chart
    version as `"v" + strings.ReplaceAll(chartVersion, ".", "-")` -- including
    the PATCH, so chart 0.1.3 is served at v0-1-3 and any literal is wrong the
    moment the chart is bumped. In THIS repo it is unconditionally wrong:
    chart/Chart.yaml carries `version: CHART_VERSION`, substituted at release,
    so at no commit does a correct literal exist to write.

    Writing one costs more than a stale doc. `no matches for kind` is what the
    API server says both when the apiVersion is wrong and when the CRD was
    never created, so the reader debugs the wrong layer entirely.

    The addressable form is to read what core-provider actually created:

        kubectl get compositiondefinition <name> -n <ns> \\
            -o jsonpath='{.status.apiVersion}'
        kubectl get compositiondefinition <name> -n <ns> \\
            -o jsonpath='{.status.resource}'    # the plural
    """
    pat = re.compile(r"composition\.krateo\.io/v[0-9A-Za-z]")
    assert pat.search("apiVersion: composition.krateo.io/v1alpha1")
    assert pat.search("composition.krateo.io/v0-3-8")
    assert not pat.search("apiVersion: core.krateo.io/v1alpha1")
    assert not pat.search("composition.krateo.io/<APIVERSION>")
    assert not pat.search("kubectl get compositiondefinition x -o json")

    files = list(_docs()) + [
        f for f in sorted(glob.glob("**/*.yaml", recursive=True))
        if not f.startswith(".git" + os.sep) and os.sep + "charts" + os.sep not in f
    ]
    for f in files:
        if f.replace(os.sep, "/") in HISTORICAL_DOCS:
            continue
        with open(f, encoding="utf-8", errors="replace") as fh:
            for n, line in enumerate(fh, 1):
                if pat.search(line):
                    errors.append(
                        f"{f}:{n}: hardcoded Composition apiVersion -- core-provider "
                        f"derives it from the FULL chart version, and this chart's "
                        f"version is a release-time placeholder, so no correct literal "
                        f"exists. Read it instead: kubectl get compositiondefinition "
                        f"<name> -n <ns> -o jsonpath='{{.status.apiVersion}}'")


def verify_documented_pin(errors):
    """Every documented oasgen-provider install must name the pinned version.

    The pin is OASGEN_VERSION in .github/workflows/validate.yaml -- one place.
    The docs spell it out in prose, so nothing otherwise stops them drifting
    apart; `aruba-provider-kog` had four documented paths below their floor,
    with no stale registry involved at all.

    The pin is not a preference. apiKey/in-header arrived in 0.19.0, and 0.26.1
    is a hard floor: below it the `krateo.io/connector-verbose` request dump
    redacted only values it had been TOLD were sensitive, so credential HEADERS
    went to pod logs in cleartext -- and this chart's credential IS a header,
    X-Auth-Token. Verbose is precisely what you switch on to debug a 401.

    Also screens the install SOURCE. Three resolve and only one is right:
    krateo-platformops (live), krateoplatformops (abandoned, disjoint tags, and
    NOT archived so it is indistinguishable by measurement), and
    charts.krateo.io -- which serves appVersion 0.10.0, below both floors, from
    the dead org's GitHub Pages. See oasgen-provider#278.
    """
    wf = os.path.join(".github", "workflows", "validate.yaml")
    if not os.path.exists(wf):
        errors.append(f"{wf}: missing -- nothing defines OASGEN_VERSION to check "
                      f"the docs against")
        return
    m = re.search(r'^\s*OASGEN_VERSION:\s*"([^"]+)"', open(wf).read(), flags=re.M)
    if not m:
        errors.append(f"{wf}: no OASGEN_VERSION to check the docs against")
        return
    pin = m.group(1)

    chart = re.compile(r"charts/oasgen-provider(-crds)?\b")
    version = re.compile(r'--version\s+"?\$?\{?([0-9]+\.[0-9]+\.[0-9]+)')
    assert version.search("  --version 0.9.20 -n krateo-system").group(1) == "0.9.20"
    assert version.search('--version "0.40.0" -n x').group(1) == "0.40.0"
    assert version.search('--version "${OASGEN_VERSION}"') is None  # indirection is fine
    assert chart.search("oci://ghcr.io/krateo-platformops/charts/oasgen-provider-crds")
    assert not chart.search("oci://ghcr.io/krateo-blueprints/charts/openstack-nova-operator-kog")

    bad_source = re.compile(r"ghcr\.io/krateoplatformops/|"
                            r"https?://charts\.krateo\.io|"
                            r"helm\s+repo\s+add\s+\S*\s+https?://charts\.krateo\.io")
    assert bad_source.search("helm install x oci://ghcr.io/krateoplatformops/charts/y")
    assert bad_source.search("helm repo add krateo https://charts.krateo.io")
    assert not bad_source.search("oci://ghcr.io/krateo-platformops/charts/oasgen-provider")

    for f in _docs():
        if f.replace(os.sep, "/") in HISTORICAL_DOCS:
            continue
        lines = open(f, encoding="utf-8", errors="replace").read().splitlines()
        for n, line in enumerate(lines):
            # Prose ABOUT the bad sources is the point of several docs, so only
            # flag them inside something that looks like a command.
            if bad_source.search(line) and re.search(r"\bhelm\b", line):
                errors.append(
                    f"{f}:{n+1}: install source is not ghcr.io/krateo-platformops -- "
                    f"that registry serves a build below the {pin} floor "
                    f"(oasgen-provider#278)")
            if not chart.search(line):
                continue
            # The --version may sit on the install line or a continuation. If it
            # cannot be located, say UNCHECKED rather than staying silent: a
            # silent skip reports "all clear" on an install it never examined,
            # which is the same way an incomplete pattern fails like a dead one.
            found = False
            for cand in lines[n:n + 3]:
                v = version.search(cand)
                if v:
                    found = True
                    if v.group(1) != pin:
                        errors.append(
                            f"{f}:{n+1}: documents oasgen-provider chart "
                            f"{v.group(1)} but the pin is {pin} (OASGEN_VERSION "
                            f"in {wf})")
                    break
            if not found and re.search(r"\bhelm\s+(install|upgrade)\b", line):
                errors.append(
                    f"{f}:{n+1}: installs the oasgen-provider chart but no --version "
                    f"within 3 lines -- this screen could not verify it against the "
                    f"{pin} pin, so treat it as unchecked rather than clean")


# Kinds and plurals a stock Kubernetes API server already serves. Hand-written
# on purpose: this must run on a PR with no cluster, which is the only place
# the collision is still cheap to fix. It is not exhaustive and does not need
# to be. The AUTHORITATIVE screen is `kubectl api-resources` minus CRD-backed
# groups on a FRESH cluster; this moves the common cases earlier.
BUILTIN_KINDS = {
    "Binding", "ComponentStatus", "ConfigMap", "Endpoints", "Event", "LimitRange",
    "Namespace", "Node", "PersistentVolume", "PersistentVolumeClaim", "Pod",
    "PodTemplate", "ReplicationController", "ResourceQuota", "Secret", "Service",
    "ServiceAccount", "ControllerRevision", "DaemonSet", "Deployment", "ReplicaSet",
    "StatefulSet", "CronJob", "Job", "Ingress", "IngressClass", "NetworkPolicy",
    "ClusterRole", "ClusterRoleBinding", "Role", "RoleBinding", "Lease",
    "CustomResourceDefinition", "APIService", "StorageClass", "VolumeAttachment",
    "CSIDriver", "CSINode", "PriorityClass", "RuntimeClass", "PodDisruptionBudget",
    "HorizontalPodAutoscaler", "MutatingWebhookConfiguration",
    "ValidatingWebhookConfiguration", "CertificateSigningRequest", "EndpointSlice",
}
BUILTIN_LOWER = {k.lower() for k in BUILTIN_KINDS} | {
    "bindings", "componentstatuses", "configmaps", "endpoints", "events",
    "limitranges", "namespaces", "nodes", "persistentvolumes",
    "persistentvolumeclaims", "pods", "podtemplates", "replicationcontrollers",
    "resourcequotas", "secrets", "services", "serviceaccounts",
    "controllerrevisions", "daemonsets", "deployments", "replicasets",
    "statefulsets", "cronjobs", "jobs", "ingresses", "ingressclasses",
    "networkpolicies", "clusterroles", "clusterrolebindings", "roles",
    "rolebindings", "leases", "customresourcedefinitions", "apiservices",
    "storageclasses", "volumeattachments", "csidrivers", "csinodes",
    "priorityclasses", "runtimeclasses", "poddisruptionbudgets",
    "horizontalpodautoscalers", "certificatesigningrequests", "endpointslices",
}


def _collides(kind):
    """The names this kind would occupy, intersected with the built-ins.

    The plural is DERIVED here, which is the weak point: oasgen derives the real
    one and irregular stems do not survive a naive +s (`Kaas` -> `kaas`, not
    `kaass`). So this screens the forms a collision could take and accepts a
    false positive over a miss -- a false positive is one comment to resolve, a
    miss is a chart that installs on an upgraded cluster and refuses on a fresh
    one.
    """
    low = kind.lower()
    return ({kind} & BUILTIN_KINDS) | ({low, low + "s", low + "es"} & BUILTIN_LOWER)


def verify_no_builtin_shadowing(errors):
    """No generated CRD may shadow a Kubernetes built-in, on EITHER of its CRDs.

    oasgen refuses such a kind from 0.32.0 (#200) -- but only on a FRESH
    cluster. Where the CRD already exists it downgrades to a warning, so a
    developer who has been upgrading one cluster for weeks sees nothing and the
    chart fails for the first person who installs it clean.
    `github-provider-kog` lost a chart to exactly this with a `Deployment` kind.

    Reads chart/values.yaml rather than chart/templates/rd-*.yaml: those are
    Helm templates and are not parseable as YAML.

    Screens BOTH CRDs per RestDefinition -- `<Kind>` and the companion
    `<Kind>Configuration` -- because oasgen emits two and the guard does not
    care which one you intended.
    """
    for probe in ("Node", "Service", "Secret", "Deployment", "Event", "Job"):
        assert _collides(probe), f"screen cannot detect the built-in {probe}"
    for ok in ("Instance", "ComputeFlavor", "ComputeKeypair", "ComputeServerGroup",
               "ComputeAggregate", "ComputeQuota"):
        assert not _collides(ok), f"screen false-positives on {ok}"

    vals = yaml.safe_load(open(os.path.join("chart", "values.yaml"))) or {}
    rds = (vals.get("restdefinitions") or {})
    if not rds:
        errors.append("chart/values.yaml: no restdefinitions -- the shadowing screen "
                      "found nothing to check, which is a failure, not a pass")
        return
    for name, cfg in sorted(rds.items()):
        kind = (cfg or {}).get("resourceKind")
        if not kind:
            errors.append(f"chart/values.yaml: restdefinitions.{name} has no "
                          f"resourceKind to screen")
            continue
        for k in (kind, kind + "Configuration"):
            hit = _collides(k)
            if hit:
                errors.append(
                    f"chart/values.yaml: restdefinitions.{name}.resourceKind -> "
                    f"'{k}' shadows a Kubernetes built-in ({', '.join(sorted(hit))}). "
                    f"oasgen refuses it on a fresh cluster (#200) and only warns where "
                    f"the CRD already exists, so this is invisible until a clean "
                    f"install.")


def _declared_auth_fields():
    """The `spec.authentication.<field>` names oasgen will generate, read from
    the OAS security schemes this chart actually ships.

    The assets are Helm templates (`servers[].url` is a `{{ include }}`), so they
    do not parse as YAML -- hence the textual read. `type: apiKey` and
    `type: http` appear only on security schemes in an OAS; schema types are
    string/object/integer/..., so this does not collide with property types.
    """
    fields = set()
    for path in sorted(glob.glob(os.path.join("chart", "assets", "*.yaml"))):
        text = open(path).read()
        for m in re.finditer(r"\btype:\s*(apiKey|http)\b", text):
            if m.group(1) == "apiKey":
                fields.add("apiKey")
                continue
            tail = text[m.end():m.end() + 200]
            sm = re.search(r"\bscheme:\s*([A-Za-z0-9_-]+)", tail)
            if sm:
                fields.add(sm.group(1).lower())
    return fields


def _auth_methods_in_text(text):
    """Yield (lineno, method) for the first key under each `authentication:`.

    Deliberately textual so it also reaches YAML inside fenced markdown blocks --
    which is where two of the three broken examples lived.
    """
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        m = re.match(r"^(\s*)authentication:\s*(#.*)?$", ln)
        if not m:
            continue
        indent = len(m.group(1))
        for j in range(i + 1, min(i + 12, len(lines))):
            nxt = lines[j]
            if not nxt.strip() or nxt.strip().lstrip("#").strip() == "":
                continue
            if nxt.strip().startswith("#"):
                continue
            km = re.match(r"^(\s*)([A-Za-z][A-Za-z0-9_]*):", nxt)
            if not km or len(km.group(1)) <= indent:
                break
            yield j + 1, km.group(2)
            break


def verify_auth_scheme_consistency(errors):
    """Every shipped `authentication.<method>` must exist in the generated CRD.

    This screens the gap that let a real defect ship. PR #13 converted the OAS
    from `http/bearer` to an `apiKey` scheme and updated samples/nova-auth.yaml,
    but left `samples/extra-resources.yaml`, `docs/api.md` and
    `docs/e2e-krateo-openstack.md` carrying `authentication.bearer` blocks. The
    generated CRDs are apiKey-ONLY -- measured in a kind cluster, all five
    report `methods=[apiKey]` with no bearer field to fall back to -- so the API
    server refuses those examples outright:

        strict decoding error: unknown field "spec.authentication.bearer"

    The failure mode this guards is not drift but a verification gap: the
    generated schema was asserted correct, and the shipped examples were
    asserted to apply, and the two were never checked AGAINST EACH OTHER. Both
    halves passed in isolation, which is why good CI did not catch it.

    Prose is deliberately NOT screened. Legitimate past-tense narrative lives in
    current docs ("the generator then accepted only http/basic and http/bearer"),
    so a prose pattern would need a per-line allowlist -- and an allowlist keyed
    to position silently stops matching when a line moves, which is how a
    sibling repo's long-allowlisted placeholder reddened its scanner. A
    structural screen has no such failure mode.
    """
    declared = _declared_auth_fields()
    if not declared:
        errors.append("chart/assets/*.yaml: no security scheme found -- the auth "
                      "consistency screen has nothing to check, which is a "
                      "failure, not a pass")
        return

    # Prove the screen works before trusting it: it must reject a method that is
    # not declared and accept one that is.
    bad = "spec:\n  authentication:\n    bearer:\n      tokenRef: {}\n"
    good = "spec:\n  authentication:\n    apiKey:\n      tokenRef: {}\n"
    bad_found = [k for _, k in _auth_methods_in_text(bad)]
    good_found = [k for _, k in _auth_methods_in_text(good)]
    if "bearer" not in bad_found:
        errors.append("validate.py: the auth screen failed its own known-BAD "
                      f"assertion (saw {bad_found!r}) -- it cannot be trusted")
        return
    if "apiKey" not in good_found:
        errors.append("validate.py: the auth screen failed its own known-GOOD "
                      f"assertion (saw {good_found!r}) -- it cannot be trusted")
        return
    if "apiKey" not in declared:
        errors.append(f"chart/assets/*.yaml declares {sorted(declared)} but the "
                      f"known-good fixture assumes apiKey; update the fixture")
        return

    targets = []
    for pat in ("**/*.md", "**/*.yaml", "**/*.yml"):
        targets.extend(glob.glob(pat, recursive=True))
    for path in sorted(set(targets)):
        if path.startswith(".git" + os.sep) or os.sep + "charts" + os.sep in path:
            continue
        if path.replace(os.sep, "/") in HISTORICAL_DOCS:
            continue
        try:
            text = open(path).read()
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"{path}: unreadable, so its auth blocks go "
                          f"UNASSERTED ({type(exc).__name__}). A file this "
                          f"screen cannot read is a failure, not a skip.")
            continue
        for lineno, method in _auth_methods_in_text(text):
            if method not in declared:
                errors.append(
                    f"{path}:{lineno}: `authentication.{method}` is not a field the "
                    f"generated CRD has. This chart's OAS declares {sorted(declared)}, "
                    f"so oasgen emits only those -- the API server rejects this with "
                    f"`strict decoding error: unknown field "
                    f"\"spec.authentication.{method}\"`.")


# The oasgen-provider release below which a credential-carrying HEADER could be
# written to pod logs in cleartext. `krateo.io/connector-verbose` redacted only
# values it had been TOLD were sensitive; since 0.26.1 it redacts by header name.
# For an X-Auth-Token chart the credential IS a header, and verbose is what a
# user enables to debug the 401 they are getting -- so anything below this is a
# credential-exposure recommendation, not a version-drift one.
HARD_SECURITY_FLOOR = (0, 26, 1)


def _ver_tuple(s):
    return tuple(int(x) for x in s.split("."))


def verify_no_under_floor_recommendation(errors):
    """No file may RECOMMEND an oasgen-provider version below the hard floor.

    Distinct from verify_documented_pin, which compares `helm ... --version`
    invocations in markdown against OASGEN_VERSION. This caught nothing when
    `kagent/agent-nova-expert.yaml` advised "bump oasgen-provider to >= 0.20.0",
    because that line is in a YAML file (the pin check sweeps only `**/*.md`) and
    carries neither `helm` nor `--version`. Two independent reasons to miss it,
    which is why the scope AND the pattern both widen here.

    Deliberately narrow: it fires only on an imperative -- bump/upgrade/install/
    pin/use/require/need/minimum/at least -- near an oasgen-provider mention and
    a version below the floor.

    KNOWN LIMIT, stated rather than implied: the scan is LINE BY LINE, so a
    version on a different line from its subject is NOT detected. That is how
    the quickstart instance survived even once "need" was in the list -- the
    version had wrapped onto the next line. Joining lines into paragraphs would
    fix it and introduce a worse bug: the version table in
    docs/configuration.md legitimately pairs `0.19.0` with the word "minimum"
    two rows apart, so a joined window would flag a correct table forever. A
    false positive in a table nobody can silence is how a guard gets disabled. Stating a fact about history ("apiKey arrived in 0.19.0", "0.26.1 is
    the floor") is correct and must not trip it, and the known-good assertion
    below pins that distinction.
    """
    # "need" and "minimum"/"at least" were MISSING, and a live instance slipped
    # through because of it: docs/quickstart.md said "`apiKey` needs >= 0.20.0",
    # below the 0.26.1 floor, and this screen passed. An incomplete pattern
    # fails exactly like a dead one -- which is the lesson in this file's own
    # module docstring, committed by the same hand that then wrote this list.
    _IMP = r"(?:bump|upgrade|install|pin|use|require|need|minimum|at least)"
    imperative = re.compile(
        r"\b" + _IMP + r"\w*\b[^.\n]{0,120}?"
        r"oasgen-provider\b[^.\n]{0,120}?([0-9]+\.[0-9]+\.[0-9]+)"
        r"|oasgen-provider\b[^.\n]{0,120}?\b" + _IMP + r"\w*\b"
        r"[^.\n]{0,120}?([0-9]+\.[0-9]+\.[0-9]+)",
        re.I)

    def _under_floor(line):
        for m in imperative.finditer(line):
            v = m.group(1) or m.group(2)
            if v and _ver_tuple(v) < HARD_SECURITY_FLOOR:
                return v
        return None

    # Prove it before trusting it.
    known_bad = "the upgrade is: bump oasgen-provider to >= 0.20.0, switch the OAS"
    known_good_fact = "`apiKey` / `in: header` has been supported by oasgen-provider since 0.19.0"
    known_good_rec = "bump oasgen-provider to >= 0.40.0 and set nova.url"
    if _under_floor(known_bad) != "0.20.0":
        errors.append("validate.py: the under-floor screen failed its known-BAD "
                      f"assertion (got {_under_floor(known_bad)!r})")
        return
    if _under_floor(known_good_fact) is not None:
        errors.append("validate.py: the under-floor screen false-positives on a "
                      "statement of HISTORICAL FACT, which must stay writable "
                      f"(flagged {_under_floor(known_good_fact)!r})")
        return
    if _under_floor(known_good_rec) is not None:
        errors.append("validate.py: the under-floor screen false-positives on a "
                      f"compliant recommendation (flagged {_under_floor(known_good_rec)!r})")
        return

    targets = []
    for pat in ("**/*.md", "**/*.yaml", "**/*.yml"):
        targets.extend(glob.glob(pat, recursive=True))
    for path in sorted(set(targets)):
        if path.startswith(".git" + os.sep) or os.sep + "charts" + os.sep in path:
            continue
        if path.replace(os.sep, "/") in HISTORICAL_DOCS:
            continue
        try:
            lines = open(path).read().splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"{path}: unreadable, so it is NOT screened for "
                          f"under-floor recommendations ({type(exc).__name__}). "
                          f"A file this screen cannot read is a failure.")
            continue
        for i, line in enumerate(lines, 1):
            v = _under_floor(line)
            if v:
                errors.append(
                    f"{path}:{i}: recommends oasgen-provider {v}, below the "
                    f"{'.'.join(map(str, HARD_SECURITY_FLOOR))} HARD security floor. "
                    f"Below that, connector-verbose wrote credential HEADERS to pod "
                    f"logs in cleartext -- and this chart's credential IS a header "
                    f"(X-Auth-Token). Recommend the pinned version instead.")


def verify_cr_apiversion_consistency(errors):
    """Resource CRs must all share one apiVersion, Configuration CRs another.

    DELIBERATELY DOES NOT DERIVE THE VALUE. This screen used to port oasgen's
    `crdgen.NormalizeVersionName` and assert it against known inputs, to catch
    the seven examples that declared `v1alpha1` for resource kinds where the CRD
    is served at `v2-1`. That worked, and it was a reimplementation of another
    project's internal rule, maintained here, able to drift from it silently
    while still passing its own self-test.

    oasgen publishes both halves on the RestDefinition and always has:

        {.status.resource.apiVersion}       -> the derived resource version
        {.status.configuration.apiVersion}  -> the fixed v1alpha1

    So the honest check is a COMPARISON, which needs a cluster. The smoke
    workflow now does it on every PR via `hack/assert-sample-gvks.py`, and the
    port is gone rather than maintained -- the commitment made in its own
    docstring when it was written.

    What is left here is the part a cluster-free check CAN do honestly: internal
    consistency. If one sample's apiVersion drifts from its siblings, this
    catches it in seconds without a cluster. It cannot catch a UNIFORMLY wrong
    value -- the original defect was uniform -- and that is precisely what the
    smoke comparison is for. Stating the limit rather than implying coverage.
    """
    group = "nova.openstack.krateo.io"
    api_re = re.compile(r"apiVersion:\s*(" + re.escape(group) + r"/[\w.-]+)")
    res, cfg = {}, {}
    for path in sorted(glob.glob("chart/samples/*.yaml")
                       + glob.glob("examples/**/*.yaml", recursive=True)):
        lines = open(path, encoding="utf-8").read().splitlines()
        for i, ln in enumerate(lines):
            m = api_re.search(ln)
            if not m:
                continue
            kind = None
            for j in range(i + 1, min(i + 4, len(lines))):
                km = re.match(r"^\s*kind:\s*([A-Za-z]+)", lines[j])
                if km:
                    kind = km.group(1)
                    break
            if not kind:
                # Underivable expectation. Every CR in a sample or example has
                # `kind` adjacent to `apiVersion`; if it cannot be found the
                # file is malformed, and dropping the line from both buckets
                # would remove it from the consistency comparison silently.
                errors.append(
                    f"{path}:{i + 1}: found `{m.group(1)}` but no `kind` within "
                    f"3 lines, so this apiVersion cannot be classified as a "
                    f"resource or a Configuration and goes UNCHECKED. Either "
                    f"the document is malformed or it is prose that belongs "
                    f"outside a sample file.")
                continue
            ver = m.group(1).split("/", 1)[1]
            (cfg if kind.endswith("Configuration") else res).setdefault(
                ver, []).append(f"{path}:{i + 1} ({kind})")

    if not res and not cfg:
        errors.append("no sample CRs in the group were found to check -- that is "
                      "a failure, not a pass")
        return
    for label, seen in (("resource", res), ("Configuration", cfg)):
        if len(seen) > 1:
            detail = "; ".join(f"{v}: {', '.join(w)}" for v, w in sorted(seen.items()))
            errors.append(
                f"{label} CRs disagree on apiVersion ({detail}). Every {label} CR "
                f"in this chart is served at the same version, so at most one of "
                f"these can be right. The authoritative value is "
                f"`.status.{'configuration' if label == 'Configuration' else 'resource'}"
                f".apiVersion` on the RestDefinition -- read it, do not derive it.")
    if res and cfg and set(res) == set(cfg):
        errors.append(
            f"resource and Configuration CRs share apiVersion {sorted(res)}, but the "
            f"two halves of a generated pair are served at DIFFERENT versions "
            f"(resource = NormalizeVersionName(info.version), Configuration = a "
            f"fixed v1alpha1). One of them is wrong.")


def verify_composition_spec_against_schema(errors):
    """A Composition CR's `spec` IS the chart's values, so it must satisfy
    chart/values.schema.json -- no unknown fields, no missing required ones.

    This exists because the Composition example shipped with:

        spec.nova.enabled: true
        spec.nova.upstreamUrl: https://...

    Neither field exists. `upstreamUrl` was the nginx auth-bridge's upstream,
    and the bridge was deleted when the OAS moved to a native apiKey scheme;
    the schema allows exactly one property there, `url`, required. core-provider
    generates the Composition CRD from values.schema.json and the API server
    decodes strictly, so the example was rejected with

        strict decoding error: unknown field "spec.nova.upstreamUrl"

    It survived the rename because the example had never been applied -- the
    fourth hand-written artifact in this repo with that same cause.

    The smoke job catches this against a real cluster, which is how it was
    found. This screen catches it in seconds without one, and unlike the
    apiVersion case there is nothing to reimplement: the schema is IN this repo
    and is the same file core-provider reads. Comparing two local files is not
    a port of anyone's internal rule.
    """
    schema_path = os.path.join("chart", "values.schema.json")
    if not os.path.exists(schema_path):
        errors.append(f"{schema_path}: missing -- the Composition spec screen "
                      f"has nothing to validate against, which is a failure, "
                      f"not a pass")
        return
    try:
        schema = json.load(open(schema_path, encoding="utf-8"))
    except ValueError as e:
        errors.append(f"{schema_path}: not valid JSON ({e})")
        return

    def unknown_fields(obj, sch, path=""):
        out = []
        allowed = sch.get("properties") or {}
        for key, val in (obj or {}).items():
            here = f"{path}.{key}" if path else key
            if key not in allowed:
                out.append(here)
            elif isinstance(val, dict):
                out.extend(unknown_fields(val, allowed[key], here))
        return out

    # Prove it both ways before trusting the sweep.
    probe_schema = {"properties": {"nova": {"properties": {"url": {}},
                                            "required": ["url"]}}}
    if unknown_fields({"nova": {"upstreamUrl": "x"}}, probe_schema) != ["nova.upstreamUrl"]:
        errors.append("validate.py: the Composition spec screen failed its "
                      "known-BAD assertion and cannot be trusted")
        return
    if unknown_fields({"nova": {"url": "x"}}, probe_schema) != []:
        errors.append("validate.py: the Composition spec screen false-positives "
                      "on a valid spec and cannot be trusted")
        return

    checked = 0
    for path in sorted(glob.glob("examples/**/*.yaml", recursive=True)
                       + glob.glob("examples/*.yaml")):
        try:
            docs = [d for d in yaml.safe_load_all(open(path, encoding="utf-8")) if d]
        except (yaml.YAMLError, OSError, UnicodeDecodeError) as exc:
            # NOT a skip. An example this screen cannot read is an example it
            # cannot assert, and a rationalised `continue` here would hide a
            # future breakage behind a reason that is no longer true: the
            # comment used to claim placeholder apiVersions make some examples
            # unparseable, and on this tree NONE are -- so the path had never
            # run. Credit: krateo-arubacloud-provider-kog, who shipped the same
            # shape in an assertion built to prevent silent passes, and whose
            # one-line check is "find where the derivation can return no answer
            # and confirm the caller treats it as a failure".
            errors.append(f"{path}: cannot be read or parsed, so its Composition "
                          f"spec goes UNASSERTED ({type(exc).__name__}: "
                          f"{str(exc).splitlines()[0]}). A file this screen "
                          f"cannot check is a failure, not a skip.")
            continue
        for doc in docs:
            av = str(doc.get("apiVersion") or "")
            if not av.startswith("composition.krateo.io/"):
                continue
            checked += 1
            spec = doc.get("spec") or {}
            for field in unknown_fields(spec, schema):
                errors.append(
                    f"{path}: Composition spec sets `{field}`, which is not in "
                    f"chart/values.schema.json. A Composition's spec IS the "
                    f"chart's values and the API server decodes it strictly, so "
                    f"this is rejected as `unknown field \"spec.{field}\"`.")
            for prop, sub in (schema.get("properties") or {}).items():
                for req in (sub.get("required") or []):
                    if prop in spec and req not in (spec.get(prop) or {}):
                        errors.append(
                            f"{path}: Composition spec is missing required "
                            f"`{prop}.{req}` (chart/values.schema.json marks it "
                            f"required).")
    if not checked:
        errors.append("no Composition examples found to validate -- that is a "
                      "failure, not a pass")


def _compile_problem(src, name):
    """Compile src with SyntaxWarning promoted to an error; return a message or None.

    BOTH exception types must be caught. With `simplefilter("error",
    SyntaxWarning)` the interpreter raises the escalated diagnostic as a
    **SyntaxError** at compile time, not as a SyntaxWarning -- measured on
    CPython 3.14.6 for `return` in `finally`, invalid escapes, assert-on-tuple
    and `is` with a literal, all four of which arrived as SyntaxError. A version
    of this that caught only SyntaxWarning would catch nothing and report
    success: a check that cannot fail.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("error", SyntaxWarning)
        try:
            compile(src, name, "exec")
        except SyntaxWarning as w:          # some interpreters/diagnostics
            return f"SyntaxWarning: {w}"
        except SyntaxError as e:            # escalated warnings land here
            return f"SyntaxError: {e}"
    return None


def verify_python_compiles_without_warnings(errors):
    """Every Python file must compile with SyntaxWarning promoted to an error.

    Turns diagnostics nobody reads into a hard failure: `return` in a `finally`
    block (which discards an in-flight exception), invalid escape sequences,
    assert-on-a-tuple (always true), `is` against a literal. Each is a defect
    that runs, and each is reported by the compiler and then ignored.

    Adopted from `krateo-arubacloud-provider-kog`, who converted exactly this
    rule from a comment into a gate after finding a `return` in a `finally`
    thirty lines below a comment explaining why not to write one. The general
    lesson does not mechanise -- there is no check for "a comment explains a
    hazard and the next function ignores it" -- so this converts the one
    instance that can be, and claims nothing about the class.

    The self-test compiles the known-bad pattern from a STRING, not a file, on
    purpose: a negative test that writes a file somewhere can break its own
    imports, produce no output, and read exactly like a pass. A string has no
    path to break.
    """
    # WHICH diagnostics exist is version-dependent, so no single fixture proves
    # the mechanism everywhere. `return` in a `finally` block only became a
    # diagnostic in CPython 3.14; on the 3.12 this repo's CI pins it compiles
    # silently. A gate asserting that one fixture alone passed locally and failed
    # in CI -- correctly, loudly, which is the whole reason the assertion is here.
    # Without it the gate would have run in CI detecting nothing and reported
    # success: a check that cannot fail.
    #
    # So: several known-bad fixtures, and the mechanism is proven if ANY is
    # caught. Each one independently exercises promote -> raise -> catch, which
    # is the thing under test; the set only buys portability. The detected ones
    # are printed so a reader sees which diagnostics this interpreter has.
    known_bad = {
        "invalid escape sequence": "x = '\\d'\n",
        "assert on a tuple": "assert (1, 'always true')\n",
        "`is` with a literal": "x = 1\nif x is 1:\n    pass\n",
        "`return` in `finally`": "def f():\n    try:\n        return 1\n    finally:\n        return 2\n",
    }
    caught = sorted(k for k, src in known_bad.items()
                    if _compile_problem(src, "<selftest-bad>") is not None)
    if not caught:
        errors.append(
            "validate.py: the syntax gate failed its known-BAD assertion -- NONE "
            f"of {sorted(known_bad)} was rejected on Python "
            f"{sys.version.split()[0]}, so the gate detects nothing and cannot be "
            "trusted. Either SyntaxWarning promotion stopped working or this "
            "interpreter has none of these diagnostics; add one it does have.")
        return
    print(f"  syntax gate: diagnostics available here -> {caught}")

    good = "def f():\n    try:\n        return 1\n    finally:\n        pass\n"
    problem = _compile_problem(good, "<selftest-good>")
    if problem is not None:
        errors.append("validate.py: the syntax gate false-positives on clean "
                      f"code ({problem})")
        return

    found = 0
    for path in sorted(glob.glob("**/*.py", recursive=True)):
        parts = path.split(os.sep)
        if ".git" in parts or "__pycache__" in parts:
            continue
        try:
            src = open(path, encoding="utf-8").read()
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"{path}: unreadable, so it is NOT compiled and its "
                          f"syntax warnings go undetected ({type(exc).__name__}). "
                          f"A Python file this gate cannot read is a failure.")
            continue
        found += 1
        problem = _compile_problem(src, path)
        if problem:
            errors.append(f"{path}: {problem}")
    if not found:
        errors.append("validate.py: the syntax gate found no Python files to "
                      "compile, which is a failure, not a pass")


def main():
    errors = []
    for check in (verify_composition_apiversion,
                  verify_documented_pin,
                  verify_no_builtin_shadowing,
                  verify_auth_scheme_consistency,
                  verify_no_under_floor_recommendation,
                  verify_cr_apiversion_consistency,
                  verify_python_compiles_without_warnings,
                  verify_composition_spec_against_schema):
        check(errors)
    if errors:
        print(f"{len(errors)} problem(s):\n", file=sys.stderr)
        for e in errors:
            print(f"  {e}\n", file=sys.stderr)
        return 1
    print("validate.py: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
