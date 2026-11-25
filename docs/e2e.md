---
type: Runbook
title: openstack-nova-operator-kog — end-to-end on OVH Public Cloud
description: The full walkthrough against a real OVH Public Cloud project, from a clean machine — bootstrap a kind cluster, install the operator, fetch a Keystone token, boot a VM from an Instance CR, and clean up.
resource: oci://ghcr.io/krateo-blueprints/charts/openstack-nova-operator-kog
tags: [runbook, e2e, ovh, kind, walkthrough]
timestamp: 2026-08-11T00:00:00Z
---

# End-to-end walkthrough on OVH Public Cloud

This guide walks through deploying the chart against a real OVH Public
Cloud project, from a clean machine with no Kubernetes cluster.

## Prerequisites

- An OVH Public Cloud project (you'll need its `OS_PROJECT_ID`, the
  32-character hex tenant id).
- An OpenStack user inside that project (created from the OVH manager
  or via `openstack user create`). **Not** your `ovhcloud.com` login.
- Local tools: `docker` (or any container runtime), `kind`, `kubectl`,
  `helm`, `curl`, `jq`.

## 1. Bootstrap a kind cluster

```bash
kind create cluster --name nova-kog
kubectl create namespace krateo-system
```

## 2. Install the Krateo OASGen Provider

```bash
helm upgrade --install oasgen-provider-crds \
  oci://ghcr.io/krateo-platformops/charts/oasgen-provider-crds \
  --version 0.74.0 -n krateo-system --create-namespace --wait
helm upgrade --install oasgen-provider \
  oci://ghcr.io/krateo-platformops/charts/oasgen-provider \
  --version 0.74.0 -n krateo-system --wait
```

> **Use the OCI charts, not `charts.krateo.io`.** That repo still serves
> oasgen-provider appVersion `0.10.0`, which predates `apiKey` support and will
> reject this chart's security scheme. `apiKey` arrived in **0.19.0**.

> [!NOTE]
> **`0.74.0` here is the version the Krateo installer ships, and every install
> page in this repo now names the same one.** This page used to say `0.52.0` —
> the declared **floor** — which was defensible on its own and confusing
> alongside README and the quickstart, both of which said `0.74.0`. A reader
> could not tell which number applied to them.
>
> **The floor is still 0.52.0 and still declared**, in README and enforced by
> `verify_provider_floor_declaration`. What changed is that **no page tells a
> human to install it**, because the floor is exercised where it cannot rot: the
> smoke workflow runs a two-arm matrix, `floor / oasgen 0.52.0` and
> `platform / oasgen 0.74.0`, on every PR. Coverage that depends on somebody
> following a walkthrough is not coverage.
>
> **Versions to avoid as of 2026-11-21:** `0.53.0` and `0.54.0` (no managed
> resource can read its auth Secret), and `0.85.0` and `0.85.1` (both retracted
> upstream — neither can create a generated controller at all). `0.80.0` and
> `0.84.0` are measured safe on the `apiKey` question; see
> [configuration](./configuration.md) for the table and what it does **not**
> claim.


Wait for the controller to be ready:

```bash
kubectl -n krateo-system rollout status deploy/oasgen-provider
```

## 3. Discover your Nova endpoint and fetch a Keystone token

Source your OpenStack credentials. The simplest is to export them:

```bash
export OS_AUTH_URL=https://auth.cloud.ovh.net/v3
export OS_USERNAME=<openstack-user>
export OS_PASSWORD=<openstack-password>
export OS_PROJECT_ID=<32hex-project-id>
export OS_REGION_NAME=GRA11    # or SBG5, DE1, BHS5, WAW1, ...
```

Run the helper to get the token **and** the region-specific Nova URL:

```bash
./scripts/get-token.sh --upstream
# TOKEN=gAAAAA...
# NOVA_URL=https://compute.gra11.cloud.ovh.net/v2.1/<projectId>
```

Capture both - you'll need `NOVA_URL` to install the chart and `TOKEN`
to seed the Secret.

