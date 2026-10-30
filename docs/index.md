---
type: Component
title: openstack-nova-operator-kog — index
description: The map of the openstack-nova-operator-kog doc bundle — the KOG blueprint that turns OpenStack Nova compute resources into native Kubernetes custom resources, validated against OVH Public Cloud.
resource: oci://ghcr.io/krateo-blueprints/charts/openstack-nova-operator-kog
tags: [blueprint, kog, oasgen-provider, openstack, nova, ovh]
timestamp: 2026-08-11T00:00:00Z
---

# openstack-nova-operator-kog

A Krateo Operator Generator (KOG) blueprint that turns OpenStack **Nova** compute
resources into native Kubernetes custom resources. `kubectl apply` an `Instance` and
KOG's [`oasgen-provider`](https://github.com/krateo-platformops/oasgen-provider) and
[`rest-dynamic-controller`](https://github.com/krateo-platformops/rest-dynamic-controller)
reconcile it into a real VM. It ships and is validated against
[OVH Public Cloud](https://www.ovhcloud.com/en/public-cloud/) as the managed
OpenStack target, and against a self-hosted
[Krateo-blueprint](https://github.com/krateo-blueprints/krateo-openstack-blueprint)
OpenStack.

The component is one Helm chart (`chart/`) with a sibling `CompositionDefinition`
(`compositiondefinition.yaml`) that registers it with Krateo. The chart emits, per
Nova resource you toggle, one `RestDefinition` (consumed by oasgen-provider). The
OAS declares Nova's authentication directly as an `apiKey` scheme
(`in: header`, `name: X-Auth-Token`), so the generated controllers call Nova
themselves — there is no proxy in the request path.

## The bundle (start here)

- [overview](./overview.md) — what the blueprint builds, the request flow, and how
  native apiKey authentication works.
- [usage](./usage.md) — install the chart or the Composition, fetch a Keystone token,
  create Nova CRs, refresh the token.
- [configuration](./configuration.md) — the whole values surface: the RestDefinition
  toggles, `nova.url`, the oasgen-provider version floor, and token rotation.
- [api](./api.md) — the `CompositionDefinition` CRD and the generated Nova CRD kinds.
- [examples](./examples.md) — the runnable examples under `examples/`.
- [release](./release.md) — how a tag publishes the chart to GHCR.
- [log](./log.md) — curated history.
- [llms.txt](./llms.txt) — the version-pinned index of this bundle.

## Deep dives

- [architecture](./architecture.md) — diagram and design rationale, including the
  native apiKey authentication.
- [quickstart](./quickstart.md) — the shortest path from install to a VM in Horizon.
- [e2e](./e2e.md) — the full OVH Public Cloud walkthrough on a `kind` cluster.
- [e2e-krateo-openstack](./e2e-krateo-openstack.md) — the walkthrough against a
  self-hosted Krateo-blueprint OpenStack, with screenshots.
- [medium-article](./medium-article.md) /
  [medium-openstack-compute-operator](./medium-openstack-compute-operator.md) — the
  long-form write-ups.

## Layout

- `chart/` — the blueprint chart: `assets/*.yaml` (the Nova OAS subsets),
  `templates/configmap-*.yaml` (each OAS bundled into a ConfigMap),
  `templates/rd-*.yaml` (one `RestDefinition` per resource, toggled by values),
  `values.yaml`,
  `values.schema.json` (types the generated Composition CRD), `samples/` (worked CRs).
- `scripts/get-token.sh` — Keystone v3 token fetcher; `--secret` mode emits a
  ready-to-apply Secret.
- `compositiondefinition.yaml` — this component's own registration.
