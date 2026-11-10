#!/usr/bin/env python3
"""Every served RESOURCE version must have a controller watching that version.

THE DEFECT THIS CATCHES, measured by the openstack-ironic-operator-kog session
on a live cluster: a composition pinned a child apiVersion that the CRD SERVED
but no controller WATCHED. The object applied successfully, `kubectl apply
--dry-run=server` accepted it -- the CRD genuinely serves that version -- and
nothing reconciled it. No error, no event, no failed condition, for 45 minutes.
Fixing the pin was not enough either: `krateo.io/composition-version` is stamped
at creation, so the existing child stayed labelled and stayed inert.

IT IS NOT PREVENTED UPSTREAM, which is why this check exists rather than
trusting the provider. From the oasgen-provider session's own source read,
filed as their #560:

    one RestDefinition per kind        enforced
    one Deployment per RestDefinition  name carries NO version
    its LabelSelector                  scoped to the CURRENT render's version
    a CRD may serve SEVERAL versions   accumulated across OAS bumps, and
                                       pruneStaleServedVersions never retires a
                                       version that HAS INSTANCES

So a version with instances is never pruned and never watched -- two rules each
correct alone. Three comments in their repo assert "Every served version runs
its own controller"; that describes the label selector's purpose, not a
per-version deployment. The sentence is why nobody noticed.

THE EXCLUSION IS THE WHOLE DIFFERENCE BETWEEN A CHECK AND NOISE. The
`<Kind>Configuration` CRDs serve a version that nothing controls, CORRECTLY --
they are resolved through `configurationRef` and never reconciled by a
label-selected watch. A sweep that flags them yields one false positive per kind
against however many real findings, and the operator chases the harmless half.
That is the github-provider-kog session's 28-versus-32 in a third field.

JOIN KEY: the Deployment's `app.kubernetes.io/version` label, which carries the
GVR's version -- the same value the CRD is rendered with, so `v2-1` and not
`2.1`. Confirmed from the provider's source (`templates.go`, `opts.Version`) and
preferred over the RestDefinition's `status.resource.apiVersion`, which is
`group/version` and records what the provider PUBLISHED rather than what a
Deployment is WATCHING. This check compares the apiserver's served list against
Deployments that actually exist, so it also catches a controller deleted,
scaled away or never deployed -- which no provider-side condition would notice.

TWO ARMS, BECAUSE ONE IS NOT ENOUGH -- found by mutation, not by design. The
first version keyed the join on the VERSION alone: "does some controller watch
v2-1". All six of this chart's resource kinds serve the SAME version, so
deleting one controller left five still answering for v2-1 and the check passed.
A per-KIND missing controller was invisible. A GVR is group+version+RESOURCE and
I had dropped the resource.

    arm 1, per RestDefinition   a Deployment named `<rd>-controller` must exist,
                                labelled with the version its resource CRD serves
                                -> catches a controller deleted, scaled away or
                                   never deployed, for ONE kind
    arm 2, per served version   some controller must watch each served resource
                                version
                                -> catches ironic's case: a CRD accumulating a
                                   second served version that nothing watches

Usage:
    kubectl get crd -o json                              > /tmp/crds.json
    kubectl -n <ns> get deploy -o json                   > /tmp/deploys.json
    kubectl -n <ns> get restdefinitions.ogen.krateo.io -o json > /tmp/rds.json
    hack/assert-served-controlled.py /tmp/crds.json /tmp/deploys.json /tmp/rds.json
    hack/assert-served-controlled.py --self-test

Exit: 0 ok, 1 finding, 2 unreadable input, 3 VOID (nothing to check).
"""
import json
import sys

GROUP = "nova.openstack.krateo.io"
VERSION_LABEL = "app.kubernetes.io/version"
COMPONENT_LABEL = "app.kubernetes.io/component"


def controllers_by_version(deploys):
    """version -> [deployment names] for RDC controller Deployments only."""
    out = {}
    for d in deploys:
        meta = d.get("metadata") or {}
        labels = meta.get("labels") or {}
        name = meta.get("name", "?")
        # selector AND suffix, the same pair the uid watcher uses: the label is
        # the precise intent, the suffix guards against a neighbour adopting it.
        if labels.get(COMPONENT_LABEL) != "controller":
            continue
        if not name.endswith("-controller"):
            continue
        v = labels.get(VERSION_LABEL)
        out.setdefault(v, []).append(name)
    return out


