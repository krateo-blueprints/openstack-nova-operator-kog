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
import subprocess
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
    # A SECOND PATTERN: a bare `>=` BOUND, with no imperative at all.
    #
    # The imperative list above is deliberately narrow so historical prose
    # ("before 0.26.1 the dump leaked", "apiKey arrives in 0.19.0") cannot trip
    # it. That narrowness let a real case through, in SHIPPED content:
    #
    #     chart/assets/server.yaml:  "oasgen-provider (>= 0.43.0) generates ..."
    #
    # No bump, no install, no "at least" -- so the screen swept the file and
    # found nothing. The asset ships INSIDE the chart and its text surfaces in
    # the generated CRD's descriptions, so an under-floor bound there reaches a
    # user's cluster. Mutating that line to `>= 0.20.0`, below the hard security
    # floor, passed validation.
    #
    # `>=` is a recommendation BY CONSTRUCTION: it states a minimum a reader is
    # invited to satisfy. Historical statements use "before", "below", "since"
    # or "at", never a `>=` bound, so this pattern can be added without
    # reintroducing the false positives the imperative list exists to avoid.
    bound = re.compile(
        r"oasgen-provider\b[^.\n]{0,120}?>=\s*v?([0-9]+\.[0-9]+\.[0-9]+)"
        r"|>=\s*v?([0-9]+\.[0-9]+\.[0-9]+)[^.\n]{0,120}?oasgen-provider\b",
        re.I)

    def _under_floor_bound(line):
        for m in bound.finditer(line):
            v = m.group(1) or m.group(2)
            if v and _ver_tuple(v) < HARD_SECURITY_FLOOR:
                return v
        return None

    # Prove THIS pattern too, with the shipped line that escaped as known-bad.
    bound_bad = "directly as an `apiKey` security scheme. oasgen-provider (>= 0.20.0)"
    bound_good = "directly as an `apiKey` security scheme. oasgen-provider (>= 0.49.0)"
    bound_hist = "before 0.26.1 the verbose dump wrote credential headers in cleartext"
    if _under_floor_bound(bound_bad) != "0.20.0":
        errors.append("validate.py: the bare-bound screen failed its known-BAD "
                      f"assertion (got {_under_floor_bound(bound_bad)!r}); the "
                      f"line it was written for would escape again")
        return
    if _under_floor_bound(bound_good) is not None:
        errors.append("validate.py: the bare-bound screen false-positives on an "
                      "ABOVE-floor bound, so it would fire on correct content")
        return
    if _under_floor_bound(bound_hist) is not None:
        errors.append("validate.py: the bare-bound screen false-positives on "
                      "HISTORICAL prose, which is exactly what the imperative "
                      "list was kept narrow to protect")
        return

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
            # Either pattern is a finding: an imperative, or a bare `>=` bound.
            v = _under_floor(line) or _under_floor_bound(line)
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
    res_src, cfg_src = set(), set()
    # hack/*.sh is swept too: hack/probe-361.sh embeds a ComputeFlavor and a
    # ComputeFlavorConfiguration in a heredoc, and a CR in a shell script is as
    # applicable as one in a sample -- it just is not where anyone looks. Adding
    # a file with CR apiVersions that this screen did not cover would have left
    # an unguarded copy of the exact defect the screen exists for. The regex
    # reads raw lines, so a heredoc needs no special handling.
    # Markdown is swept too, EXCEPT the historical narratives. docs/api.md
    # prescribes CRs a reader copies, and its Instance at `v2-1` plus its
    # InstanceConfiguration at `v1alpha1` were correct but UNGUARDED: flipping
    # every `v2-1` in that file to `v1alpha1` passed validation. That is the
    # same defect class that once made seven examples unapplicable, in the one
    # document a reader follows most closely.
    #
    # HISTORICAL_DOCS are excluded on purpose, and this is not a loophole. The
    # medium articles print a reconciled `Instance` at `v1alpha1` under the words
    # "here's the proof", and that was CORRECT when written -- oasgen's
    # TargetVersion landed in 0.28.1, and before it both halves really were
    # `v1alpha1`. Rewriting those blocks would falsify a record of a real run.
    # They carry a reader warning instead.
    doc_crs = [f for f in _docs()
               if f.replace(os.sep, "/") not in HISTORICAL_DOCS]
    for path in sorted(glob.glob("chart/samples/*.yaml")
                       + glob.glob("examples/**/*.yaml", recursive=True)
                       + glob.glob("hack/*.sh")
                       + doc_crs):
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
            is_cfg = kind.endswith("Configuration")
            (cfg if is_cfg else res).setdefault(
                ver, []).append(f"{path}:{i + 1} ({kind})")
            (cfg_src if is_cfg else res_src).add(path)

    # Coverage gate for the hack/*.sh glob. `res`/`cfg` stay populated from
    # chart/samples/ even if hack/*.sh matched nothing, so the combined
    # emptiness check below cannot notice this sweep going blind -- a rename, a
    # moved heredoc or a changed glob would all keep this screen green while
    # silently dropping the file it was extended for. Absence is only readable
    # once presence is established: assert only if the file IS there, so
    # deleting the probe relaxes the gate instead of breaking the build.
    probe = "hack/probe-361.sh"
    if os.path.exists(probe):
        covered = {pth for pth in list(res_src) + list(cfg_src)}
        if not any(c.startswith("hack/") for c in covered):
            errors.append(
                f"{probe} exists and declares CRs in a heredoc, but this screen "
                f"found none in hack/ -- the sweep that was added for it is no "
                f"longer reaching it. Scanned: {sorted(covered)}. A guard whose "
                f"corpus silently empties reports a clean pass, which is the "
                f"defect this screen exists to catch.")

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



