---
type: Runbook
title: openstack-nova-operator-kog — release
description: How a release ships — a SemVer tag drives release-tag.yaml, which stamps the CHART_VERSION placeholder from the tag, packages the chart, pushes it to the org's OCI charts/ namespace on GHCR, and publishes the stamped CompositionDefinition as a release asset.
resource: oci://ghcr.io/krateo-blueprints/charts/openstack-nova-operator-kog
tags: [release, oci, ghcr, chart]
timestamp: 2026-08-11T00:00:00Z
---

# Release

A single plain-SemVer tag (`X.Y.Z`, **no** `v` prefix) publishes the chart. The tag
push triggers `release-tag.yaml`.

## What a tag ships

`release-tag.yaml` packages `chart/` and pushes it to the org's classic OCI charts
namespace:

```
oci://ghcr.io/<owner>/charts/openstack-nova-operator-kog:<version>
```

The owner namespace is derived from the repository (`GITHUB_REPOSITORY_OWNER`) — the
`GITHUB_TOKEN` can only write its own namespace, so this survives a repo move. This is
the artifact the `CompositionDefinition`'s `spec.chart.url` points at.

The workflow, in order:

1. **Resolve version and registry** — the version comes from the git tag, and the
   registry namespace from `GITHUB_REPOSITORY_OWNER`. The `GITHUB_TOKEN` can only
   write its own namespace, so this survives a repo move.
2. **Stamp every CompositionDefinition** — `sed s/CHART_VERSION/<tag>/g`, so the
   published asset carries a concrete version rather than the placeholder.
3. **Package and push every chart** — the same stamp is applied to `Chart.yaml`,
   then `helm package --version <tag>` and `helm push` to
   `oci://ghcr.io/<owner>/charts`.
4. **Publish the stamped CompositionDefinitions** as release assets.

> [!IMPORTANT]
> **`chart/Chart.yaml` keeps `version: CHART_VERSION`. Do not replace it with a
> number.** The tag is the single source of the version, and the placeholder is
> the steady state. An earlier version of this runbook said to bump
> `Chart.yaml`'s `version` before tagging, and also described a
> "tag-matches-Chart.yaml guard" that does not exist in any workflow.
>
> Committing a concrete version would not break the release — `helm package
> --version` pins it from the tag either way — but it would silently remove the
> placeholder, which makes the stamp step a no-op and `smoke.yaml`'s
> `grep -q CHART_VERSION` stamp check vacuous: that check fires when the
> placeholder SURVIVES stamping, so a file that never had one always passes.
>
> `helm` refuses the chart while the placeholder is present, which is why local
> work renders a stamped copy (`0.0.0-ci`) instead of editing the file.

> [!IMPORTANT]
> **The docs pin this chart's version explicitly, and a release makes them
> stale.** Six documents install it with `--version X.Y.Z`, because `helm`
> refuses the working tree while `Chart.yaml` holds the placeholder.
> `verify_documented_chart_version` compares every one of them against the newest
> SemVer tag, so the first PR after a release FAILS until they are updated. That
> is deliberate: a stale `--version` sends a reader to an older chart than the
> document describes, and nothing else would notice.
>
> So the order is: merge the content, tag, then update the six `--version`
> references on `main` in a follow-up PR — which the screen will insist on.

## Steps

Nothing to bump. Merge whatever the release should contain, then tag:
```console
$ git tag X.Y.Z && git push origin X.Y.Z
```

Verify the artifact exists:

```console
$ helm show chart oci://ghcr.io/krateo-blueprints/charts/openstack-nova-operator-kog \
    --version X.Y.Z | head -3
```

`compositiondefinition.yaml` on `main` also keeps `spec.chart.version:
CHART_VERSION`, and **it is not bumped by hand either** — step 2 above stamps it
from the tag and attaches the result as a release asset, which is the file a user
applies. An earlier version of this runbook said to bump it on `main` after each
release; that would remove the placeholder the stamp step depends on, for the
same reason as `Chart.yaml`.

