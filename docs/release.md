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
