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

## Version floor (security)

Pin **oasgen-provider `0.52.0`, or `0.54.1` or later. NOT `0.53.0` and NOT
`0.54.0`** — both break every kind this chart ships.

> [!CAUTION]
> **`0.53.0` and `0.54.0` cannot read their auth Secret when the Configuration
> uses `apiKey`, and all six of this chart's documents use `apiKey`** — `type:
> apiKey`, `in: header`, `name: X-Auth-Token`, zero bearer. So this is not a
> partial exposure: it is every generated kind.
>
> `0.53.0` carries the breaking change `fix(deploy,restdefinition)!: the
> auth-secret RBAC actually scopes credential access now`
> ([#475](https://github.com/krateo-platformops/oasgen-provider/pull/475)),
> which introduced the collector that gathers credential references. That
> collector never saw an `apiKey` `tokenRef`, fixed by
> [#501](https://github.com/krateo-platformops/oasgen-provider/pull/501).
> `0.54.0`'s own notes say the affected resources *"cannot read their credential
> in any namespace and never reconcile"*.
>
> **The evidence differs per version and is labelled deliberately:**
>
> | version | carries #501 | status |
> |---|---|---|
> | `0.52.0` | no — predates the collector entirely | **safe, and the pin** |
> | `0.53.0` | **no** (verified by commit ancestry) | **INFERRED broken** — the collector arrives here and #501 fixes it, but [#500](https://github.com/krateo-platformops/oasgen-provider/issues/500) names only `0.54.0` and nobody has measured `0.53.0` itself |
> | `0.54.0` | **no** (verified by commit ancestry) | **MEASURED broken** — reported by the aruba-provider-kog session across 25 `apiKey` kinds |
> | `0.54.1` | **yes** (verified by commit ancestry) | fixed |
>
> `0.52.0` is the recommendation because it **predates the collector**, which is
> the only part needing no inference. An earlier version of this page said
> "0.52.0 or later", which was a range containing two broken versions.

> [!NOTE]
> **What "exercised" means for this pin, precisely.** CI installs the provider,
> generates all twelve CRDs, reconciles every RestDefinition to `Ready`, and
> server-dry-run applies every shipped sample and the Composition example. It
> points `nova.url` at `nova.invalid`, which is unroutable by design, so **no
> reconciliation against a responding Nova endpoint happens in CI** and no
> credential is ever present. Behaviour that only appears when a vendor API
> answers -- retries, pagination, existence checks, token acceptance -- is
> therefore *not* covered by a green run here. `hack/probe-361.sh` exercises
> that path against an in-cluster mock when a specific question needs it.
>
> **Verified outside CI, and worth distinguishing from the above.** The
> `0.50.0` host policy was exercised in a kind cluster against responding
> endpoints: `hack/probe-redirect.sh` showed an off-host redirect refused,
> and `hack/probe-361.sh` showed a legitimate same-host create still
> succeeding. Neither is a verdict alone — the first arm's mock answers the
> create *with* the redirect, so a refusal there is also produced by a
> policy that refused everything. Those runs are by hand, load-gated, and
> not part of a green PR.

| version | why it matters |
|---|---|
| `0.19.0` | `apiKey` / `in: header` arrives — below this the chart cannot authenticate at all |
| `0.26.1` | **hard minimum.** Before it, the `krateo.io/connector-verbose` request dump redacted only values it had been told were sensitive, so credential *headers* were logged in cleartext. This chart's credential **is** a header (`X-Auth-Token`), and verbose is exactly what you switch on to debug a 401. Since 0.26.1 credential headers are redacted by name. |
| `0.40.0` | carries the stdlib security work; the `crypto/x509` findings land hardest on rest-dynamic-controller, whose whole job is outbound TLS to Nova. |
| `0.41.0` | Swaps the provider image to distroless/static and bumps `unstructured-runtime` to v1.6.1 (an orphan-delete/finalizer change). Both are the class of change that passes every static check and only fails on a cluster, so this line is documented because 0.41.0 has been **exercised** — a pod starts and reconciles, and this chart's own `apiKey` assertion was re-run against it. A published tag nobody has run does not belong in this table. |

| `0.43.0` | carries three fixes that each matter here: **#325** removes the required `spec.<pathParam>` that made all five of this chart's resource CRs unapplicable; **#322** makes a populated CR object differ from an empty remote `{}` instead of reporting `Synced=True` having never written (this chart is exposed through `spec.server.metadata`, which Nova returns as `{}` when unset); **#308** accepts array request bodies. Also puts `API VERSION` in the default `kubectl get restdefinition` output. |
| `0.48.0` | Adds **#363**: `rest-dynamic-controller` now honours path-level `parameters` (per the spec they apply to every operation in the path item; the inheritance was simply not implemented) **and refuses a request with an unresolved placeholder**. This chart is directly affected: all six OAS assets declare path-level parameters with `required: true`, so before this fix the controller sent `GET /flavors/{id}` literally in the window before `status` held an identifier -- measured here in a mock's access log. Against a real Nova that is a 404, indistinguishable from "this resource does not exist". |
| `0.49.0` | **#367** *can* change generated CRD shape by merging a parameter's constraint into the body schema; this chart is unexposed, verified from the six assets (no parameter carries a constraint) **and** from the generated CRDs -- `hack/crd-shape.py` fingerprints are byte-identical at 0.48.0 and 0.49.0, 212 lines each. **#373** (a findby/list that matched no item no longer counts as proof of absence) is **NOT a path this chart traverses**: all six RestDefinitions declare `create`, `delete` and `get` only, and no `findby`, so there is no list-based existence check here for the fix to reach. An earlier version of this row claimed the opposite; it was wrong. |
| `0.50.0` | **hard minimum for this chart, and the pin it is exercised against.** Below it, a credentialed request's destination host was never validated: the OAS document's `servers[0]` set the base URL, the credential was attached unconditionally, and nothing in between checked where the request was going. So a `302` from the configured Nova endpoint sent the Keystone token to the redirect target — **a host this chart's documents never name**. Measured in a kind cluster on 0.49.0, with the receiving host reporting what it got: `x_auth_token=PRESENT`. Fixed by oasgen-provider [#379](https://github.com/krateo-platformops/oasgen-provider/issues/379) part 2, which validates the destination at the request path. Re-measured on 0.50.0 with the same harness: the hop is refused, and a legitimate same-host create still succeeds. `hack/probe-redirect.sh` and `hack/probe-361.sh` are the two arms; neither is a verdict alone. No configuration is needed here — all six assets declare one `servers[0]` and zero per-operation overrides, so the permitted set is exactly the operator's `nova.url`. `rdc.endpointAllowlist` is for a deployment whose OpenStack sits behind a redirecting proxy; `rdc.allowAnyHost` disables the redirect check too, which its name does not say. |

| `0.52.0` | **hard minimum while `quota.enabled` is `true`, and the current pin.** Carries **#421** (for [#419](https://github.com/krateo-platformops/oasgen-provider/issues/419)): a `requestFieldMapping` no longer deletes the spec property it reads from. All three `ComputeQuota` verbs map `inPath: tenant_id` → `spec.tenant_id`, and below `0.52.0` that self-reference made the generator delete `spec.tenant_id`, so the verbs read a property that does not exist. Keeping `tenant_id` in the request body kept the property alive but made Nova return 400 (`additionalProperties: false`), so the resource could not be both generated and callable — which is why it shipped disabled. Measured on the same document: `0.50.0` with `tenant_id` removed → `spec.tenant_id` **absent** (CRD resourceVersion 680 → 1647, so it did regenerate); `0.52.0` with it removed → **present and required**, `quota_set` 7 children as the control. On the wire against a request-logging stand-in: `PUT /v2.1/p/os-quota-sets/proj-arm23 body={"quota_set":{...}}` — template substituted, `tenant_id` absent from the body, CR `Ready=True/Available`. Over-reach was checked too and did not happen: the four `status.*`-sourced mappings still produce no `spec.id`, so `ComputeFlavor`/`Instance`/`ComputeAggregate`/`ComputeServerGroup` stay creatable. The quota-disabled fingerprint is **byte-identical** to `0.50.0` (212 lines, 10 CRDs, 211 constrained fields), and our documents contain zero `nullable` and zero `minProperties` so #424 cannot have moved it. Set `quota.enabled: false` and `0.50.0` is sufficient again. |
Neither `0.41.0`, `0.43.0`, `0.48.0`, `0.49.0`, `0.50.0` nor `0.52.0` changes the derived resource `apiVersion`:
both pin `plumbing v1.14.2` and their `internal/tools/render/` trees are
byte-identical, so resource CRs stay at `v2-1`.

Neither `0.40.0` nor `0.41.0` changes it either: both pin
`plumbing v1.14.2` and their `internal/tools/render/` trees are identical, so
resource CRs stay at `v2-1` (see [api.md](./api.md)).

> **Neither image has a shell.** `rest-dynamic-controller` has been
> distroless since 0.20.0 and `oasgen-provider` joins it at 0.41.0, so
> `kubectl exec -- env` or `-- sh` finds nothing in either pod. This is the
> kind of thing you discover mid-incident, so: read configuration with
> `kubectl get ... -o jsonpath` or `kubectl set env --list`, read behaviour
> with `kubectl logs` (plus `krateo.io/connector-verbose`, but only on
> >= 0.26.1 — see the row above), and when you genuinely need a shell in the
> pod's namespaces use `kubectl debug --target`.

Note that `https://charts.krateo.io` still serves appVersion `0.10.0`, which is
below every line of this table. Install from the OCI charts under
`ghcr.io/krateo-platformops/charts/` instead.


> [!IMPORTANT]
> **Three rows say "hard minimum", for three independent reasons, and the
> effective floor is the highest of them: `0.52.0`.** Below `0.26.1` the verbose
> request dump wrote credential *headers* to pod logs in cleartext; below
> `0.50.0` a credentialed request's destination host was never validated; below
> `0.52.0` this chart's enabled `ComputeQuota` is unusable, because the
> generator deleted the very `spec.tenant_id` its three verbs read
> (oasgen-provider#419). None subsumes another — they are different defects in
> different code — and a deployment on `0.49.0` is unaffected by the first and
> exposed to the second. All three are kept in the table because each explains a
> different thing a reader may be trying to understand about an older pin.
>
> The third is the only one that is **this chart's own doing**: it became a floor
> the moment `quota.enabled` flipped to `true`. Set `quota.enabled: false` and
> `0.50.0` is sufficient again.

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
