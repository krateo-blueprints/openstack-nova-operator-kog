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
$ helm install nova oci://ghcr.io/krateo-blueprints/charts/openstack-nova-operator-kog --version 0.1.8 \
    -n krateo-system --create-namespace \
    --set nova.url="$NOVA_URL"
```

> [!NOTE]
> **The Marketplace index serves `0.1.7`, and this is the index it was read
> from** — the one thing every earlier version of this note could not tell you.
> **The install above now pins `0.1.8`, so these two numbers differ on purpose:**
> the reading below is dated evidence about the index, not a pin, and whether the
> index has picked `0.1.8` up is unknown rather than assumed until someone reads
> it again. Bumping the numbers inside a dated measurement to match the current
> release would turn evidence into a false claim, which is the defect this whole
> note exists to record.
> Measured 2026-11-18 from
> [krateo-blueprints.github.io/charts/blueprints/index.yaml](https://krateo-blueprints.github.io/charts/blueprints/index.yaml)
> (HTTP 200, `apiVersion: v1`, 819 chart names / 3004 versions parsed, 3 entries
> for this chart):
>
> ```
> version   0.1.7   created 2026-11-17T22:06:08
> sources   https://github.com/krateo-blueprints/openstack-nova-operator-kog
> icon      https://raw.githubusercontent.com/krateo-platformops/.github/main/brand/logo.svg
> ```
>
> `0.1.7` is both the first entry in file order and the newest by `created`, so
> the two possible readings of "newest" agree here and the distinction costs
> nothing. The `icon` was fetched rather than copied across: 200, 5219 bytes,
> body begins `<svg` — a dead icon URL and a live one look identical in that
> field, so the field on its own is evidence of nothing.
>
> **The index URL was in this repo's own history the whole time.** This note
> used to say the reading "CANNOT BE REPRODUCED FROM THIS REPO" because no index
> was named — and the index had been named, in the `[!CAUTION]` block that #57
> added and #75 rewrote in place. `git log -L 70,74:docs/usage.md` prints it.
> Two other indexes were checked on 2026-11-17 and are NOT it, which is worth
> keeping so the next reader does not re-check them:
> [charts.krateo.io/index.yaml](https://charts.krateo.io/index.yaml) (150 charts)
> and [marketplace.krateo.io/index.yaml](https://marketplace.krateo.io/index.yaml)
> (52 charts) both return 200 and neither contains this chart.
>
> `sources` is the load-bearing field: portal #383, widened by #384 to match
> case-insensitively, decides from the **newest** entry's `sources` whether to
> use the index or fall back to its bundled snapshot. The old `0.1.0` entry
> (created 2026-08-19 — a different architecture, with the `nova-auth-bridge`
> nginx reverse proxy that was deleted when this chart moved to a native
> `apiKey` scheme) is still in the index, because a helm index keeps its
> history, and that is fine: only the newest is read. Installing by OCI
> reference with an explicit `--version`, as above, resolves against the
> registry and was never affected either way.
>
> **This callout rendered as a shell-session code block from #57 (2026-11-07)
> until 2026-11-18.** It was inserted directly beneath an open fence, so GitHub
> swallowed all 37 lines of it plus the `helm install` command underneath —
> `/markdown` returned one `<pre>` and zero alert divs for this section. The
> fences paired, so the fence screen passed it eleven times.
> `verify_admonition_not_in_code_fence` in `scripts/validate.py` is the screen
> that asks the other question, and it found the same defect in `README.md`.

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
