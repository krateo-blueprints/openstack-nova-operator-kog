---
type: Runbook
title: openstack-nova-operator-kog — release
description: How a release ships — a SemVer tag drives release-chart.yaml to package the chart and push it to the org's OCI charts/ namespace on GHCR, with the tag-matches-Chart.yaml guard and the CompositionDefinition version bump.
resource: oci://ghcr.io/krateo-blueprints/charts/openstack-nova-operator-kog
tags: [release, oci, ghcr, chart]
timestamp: 2026-08-11T00:00:00Z
---

# Release

A single plain-SemVer tag (`X.Y.Z`, **no** `v` prefix) publishes the chart. The tag
push triggers `release-chart.yaml`.

## What a tag ships

`release-chart.yaml` packages `chart/` and pushes it to the org's classic OCI charts
namespace:

```
oci://ghcr.io/<owner>/charts/openstack-nova-operator-kog:<version>
```

The owner namespace is derived from the repository (`GITHUB_REPOSITORY_OWNER`) — the
`GITHUB_TOKEN` can only write its own namespace, so this survives a repo move. This is
the artifact the `CompositionDefinition`'s `spec.chart.url` points at.

The workflow, in order:

1. `helm lint chart` — validates the chart and its `values.schema.json` against
   `values.yaml`. (`helm package` does **not** render templates, so a chart that
   requires runtime input like `nova.url` still publishes cleanly.)
2. **Tag-matches-Chart.yaml guard** — on a tag push, fails if the git tag does not
   equal the `version:` in `chart/Chart.yaml`.
3. `helm package chart` → `dist/*.tgz`.
4. `helm registry login ghcr.io` with the workflow token.
5. `helm push` each `.tgz` to `oci://ghcr.io/<owner>/charts`.

It also runs on `workflow_dispatch` for a manual publish (the tag guard is skipped
when not triggered by a tag).

## Steps

Bump `chart/Chart.yaml` `version` (and `appVersion`) to `X.Y.Z`, merge, then:

```console
$ git tag X.Y.Z && git push origin X.Y.Z
```

Verify the artifact exists:

```console
$ helm show chart oci://ghcr.io/krateo-blueprints/charts/openstack-nova-operator-kog \
    --version X.Y.Z | head -3
```

After the chart is published, bump `compositiondefinition.yaml`'s
`spec.chart.version` to `X.Y.Z` on `main` — it is this component's own registration
and must point at a version that exists. (The current `0.1.0` chart is not yet
published; the OCI URL resolves only once the release workflow has pushed it.)

## PR-time checks

Two other workflows run on every PR and push to `main`:

- `lint.yaml` — `helm lint` (with a placeholder `nova.url`, since the
  chart hard-fails on an empty one), `helm template`, and `kubeconform` over the
  rendered manifests; plus `shellcheck` over `scripts/`.
- `security.yml` — the shared `krateo-platformops/.github` security workflow.
- `lint.yaml` — the shared `lint-docs` documentation-standard check (this bundle).