def verify_every_script_is_exercised(errors):
    """Every repo script must be invoked by CI, or exempted here with a reason.

    FOUND BY HAND, WHICH IS THE PROBLEM. `scripts/get-token.sh` -- the script a
    quickstart reader actually runs, and the one that handles credentials -- had
    ZERO CI coverage, while every other script in hack/ and scripts/ was
    referenced by at least one workflow. Nothing would have reported that, and
    nothing would report the next one.

    A script nobody invokes is the same defect as a check nobody invokes, one
    level up: its absence from CI is indistinguishable from it passing.

    EXEMPTIONS ARE NAMED, NOT PATTERNED. Each carries the reason it cannot run
    in CI, so a future reader can judge whether the reason still holds rather
    than inheriting a silent allowlist.
    """
    exempt = {
        "hack/probe-361.sh":
            "needs a REAL CLUSTER it does not create -- it takes a kubeconfig "
            "and context and reconciles against an in-cluster mock for ~10 "
            "minutes; run by hand to answer a specific upstream question. (It "
            "used to create its own kind cluster; that was removed when Diego "
            "directed all sessions onto disposable GKE, and this reason was "
            "updated with it rather than left saying 'creates a kind cluster'.)",
        "hack/probe-redirect.sh":
            "same -- takes a kubeconfig and context, two in-cluster hosts, and "
            "it answers a security question that must not be reduced to a CI "
            "boolean. Its MOCK is separately validated in CI by "
            "hack/validate-redirect-mock.sh, which needs only Docker, so the "
            "measuring device is covered even though the measurement is not",
        "scripts/test-get-token.sh":
            "IS the CI invocation for get-token.sh; a test cannot be its own "
            "caller without the circularity this screen exists to prevent",
    }
    scripts = sorted(
        set(glob.glob("hack/*.sh")) | set(glob.glob("hack/*.py"))
        | set(glob.glob("scripts/*.sh")) | set(glob.glob("scripts/*.py"))
    )
    if not scripts:
        errors.append("verify_every_script_is_exercised found NO scripts to "
                      "check -- its globs reach nothing, so it would pass "
                      "whatever the repo contained")
        return

    workflows = glob.glob(".github/workflows/*.yaml") + glob.glob(".github/workflows/*.yml")
    if not workflows:
        errors.append("verify_every_script_is_exercised found NO workflows, so "
                      "every script would look uncovered -- or, inverted, "
                      "covered. Either way it is not measuring")
        return
    def run_commands(paths):
        """Collect only the `run:` scripts from the workflows, STRUCTURALLY.

        THIS SCREEN WAS DEFEATED THREE TIMES BY TEXTUAL MATCHING, each time by
        its own documentation, and each time the false pass was indistinguishable
        from a real one:

          1. validate.py's docstring named `scripts/get-token.sh`, and
             validate.py is itself referenced by a workflow, so the screen read
             its own prose and called the script covered.
          2. the explanatory COMMENT beside the new CI step named it, so
             deleting the step entirely still passed.
          3. the step's `name:` label named it, so neutering `run:` while
             leaving the label still passed.

        A mention is not an invocation, and no amount of comment-stripping
        separates them -- a label, a description or an `if:` expression can all
        contain a filename. So this parses the YAML and takes `run:` values
        only. A step that names a script in its label but does not run it is now
        correctly uncovered.
        """
        cmds = []

        def walk(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    if k == "run" and isinstance(v, str):
                        cmds.append(v)
                    else:
                        walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        for path in paths:
            try:
                walk(yaml.safe_load(open(path, encoding="utf-8")))
            except (OSError, yaml.YAMLError) as e:
                errors.append(f"{path}: cannot be parsed, so the scripts it "
                              f"runs cannot be read ({e}). Calling that "
                              f"'nothing is covered' flags every script and "
                              f"'everything is' flags none. Neither measures.")
        return "\n".join(cmds)

    blob = run_commands(workflows)
    if not blob.strip():
        errors.append("no `run:` commands found in any workflow, so every "
                      "script would look uncovered. That is a parse failure, "
                      "not a finding.")
        return

    def named_in(text, path):
        """Is `path` invoked in `text`? Matched as a WHOLE filename.

        A bare `in` check would make `get-token.sh` look covered the moment
        `test-get-token.sh` appeared anywhere, because one basename is a
        substring of the other. That answer happens to be right here -- the test
        does invoke it -- but it would be right BY ACCIDENT, and the same
        accident would hide a genuinely uncovered script whose name is a
        substring of a covered one.
        """
        base = re.escape(os.path.basename(path))
        return re.search(r"(?<![\w.-])(?:[\w./-]*/)?" + base + r"(?![\w.-])", text) is not None

    # Transitive, one level: a script invoked BY a covered script is covered.
    # scripts/test-get-token.sh runs get-token.sh, which is real coverage and
    # should be recognised deliberately rather than by substring luck.
    #
    # validate.py is EXCLUDED as a transitive source, and the reason is the best
    # instance of this screen's own subject found so far. validate.py is named
    # in the workflows, so it counted as covered; its contents were then added
    # to the transitive reach; and this screen's docstring and exemption notes
    # MENTION `scripts/get-token.sh` by name. So the screen read its own
    # documentation, found the script named there, and reported it as covered.
    #
    # The measurement included the measurer, and the first version of this
    # screen passed cleanly on the very gap it was written to find. validate.py
    # names scripts as DATA -- it lints them -- never as invocations, so it can
    # never be a legitimate source of coverage.
    covered_scripts = [s for s in scripts
                       if s != "scripts/validate.py" and named_in(blob, s)]
    indirect = ""
    for s in covered_scripts:
        try:
            indirect += "\n" + open(s, encoding="utf-8").read()
        except OSError:
            pass
    reach = blob + indirect

    uncovered = []
    for s in scripts:
        if s in exempt or s == "scripts/validate.py":
            continue
        if not named_in(reach, s):
            uncovered.append(s)
    if uncovered:
        errors.append(
            "these scripts are invoked by NO workflow and are not exempted: "
            f"{uncovered}. Either wire one into CI, or add it to `exempt` in "
            f"this screen WITH THE REASON it cannot run there. An uncovered "
            f"script's absence from CI is indistinguishable from it passing.")

    stale = [s for s in exempt if not os.path.exists(s)]
    if stale:
        errors.append(f"these exemptions name scripts that no longer exist: "
                      f"{stale}. A stale exemption is a hole kept open for a "
                      f"file that is gone.")


def verify_doc_filenames_exist(errors):
    """A backticked repo filename in the docs must name a file that exists.

    FOUND BY AUDIT, WHICH IS THE PROBLEM. `docs/release.md` -- the runbook a
    maintainer follows to cut a release -- named `release-chart.yaml` three
    times. No such workflow exists; the file is `release-tag.yaml`. The README
    named it too, plus `ci.yaml`, which has never existed either. Nothing
    reported any of it: `hack/preflight-refs.py` resolves OCI and image
    references, not filenames in prose, and never claimed otherwise.

    The runbook was wrong in a way that would have cost something. It said to
    bump `chart/Chart.yaml`'s `version` before tagging, and described a
    "tag-matches-Chart.yaml guard" that is in no workflow. The real mechanism
    stamps `CHART_VERSION` FROM the tag, so committing a concrete version would
    have silently removed the placeholder and made `smoke.yaml`'s stamp check
    vacuous -- that check fires when the placeholder SURVIVES, so a file that
    never had one always passes.

    EXEMPTIONS ARE NAMED WITH REASONS, not patterned, so a reader can judge
    whether each still holds.
    """
    exempt = {
        "/creds/key.json": "a path INSIDE a container, not a repo file",
        "key.json": "a service-account key the operator supplies themselves",
        "openrc.sh": "downloaded from the OpenStack dashboard by the reader; "
                     "deliberately gitignored so it can never be committed",
    }
    token = re.compile(r"`([A-Za-z0-9_.\-/]+\.(?:yaml|yml|py|sh|json|tpl))`")

    # Prove the pattern before trusting the sweep.
    if token.findall("see `release-tag.yaml` and `scripts/validate.py`") != \
            ["release-tag.yaml", "scripts/validate.py"]:
        errors.append("validate.py: the doc-filename screen's pattern does not "
                      "extract the names it is built to check")
        return
    if token.findall("a bare word.yaml outside backticks"):
        errors.append("validate.py: the doc-filename screen matches outside "
                      "backticks, so prose would trip it")
        return

    present = set()
    for root, dirs, files in os.walk("."):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "charts")]
        present.update(files)
    if not present:
        errors.append("the doc-filename screen walked the repo and found NO "
                      "files, so every reference would look broken")
        return

    docs = [f for f in _docs()]
    if not docs:
        errors.append("the doc-filename screen found NO markdown, so it has "
                      "nothing to check and would pass regardless")
        return

    broken, seen_any = [], False
    for path in docs:
        try:
            lines = open(path, encoding="utf-8").read().splitlines()
        except OSError as e:
            errors.append(f"{path}: unreadable, so its references are NOT "
                          f"checked ({e}). A file this screen cannot read is a "
                          f"failure, not a skip.")
            continue
        for i, line in enumerate(lines, 1):
            for tok in token.findall(line):
                seen_any = True
                if tok in exempt:
                    continue
                if os.path.exists(tok) or os.path.basename(tok) in present:
                    continue
                broken.append(f"{path}:{i}: `{tok}`")
    if not seen_any:
        errors.append("the doc-filename screen matched NO filenames in any doc, "
                      "which cannot be right for this repo -- the pattern has "
                      "stopped reaching the content")
        return
    if broken:
        errors.append(
            "these docs name files that do not exist in this repo: "
            f"{broken}. A runbook naming a workflow that is not there sends a "
            f"maintainer looking for it, or worse, describes a mechanism that "
            f"does not run. Fix the name, or exempt it in this screen WITH THE "
            f"REASON it is not a repo file.")

    resolved = [t for t in exempt if os.path.exists(t)]
    if resolved:
        errors.append(f"these exemptions now resolve to real files: {resolved}. "
                      f"An exemption that is no longer needed hides a check.")


