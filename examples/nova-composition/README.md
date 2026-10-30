---
type: Example
title: nova-composition — install the blueprint as a Krateo Composition
description: A runnable OpenstackNovaOperatorKog CR that installs the operator packaging (the toggled Nova RestDefinitions) through the Krateo control plane, then points you at the per-resource CRs.
resource: oci://ghcr.io/krateo-blueprints/charts/openstack-nova-operator-kog
tags: [example, composition, blueprint, nova]
timestamp: 2026-08-11T00:00:00Z
---

# nova-composition

An example [`composition.yaml`](./composition.yaml) — an `OpenstackNovaOperatorKog` CR
that installs this blueprint through Krateo instead of a bare `helm install`. Applying
it deploys the operator packaging: the `RestDefinition`s you toggle in
`spec.restdefinitions`. It does **not** create individual Nova resources — you do
that afterward with `Instance` / `ComputeFlavor` / ... CRs.

The generated Composition kind is derived from the sibling
[`compositiondefinition.yaml`](../../compositiondefinition.yaml):

- **group** — `composition.krateo.io`
- **version** — `"v"` + the full chart version, dots to dashes: chart `0.1.3` →
  `v0-1-3`. **Including the patch**, so this moves on every release. Read the real
  value with
  `kubectl get compositiondefinition openstack-nova-operator-kog -n krateo-system -o jsonpath='{.status.apiVersion}'`
  instead of copying it from here.
- **kind** — `OpenstackNovaOperatorKog` (PascalCase of the CompositionDefinition
  `metadata.name`)

## Run it

First register the blueprint (once), then apply the Composition CR:

```console
$ kubectl apply -f compositiondefinition.yaml
$ kubectl create namespace openstack
$ kubectl apply -f examples/nova-composition/composition.yaml
```

The one required input is `spec.nova.url` — the region-specific Nova
endpoint from your Keystone service catalog (replace `<projectId>` with your project
id). This example enables the `server` and `flavor` RestDefinitions and leaves the
rest off.

## Then create Nova resources

Once the packaging is installed, create the per-resource CRs. Worked examples live in
`chart/samples/` (`nova-auth.yaml`, `test-server.yaml`, `extra-resources.yaml`); the
flow (token Secret → `<Kind>Configuration` → resource CR) is documented in
[usage](../../docs/usage.md) and [api](../../docs/api.md).
