<p align="center">
  <img src="docs/krateo-loves-nova.png" alt="Krateo loves OpenStack Nova" width="900"/>
</p>

# openstack-nova-operator-kog

## What is this

A Krateo Operator Generator (KOG) blueprint that turns OpenStack **Nova** compute
resources into native Kubernetes custom resources — validated against
[OVH Public Cloud](https://www.ovhcloud.com/en/public-cloud/) as the managed OpenStack
target.

`kubectl apply` an `Instance` CR → KOG's
[`oasgen-provider`](https://github.com/krateo-platformops/oasgen-provider) and
[`rest-dynamic-controller`](https://github.com/krateo-platformops/rest-dynamic-controller)
reconcile it into a real VM. The chart ships **no controller code of its own**: it
bundles a curated subset of the Nova OpenAPI (one file per resource under
`chart/assets/`) and, for each resource you toggle, emits a `RestDefinition` that KOG
turns into a CRD plus controller. The generated kinds are `Instance` (Servers),
`ComputeFlavor`, `ComputeKeypair`, `ComputeServerGroup`, `ComputeAggregate`, and
`ComputeQuota`.

Nova authenticates with a Keystone token in `X-Auth-Token`, and the OpenAPI subsets
declare exactly that as an `apiKey` security scheme. oasgen-provider (>= 0.52.0) turns
it into `spec.authentication.apiKey.{tokenRef,header}` on the generated
`<Kind>Configuration` CRD, and rest-dynamic-controller sets the header on every
request. The controllers therefore call Nova **directly** — there is no proxy in the
request path. See [docs/architecture.md](docs/architecture.md).

## Install

KOG's `oasgen-provider` must already be in the cluster:

```bash
helm upgrade --install oasgen-provider-crds \
  oci://ghcr.io/krateo-platformops/charts/oasgen-provider-crds \
  --version 0.52.0 -n krateo-system --create-namespace --wait
helm upgrade --install oasgen-provider \
  oci://ghcr.io/krateo-platformops/charts/oasgen-provider \
  --version 0.52.0 -n krateo-system --wait
```

> **Use the OCI charts, not `charts.krateo.io`.** That repo still serves
> oasgen-provider appVersion `0.10.0`, which predates `apiKey` support and will
> reject this chart's security scheme. `apiKey` arrived in **0.19.0**, but pin
> **0.52.0**. Three independent reasons, the highest governing: below **0.26.1**
> the verbose request dump wrote credential headers to pod logs in cleartext and
> `X-Auth-Token` is a credential header; below **0.50.0** a credentialed
> request's destination host was never validated; and below **0.52.0** this
> chart's `ComputeQuota` generates three verbs that read a spec property the
> generator deleted (oasgen-provider#419).


Then install this chart, supplying the upstream Nova endpoint (required — the chart
hard-fails on an empty value):

```bash
# Discover the Nova URL + a Keystone token (OVH example)
export OS_AUTH_URL=https://auth.cloud.ovh.net/v3
export OS_USERNAME=... OS_PASSWORD=... OS_PROJECT_ID=... OS_REGION_NAME=GRA11
./scripts/get-token.sh --upstream
# NOVA_URL=https://compute.gra11.cloud.ovh.net/v2.1/<projectId>

helm install nova oci://ghcr.io/krateo-blueprints/charts/openstack-nova-operator-kog --version 0.1.5 \
  -n krateo-system --create-namespace \
  --set nova.url="$NOVA_URL"
```

Or install it as a Krateo Composition — apply the `CompositionDefinition`
(`compositiondefinition.yaml`) and drive it through an `OpenstackNovaOperatorKog` CR
(see [Examples](#examples)).

## Configure

Everything is `chart/values.yaml`, fully typed by `chart/values.schema.json`. The two
runtime inputs you always supply are `nova.url` (the region-specific Nova endpoint) and
a Keystone token stored in the `nova-token` Secret. Toggle which Nova resources are
exposed with `restdefinitions.<name>.enabled` (`server` and `flavor` on by default,
`quota` off). The whole surface is documented in
[docs/configuration.md](docs/configuration.md).

Keystone tokens expire (~1h on OVH). **Rotating that Secret is a secret-manager's
job** — point External Secrets Operator or Vault Agent at it. This chart ships no
refresh logic of its own; the Secret holds the raw token value only, so a rotation
path never has to know about HTTP framing.

You are responsible for keeping the Keystone token fresh (OVH default lifetime ~1h);
`./scripts/get-token.sh --secret | kubectl apply -f -` is the supported refresh path.

## Examples

- [examples/nova-composition](examples/nova-composition/README.md) — install the
  blueprint as a Krateo Composition (an `OpenstackNovaOperatorKog` CR).
- `chart/samples/` — worked CRs: `nova-auth.yaml` (the `apiKey` `InstanceConfiguration`),
  `test-server.yaml` (a sample `Instance`), `extra-resources.yaml`
  (`ComputeFlavor` / `ComputeKeypair` / `ComputeServerGroup` / `ComputeAggregate`).

```bash
./scripts/get-token.sh --secret | kubectl apply -f -
kubectl apply -f chart/samples/nova-auth.yaml
# edit chart/samples/test-server.yaml: flavorRef / imageRef / network UUID
kubectl apply -f chart/samples/test-server.yaml
kubectl -n krateo-system get instances.nova.openstack.krateo.io -w
```

## Ask an agent about this bundle (optional, not in the chart)

`kagent/` holds a [kagent](https://kagent.dev) `Agent` that answers questions about
this bundle's CRDs, samples and failure modes using the cluster's own state.

**It does not ship with the chart, and installing the chart does not give you one.**
It is a `kagent.dev/v1alpha2` resource you apply yourself, and it needs kagent in the
cluster plus a `ModelConfig` for whichever provider you use:

```bash
kubectl apply -f kagent/modelconfig-vertex-gemini.yaml   # edit projectID/location first
kubectl apply -f kagent/agent-nova-expert.yaml
kubectl -n kagent get agent nova-kog-expert
```

Install steps, the provider/secret matrix and the example questions are in
[kagent/README.md](kagent/README.md) — verified against kagent v0.9.6.

> [!NOTE]
> The Agent's own text states an `oasgen-provider` version (`>= 0.52.0` today).
> `scripts/validate.py` screens that it never drops **below the 0.26.1 hard
> security floor** — that screen exists *because* this file once advised
> `>= 0.20.0` — but it does **not** tie the number to the chart's current pin.
> So treat it as a floor claim, not a mirror of the pin: bumping the pin does
> not make this file fail CI. Update it in the same PR.

## Docs

Full documentation lives in [docs/](docs/):

- [docs/index.md](docs/index.md) — the map of the whole bundle.
- [docs/overview.md](docs/overview.md) — what the blueprint builds and the request flow.
- [docs/usage.md](docs/usage.md) — install, token, create Nova CRs, refresh the token.
- [docs/configuration.md](docs/configuration.md) — the whole values surface.
- [docs/api.md](docs/api.md) — the `CompositionDefinition` and the generated CRDs.
- [docs/examples.md](docs/examples.md) — the runnable examples and sample CRs.
- [docs/release.md](docs/release.md) — how a tag publishes the chart.
- [docs/architecture.md](docs/architecture.md) — diagram and the authentication design.
- [docs/quickstart.md](docs/quickstart.md) — the short path to a VM in Horizon.
- [docs/e2e.md](docs/e2e.md) — the full OVH Public Cloud walkthrough on `kind`.
- [docs/e2e-krateo-openstack.md](docs/e2e-krateo-openstack.md) — the same against a
  self-hosted [Krateo-blueprint](https://github.com/krateo-blueprints/krateo-openstack-blueprint)
  OpenStack, with screenshots.

## Develop & release

A single plain-SemVer tag (`X.Y.Z`, no `v` prefix) publishes the chart. **Nothing
is bumped by hand** — `chart/Chart.yaml` keeps `version: CHART_VERSION`, which the
tag stamps. Merge whatever the release should contain, then:

```bash
git tag X.Y.Z && git push origin X.Y.Z
```

`release-tag.yaml` stamps the `CHART_VERSION` placeholder from the tag, packages
`chart/`, pushes it to
`oci://ghcr.io/<owner>/charts/openstack-nova-operator-kog:X.Y.Z` — the artifact the
`CompositionDefinition`'s `spec.chart.url` points at — and attaches the stamped
`compositiondefinition.yaml` as a release asset. Nothing is bumped by hand: the tag
is the only source of the version. On every PR, `lint.yaml` (`helm lint` +
`helm template` + `kubeconform` + `shellcheck`, plus the shared `lint-docs`),
`validate.yaml` (repo guards and the `get-token.sh` refusals), `smoke.yaml` (a kind
cluster, twelve generated CRDs, every sample applied), `preflight-refs.yaml` and
`security.yml` run. Full runbook in
[docs/release.md](docs/release.md).

## License

Apache-2.0 — see [LICENSE](LICENSE).