def judge(crds, deploys, rds=None):
    """Return (findings, stats). The real sweep and the self-tests both call this."""
    by_version = controllers_by_version(deploys)
    findings = []
    resource_versions = 0
    config_versions = 0
    for c in crds:
        spec = c.get("spec") or {}
        if spec.get("group") != GROUP:
            continue
        kind = (spec.get("names") or {}).get("kind") or "?"
        is_config = kind.endswith("Configuration")
        for v in spec.get("versions") or []:
            if not v.get("served"):
                continue
            vname = v.get("name", "?")
            if is_config:
                config_versions += 1
                # Asserted, not merely skipped: if one ever GAINED a controller
                # that is a change worth seeing, not a silent pass.
                if vname in by_version:
                    findings.append(
                        f"{kind} {vname} is a Configuration kind and MUST NOT have "
                        f"a controller, but {by_version[vname]} watches that "
                        f"version. Configuration objects are resolved through "
                        f"configurationRef, never reconciled by a watch.")
                continue
            resource_versions += 1
            if vname not in by_version:
                findings.append(
                    f"{kind} serves {vname} and NO controller watches it. An "
                    f"object at that version will apply cleanly, pass "
                    f"--dry-run=server, and never reconcile -- no error, no "
                    f"event, no condition. Served and controlled are different "
                    f"populations (oasgen-provider#560).")
    # ARM 1 -- per RestDefinition. The version arm above cannot see a single
    # kind losing its controller when its siblings serve the same version, so
    # join on the Deployment NAME, which is `<restdefinition>-controller` and
    # whose suffix the provider holds constant across 0.52.0-0.56.0.
    names = {(d.get("metadata") or {}).get("name") for d in deploys}
    rds_checked = 0
    for rd in (rds or []):
        rd_name = (rd.get("metadata") or {}).get("name")
        if not rd_name:
            continue
        rds_checked += 1
        want = rd_name + "-controller"
        if want not in names:
            findings.append(
                f"RestDefinition {rd_name} has no Deployment named {want}. Its "
                f"kind's CRD may still serve its version and still be answered "
                f"for by a sibling kind's controller, so the version arm cannot "
                f"see this -- a deleted or never-deployed controller for ONE "
                f"kind is silent.")

    return findings, {"resource_versions": resource_versions,
                      "config_versions": config_versions,
                      "rds_checked": rds_checked,
                      "controller_versions": sorted(
                          k for k in by_version if k is not None)}


