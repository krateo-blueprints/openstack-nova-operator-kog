---
type: API
title: openstack-nova-operator-kog — API
description: The contract this blueprint exposes — the CompositionDefinition that registers it, the generated OpenstackNovaOperatorKog Composition CRD and how its GVK is derived, and the per-resource Nova CRD kinds (Instance, ComputeFlavor, ...) with their Configuration objects.
resource: oci://ghcr.io/krateo-blueprints/charts/openstack-nova-operator-kog
tags: [api, compositiondefinition, crd, instance, restdefinition]
timestamp: 2026-08-11T00:00:00Z
---

# API

This blueprint has two API surfaces: the **Composition** surface (how Krateo installs
the chart) and the **generated Nova** surface (the CRDs oasgen-provider derives from
the chart's `RestDefinition`s).

## The `CompositionDefinition` (registration)

`compositiondefinition.yaml` registers the blueprint with Krateo. It is a
`core.krateo.io/v1alpha1` `CompositionDefinition` pointing at the published chart:

```yaml
apiVersion: core.krateo.io/v1alpha1
kind: CompositionDefinition
metadata:
  name: openstack-nova-operator-kog
  namespace: krateo-system
spec:
  chart:
    url: oci://ghcr.io/krateo-blueprints/charts/openstack-nova-operator-kog
    version: "0.1.0"
```

| field | meaning |
|---|---|
| `spec.chart.url` | the OCI location of the packaged chart. Resolves only once a release workflow has pushed the chart to the `charts/` namespace. |
| `spec.chart.version` | the published chart version; must match a released `Chart.yaml` `version`. |

## The generated `OpenstackNovaOperatorKog` CRD

Applying the `CompositionDefinition` makes core-provider generate a Composition CRD
from the chart's `values.schema.json`. The GVK is derived as:

- **group** — `composition.krateo.io`
- **version** — from the chart version: `0.1.0` → `v0-1-0`
- **kind** — PascalCase of the CompositionDefinition `metadata.name`:
  `openstack-nova-operator-kog` → `OpenstackNovaOperatorKog`

A CR of that kind carries the chart's values as its spec:

```yaml
apiVersion: composition.krateo.io/v0-1-0
kind: OpenstackNovaOperatorKog
metadata:
  name: openstack-nova-operator-kog
  namespace: openstack
spec:
  nova:
    enabled: true
    upstreamUrl: https://compute.gra11.cloud.ovh.net/v2.1/<projectId>
  restdefinitions:
    server:
      enabled: true
    flavor:
      enabled: true
    keypair:
      enabled: false
    server_group:
      enabled: false
    aggregate:
      enabled: false
    quota:
      enabled: false
```

The full spec schema is `chart/values.schema.json`, documented field by field in
[configuration](./configuration.md). Applying this CR installs the operator packaging
(the toggled RestDefinitions); it does not create individual Nova resources.

## The generated Nova CRDs

Each enabled `restdefinitions.<name>` block produces a `RestDefinition`
(`ogen.krateo.io/v1alpha1`) that oasgen-provider turns into a CRD in group
`nova.openstack.krateo.io`, version `v1alpha1`:

| resource | Kind | Nova API |
|---|---|---|
| server | `Instance` | Servers (compute instances) |
| flavor | `ComputeFlavor` | Flavors |
| keypair | `ComputeKeypair` | Keypairs |
| server_group | `ComputeServerGroup` | Server Groups |
| aggregate | `ComputeAggregate` | Host Aggregates |
| quota | `ComputeQuota` | Quota Sets (off by default) |

### Authentication: the `<Kind>Configuration` object

Because each OAS declares an `http/bearer` security scheme, oasgen-provider also
generates a companion `<Kind>Configuration` CRD per resource
(`InstanceConfiguration`, `ComputeFlavorConfiguration`, ...). It carries the bearer
token reference; a resource CR points at it via `spec.configurationRef`:

```yaml
apiVersion: nova.openstack.krateo.io/v1alpha1
kind: InstanceConfiguration
metadata:
  name: nova-config
  namespace: krateo-system
spec:
  authentication:
    bearer:
      tokenRef:
        name: nova-token
        namespace: krateo-system
        key: token
```

### An `Instance` CR

The request body is envelope-wrapped, so the spec has a `server` block (hence the kind
is `Instance`, not `Server`):

```yaml
apiVersion: nova.openstack.krateo.io/v1alpha1
kind: Instance
metadata:
  name: kog-demo-1
  namespace: krateo-system
spec:
  configurationRef:
    name: nova-config
    namespace: krateo-system
  server:
    name: kog-demo-1
    flavorRef: "b2-7"
    imageRef: "00000000-0000-0000-0000-000000000000"
    networks:
      - uuid: "00000000-0000-0000-0000-000000000000"
```

Worked CRs for every kind live in `chart/samples/` (`nova-auth.yaml`,
`test-server.yaml`, `extra-resources.yaml`); see [examples](./examples.md).