`hack/assert-release-cd.py` checks the published asset on every release: stamped
rather than left as `CHART_VERSION`, an `oci://` url, naming *this* chart, and
the expected apiVersion/kind. A CompositionDefinition pointing at somebody
else's chart would still reach `Available`, which is why the name is asserted.

## Bumping the `oasgen-provider` pin

**This is not part of cutting a tag.** It is here because it is the one change in
this repo whose failure mode is silent, and the verification has to happen at the
moment someone does it.

> [!WARNING]
> **After bumping the pin, verify that the CRD schema actually re-rendered. Do
> not trust `status.generatorBuild`.** On an existing installation an upgrade can
> stamp that field while skipping generation entirely, leaving the served schema
> as the previous version rendered it — and the field then reads correct forever.
> Measured here on `0.52.0` → `0.56.0`; tracked upstream as
> [oasgen-provider#541](https://github.com/krateo-platformops/oasgen-provider/issues/541).

### What goes wrong

The provider's `Observe` returns early when the per-kind
`rest-dynamic-controller` Deployment is **not found**, and on that path it never
reaches the check that decides whether to regenerate. `Create` runs instead,
finds the CRD already present, **skips generation**, and stamps
`status.generatorBuild` anyway.

That consumes a one-shot activation. The regenerate check returns true
unconditionally while the stored build is empty — the branch that exists so a
pre-existing cluster re-renders once — and once stamped it is false forever. No
later reconcile, restart, or re-upgrade to the same version retries it.

Whether a given upgrade lands this way is **not** determined by the versions
involved: another consumer's `0.48.0` → `0.56.0` upgrade found the Deployments
present, reached the check, and regenerated 32 of 32. Why they were absent here
and present there is unexplained.

### The check that always works

Render the same documents on both versions and diff the schema. This is the only
check that does not depend on what the new release happened to change:

```bash
# on the OLD pin, in a throwaway cluster
kubectl get crd -o json | hack/crd-shape.py > /tmp/shape-old.txt
# fresh install on the NEW pin -- a fresh render, not an upgrade
kubectl get crd -o json | hack/crd-shape.py > /tmp/shape-new.txt
diff /tmp/shape-old.txt /tmp/shape-new.txt
```

Then compare the **live** cluster's shape against `shape-new.txt`. If the live
one matches the *old* render while the provider reports the new build, this
happened.

`hack/crd-shape.py` stamps its keyword set on line 1, so comparing fingerprints
produced by two different builds of the script shows up as the first line of the
diff rather than as a phantom schema change.

### A quicker check, and exactly when it is worthless

For a release that **adds a field**, look for that field:

```bash
kubectl get crd instances.nova.openstack.krateo.io \
  -o jsonpath='{.spec.versions[*].schema.openAPIV3Schema.properties.status.properties.observedGeneration}'
```

Empty, while `status.generatorBuild` reads the new version, means it happened.
`status.observedGeneration` is the field `0.56.0` adds — measured on 0 of 12
served versions at `0.52.0` and 6 of 12 at `0.56.0`, confirmed independently by
another consumer at 32 of 32.

> [!CAUTION]
> **This check only works by accident of what `0.56.0` changed.** For a release
> that alters no schema it reports nothing and *means* nothing — the empty output
> is identical whether the schema is current or stale. Do not reuse it for a
> different version bump without first establishing what that version adds.

### Before you upgrade: start the uid watcher

**If you are about to bump the provider under live CRs, start this first.** It is
the only way to answer the question [#541](https://github.com/krateo-platformops/oasgen-provider/issues/541)
leaves open — *why* the per-kind controller Deployments are ever absent at the
moment the new provider first observes them.

```bash
hack/watch-rdc-uids.sh <kubeconfig> <context> [namespace] [logfile]
```

It appends `EVENT NAME UID GENERATION RV CREATED` per watch event, and it must be
started **before** the upgrade: the baseline uid is the datum, and a watcher
started afterwards has nothing to compare against.

**Why a watcher and not a before/after snapshot.** `--output-watch-events` records
the event **type**, so a `DELETED` line distinguishes *something deleted them*
from *they were briefly unobservable*. A pair of snapshots cannot tell those
apart — both yield two uids, or two identical ones, with no record of what
happened in between. The idea is the oasgen-provider session's; the point is that
the natural occurrence needs luck **and** preparation at the same instant, while a
standing watcher needs only luck.

> [!IMPORTANT]
> **It uses `--watch`, not `--watch-only`, and that distinction is the whole
> script.** `--watch-only` means "without listing/getting first", so it emits **no
> baseline at all** — and over a set that is not changing, its correct output is
> **zero lines**, which is indistinguishable from a broken pipeline. Measured side
> by side on one cluster in one window: `--watch` → 6 ADDED events with 6 distinct
> uids; `--watch-only` → 0. The script refuses to start (rc=2) if the API server is
> unreachable and (rc=3) if the namespace holds no `*-controller` Deployments,
> because an empty log reads exactly like "the Deployments were never touched".

### Also check the `krateo.io/oas-version` label on resource instances

**Separate from the schema check, and a different failure: an `Instance` without
that label is never observed at all.** The per-version controller's listwatcher
filters on `krateo.io/oas-version` **server-side**, so an unlabelled resource
instance is not merely unreconciled — it is invisible, carries no condition, and
therefore cannot report that anything is wrong.

A `MutatingAdmissionPolicy` stamps the label at admission, so **only instances
admitted before their group's policy existed are at risk** — i.e. CRs created on
an older provider and still present across the bump. And oasgen-provider's
ServiceAccount has no write verbs on any generated group, so its backfill can
*list* those instances but never patch them: the warning it emits is the whole of
what it can do.

```bash
# every generated resource kind in this chart; empty LABEL column = invisible
for k in instances computeflavors computekeypairs computeservergroups \
         computeaggregates computequotas; do
  kubectl get "$k.nova.openstack.krateo.io" -A \
    -o custom-columns="KIND:.kind,NAME:.metadata.name,LABEL:.metadata.labels['krateo\.io/oas-version']"
done
```

**The `<Kind>Configuration` objects are a different case and do not matter here.**
They are resolved through `configurationRef`, not selected by label, so a missing
label on a Configuration is cosmetic — which is also why the provider's own
warning wording is being made per-family rather than saying "never reconciled"
for both.

> [!CAUTION]
> **If you find an unlabelled resource instance, TOUCH it — do NOT delete it.
> Deleting it destroys the Nova resource it owns.** An `Instance` is a real VM, a
> `ComputeKeypair` a real keypair. "Orphaned" describes the label, not the object.
>
> ```bash
> kubectl annotate instances.nova.openstack.krateo.io <name> -n <ns> \
>   --overwrite krateo.io/touch="$(date -u +%s)"
> ```
>
> > **RE-CONFIRMED FROM SOURCE (2026-11-10)**, having been marked undated earlier
> > the same day. The oasgen-provider session re-derived all seven claims from
> > oasgen-provider's own `internal/tools/policy/policy.go` and its chart's
> > ClusterRole template at that repo's `origin/main` — **not** from their own earlier note, on their own principle
> > that *"a claim of mine from an earlier session is not evidence now"* — three peer confirmations
> > expired within hours on 2026-11-10, one of them theirs, cited twice after it
> > stopped being true. The recovery procedure itself (**touch, do not delete**)
> > was measured **here**, on a real cluster, and stands on that measurement
> > rather than on either passage. What is pending is the *explanation*.
>
> **Why a touch is sufficient**, from the oasgen-provider session's read of the
> `oas-version` MutatingAdmissionPolicy: it fires on **CREATE *and UPDATE***, its
> patch is an `add` (which overwrites), and the value is
> `request.requestKind.version` — **the version of the endpoint the write came
> through**. So any write through the current endpoint relabels the object, and
> the inert population is not "instances of the old version" but **instances that
> have not been written since the bump**.
>
> **And waiting does not work.** The provider's backfill *wants* to relabel these
> and cannot: its ServiceAccount holds `get`/`list`/`watch` on generated groups
> and write on nothing, which is the whole of oasgen-provider#554. So the
> admission policy *can* relabel but only when something writes the object, while
> the backfill *wants* to and is forbidden. **An operator who waits for Krateo
> waits forever; one who touches the object fixes it immediately** — and neither
> fact is visible from the CR. More precisely, in the form that stays true after
> the fix lands: **nothing in the current release will write them for you
> ([oasgen-provider#554](https://github.com/krateo-platformops/oasgen-provider/issues/554))**.
> Grant the backfill `patch` and waiting starts working.
>
> > **TOUCH MEANS A MAIN-RESOURCE WRITE. A `/status` PATCH DOES NOTHING.**
> > Verified from source at `origin/main` by the oasgen-provider session, and in
> > neither my text nor their own earlier note: the policy's rule is
> > `"resources": ["*"]`, and in admission-rule semantics `*` means *all
> > resources but **not** subresources* — `*/*` would be needed for both. So a
> > `kubectl patch --subresource=status` **appears to work and relabels
> > nothing**, and ordinary status churn from reconciliation cannot help either.
> > `kubectl annotate` and `kubectl label` are main-resource writes and qualify.
> >
> > **And do not touch it through a pinned old endpoint.** The stamped value is
> > the version of the *endpoint the write came through* —
> > `request.requestKind.version`, which the API contract distinguishes from
> > `request.kind` (the latter being what the object was converted *to*). A write
> > through an **older** served endpoint therefore stamps the **older** label,
> > relabelling backwards. `kubectl annotate` uses the preferred version and is
> > safe by default; a versioned client, or anything pinning `--api-version`, is
> > not.
>
> **And "waits forever" is stronger than the forbidden backfill alone.** From the
> same read: `rest-dynamic-controller` *does* write the main resource of every
> instance it reconciles, unconditionally — one `tools.Update` at
> `restResources.go:275` against sixteen `tools.UpdateStatus` in the same file —
> and that write would relabel them. It never reaches these objects, because the
> watch is **exact equality on the version label** (`main.go:102-111`,
> `restResources.go:229`), so an unlabelled instance matches no watch and is
> never delivered. The fallback that once stamped the label on first reconcile,
> `needsVersionLabel`/`ensureVersionLabel`, was deliberately removed when
> version-scoped watching landed. **So the one component that writes the main
> resource is structurally blind to exactly the objects that need writing** — a
> better reason than "the backfill is forbidden", and one that survives the
> backfill being granted `patch`.
>
> **Do not confuse this with the composition case.** The
> openstack-ironic-operator-kog session hit a *composition child* stamped with
> `krateo.io/composition-version`, where deleting the dead object genuinely was
> the fix because the parent rebuilds it. That is a **different label on a
> different object**: a composition child is re-creatable from its parent, a
> generated resource instance owns something in Nova. Same symptom, opposite
> remedy.

> [!NOTE]
> **Not exercised in this repo.** Reported by the github-provider-kog session,
> measured on a live cluster at `0.56.0` (32 resource instances, 0 unlabelled, so
> their exposure was zero; only their 28 Configurations lacked it), with the
> mechanism confirmed by the oasgen-provider session as
> [#554](https://github.com/krateo-platformops/oasgen-provider/issues/554). Our
> pin is `0.52.0` and **whether the backfill or the policy exists there at all is
> unknown** — so running the command above today and finding nothing would be a
> statement about our version, not about our instances. Check it after a pin move,
> and look at the resource instances rather than the Configurations.

### Recovery

Clear the stamp. This hands back the activation branch rather than inventing a
trigger, and it destroys nothing:

```bash
kubectl patch restdefinition <name> -n krateo-system --subresource=status \
  --type=merge -p '{"status":{"generatorBuild":""}}'
```

**Do it after the cluster is quiet, not during an upgrade** — that precondition
is the defect's own trigger inverted: while the controller Deployment is missing,
`Observe` never reaches the check, so the patch has no effect.

| alternative | verdict |
|---|---|
| edit an annotation on the RestDefinition | **does nothing** — only the OAS content, `spec.resource` and the generator are compared |
| edit `spec.resource` | works, but it is a spec change made for a non-spec reason and reads as one in an audit |
| delete the generated CRD | destroys every CR of that kind. Not a recovery |

> [!NOTE]
> **MEASURED, and it works.** Forced a cluster into the broken state — deleted
> the six `*-controller` Deployments, then upgraded both charts, which took the
> `Create` branch (`Creating RestDefinition` ×6, `Regenerating CRD` ×0) and left
> `status.observedGeneration` declared on **0 of 12** served versions while
> `status.generatorBuild` read `0.56.0-fadf5ed…`. Then applied the patch to all
> six RestDefinitions:
>
> ```
> before patch   observedGeneration 0 of 12   generatorBuild 0.56.0-fadf5ed…
> after 4 min    observedGeneration 6 of 12   generatorBuild 0.56.0-fadf5ed…
> ```
>
> **Both halves moved** — the schema was re-rendered *and* the stamp was
> rewritten after it, which is the ordering that was missing. An earlier attempt
> to exercise this reached the patch with a *healthy* cluster, so its apparent
> success was pinned by construction and proved nothing; that is why the state is
> asserted broken first.
>
> **Two limits, stated.** The broken state was **forced** by deleting the
> Deployments, not caught occurring naturally — so this shows the recovery works
> on that state, not that the state arises the same way in the wild. And the
> damage was shown to persist for **5 rounds over 5 minutes with the Deployments
> healthy throughout and no CRD write in that window** — which supports "it does
> not repair itself" and cannot establish it, since a repair on a later round
> would look identical.

## PR-time checks

Two other workflows run on every PR and push to `main`:

- `lint.yaml` — **two real gates and one informational step**, and the
  difference matters:
  - `shellcheck` over `scripts/*.sh` and `hack/*.sh` — a real failing gate. It
    asserts the glob matched before running, so an empty set is `exit 3` (VOID)
    rather than a pass.
  - `kubeconform -strict` over the rendered manifests — a real failing gate,
    **limited to the built-in kinds**. This chart renders 6 ConfigMaps and 6
    `RestDefinition`s; kubeconform has a schema for the former and none for the
    latter, so `RestDefinition` is skipped **by name**. That is the scope, not a
    convenience: this step cannot see the half of the chart that is its actual
    substance. `-ignore-missing-schemas` would have hidden that behind a green
    tick. The `RestDefinition`s are guarded instead by `smoke.yaml` applying
    them to a live API server against the CRDs oasgen actually generated, which
    is stronger than any schema store. We deliberately did **not** vendor a copy
    of oasgen's `RestDefinition` schema to close the gap — this repo already
    deleted a ported copy of their version normalizer for exactly that reason.
  - `helm template` + `helm lint` (with a placeholder `nova.url`, since the
    chart hard-fails on an empty one) — **informational only**, never failing
    the build, because charts across repos legitimately need values or private
    dependencies to render.

  > Until 2026-11-07 this list named `kubeconform` and `shellcheck` and neither
  > ran anywhere: `grep -ri shellcheck .github/` returned nothing, and the two
  > checks that did run were `::warning::` and `|| true`, so the job could not
  > fail. Wiring shellcheck up immediately found three `SC2034`s, one of which
  > was a refusal arm nobody had written rather than lint noise.
- `security.yml` — the shared `krateo-platformops/.github` security workflow.
- `lint.yaml` — the shared `lint-docs` documentation-standard check (this bundle).
