---
type: Configuration
title: openstack-nova-operator-kog — configuration
description: The whole values surface — the per-resource RestDefinition toggles, the Nova endpoint, and the serviceAccount and name overrides — all typed by values.schema.json.
resource: oci://ghcr.io/krateo-blueprints/charts/openstack-nova-operator-kog
tags: [configuration, values, restdefinitions, apikey]
timestamp: 2026-08-11T00:00:00Z
---

# Configuration

Everything is `chart/values.yaml`, fully typed by `chart/values.schema.json`. The
schema requires `nova`, and `nova.url` in particular — the chart hard-fails on an
empty Nova endpoint. When the chart is installed as a Krateo
Composition, the same keys are the spec of the generated `OpenstackNovaOperatorKog` CR
([api](./api.md)).

## RestDefinition toggles (`restdefinitions.*`)

One block per Nova resource. Each emits (when enabled) a ConfigMap holding the OAS
subset plus a `RestDefinition` that oasgen-provider turns into a CRD and controller.

| resource | key | `enabled` default | generated `resourceKind` |
|---|---|---|---|
| Nova Servers (instances) | `restdefinitions.server` | `true` | `Instance` |
| Nova Flavors | `restdefinitions.flavor` | `true` | `ComputeFlavor` |
| Nova Keypairs | `restdefinitions.keypair` | `true` | `ComputeKeypair` |
| Nova Server Groups | `restdefinitions.server_group` | `true` | `ComputeServerGroup` |
| Nova Host Aggregates | `restdefinitions.aggregate` | `true` | `ComputeAggregate` |
| Nova Quota Sets | `restdefinitions.quota` | `false` | `ComputeQuota` |

Every block carries the same three keys:

| key | default | effect |
|---|---|---|
| `enabled` | per table above | emit the ConfigMap + RestDefinition for this resource. |
| `resourceGroup` | `nova.openstack.krateo.io` | the Kubernetes API group of the generated CRD; the GVK becomes `<resourceGroup>/v1alpha1`. |
| `resourceKind` | per table above | the generated CRD Kind. **Prefixed on purpose** — a Kind matching the envelope property name (`Server`, `Flavor`, ...) collides in `crdgen` and yields an invalid schema, so the kinds are `Instance`, `ComputeFlavor`, etc. |

`quota` is off by default: Nova's `PUT /os-quota-sets/{tenant_id}` rejects the
`tenant_id` path field leaking into the body (`additionalProperties: false`), which
needs a KOG path-only / excluded-from-body mapping that has not landed yet. See
`chart/assets/quota.yaml`.

## The Nova endpoint (`nova.*`)

| key | default | effect |
|---|---|---|
| `nova.url` | `""` | **THE key input** — the Nova Compute endpoint from your Keystone service catalog, e.g. `https://compute.gra11.cloud.ovh.net/v2.1/<projectId>`. Required; empty fails the install with an explicit message. |

The generated controllers call this URL **directly**. There is no proxy in the request
path: the OAS declares an `apiKey` security scheme (`in: header`,
`name: X-Auth-Token`), oasgen-provider turns that into
`spec.authentication.apiKey.{tokenRef,header}` on the generated `<Kind>Configuration`
CRD, and rest-dynamic-controller sets the header on each request itself.

The URL must be resolvable and reachable from inside the cluster. For an in-cluster
Nova, the `nova-api` **ClusterIP** remains the safest value — the historic reason (an
nginx `resolver` that could not see `*.svc.cluster.local`) died with the proxy, but an
IP literal still avoids depending on cluster DNS from the controller pod.

## Authentication and token rotation

The Keystone token lives in a Secret referenced by each `<Kind>Configuration` CR:

```yaml
spec:
  authentication:
    apiKey:
      tokenRef:
        name: nova-token
        namespace: krateo-system
        key: token
      header: X-Auth-Token
```

Two properties of that shape are worth knowing:

- **The Secret holds the raw token value only** — no prefix, no `Bearer `. The wire
  format is kept out of the Secret deliberately, so whatever mints the token never has
  to know about HTTP framing. (`valuePrefix` exists upstream for APIs that declare
  `apiKey` but want a prefix; for Keystone it stays empty.)
- **`header` is a real field, not a hidden constant.** oasgen-provider defaults it from
  the OAS scheme name, but surfaces it on the CR so the value it derived is visible when
  you are debugging a 401.

**Keystone tokens expire (~1h on OVH), and keeping that Secret fresh is a
secret-manager's job.** Point External Secrets Operator, Vault Agent, or your
platform's equivalent at the Secret and let it own minting and rotation. This chart
ships no CronJob, no sidecar and no refresh logic by design — rotation is a solved
problem that does not belong in an operator package. `scripts/get-token.sh --secret`
remains a convenience for bootstrapping a local demo, not a production path.

## ServiceAccount and overrides

| key | default | effect |
|---|---|---|
| `serviceAccount.create` | `false` | create a ServiceAccount for chart workloads. |
| `serviceAccount.name` | `""` | name to use; empty + `create: false` uses the namespace `default` SA. |
| `nameOverride` | `""` | override the chart name used in resource names. |
| `fullnameOverride` | `""` | override the fully-qualified app name used in resource names. |