## 4. Install this chart

```bash
helm install nova oci://ghcr.io/krateo-blueprints/charts/openstack-nova-operator-kog --version 0.1.11 \
  --namespace krateo-system --create-namespace \
  --set nova.url="https://compute.gra11.cloud.ovh.net/v2.1/<projectId>"
```

Verify:

```bash
kubectl -n krateo-system get restdefinitions
kubectl -n krateo-system get deploy
#         nova-server-controller                         1/1   (appears once Ready)
# Wait for the RestDefinition to reach Ready=True - that means the
# generated CRDs have been installed and the rest-dynamic-controller
# for them has been deployed.
kubectl get crd | grep nova.openstack.krateo.io
# Expect two CRDs:
#   instances.nova.openstack.krateo.io
#   instanceconfigurations.nova.openstack.krateo.io
```

## 5. Provision a server

Push the Keystone token into a Secret (refresh whenever it expires;
OVH's default lifetime is ~1h):

```bash
./scripts/get-token.sh --secret | kubectl apply -f -
```

Apply the InstanceConfiguration CR (it references the `nova-token`
Secret created above, and is what the Instance CR points at via
`spec.configurationRef`):

```bash
kubectl apply -f chart/samples/nova-auth.yaml
```

Pick a flavor / image / network from your project:

```bash
openstack flavor list
openstack image  list
openstack network list
```

Edit `chart/samples/test-server.yaml` with those values, then:

```bash
kubectl apply -f chart/samples/test-server.yaml
kubectl -n krateo-system get instances.nova.openstack.krateo.io -w
```

Cross-check on OVH:

```bash
openstack server show kog-demo-1
```

## 6. Cleanup

```bash
kubectl delete -f chart/samples/test-server.yaml
helm -n krateo-system uninstall nova
helm -n krateo-system uninstall oasgen-provider
kind delete cluster --name nova-kog
```

## Token refresh

Keystone tokens expire (~1h on OVH). **Keeping the Secret fresh is a
secret-manager's job** — point External Secrets Operator, Vault Agent, or your
platform's equivalent at `nova-token` and let it own minting and rotation. The
chart deliberately ships no CronJob, no sidecar and no refresh logic: the Secret
holds the raw token value only, which is exactly what a rotation path can write.

For a local demo you can bootstrap or refresh it by hand:

```bash
./scripts/get-token.sh --secret | kubectl apply -f -
```

The next reconciliation picks up the new token automatically — nothing needs
restarting, because the token is read per request.

## Troubleshooting

- **`401 Unauthorized` from Nova**: the token in the Secret is expired
  or wasn't scoped to the right project. Re-run `get-token.sh --secret`.
- **`404` on `POST /servers`**: confirm `nova.url` includes
  the `/v2.1/<projectId>` suffix.

  > **THE LATCH HAS A VERSION WINDOW, AND THIS PAGE USED TO SAY IT DID NOT.**
  > (Not a GFM alert on purpose: this blockquote is indented inside a list item,
  > where `[!IMPORTANT]` renders as literal text rather than an alert box --
  > hack/check-doc-rendering.py refused the first version of this edit for
  > exactly that.)
  > An earlier version of this entry said a `404`'d create stamps
  > `external-create-pending` and **never clears it**, citing
  > [oasgen-provider#818](https://github.com/krateo-platformops/oasgen-provider/issues/818)
  > measured on `0.80.0`, and added *"so it is not specific to any recent
  > release"*. That generalisation was wrong. The clearing happens in
  > **unstructured-runtime**, not in rest-dynamic-controller, which is why
  > reading the two oasgen worktrees could not settle it. Measured here by
  > reading the runtime at the versions each oasgen tag pins, with the tree's
  > `.go` file count as the control that the tree was read at all:
  >
  > | oasgen | runtime | `SetExternalCreateFailed` on a create error | a `404` |
  > |---|---|---|---|
  > | `0.52.0`–`0.74.0` | `v1.18.0` | called **unconditionally** (`pkg/controller/worker.go:1300`) | **clears** |
  > | `0.75.0`–`0.83.1` | `v1.22.0`+ | guarded by `createCommittedNot(actionErr) \|\| !created.Requested` | **latches** |
  > | `0.84.0` | — | `404` joins the committed-not set | **clears again** |
  >
  > **At this chart's floor (`0.52.0`) and at the version the Krateo installer
  > ships (`0.74.0`), a `404`'d create CLEARS the stamp** — the guard does not
  > exist yet, so the failure is recorded unconditionally and
  > `external-create-pending` stops being the newest marker. The `0.80.0`
  > measurement was correct for `0.80.0`, which sits inside the window.
  >
  > **All three rows are this repo's own measurement.** The `0.84.0` row was
  > relayed when first written and is now read here, from the committed-not
  > classification's own `switch`, at each published tag:
  >
  > ```
  > go/rest-dynamic-controller/internal/controllers/create_not_committed.go
  >   <= 0.74.0            the file does not exist -- no classification at all
  >   0.75.0, 0.80.0       case StatusBadRequest, StatusUnprocessableEntity
  >   0.82.0, 0.83.1       case StatusBadRequest, StatusUnauthorized,
  >                             StatusUnprocessableEntity
  >   0.84.0               case StatusBadRequest, StatusUnauthorized,
  >                             StatusNotFound, StatusUnprocessableEntity
  > ```
  >
  > So a `404`'d create is classified "not committed" from `0.84.0` only, which
  > is why the latch releases there. Below `0.75.0` there is no classification to
  > consult and the clearing comes from the runtime instead, as the first row
  > says. (`0.84.0` was published 2026-11-18; a tag is not a release, and this
  > was read after its artifact sweep reported 5/5.)

  **If you are running a provider in the `0.75.0`–`0.83.1` window, check the CR
  after fixing the URL, because fixing it may not be enough.** There, `404` is
  excluded from the "refused without creating" classification that would clear
  the stamp, and on this chart that compounds: **no kind here declares a
  `findby`**, so the existence check cannot answer either, and the CR has no
  road back to a clean state.
  **An earlier version of this entry said "delete and re-create it"; that may
  not work.** The oasgen-provider session states (2026-11-18, from
  [#793](https://github.com/krateo-platformops/oasgen-provider/issues/793)'s own
  comment) that a kind which cannot answer existence *"sits at
  `CreateOutcomeUnknown` indefinitely AND CANNOT BE DELETED"*, and that the
  recovery gate runs **before** the deletion dispatch — so `kubectl delete` can
  hang rather than clear it. Marked as their reading of their code, not measured
  here.

  **What to do, in this order.** Fix the URL. Then **verify at the vendor that
  nothing was created** — `openstack server list`, or the Horizon console — which
  a `404` from a wrong project path makes very likely, since the request never
  reached a valid endpoint. **Only once that is confirmed** is removing the CR's
  finalizer the escape, and it is a deliberate last resort: a refused delete
  holds its finalizer **on purpose**
  ([#261](https://github.com/krateo-platformops/oasgen-provider/issues/261)), so
  that a CR cannot disappear while a resource it represents still exists and
  still costs money. Removing it without checking the vendor first is how you
  turn a stuck CR into an orphaned paid instance.
- **Instance stays `pending` in `kubectl get`**: *check the two causes above
  first.* The entry below used to be the only answer here and it is the
  **less likely** one once a `401` or `404` has occurred — a latched
  `external-create-pending` from a refused create produces the same `pending`
  and sends you to the provider logs for nothing. Only if neither applies: the
  RestDefinition hasn't reconciled yet, so `kubectl describe restdefinition`
  and check the `oasgen-provider` logs.
- **CRD rejected with `properties[spec].properties[server].properties[spec].type`**:
  the CRD Kind is colliding with the `server` envelope property. Keep
  `restdefinitions.server.resourceKind` set to something other than
  `Server` (the chart defaults to `Instance`).