def verify_no_unstampable_install(errors):
    """No live doc may tell a reader to `helm install ./chart`.

    IT DOES NOT WORK, AND NOTHING CAUGHT IT. `chart/Chart.yaml` carries
    `version: CHART_VERSION`, which the release tag stamps, so `helm` refuses the
    working tree outright:

        Error: validation: chart.metadata.version "CHART_VERSION" is invalid

    SIX live documents instructed exactly that, including README.md and
    docs/quickstart.md -- the first command a new reader runs. CI never noticed
    because every workflow installs a STAMPED STAGED COPY (`smoke.yaml`: "Stage a
    stamped chart"), never `./chart` as the docs said. The documented path and
    the tested path had diverged, and only the tested one was exercised.

    HISTORICAL_DOCS are exempt because they are accurate: Chart.yaml held a
    literal version until 2026-08-11, and the medium articles were written
    2026-05-28. They carry a reader warning instead of a rewrite.

    The screen is line-scoped: a finding is a `helm install`/`helm upgrade`
    naming `./chart` ON ONE LINE. overview.md's prose survives because it says
    "Installing from a clone instead (`./chart`)" without `helm install` beside
    it.

    BUT IT WILL FIRE ON A WARNING THAT QUOTES THE COMMAND VERBATIM. The
    historical articles' new note says "The `helm install ./chart` commands below
    no longer work" -- which matches, and would be flagged if those files were
    not exempt. That is deliberate: the alternative is to exempt lines containing
    "no longer" or similar, which hands any author a phrase that silences a real
    finding. A false positive here fails loudly and is fixed by splitting the
    line or fencing the command; an escape hatch fails silently forever.

    So: if this screen trips on correct prose, rephrase the prose. Do not widen
    the exemption.
    """
    cmd = re.compile(r"\bhelm\b[^\n]*\b(?:install|upgrade)\b[^\n]*\./chart\b")

    bad = 'helm upgrade --install nova-kog ./chart -n krateo-system \\'
    good_oci = ('helm upgrade --install nova-kog '
                'oci://ghcr.io/krateo-blueprints/charts/openstack-nova-operator-kog '
                '--version 0.1.5 \\')
    good_prose = "Installing from a clone instead (`./chart`) needs the placeholder stamped"
    if not cmd.search(bad):
        errors.append("validate.py: the unstampable-install screen fails its "
                      "known-BAD assertion; the six commands it was written for "
                      "would escape again")
        return
    if cmd.search(good_oci):
        errors.append("validate.py: the unstampable-install screen fires on the "
                      "CORRECT published-chart command")
        return
    if cmd.search(good_prose):
        errors.append("validate.py: the unstampable-install screen fires on "
                      "prose about ./chart, which overview.md needs in order to "
                      "explain the prerequisite")
        return

    hits = []
    for path in _docs():
        if path.replace(os.sep, "/") in HISTORICAL_DOCS:
            continue
        try:
            lines = open(path, encoding="utf-8").read().splitlines()
        except OSError as e:
            errors.append(f"{path}: unreadable, so it is NOT screened for an "
                          f"unstampable install ({e})")
            continue
        for i, line in enumerate(lines, 1):
            if cmd.search(line):
                hits.append(f"{path}:{i}")
    if hits:
        errors.append(
            f"these docs instruct an install from the working tree: {hits}. "
            f"`helm` refuses it -- chart.metadata.version \"CHART_VERSION\" is "
            f"invalid -- so the command fails on the reader's first attempt. "
            f"Point at the published chart with an explicit --version instead.")


