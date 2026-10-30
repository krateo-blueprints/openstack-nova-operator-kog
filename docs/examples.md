---
type: ExampleIndex
title: openstack-nova-operator-kog — examples
description: Index of the runnable examples under examples/ and the worked CRs shipped in chart/samples/.
resource: oci://ghcr.io/krateo-blueprints/charts/openstack-nova-operator-kog
tags: [examples, composition, samples]
timestamp: 2026-08-11T00:00:00Z
---

# Examples

## Under `examples/`

- [examples/nova-composition](../examples/nova-composition/README.md) — install the
  blueprint as a Krateo Composition: an `OpenstackNovaOperatorKog` CR that deploys the
  toggled Nova `RestDefinition`s through the Krateo control
  plane. It doubles as the reference for the derived Composition GVK
  (`composition.krateo.io/v0-1-0`, kind `OpenstackNovaOperatorKog`).

## Worked CRs in `chart/samples/`

These are applied after the operator packaging is installed, to drive real Nova
resources ([usage](./usage.md)):

- `chart/samples/nova-auth.yaml` — the `InstanceConfiguration` that references the
  `nova-token` bearer Secret.
- `chart/samples/test-server.yaml` — a sample `Instance` CR (a Nova server); edit
  `flavorRef` / `imageRef` / network UUID for your project.
- `chart/samples/extra-resources.yaml` — sample `ComputeFlavor` / `ComputeKeypair` /
  `ComputeServerGroup` / `ComputeAggregate` CRs with their `<Kind>Configuration`
  objects.

## Full walkthroughs

- [quickstart](./quickstart.md) — install to a VM in Horizon, the short path.
- [e2e](./e2e.md) — the complete OVH Public Cloud walkthrough on a `kind` cluster.
- [e2e-krateo-openstack](./e2e-krateo-openstack.md) — the same against a self-hosted
  Krateo-blueprint OpenStack, with screenshots.
