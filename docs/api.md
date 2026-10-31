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
- **version** — `"v"` + the **full** chart version with dots replaced by dashes
  (`"v" + strings.ReplaceAll(chartVersion, ".", "-")`). Note this includes the
  patch: chart `0.1.3` → `v0-1-3`, and a patch bump therefore changes the served
  apiVersion. Stale versions are pruned once no Composition still uses them, so an
  older apiVersion will not stay addressable as a fallback.
- **kind** — PascalCase of the CompositionDefinition `metadata.name`:
  `openstack-nova-operator-kog` → `OpenstackNovaOperatorKog`

A CR of that kind carries the chart's values as its spec. **No literal apiVersion is
correct here**: `chart/Chart.yaml` carries `version: CHART_VERSION`, substituted at
release time, so at no commit in this repo does a correct literal exist to write.
Substitute the value you read back:

> **Do not hardcode this version — read it off the object.** core-provider
> publishes the GVK it actually created on the CompositionDefinition's status:
>
> ```bash
> # Wait on the CONDITION first: an unresolvable chart sets Ready=False with
> # reason Unavailable and the registry error in the message, so this fails
> # fast and says why. Polling for a non-empty field explains nothing.
> kubectl wait compositiondefinition/openstack-nova-operator-kog -n krateo-system \
>   --for=condition=Ready --timeout=300s
>
>
> **`Ready`/`Available` on a CompositionDefinition does NOT mean the generated
> CRD answers.** core-provider sets `Available()` after writing the CRD and
> refreshing status from `crd.Spec.Group` / `crd.Spec.Names.Kind` — **spec**
> fields, present the moment the object is accepted — so nothing on that path
> can tell an accepted CRD from a served one. An `IsReady(crd)` that checks
> `Established` exists at `internal/tools/crd/crd.go:283` with **zero callers**
> (core-provider#269). So add the honest gate before applying a Composition:
>
> ```bash
> kubectl wait --for=condition=Established \
>   crd/$(kubectl get compositiondefinition openstack-nova-operator-kog \
>           -n krateo-system -o jsonpath='{.status.resource}').composition.krateo.io \
>   --timeout=300s
> ```
>
> This is the same defect as oasgen-provider#314 for RestDefinitions, in a
> different component: a readiness signal that attests the write rather than the
> service. For a Composition *instance* the distinction IS carried — gate on
> `.status.conditions[Ready].reason`, where `ZeroEvidence` (`Status: Unknown`)
> means "not evaluated" and cannot be mistaken for healthy.
> kubectl get compositiondefinition openstack-nova-operator-kog -n krateo-system \
>   -o jsonpath='{.status.apiVersion}'   # e.g. v0-1-3 for chart 0.1.3
> kubectl get compositiondefinition openstack-nova-operator-kog -n krateo-system \
>   -o jsonpath='{.status.kind}'         # served too - no need to read the CRD
> kubectl get compositiondefinition openstack-nova-operator-kog -n krateo-system \
>   -o jsonpath='{.status.resource}'     # the plural, which you cannot reliably compute
> ```
>
> The plural genuinely is not derivable: core-provider resolves it through
> `plumbing/kubeutil/plurals`, not a string rule. One counterexample is enough
> to show why `strings.ToLower(kind)+"s"` is a guess rather than a rule — a
> kind like `Kaas` pluralises to `kaas`, not `kaass` — and a wrong plural
> fails silently, by matching nothing, rather than by erroring.
>
> These fields are populated once the CompositionDefinition reconciles
> successfully; they are absent while the Composition CRD does not yet exist.

```yaml
apiVersion: composition.krateo.io/<APIVERSION>   # substitute, do not hardcode
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

Because each OAS declares an `apiKey` security scheme (`in: header`,
`name: X-Auth-Token`), oasgen-provider also generates a companion
`<Kind>Configuration` CRD per resource (`InstanceConfiguration`,
`ComputeFlavorConfiguration`, ...). It carries the Keystone token reference; a
resource CR points at it via `spec.configurationRef`:

```yaml
apiVersion: nova.openstack.krateo.io/v1alpha1
kind: InstanceConfiguration
metadata:
  name: nova-config
  namespace: krateo-system
spec:
  authentication:
    apiKey:
      tokenRef:
        name: nova-token
        namespace: krateo-system
        key: token
      header: X-Auth-Token
```

### An `Instance` CR

The request body is envelope-wrapped, so the spec has a `server` block (hence the kind
is `Instance`, not `Server`):

```yaml
apiVersion: nova.openstack.krateo.io/v2-1
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