def verify_documented_chart_version(errors):
    """Every documented `--version` for THIS chart must equal the newest tag.

    A RISK INTRODUCED BY A FIX. Six live docs told a reader to
    `helm install ./chart`, which `helm` refuses outright because Chart.yaml
    carries the CHART_VERSION placeholder. The fix points them at the published
    chart with an explicit `--version`, which works -- and creates a new way to
    be wrong: on the next release all six go stale, and a reader installs an
    older chart than the document claims to describe.

    `verify_documented_pin` guards the OASGEN pin, not ours, so nothing covered
    this. The newest SemVer tag is the only honest source: the release workflow
    derives the published version from exactly that.

    MATCHES OUR CHART ONLY. The docs also pin oasgen (0.49.0) and the kagent
    chart (0.9.6) with `--version`; those are different artifacts and this screen
    must not touch them, so the pattern requires this chart's name on the line.

    IT ERRORS RATHER THAN SKIPS when it cannot find a tag. A skip would read as a
    pass, which is the defect this repo has found seven times -- and a missing
    tag in CI has a specific cause worth naming (a shallow checkout), so the
    message names it.
    """
    tags = []
    try:
        out = subprocess.run(("git", "tag", "--list"), capture_output=True,
                             text=True, timeout=30)
        if out.returncode == 0:
            tags = [t for t in out.stdout.split()
                    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", t)]
    except (OSError, subprocess.SubprocessError) as e:
        errors.append(f"the chart-version screen could not run `git tag` ({e}), "
                      f"so it cannot compare. That is a failure, not a pass.")
        return
    if not tags:
        errors.append("the chart-version screen found NO SemVer tags, so it has "
                      "nothing to compare the docs against. In CI this usually "
                      "means a shallow checkout -- validate.yaml sets "
                      "fetch-depth: 0 for exactly this reason. Erroring rather "
                      "than skipping, because a skip would read as a pass.")
        return
    newest = max(tags, key=lambda t: tuple(int(x) for x in t.split(".")))

    pat = re.compile(r"openstack-nova-operator-kog\b[^\n]*?--version\s+"
                     r"([0-9]+\.[0-9]+\.[0-9]+)")

    # Prove the pattern discriminates before trusting the sweep.
    ours = ("helm install nova oci://ghcr.io/krateo-blueprints/charts/"
            "openstack-nova-operator-kog --version 0.1.5 \\")
    theirs = ("helm upgrade --install oasgen-provider oci://ghcr.io/"
              "krateo-platformops/charts/oasgen-provider --version 0.49.0 \\")
    if pat.findall(ours) != ["0.1.5"]:
        errors.append("validate.py: the chart-version screen's pattern does not "
                      "match this chart's own install command")
        return
    if pat.findall(theirs):
        errors.append("validate.py: the chart-version screen matches ANOTHER "
                      "chart's --version, so it would demand our tag of theirs")
        return

    stale, found = [], 0
    for path in _docs():
        if path.replace(os.sep, "/") in HISTORICAL_DOCS:
            continue
        try:
            lines = open(path, encoding="utf-8").read().splitlines()
        except OSError as e:
            errors.append(f"{path}: unreadable, so its chart version is NOT "
                          f"checked ({e})")
            continue
        for i, line in enumerate(lines, 1):
            for v in pat.findall(line):
                found += 1
                if v != newest:
                    stale.append(f"{path}:{i} says {v}")
    if found == 0:
        errors.append("the chart-version screen matched NO documented version "
                      "for this chart, which cannot be right -- the docs install "
                      "it in several places. The pattern has stopped reaching "
                      "the content, so its silence means nothing.")
        return
    if stale:
        errors.append(f"the newest tag is {newest}, but these docs pin a "
                      f"different version of this chart: {stale}. A reader would "
                      f"install an older chart than the document describes. "
                      f"Update them, or cut the release the docs already claim.")


def verify_no_credential_following_redirects(errors):
    """No curl in this repo may follow redirects. The credential would follow.

    `scripts/get-token.sh` POSTs the operator's password -- or application
    credential secret -- to `$OS_AUTH_URL`, which the operator supplies. It
    passes no `-L`, so curl stops at a 302 and the script exits 1 naming the
    missing X-Subject-Token. That safety currently rests on a DEFAULT, and a
    default is a weak place to keep a security property: adding `-L` for
    convenience would be a one-character change with no visible consequence
    until a redirecting endpoint appeared.

    WHY A SUBDOMAIN HOP IS THE REAL RISK, not an exotic one. Go's net/http
    documents it in `shouldCopyHeaderOnRedirect`: "Permit sending auth/cookie
    headers from foo.com to sub.foo.com", comparing url.Hostname() so the PORT is
    dropped as well. That is deliberate stdlib behaviour no Go release will
    change, and curl's --location-trusted has the same shape. oasgen-provider
    measured a Bearer token arriving at evil.krateo.test from krateo.test on a
    plain http.Client for exactly this reason (their #401).

    This chart ships no Go, so the stdlib row does not reach it -- but it ships a
    credential-posting shell script, which has the same exposure through `-L`.

    THE CONTROL MATTERS MORE THAN THE SWEEP HERE, because "no match" is the
    reassuring answer: a typo'd pattern produces it too. So the regexes are
    asserted against planted known-bad lines in both spellings before use.
    """
    follows = re.compile(r"curl\b[^|;&\n]*(?:\s-L\b|\s--location(?:-trusted)?\b)")

    bad_short = 'body=$(curl -sS -L -D "$tmp" --url "$u")'
    bad_long = 'body=$(curl -sS --location -D "$tmp")'
    bad_trusted = 'curl --location-trusted -H "X-Auth-Token: $t" "$u"'
    good = 'body=$(curl -sS -D "$tmp_headers" -X POST "$url" -d "$payload")'
    for label, line in (("-L", bad_short), ("--location", bad_long),
                        ("--location-trusted", bad_trusted)):
        if not follows.search(line):
            errors.append(f"validate.py: the redirect-following screen does not "
                          f"detect {label}, so its silence over the repo would "
                          f"mean nothing")
            return
    if follows.search(good):
        errors.append("validate.py: the redirect-following screen fires on a "
                      "curl that does NOT follow redirects, so it would flag "
                      "the correct form")
        return

    targets = sorted(set(glob.glob("scripts/*.sh")) | set(glob.glob("hack/*.sh"))
                     | set(glob.glob(".github/workflows/*.yaml")))
    if not targets:
        errors.append("the redirect-following screen found NO shell or workflow "
                      "files, so it swept nothing and would pass regardless")
        return

    def logical_commands(lines):
        """Join backslash continuations, so a multi-line invocation is one unit.

        A line-scoped match MISSED a real case. The probes run curl through
        `kubectl run --image=curlimages/curl:8.11.0 --`, with a trailing
        continuation and the flags on following lines, so the word `curl` and
        the flag are never on the same line. Planting `--location-trusted` there
        was NOT caught until this joined them -- found by mutation, after the
        single-line case passed and made the screen look finished.
        """
        out, buf, start = [], "", 1
        for i, line in enumerate(lines, 1):
            if line.lstrip().startswith("#"):
                continue   # the rationale comments quote the flags on purpose
            if not buf:
                start = i
            stripped = line.rstrip()
            if stripped.endswith("\\"):
                buf += stripped[:-1] + " "
                continue
            out.append((start, buf + stripped))
            buf = ""
        if buf:
            out.append((start, buf))
        return out

    hits = []
    for path in targets:
        try:
            lines = open(path, encoding="utf-8").read().splitlines()
        except OSError as e:
            errors.append(f"{path}: unreadable, so it is NOT screened for "
                          f"redirect-following curl ({e})")
            continue
        for i, command in logical_commands(lines):
            if follows.search(command):
                hits.append(f"{path}:{i}")
    if hits:
        errors.append(
            f"these invocations follow redirects: {hits}. A credential follows a "
            f"redirect to a SUBDOMAIN or another port -- documented, deliberate "
            f"behaviour in both net/http and curl --location-trusted -- so the "
            f"operator's password can reach a host nobody declared. Drop the "
            f"flag; a 302 should fail loudly instead.")

def verify_chart_icon_is_org_controlled(errors):
    """`chart/Chart.yaml`'s icon must be https and under a path this org controls.

    WHY THIS EXISTS, measured not hypothetical. The icon shipped in this chart
    was the OpenStack Nova mascot on `www.openstack.org`, and it returns **404**
    -- confirmed three ways (plain, browser User-Agent, and following redirects)
    with `https://www.openstack.org/` itself returning 200 as the control, so
    the site moved the file rather than being down. It was found one command
    before cutting the first tag in five releases, which would have published a
    dead icon into the live helm index and rendered it as broken art on the
    portal Marketplace card.

    NOTHING COULD HAVE CAUGHT IT EARLIER, and that is the point: a dead
    third-party URL is still a syntactically valid https icon, `helm lint` has
    no opinion on it, and this chart had not been published since 2026-08-19, so
    no card had ever tried to render it. The failure needed a release to become
    visible, which is the worst time to find it.

    THIS SCREEN DOES NOT CHECK THAT THE URL IS LIVE, and says so rather than
    implying coverage it lacks. A network fetch here would redden CI for
    reasons that have nothing to do with this repo -- a proxy, a rate limit, an
    outage -- and a screen that fails for unrelated reasons gets disabled, which
    is worse than one with a stated limit.

    WHAT IT CHECKS INSTEAD is the property that actually prevents a recurrence:
    the icon lives under a path the Krateo org controls, so it cannot rot
    because a third party reorganised their CDN. That is fully checkable
    offline and is the real lesson of the 404.

    IT ALSO REFUSES THE UNHYPHENATED ORG. `raw.githubusercontent.com/
    krateoplatformops/...` (no hyphen) resolves **200** through GitHub's
    org-rename redirect, so it fails silently and forever. The hyphenated
    `krateo-platformops` is the live org. See the hyphen-trap note in the repo
    docs -- this is the same trap in a different field.

    HOW WIDESPREAD, WITH THE SELECTOR NAMED, because an earlier version of this
    docstring said "385 entries" with no selector and I quoted "roughly 39" for
    the same thing in the same hour. Both were real; they count different
    populations, and without the selector written beside them they looked like a
    contradiction. Measured in the live blueprints index on 2026-11-10:

        37  distinct charts whose NEWEST entry's icon is unhyphenated
       289  distinct charts where ANY version mentions it
       397  raw occurrences, i.e. one per affected chart VERSION

    THE FIRST IS THE DECISION-RELEVANT ONE: portal #383 reads only the newest
    entry, so 37 is the number of charts actually rendering a redirected icon
    today. The other two describe accumulated history that no publish can undo,
    because a helm index keeps every version it has ever served.

    AND ALL THREE DRIFT. The raw count was 385 when first measured and 397 a few
    hours later; charts publish continuously. So treat any figure here as a
    point-in-time reading, not a fact -- which is the reason the selector matters
    more than the number.
    """
    path = "chart/Chart.yaml"
    try:
        raw = open(path, encoding="utf-8").read()
    except OSError as e:
        errors.append(f"{path}: unreadable, so the chart icon is NOT checked "
                      f"({e}). That is a failure, not a pass.")
        return

    OK_PREFIXES = ("https://raw.githubusercontent.com/krateo-platformops/",
                   "https://raw.githubusercontent.com/krateo-blueprints/")
    DEAD_ORG = "krateoplatformops"

    def judge(url):
        """Return a complaint about `url`, or None if it is acceptable.

        The real check and the self-tests below both call THIS, so a passing
        self-test is evidence about the code that runs. A self-test that
        exercised a different expression would prove nothing -- which is a
        mistake this repo has made and does not intend to repeat.
        """
        if not url:
            return "the chart declares no icon, which the channel gate requires"
        if not url.startswith("https://"):
            return f"the icon is not https: {url!r}"
        if DEAD_ORG in url:
            return (f"the icon names the UNHYPHENATED org, which resolves 200 "
                    f"through a rename redirect and so fails silently: {url!r}")
        if not url.startswith(OK_PREFIXES):
            return (f"the icon is not under a Krateo-controlled path, so it can "
                    f"rot without warning -- the previous value did exactly "
                    f"that (openstack.org, HTTP 404): {url!r}")
        return None

    # Prove the judgement discriminates before trusting it on the real value.
    cases = (
        ("https://www.openstack.org/themes/openstack/images/project-mascots/"
         "Nova/OpenStack_Project_Nova_vertical.png", True),
        ("https://raw.githubusercontent.com/krateoplatformops/krateo/main/"
         "docs/media/logo.svg", True),
        ("http://raw.githubusercontent.com/krateo-platformops/.github/main/"
         "brand/logo.svg", True),
        ("", True),
        ("https://raw.githubusercontent.com/krateo-platformops/.github/main/"
         "brand/logo.svg", False),
        ("https://raw.githubusercontent.com/krateo-blueprints/"
         "openstack-nova-operator-kog/main/docs/images/icon.svg", False),
    )
    for url, want_complaint in cases:
        got = judge(url)
        if want_complaint and got is None:
            errors.append(f"validate.py: the icon screen ACCEPTS a value it "
                          f"must refuse ({url!r}), so its silence means "
                          f"nothing")
            return
        if not want_complaint and got is not None:
            errors.append(f"validate.py: the icon screen refuses a legitimate "
                          f"value ({url!r}): {got}")
            return

    m = re.search(r"^icon:\s*(\S*)\s*$", raw, re.M)
    if not m:
        errors.append(f"{path}: no `icon:` key found. The blueprints channel "
                      f"gate requires an https icon, so its absence fails the "
                      f"publish rather than defaulting to anything.")
        return
    complaint = judge(m.group(1))
    if complaint:
        errors.append(f"{path}: {complaint}")

    # A self-hosted icon must point at a file that is actually committed.
    url = m.group(1)
    own = ("https://raw.githubusercontent.com/krateo-blueprints/"
           "openstack-nova-operator-kog/main/")
    if url.startswith(own):
        rel = url[len(own):]
        if rel and not os.path.exists(rel):
            errors.append(f"{path}: the icon is self-hosted at {rel!r}, but no "
                          f"such file is committed, so the published icon would "
                          f"404 exactly as the previous one did.")


def verify_no_header_parameters_in_assets(errors):
    """No OAS asset may declare an `in: header` PARAMETER.

    WHY, and it is a structural immunity worth keeping rather than a style rule.
    The openstack-ironic-operator-kog session measured this in their own repo:
    a required `in: header` PARAMETER can be **dropped with no error** -- the
    request goes out without it, and their workaround is a static per-verb header
    on the RestDefinition instead.

    THE MECHANISM, VERIFIED HERE IN THE PROVIDER'S SOURCE. This paragraph has had
    three states -- asserted, retracted, restored -- so it is written from a read
    of the code rather than from anyone's summary, including mine:

        rest-dynamic-controller/internal/tools/client/builder/builder.go
        applyConfigSpec, BEFORE commit 1da37f6:
            process("path",    req.Parameters)
            process("query",   req.Query)
            process("headers", req.Headers)     <- PLURAL
            process("cookies", req.Cookies)     <- PLURAL
        AFTER:
            process("header",  req.Headers)
            process("cookie",  req.Cookies)

    The key is passed to `unstructured.NestedMap(configSpec, key, action)`, and
    the Configuration schema is generated from the OpenAPI location verbatim --
    and OpenAPI's locations are SINGULAR. So `path` and `query` worked by
    coincidence of spelling, while every `configurationFields` header or cookie
    was generated into the CRD and then never applied, with no error. The fix's
    own commit message says it: "generated into the Configuration CRD and then
    NEVER APPLIED".

    THE VERSION WAS MISATTRIBUTED, NOT THE MECHANISM. Measured by ancestry with
    controls: `1da37f6` is absent at 0.43.0 and present from 0.44.0 (#304/#338,
    2026-10-31). A SECOND and distinct defect -- a *spec-field* header never
    populated from the CR -- is `4acbf8a`, absent at 0.49.0 and present from
    0.50.0 (#305/#392). THIS CHART'S PIN, 0.52.0, CARRIES BOTH. The two fail in
    opposite directions, which is the discriminator: the configurationFields one
    is SENT without the header and logs nothing, the spec-field one is never sent
    and logs `missing header: <name>`. Tracked upstream as oasgen-provider#561.

    The screen is kept, and would be kept even with the mechanism in doubt: a
    measured symptom is enough to avoid the shape, and oasgen-provider#391 gives
    an independent reason -- its missing-header check exempts a required header
    parameter only when the name contains "authorization", so a required
    `X-Auth-Token` parameter would be refused outright.

    THIS CHART IS IMMUNE FOR A STRUCTURAL REASON, NOT BY LUCK. Its credential is
    a `type: apiKey` SECURITY SCHEME, which the provider turns into
    `spec.authentication.apiKey.{tokenRef,header}` and attaches through its own
    auth path. A security scheme never enters the parameter-header code path at
    all. That is a different claim from "we grepped and found nothing", which
    describes today and stops being true the moment somebody adds a parameter.

    So this screen makes the structural property enforceable: an `in: header`
    inside `securitySchemes` is fine, and one anywhere else is refused with the
    reason, so whoever adds it finds out here rather than from a silently
    unsent header.

    THE FILES DO NOT PARSE AS YAML, so this is textual. OAS path templates like
    `/os-quota-sets/{tenant_id}:` are flow mappings used as keys, and
    `yaml.safe_load` raises "while constructing a mapping" on every one of these
    assets. A screen that tried to parse them would error on all six and the
    error would read as a finding.

    THE DISCRIMINATOR is a sibling `type: apiKey` at the same indentation. A
    security scheme has `type`, `in` and `name` as siblings; a header parameter
    has `name`, `in`, `required` and `schema`, with its `type` nested under
    `schema`. Siblings are read by indentation rather than by looking upward for
    a section header, because `securitySchemes` and a path-level `parameters`
    block sit at different depths in different files.
    """
    import glob

    def judge(text):
        """Return a list of (line_no, why) for every offending `in: header`.

        The real sweep and the self-tests below both call THIS, so a passing
        self-test is evidence about the code that runs.
        """
        out, lines = [], text.splitlines()
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped != "in: header":
                continue
            indent = len(line) - len(line.lstrip())
            # Walk the sibling block: same indent, until the block ends.
            sibling_type_apikey = False
            for j in range(i - 1, -1, -1):          # upward
                ln = lines[j]
                if not ln.strip() or ln.strip().startswith("#"):
                    continue
                ind = len(ln) - len(ln.lstrip())
                if ind < indent:
                    break
                if ind == indent and ln.strip() == "type: apiKey":
                    sibling_type_apikey = True
            for j in range(i, len(lines)):           # downward
                ln = lines[j]
                if not ln.strip() or ln.strip().startswith("#"):
                    continue
                ind = len(ln) - len(ln.lstrip())
                if ind < indent:
                    break
                if ind == indent and ln.strip() == "type: apiKey":
                    sibling_type_apikey = True
            if not sibling_type_apikey:
                out.append((i, "an `in: header` with no sibling `type: apiKey`, "
                               "so it is a header PARAMETER, not a security "
                               "scheme"))
        return out

    # Prove the judgement discriminates before trusting it on the real assets.
    scheme = ("components:\n"
              "  securitySchemes:\n"
              "    apiKey:\n"
              "      type: apiKey\n"
              "      in: header\n"
              "      name: X-Auth-Token\n")
    param = ("paths:\n"
             "  /thing:\n"
             "    parameters:\n"
             "      - name: X-Thing\n"
             "        in: header\n"
             "        required: true\n"
             "        schema:\n"
             "          type: string\n")
    if judge(scheme):
        errors.append("validate.py: the header-parameter screen refuses a "
                      "legitimate apiKey security scheme, so it would fail on "
                      "every asset this chart ships")
        return
    if not judge(param):
        errors.append("validate.py: the header-parameter screen ACCEPTS a real "
                      "header parameter, so its silence means nothing")
        return
    if not judge(scheme + param):
        errors.append("validate.py: the header-parameter screen misses a "
                      "parameter when a scheme is also present, which is the "
                      "only shape these assets could actually take")
        return

    assets = sorted(glob.glob(os.path.join("chart", "assets", "*.yaml")))
    if not assets:
        errors.append("the header-parameter screen found NO files under "
                      "chart/assets/, so it swept nothing. An empty sweep "
                      "passes vacuously; refusing.")
        return
    hits, schemes = [], 0
    for path in assets:
        try:
            text = open(path, encoding="utf-8").read()
        except OSError as e:
            errors.append(f"{path}: unreadable, so it is NOT screened for "
                          f"header parameters ({e})")
            continue
        schemes += text.count("in: header") - len(judge(text))
        for line_no, why in judge(text):
            hits.append(f"{path}:{line_no} {why}")
    if schemes == 0:
        errors.append("the header-parameter screen matched NO `in: header` at "
                      "all across chart/assets/. Every asset declares an apiKey "
                      "security scheme with one, so the pattern has stopped "
                      "reaching the content and its silence means nothing.")
        return
    if hits:
        errors.append(
            f"these assets declare an `in: header` PARAMETER: {hits}. "
            f"oasgen-provider emits the generated property as `header` while "
            f"rest-dynamic-controller reads `headers`, so the lookup misses and "
            f"the value is DROPPED WITH NO ERROR -- measured by the "
            f"openstack-ironic-operator-kog session. This chart's credential is "
            f"a security scheme precisely so it never enters that path. If you "
            f"need a header on the wire, pin it as a static per-verb header on "
            f"the RestDefinition instead.")


def _registered_checks():
    return (verify_composition_apiversion,
                  verify_documented_pin,
                  verify_no_builtin_shadowing,
                  verify_auth_scheme_consistency,
                  verify_no_under_floor_recommendation,
                  verify_cr_apiversion_consistency,
                  verify_python_compiles_without_warnings,
                  verify_composition_spec_against_schema,
                  verify_every_script_is_exercised,
                  verify_doc_filenames_exist,
                  verify_no_unstampable_install,
                  verify_documented_chart_version,
                  verify_no_credential_following_redirects,
                  verify_release_dry_run_gates_every_mutation,
                  verify_chart_icon_is_org_controlled,
                  verify_no_header_parameters_in_assets,
                  verify_no_conflict_markers,
                  verify_doc_link_fragments_resolve,
                  verify_no_544_refused_path_forms)


def verify_no_conflict_markers(errors):
    """No tracked text file may contain a git conflict marker.

    WHY THIS EXISTS, and it is the plainest instance in this file. On
    2026-11-10 a `git merge origin/main` on a docs branch conflicted, printed
    its failure in Italian, and I edited the file and committed anyway --
    pushing all three marker lines into docs/architecture.md. **validate.py
    returned rc=0 on that tree**, and so did every other gate: markdown with
    conflict markers is still valid markdown, the YAML was untouched, and no
    test reads prose. The defect went past a green suite into a document a
    reader would act on, and what caught it was opening the file by hand.

    A gate that cannot see a half-merged file is not protecting the documents
    it claims to cover.

    Matched ANCHORED at line start, which is what git writes, and at git's
    exact widths (seven characters, then a space or end of line). Otherwise a
    diff quoted inside a fenced block would trip it. This file is skipped
    because this docstring discusses the markers.
    """
    pattern = re.compile(r"^(?:<{7}|>{7})[ \t]|^={7}$")
    exts = (".md", ".yaml", ".yml", ".py", ".sh", ".json", ".txt")
    skip = {"scripts/validate.py"}
    hits = []
    scanned = 0
    # os.walk, NOT glob("**/*"), and the reason is a gap this check found in
    # itself: Python's glob skips hidden names, so `**/*.yaml` never descends
    # into `.github/` -- the directory where a conflict marker would do the most
    # damage, since a half-merged workflow is a half-merged release gate. The
    # arm that exposed it used a dotfile fixture and silently scanned nothing.
    candidates = []
    for dirpath, dirnames, filenames in os.walk("."):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__", "node_modules")]
        for name in filenames:
            if name.endswith(exts):
                candidates.append(os.path.relpath(os.path.join(dirpath, name), "."))
    for rel in sorted(set(candidates)):
        if rel.startswith(".git/") or rel in skip:
            continue
        if not os.path.isfile(rel):
            continue
        try:
            with open(rel, encoding="utf-8") as fh:
                text = fh.read()
        except (UnicodeDecodeError, OSError):
            continue
        scanned += 1
        for n, line in enumerate(text.splitlines(), 1):
            if pattern.match(line):
                hits.append("{}:{}: {}".format(rel, n, line[:40]))
    if scanned == 0:
        errors.append(
            "verify_no_conflict_markers scanned 0 files, so it would pass on a "
            "tree full of markers. VOID, not a pass."
        )
        return
    for h in hits:
        errors.append("git conflict marker left in a tracked file -- {}".format(h))


def _github_heading_slugs(markdown_text):
    """Heading anchors as GitHub derives them, plus its duplicate suffixes.

    GitHub lowercases, drops everything that is not a letter, digit, space or
    hyphen, then turns spaces into hyphens. A repeated slug gets -1, -2, ... in
    document order. Both rules matter here: this repo has headings with
    parentheses and backticks, and two sections could easily collide.
    """
    slugs = []
    seen = {}
    in_fence = False
    for line in markdown_text.split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line.startswith("#"):
            continue
        title = line.lstrip("#").strip()
        base = re.sub(r"[^a-z0-9 -]", "", title.lower()).replace(" ", "-")
        if base in seen:
            seen[base] += 1
            slugs.append("{}-{}".format(base, seen[base]))
        else:
            seen[base] = 0
            slugs.append(base)
    return slugs


def verify_doc_link_fragments_resolve(errors):
    """A `](file.md#anchor)` link must name a heading that exists.

    WHY THIS EXISTS, and it was nearly shipped the hour it was written. A commit
    whose whole purpose was to stop three sentences asserting a mechanism
    without pointing at its source added three pointers to
    `architecture.md#the-request-path` -- an anchor that does not exist. The
    sibling screen verify_doc_filenames_exist passed them, because it checks
    backticked FILENAMES and says nothing about link FRAGMENTS.

    The failure is silent in the worst way: a dead anchor does not 404. The
    browser scrolls to the top of the page, so a reader lands on a real document
    and simply does not find the thing they were sent for, and concludes the
    reference was vague rather than broken.

    Same-repo markdown only. External URLs are never fetched, so this screen
    stays offline and cannot fail on network conditions.
    """
    import posixpath
    doc_files = sorted(glob.glob("**/*.md", recursive=True)) + sorted(
        glob.glob(os.path.join(".github", "**", "*.md"), recursive=True))
    slug_cache = {}
    checked = 0
    for src in sorted(set(doc_files)):
        try:
            with open(src, encoding="utf-8") as fh:
                text = fh.read()
        except (UnicodeDecodeError, OSError):
            continue
        for target, frag in re.findall(r"\]\(([^)\s#]*\.md)#([A-Za-z0-9_.-]+)\)", text):
            resolved = posixpath.normpath(
                posixpath.join(posixpath.dirname(src.replace(os.sep, "/")), target))
            if not os.path.isfile(resolved):
                continue   # verify_doc_filenames_exist owns the missing-file case
            if resolved not in slug_cache:
                with open(resolved, encoding="utf-8") as fh:
                    slug_cache[resolved] = _github_heading_slugs(fh.read())
            checked += 1
            if frag not in slug_cache[resolved]:
                near = [x for x in slug_cache[resolved] if frag.split("-")[0] in x][:2]
                errors.append(
                    "{}: link points at {}#{}, and that heading does not exist. "
                    "A dead anchor does not 404 -- the reader lands at the top of "
                    "a real page and concludes the reference was vague.{}".format(
                        src, resolved, frag,
                        " Nearest: {}".format(near) if near else ""))
        # same-file fragments
        own = _github_heading_slugs(text)
        for frag in re.findall(r"\]\(#([A-Za-z0-9_.-]+)\)", text):
            checked += 1
            if frag not in own:
                errors.append(
                    "{}: same-file link points at #{}, which is not a heading in "
                    "that file.".format(src, frag))
    if checked == 0:
        errors.append(
            "verify_doc_link_fragments_resolve checked 0 fragments, so it would "
            "pass on a document full of dead anchors. VOID, not a pass."
        )


def _refused_544_form(value):
    """True when a JSONPath value uses a form oasgen 0.57.0 (#544) refuses.

    SCOPE, from oasgen-provider's own differential test, 2026-11-11 -- and it is
    NARROWER than the advice that circulated first:

        REFUSED    a.0       against an ARRAY   dotted index        -- decidable
                   a['0']    against an ARRAY   quoted key as index -- decidable
                   a[0]      against a MAP      index into a map    -- NOT decidable
        UNCHANGED  a[0]      against an ARRAY   works
                   a['0']    against a MAP      works
                   spec.a.b  plain traversal, any depth
                   [?k=v]    predicate selectors

    So this screen flags ONLY the two forms decidable from the path text. It
    deliberately does NOT flag `a[0]`: against an array that is the working
    form, and against a map it is refused but no static screen can tell which,
    because it depends on the data's shape at that point. An earlier regex of
    mine matched any bracketed index and would have raised a false alarm on the
    working form; it reported 0 only because this corpus has none of either.
    """
    import re as _re
    # SEGMENT-WISE, not a regex over the whole string, and the self-test below is
    # why: `(^|\.)[0-9]+(\.|$)` flagged `2.72`, a Nova microversion, because it
    # only asked whether SOME segment was numeric. A version string is numeric in
    # EVERY segment; a path has non-numeric ones too. So require both.
    if _re.search(r"\[\s*'[0-9]+'\s*\]", value):           # a['0']
        return "quoted numeric key used as an index"
    segments = value.split(".")
    if len(segments) < 2:
        return None
    numeric = [x for x in segments if x.isdigit()]
    named = [x for x in segments if x and not x.isdigit()]
    if numeric and named:
        return "dotted numeric segment ({})".format(numeric[0])
    return None


CR_SCALAR_KEYS = ("inCustomResource", "nameFromCustomResource",
                  "keyFromCustomResource", "equalsFromCustomResource",
                  "customResourceFieldPresent", "customResourceFieldAbsent")
CR_LIST_KEYS = ("identifiers", "additionalStatusFields")


def _scan_544_lines(path, lines, errors):
    """Scan one document's lines for CR-reading paths in a refused form.

    Split out of the check so the WALKER can be tested on a planted fixture.
    Returns the number of paths examined.
    """
    checked = 0
    in_list, list_indent, list_key = False, 0, ""
    for n, line in enumerate(lines, 1):
        indent = len(line) - len(line.lstrip())
        m = re.match(r"\s*(" + "|".join(CR_SCALAR_KEYS) + r")\s*:\s*(.+?)\s*$", line)
        if m:
            in_list = False
            value = m.group(2).strip().strip("'\"")
            checked += 1
            why = _refused_544_form(value)
            if why:
                errors.append(
                    "{}:{}: {} = {!r} uses a path form oasgen 0.57.0 refuses "
                    "({}). It works on the current pin and breaks on upgrade "
                    "(#544).".format(path, n, m.group(1), value, why))
            continue
        m2 = re.match(r"\s*(" + "|".join(CR_LIST_KEYS) + r")\s*:\s*$", line)
        if m2:
            in_list, list_indent, list_key = True, indent, m2.group(1)
            continue
        if in_list:
            m3 = re.match(r"\s*-\s*(.+?)\s*$", line)
            if m3 and indent > list_indent:
                value = m3.group(1).strip().strip("'\"")
                checked += 1
                why = _refused_544_form(value)
                if why:
                    errors.append(
                        "{}:{}: {}[] entry {!r} uses a path form oasgen 0.57.0 "
                        "refuses ({}).".format(path, n, list_key, value, why))
            elif line.strip() and indent <= list_indent:
                in_list = False
    return checked


def verify_no_544_refused_path_forms(errors):
    """No CR-reading JSONPath in the assets may use a form 0.57.0 refuses.

    WHY A SCREEN RATHER THAN A ONE-OFF CHECK. The risk is on the UPGRADE, not on
    the tag existing: these paths work today and stop working when the pin moves
    to 0.57.0. A manual screen protects the upgrade I am about to do; a screen in
    CI protects the one after it, and the one someone else does.

    Textual, not YAML-parsed, for the same reason as
    verify_no_header_parameters_in_assets: the assets carry Helm template syntax
    (`{{ include ... }}`) and are not valid YAML. A YAML-based screen on them
    VOIDs -- measured.

    Scoped to the keys that read INTO the custom resource, because an unscoped
    search for a dotted numeric segment matches `/v2.1/flavors` and every
    microversion in the corpus.

    THE KEY LIST WAS WRONG AND A CROSS-CHECK FOUND IT. This docstring used to
    claim the list was "oasgen's full surface". On 2026-11-11 I compared it
    against oasgen-provider's own `check544.py` and mine was MISSING
    `equalsFromCustomResource` and `customResourceFieldAbsent` -- two of the
    three keys they independently reported getting wrong. My census had
    reported "39 CR-reading paths across 18 files" and a clean result, and that
    clean result was true of the four keys I happened to list. A screen scoped
    by an enumerated key list reports exactly as clean as its list is complete,
    and nothing in the output distinguishes "no refused forms" from "did not
    look". So the completeness of this tuple is now asserted below rather than
    claimed here: every key gets a planted refused value and must be caught.

    Still deliberately NOT flagged: `a[0]`, which 0.57.0 refuses against a MAP
    and accepts against an ARRAY. That depends on the schema the path resolves
    against, which is not in the RestDefinition -- so a clean run here means
    "no TEXT-DECIDABLE refusal", not a clean bill of health.
    """
    files = sorted(glob.glob(os.path.join("chart", "assets", "*.yaml")) +
                   glob.glob(os.path.join("chart", "templates", "*.yaml")))
    if not files:
        errors.append("verify_no_544_refused_path_forms found no assets to screen. "
                      "VOID, not a pass.")
        return
    checked = 0
    for path in files:
        with open(path, encoding="utf-8", errors="replace") as fh:
            checked += _scan_544_lines(path, fh.read().splitlines(), errors)
    if checked == 0:
        errors.append(
            "verify_no_544_refused_path_forms examined 0 CR-reading paths across "
            "{} asset file(s), so it would pass on a corpus full of refused "
            "forms. VOID, not a pass.".format(len(files)))
        return
    # Self-test the judge, with the forms that must and must NOT fire. The
    # screen's own 0 is only as good as this.
    must_fire = ("status.items.0.id", "a.0", "spec.a['0'].b", "status.0")
    must_not = ("status.aggregate.id", "spec.a.b.c", "a[0]", "a['x']",
                "spec.items[?name=x].id", "/v2.1/flavors", "2.72")
    for v in must_fire:
        if not _refused_544_form(v):
            errors.append("_refused_544_form missed {!r}, which 0.57.0 refuses "
                          "-- the screen above cannot be trusted.".format(v))
    for v in must_not:
        if _refused_544_form(v):
            errors.append("_refused_544_form flagged {!r}, which 0.57.0 accepts "
                          "-- a false alarm would send a reader to inspect a "
                          "working path.".format(v))
    # PLANTED CONTROL FOR THE WALKER, one needle per key. The judge self-test
    # above proves the CLASSIFIER works; it says nothing about whether the
    # walker ever hands it the value sitting under a given key. A key silently
    # not reached is exactly the defect the oasgen cross-check found, and it
    # reads identically to a clean corpus.
    # THE PLANTED ARM BELOW CANNOT CATCH A DELETED KEY, and saying so matters
    # more than the arm. Its fixture is built FROM these tuples, so dropping a
    # key drops the needle with it and the arm still passes -- a vacuous test,
    # which is worse than none because it licenses the word "controlled". What
    # it does catch is a key that is listed but unreachable: wrong regex, a list
    # key handled as a scalar, a walker that returns early. Mutation-tested by
    # excluding one key inside _scan_544_lines, which names the key it lost.
    #
    # A deleted key needs a source OUTSIDE the tuple. That source is
    # oasgen-provider's own check544.py, cross-checked by hand on 2026-11-11
    # (12/12 agreement on the classifier rows, and that is where these two keys
    # came from). A count is the cheapest honest proxy: edit the tuples and CI
    # fails until someone re-runs that comparison and updates the number
    # deliberately. It is a canary, not a proof.
    if len(CR_SCALAR_KEYS) != 6 or len(CR_LIST_KEYS) != 2:
        errors.append(
            "the #544 key list changed ({} scalar + {} list, expected 6 + 2). "
            "This screen reports exactly as clean as its key list is complete, "
            "so re-run the comparison against oasgen-provider's check544.py "
            "before updating these numbers -- do not just make CI green."
            .format(len(CR_SCALAR_KEYS), len(CR_LIST_KEYS)))
    planted = []
    for k in CR_SCALAR_KEYS:
        planted.append("  {}: spec.a.0.b".format(k))
    for k in CR_LIST_KEYS:
        planted.append("  {}:".format(k))
        planted.append("    - spec.a.0")
    probe = []
    n_examined = _scan_544_lines("<planted>", planted, probe)
    want = len(CR_SCALAR_KEYS) + len(CR_LIST_KEYS)
    if len(probe) != want:
        caught = set()
        for msg in probe:
            for k in CR_SCALAR_KEYS + CR_LIST_KEYS:
                if k in msg:
                    caught.add(k)
        missed = [k for k in CR_SCALAR_KEYS + CR_LIST_KEYS if k not in caught]
        errors.append(
            "the #544 walker caught {} of {} planted refused paths -- it does "
            "not reach: {}. Every key it cannot reach is a key the clean result "
            "above says nothing about.".format(len(probe), want,
                                               ", ".join(missed) or "unknown"))
    if n_examined != want:
        errors.append(
            "the #544 walker examined {} planted paths, expected {}. The "
            "fixture and the walker disagree, so the control is not measuring "
            "the walker.".format(n_examined, want))


def verify_release_dry_run_gates_every_mutation(errors):
    """Every mutating step in release-tag.yaml must reference DRY_RUN.

    WHY THIS EXISTS, and it is not hypothetical. `release-tag.yaml` gained a
    `workflow_dispatch` with `dry_run` so the release path could be exercised
    without burning a version. `helm push` was gated. The step that runs
    `gh release create` was NOT -- it had zero DRY_RUN references -- and
    `gh release create` creates a TAG. The first dry run therefore produced a
    real release 0.1.6 and tag 0.1.6 for a chart that was never pushed: a
    CompositionDefinition pinning a version absent from the registry, which is
    the defect PR #11 fixed once already, plus a repo-wide red `validate`
    because the newest tag stopped matching the documented version.

    "Dry run" is a property of EVERY mutating step, not of the one you had in
    mind when you added the flag. So this asserts the property rather than
    trusting the next author to remember it.

    WHAT COUNTS AS MUTATING is a deliberately short, explicit list rather than a
    clever heuristic: a pattern that tries to infer mutation would quietly stop
    matching when someone writes the same call differently, and this screen's
    whole value is that it cannot miss one silently. A new mutating verb must be
    added here on purpose -- and the VERBS list being wrong is caught by the
    self-test below, which requires each verb to be found somewhere in the file.
    """
    path = ".github/workflows/release-tag.yaml"
    try:
        raw = open(path, encoding="utf-8").read()
    except OSError as e:
        errors.append(f"{path}: unreadable, so the dry-run gate is NOT checked "
                      f"({e}). A file this screen cannot read is a failure, not "
                      f"a skip.")
        return
    try:
        doc = yaml.safe_load(raw)
    except Exception as e:  # noqa: BLE001 - any parse failure is a failure here
        errors.append(f"{path}: does not parse as YAML ({e}), so its steps "
                      f"cannot be inspected. VOID, not a pass.")
        return

    VERBS = ("gh release create", "gh release upload", "helm push")

    # SELF-TEST FIRST: if a verb no longer appears anywhere, this screen has
    # silently stopped checking that verb. A green result would then mean "the
    # pattern missed it", which is the failure mode being guarded against.
    missing_verbs = [v for v in VERBS if v not in raw]
    if missing_verbs:
        errors.append(
            f"{path}: the dry-run screen looks for {missing_verbs} and the file "
            f"contains none of them. Either the release path no longer mutates "
            f"that way -- in which case update VERBS deliberately -- or this "
            f"screen is now checking nothing. VOID, not a pass.")
        return

    jobs = (doc.get("jobs") or {})
    ungated = []
    for job_name, job in jobs.items():
        for step in (job.get("steps") or []):
            run = step.get("run") or ""
            if not any(v in run for v in VERBS):
                continue
            # COMMENTS DO NOT GATE ANYTHING, and checking the raw body for the
            # string "DRY_RUN" was satisfied by the comment explaining the bug.
            # The first version of this screen passed WITH the defect injected:
            # a guard that reads prose as if it were code, inside a guard built
            # to stop a step being ungated. Strip comment lines first, so this
            # asks about executable text only.
            code = "\n".join(ln for ln in run.splitlines()
                              if not ln.lstrip().startswith("#"))
            # And require a CONDITIONAL on it, not a mention: `echo $DRY_RUN`
            # would satisfy a substring test while gating nothing.
            gated = any(pat in code for pat in
                        ('if [ "${DRY_RUN}"', 'if [ "$DRY_RUN"',
                         'if [[ "${DRY_RUN}"', 'if [[ "$DRY_RUN"'))
            if not gated:
                ungated.append(f"{job_name} / {step.get('name') or '(unnamed)'}")
    if ungated:
        errors.append(
            "these release steps MUTATE something (a release, a tag or the "
            "registry) and carry no `if [ \"${{DRY_RUN}}\" = \"true\" ]` "
            "conditional in their executable text, so a dry run would perform "
            "them: {}. A COMMENT mentioning DRY_RUN does not count, and neither "
            "does echoing it -- the first version of this screen accepted both. "
            "`gh release create` also creates a tag. Gate them, or the dispatch "
            "is not a dry run.".format(ungated))


def _verify_every_check_is_registered(errors):
    '''Every `verify_*` defined in this module must be in _registered_checks().

    A screen that exists and is never reached is the defect this whole file
    exists to prevent, and until this was added it was reachable right here:
    deleting a check from the registration tuple left "all checks passed",
    because each screen's self-tests live INSIDE the screen and a screen that
    is never called never runs them. Verified by mutation -- removing
    verify_auth_scheme_consistency from the tuple produced exit 0.

    Credit: oasgen-provider, who hit the identical shape fixing #361. Every
    test they wrote for a merge helper called it directly, so the suite stayed
    green when they reverted the call site -- which was the original bug. They
    found it by mutating each half separately; the first mutation passing had
    looked like adequate coverage.
    '''
    defined = {n for n, v in globals().items()
               if n.startswith("verify_") and callable(v)}
    registered = {f.__name__ for f in _registered_checks()}
    unregistered = defined - registered
    if unregistered:
        errors.append(
            "these screens are DEFINED but never registered, so they never run "
            "and their self-tests never execute: {}. A screen that exists and "
            "is not reached is worse than an absent one -- it reads as "
            "coverage.".format(sorted(unregistered)))
    stale = registered - defined
    if stale:
        errors.append("registered but not defined: {}".format(sorted(stale)))
    if not registered:
        errors.append("no checks registered at all -- a failure, not a pass")


def main():
    errors = []
    # Called explicitly, NOT through the tuple it validates: a check on the
    # registration cannot depend on being registered.
    _verify_every_check_is_registered(errors)
    for check in _registered_checks():
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