def _self_test():
    """Arms that must fail, and arms that must not. Both call judge()."""
    def dep(name, version, component="controller"):
        return {"metadata": {"name": name, "labels": {
            COMPONENT_LABEL: component, VERSION_LABEL: version}}}

    def crd(kind, versions):
        return {"spec": {"group": GROUP, "names": {"kind": kind},
                         "versions": [{"name": v, "served": True} for v in versions]}}

    problems = []

    # healthy: one resource version, one controller at it; a Configuration with none
    f, st = judge([crd("Instance", ["v2-1"]), crd("InstanceConfiguration", ["v1alpha1"])],
                  [dep("nova-server-controller", "v2-1")])
    if f:
        problems.append(f"healthy shape reported findings: {f}")
    if st["resource_versions"] != 1 or st["config_versions"] != 1:
        problems.append(f"healthy stats wrong: {st}")

    # THE DEFECT: two served versions, controller on only the newer one
    f, _ = judge([crd("Instance", ["v2-1", "v2-2"])], [dep("nova-server-controller", "v2-2")])
    if not any("v2-1" in x and "NO controller" in x for x in f):
        problems.append(f"the unwatched served version was NOT reported: {f}")

    # a Configuration that somehow gained a controller must be reported
    f, _ = judge([crd("InstanceConfiguration", ["v1alpha1"])],
                 [dep("nova-server-controller", "v1alpha1")])
    if not any("MUST NOT have" in x for x in f):
        problems.append(f"a controlled Configuration was NOT reported: {f}")

    # a Deployment without the component label must not count as a controller
    f, _ = judge([crd("Instance", ["v2-1"])],
                 [dep("nova-server-controller", "v2-1", component="something-else")])
    if not any("NO controller" in x for x in f):
        problems.append("a non-controller Deployment was counted as a controller")

    # a Deployment whose NAME lacks the suffix must not count either
    f, _ = judge([crd("Instance", ["v2-1"])], [dep("nova-server", "v2-1")])
    if not any("NO controller" in x for x in f):
        problems.append("a Deployment without the -controller suffix was counted")

    # another group's CRD must be ignored entirely
    other = {"spec": {"group": "other.example.com", "names": {"kind": "Thing"},
                      "versions": [{"name": "v1", "served": True}]}}
    f, st = judge([other], [])
    if f or st["resource_versions"]:
        problems.append(f"a foreign group was not ignored: {f} {st}")

    # THE ARM MUTATION EXPOSED: two kinds serving the SAME version, one
    # controller removed. The version arm alone passes this; arm 1 must not.
    two = [crd("Instance", ["v2-1"]), crd("ComputeFlavor", ["v2-1"])]
    rds = [{"metadata": {"name": "nova-server"}}, {"metadata": {"name": "nova-flavor"}}]
    both = [dep("nova-server-controller", "v2-1"), dep("nova-flavor-controller", "v2-1")]
    f, st = judge(two, both, rds)
    if f:
        problems.append(f"healthy two-kind shape reported findings: {f}")
    if st.get("rds_checked") != 2:
        problems.append(f"arm 1 did not run over both RestDefinitions: {st}")
    f, _ = judge(two, both[:1], rds)          # nova-flavor-controller removed
    if not any("nova-flavor-controller" in x for x in f):
        problems.append(
            "removing ONE kind's controller was not reported -- this is the exact "
            "mutation the version-only check passed, so arm 1 is not working")

    if problems:
        print("SELF-TEST FAILED:", file=sys.stderr)
        for p in problems:
            print("  " + p, file=sys.stderr)
        return False
    return True


def main():
    if "--self-test" in sys.argv[1:]:
        ok = _self_test()
        print("self-test: %s" % ("all arms discriminate" if ok else "FAILED"))
        return 0 if ok else 4
    if not _self_test():
        print("refusing to sweep: the self-test above failed, so a clean result "
              "from this build would mean nothing.", file=sys.stderr)
        return 4

    if len(sys.argv) < 3:
        print(__doc__.strip().split("Usage:")[-1], file=sys.stderr)
        return 2
    try:
        crds = json.load(open(sys.argv[1], encoding="utf-8")).get("items", [])
        deploys = json.load(open(sys.argv[2], encoding="utf-8")).get("items", [])
        rds = (json.load(open(sys.argv[3], encoding="utf-8")).get("items", [])
               if len(sys.argv) > 3 else [])
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"could not read input ({e}). That is a failure, not a pass.",
              file=sys.stderr)
        return 2

    findings, stats = judge(crds, deploys, rds)
    print("served resource versions: %d | served Configuration versions: %d | "
          "RestDefinitions checked: %d | controller versions present: %s"
          % (stats["resource_versions"], stats["config_versions"],
             stats["rds_checked"], stats["controller_versions"] or "none"))
    if not rds:
        print("NOTE: no RestDefinition input given, so arm 1 (per-kind controller "
              "presence) did not run. Arm 2 alone cannot see one kind losing its "
              "controller when a sibling serves the same version.", file=sys.stderr)
    if stats["resource_versions"] == 0:
        print(f"VOID: no served resource-kind version in group {GROUP}. An empty "
              f"sweep passes vacuously, which is indistinguishable from every "
              f"version being controlled. Refusing.", file=sys.stderr)
        return 3
    if findings:
        print("%d finding(s):" % len(findings), file=sys.stderr)
        for f in findings:
            print("  " + f, file=sys.stderr)
        return 1
    print("every served resource version has a controller watching it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
