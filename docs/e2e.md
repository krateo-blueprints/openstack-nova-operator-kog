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
  --version 0.41.0 -n krateo-system --create-namespace --wait
helm upgrade --install oasgen-provider \
  oci://ghcr.io/krateo-platformops/charts/oasgen-provider \
  --version 0.41.0 -n krateo-system --wait
```

> **Use the OCI charts, not `charts.krateo.io`.** That repo still serves
> oasgen-provider appVersion `0.10.0`, which predates `apiKey` support and will
> reject this chart's security scheme. `apiKey` arrived in **0.19.0**, but pin
> **0.41.0** - see the security note in [configuration](./configuration.md).


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
helm install nova ./chart \
  --namespace krateo-system \
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
- **Instance stays `pending` in `kubectl get`**: the RestDefinition hasn't
  reconciled yet. `kubectl describe restdefinition` and check the
  `oasgen-provider` logs.
- **CRD rejected with `properties[spec].properties[server].properties[spec].type`**:
  the CRD Kind is colliding with the `server` envelope property. Keep
  `restdefinitions.server.resourceKind` set to something other than
  `Server` (the chart defaults to `Instance`).
