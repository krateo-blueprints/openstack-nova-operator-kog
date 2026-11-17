---
type: Usage
title: openstack-nova-operator-kog — usage
description: How to run the blueprint — install the chart (or the Composition), point nova.url at your Nova endpoint, store a Keystone token, create Nova CRs, and delegate token rotation to a secret-manager.
resource: oci://ghcr.io/krateo-blueprints/charts/openstack-nova-operator-kog
tags: [usage, install, keystone, token, instance]
timestamp: 2026-08-11T00:00:00Z
---

# Usage

There are two entry points: install the chart directly (below), or install it as a
Krateo Composition (further down). Either way you must supply two things at runtime:
the **upstream Nova endpoint** and a fresh **Keystone token**.

## Prerequisites

KOG's `oasgen-provider` must be in the cluster — it is what turns the chart's
`RestDefinition`s into CRDs and controllers:

```console
$ helm upgrade --install oasgen-provider-crds \
  oci://ghcr.io/krateo-platformops/charts/oasgen-provider-crds \
  --version 0.52.0 -n krateo-system --create-namespace --wait
$ helm upgrade --install oasgen-provider \
  oci://ghcr.io/krateo-platformops/charts/oasgen-provider \
  --version 0.52.0 -n krateo-system --wait
```

> [!IMPORTANT]
> **If this cluster already runs Krateo, do not run the two commands above
> blind — check what is there first.** `restdefinitions.ogen.krateo.io` is
> cluster-scoped and singleton: one object per cluster, shared by every KOG in
> it. The Krateo installer at release `0.3.673` pins the provider at `0.74.0`,
> 22 minors above this chart's pin, and the `0.74.0` CRD is a strict superset of
> the `0.52.0` one — **39 field paths present at `0.74.0` and absent at
> `0.52.0`, and 0 the other way** (measured 2026-11-18). Re-installing at
> `0.52.0` can only lower it, and pruning is silent with a green apply.
>
> This chart needs a provider **at or above** `0.52.0`, not one at exactly
> `0.52.0`, so an existing newer provider is already correct. The one-command
> preflight, the measured list and the one thing about it that has NOT been
> measured are in
> [configuration](./configuration.md#installing-this-pin-onto-a-cluster-that-already-has-a-provider).
> On a cluster you created yourself moments ago there is nothing to check.

> **Use the OCI charts, not `charts.krateo.io`.** That repo still serves
> oasgen-provider appVersion `0.10.0`, which predates `apiKey` support and will
> reject this chart's security scheme. **Pin 0.52.0**; `apiKey` first worked in
> 0.19.0, but **0.26.1 is a hard floor** -- below it `krateo.io/connector-verbose`
> wrote credential *headers* to pod logs in cleartext, and this chart's
> credential IS a header.


You also need the upstream Nova URL from your Keystone service catalog. For OVH Public
Cloud it is region-specific, e.g.
`https://compute.gra11.cloud.ovh.net/v2.1/<projectId>`. Discover it (and a token) with
the bundled fetcher:

```console
$ export OS_AUTH_URL=https://auth.cloud.ovh.net/v3
$ export OS_USERNAME=... OS_PASSWORD=... OS_PROJECT_ID=... OS_REGION_NAME=GRA11
$ ./scripts/get-token.sh --upstream
# TOKEN=...
# NOVA_URL=https://compute.gra11.cloud.ovh.net/v2.1/<projectId>
```

## Install the chart

`nova.url` is required — the chart hard-fails on an empty value:

```console

> [!NOTE]
> **The portal Marketplace served the current chart as of 2026-11-09.** It served `0.1.0`
> from 2026-08-19 until 2026-11-09 — a different architecture, with the
> `nova-auth-bridge` nginx reverse proxy that was deleted when this chart moved
> to a native `apiKey` scheme. Tag `0.1.6` published through the
> `krateo-blueprints/charts` publisher and the entry now reads, measured from
> the served index rather than inferred from a green workflow:
>
> ```
> version   0.1.6   created 2026-11-09
> sources   https://github.com/krateo-blueprints/openstack-nova-operator-kog
> icon      .../krateo-platformops/.github/main/brand/logo.svg
> ```
>
> **That reading is from 2026-11-09 and has NOT been refreshed for `0.1.7`
> (2026-11-17).** Whether the index has picked the new version up is unknown
> rather than assumed, so this note is dated instead of claiming the present
> tense. The heading used to read "now serves the current chart", which stopped
> being true the moment a newer tag existed.
>
> **AND THE READING CANNOT BE REPRODUCED FROM THIS REPO, which is the part worth
> fixing.** It says "measured from the served index" without naming the index.
> Checked on 2026-11-17: [charts.krateo.io/index.yaml](https://charts.krateo.io/index.yaml)
> (150 charts) and [marketplace.krateo.io/index.yaml](https://marketplace.krateo.io/index.yaml)
> (52 charts) both return 200 and **neither
> contains this chart**, so neither is the index that was measured. A
> measurement whose source is unrecorded is not re-runnable by the next reader,
> which is the same defect as a claim with no measurement behind it -- it just
> takes longer to discover. Whoever next has the index URL should put it here.
>
> The `sources` field is the load-bearing one: portal #383, widened by #384 to
> match case-insensitively, decides from the **newest** entry's `sources`
> whether to use the index or fall back to its bundled snapshot. The old `0.1.0`
> entry is still in the index — a helm index keeps its history — and that is
> fine, because only the newest is read. Installing by OCI reference with an
> explicit `--version`, as below, resolves against the registry and was never
> affected either way.

$ helm install nova oci://ghcr.io/krateo-blueprints/charts/openstack-nova-operator-kog --version 0.1.7 \
    -n krateo-system --create-namespace \
    --set nova.url="$NOVA_URL"
```

Toggle which Nova resources are exposed with `--set restdefinitions.<name>.enabled`
(see [configuration](./configuration.md)); `server` and `flavor` are on by default,
`quota` is off.

## Store the Keystone token

The controller reads the token from a Secret named `nova-token`. Create it from the
fetcher (the supported refresh path) and apply the `InstanceConfiguration` that
references it:

```console
$ ./scripts/get-token.sh --secret | kubectl apply -f -
$ kubectl apply -f chart/samples/nova-auth.yaml
```

`InstanceConfiguration` is generated by oasgen-provider from the OAS `apiKey`
security scheme; each Instance CR points at it via `spec.configurationRef`.

## Create a Nova resource

Edit `chart/samples/test-server.yaml` — set a real `flavorRef`, `imageRef` and network
UUID (from `openstack flavor list` / `image list` / `network list`) — then apply and
watch it reconcile:

```console
$ kubectl apply -f chart/samples/test-server.yaml
$ kubectl -n krateo-system get instances.nova.openstack.krateo.io -w
```

Additional kinds (`ComputeFlavor`, `ComputeKeypair`, `ComputeServerGroup`,
`ComputeAggregate`) have worked CRs in `chart/samples/extra-resources.yaml`. Each kind
references its own `<Kind>Configuration` carrying the same Keystone-token Secret pattern.

## Refresh the token

OVH Keystone tokens expire (~1h by default). When calls start failing with `401`,
re-run the refresh — it overwrites the `nova-token` Secret in place, and the
controller picks the new token up on its next reconcile:

```console
$ ./scripts/get-token.sh --secret | kubectl apply -f -
```

Keeping the token in a single Secret (never inline in `nova-auth.yaml`) avoids
clobbering a fresh token with a stale placeholder.

## Install as a Krateo Composition

Instead of `helm install`, register the blueprint with Krateo and drive it through a
Composition CR. Apply the registration once:

```console
$ kubectl apply -f compositiondefinition.yaml
```

Then apply an `OpenstackNovaOperatorKog` CR — the generated kind — to set the same
values through the Krateo control plane:

```console
$ kubectl create namespace openstack
$ kubectl apply -f examples/nova-composition/composition.yaml
```

Applying the Composition installs the operator packaging (the toggled
RestDefinitions); it does **not** create individual Nova resources — you still create
`Instance` / `ComputeFlavor` / ... CRs afterward. See
[examples](./examples.md) and [api](./api.md).

## Walkthroughs

- [quickstart](./quickstart.md) — the shortest path to a VM in Horizon.
- [e2e](./e2e.md) — the full OVH Public Cloud walkthrough from a clean machine,
  including a `kind` bootstrap and cleanup.
- [e2e-krateo-openstack](./e2e-krateo-openstack.md) — the same against a self-hosted
  Krateo-blueprint OpenStack, with screenshots.
