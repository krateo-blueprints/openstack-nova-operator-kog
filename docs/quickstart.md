---
type: Runbook
title: openstack-nova-operator-kog — quickstart
description: The shortest path from installing the operator to seeing a Nova compute Instance appear in the Horizon dashboard — install oasgen-provider, deploy the chart, apply an Instance CR.
resource: oci://ghcr.io/krateo-blueprints/charts/openstack-nova-operator-kog
tags: [quickstart, runbook, install, instance, horizon]
timestamp: 2026-08-11T00:00:00Z
---

# Quickstart — Nova (compute) operator

Manage OpenStack **Nova** compute as Kubernetes CRs. End to end: install the
operator, `kubectl apply` an `Instance`, and watch the VM appear in the Horizon
dashboard.

## 1. Prerequisites

Krateo's KOG provider in the cluster:

```bash
helm upgrade --install oasgen-provider-crds \
  oci://ghcr.io/krateo-platformops/charts/oasgen-provider-crds \
  --version 0.43.0 -n krateo-system --create-namespace --wait
helm upgrade --install oasgen-provider \
  oci://ghcr.io/krateo-platformops/charts/oasgen-provider \
  --version 0.43.0 -n krateo-system --wait
```

> **Kubernetes 1.36.0 or later.** Both oasgen-provider charts above enforce
> `kubeVersion: ">=1.36.0-0"` at RENDER time, so a 1.35 cluster fails with
> `chart requires kubeVersion: >=1.36.0-0 which is incompatible with
> Kubernetes v1.35.0` before anything is applied -- a Helm error from a chart
> you did not choose to install, which is a confusing place to meet it.
>
> **Use the OCI charts, not `charts.krateo.io`.** That repo still serves
> oasgen-provider appVersion `0.10.0`, which predates `apiKey` support and will
> reject this chart's security scheme. **Pin 0.43.0** (see
> [configuration](./configuration.md)); `apiKey` first worked in 0.19.0, but
> **0.26.1 is a hard floor** -- below it the `krateo.io/connector-verbose`
> request dump wrote credential *headers* to pod logs in cleartext, and this
> chart's credential IS a header.


A reachable Nova endpoint. The generated controllers call it directly and
authenticate with `X-Auth-Token`, taken from the Secret referenced by the
`<Kind>Configuration` CR; you supply a Keystone token (see `scripts/get-token.sh`).
For an in-cluster (Krateo-blueprint) Nova, the `nova-api` **ClusterIP** is the
safest value — it avoids depending on cluster DNS from the controller pod:

```bash
NOVA_IP=$(kubectl -n openstack get svc nova-api -o jsonpath='{.spec.clusterIP}')
PROJECT_ID=$(openstack project show admin -f value -c id)
helm upgrade --install nova-kog ./chart -n krateo-system \
  --set nova.url="http://$NOVA_IP:8774/v2.1/$PROJECT_ID"
kubectl -n krateo-system wait restdefinition/nova-kog-server --for=condition=Ready --timeout=300s
```

## 2. Supply a token + create an Instance

```bash
./scripts/get-token.sh --secret | kubectl apply -f -      # nova-token Secret
kubectl apply -f chart/samples/nova-auth.yaml             # InstanceConfiguration

# edit flavorRef / imageRef / network uuid for your cloud, then:
kubectl apply -f chart/samples/test-server.yaml
kubectl -n krateo-system get instances.nova.openstack.krateo.io -w
```

Within seconds the `Instance` CR is `Ready=True` and the VM is `ACTIVE` — the CR
records the server it created in `status.server.id`.

## 3. See it in Horizon

The instances the operator booted appear under **Compute → Instances**, `ACTIVE`,
on a Neutron network:

![Operator-created instances in Horizon](images/krateo-openstack/horizon-instances.png)

Beyond `Instance`, the operator also manages `ComputeFlavor`, `ComputeKeypair`,
`ComputeServerGroup` and `ComputeAggregate` the same way.

> **Two of those need the `admin` role, which a public cloud will not give you.**
> Nova gates flavor and aggregate writes behind `rule:context_is_admin`, so on
> OVH — this chart's validated target — creating a `ComputeFlavor` or a
> `ComputeAggregate` is expected to return **403** even though the CR is
> correct. `ComputeKeypair` and `ComputeServerGroup` are project-scoped and work
> with an ordinary user. Both admin-gated resources are still `enabled: true`,
> so the CRDs exist either way; the 403 arrives on apply, not on install. Not
> yet confirmed against OVH — it needs a live credential — so treat it as the
> expected outcome rather than a measured one.
