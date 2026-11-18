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
declare exactly that as an `apiKey` security scheme. oasgen-provider (**0.52.0, or >= 0.54.1 — not 0.53.0 or 0.54.0**, see
[configuration](./docs/configuration.md)) turns
it into `spec.authentication.apiKey.{tokenRef,header}` on the generated
`<Kind>Configuration` CRD, and rest-dynamic-controller sets the header on every
request. The controllers therefore call Nova **directly** — there is no proxy in the
request path. See [docs/architecture.md](docs/architecture.md).

<!-- THE MACHINE-READABLE FORM OF THIS REPOSITORY'S oasgen-provider FLOOR.
     Read by verify_provider_floor_declaration in scripts/validate.py, which
     requires exactly ONE such declaration and refuses a floor below 0.52.0, an
     exclusion at or below the floor, or a duplicate exclusion -- and also
     refuses an exclusion no prose in this file mentions, since the paragraph
     below is meant to say the same thing in words.

     THIS LINE NAMED A SCREEN THAT WAS NEVER IN THIS REPOSITORY until
     2026-11-18: hack/validate-findby-identifiers.py, which was going to arrive
     as a copy from another repository. So this declaration had NO READER while
     a comment said it had one -- the same defect this repo found in its own CI
     description, where README and docs/release.md both claimed a shellcheck
     step that `grep -ri shellcheck .github/` could not find. The four rules are
     now implemented here, from the specification this comment already gave.

     THE EXCEPTIONS ARE NOT A PREFERENCE. 0.53.0 and 0.54.0 broke apiKey RBAC, and
     all six of this chart's documents are apiKey -- so a bare ">= 0.52.0" would be
     a FALSE statement about this repository. The prose above says the same thing in
     words; this says it in a form a gate can parse.

     AND THE EXCLUSION LIST IS COMPLETE, not merely complete-for-now: the only
     oasgen-provider releases in [0.52.0, 0.54.1] are 0.52.0, 0.53.0, 0.54.0 and
     0.54.1, and no new version can ever appear between existing ones. So this
     window is closed and the list cannot rot. -->

**Minimum provider version:** `oasgen-provider >= 0.52.0 except 0.53.0, 0.54.0`

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
> [configuration](docs/configuration.md#installing-this-pin-onto-a-cluster-that-already-has-a-provider).
> On a cluster you created yourself moments ago there is nothing to check.

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

helm install nova oci://ghcr.io/krateo-blueprints/charts/openstack-nova-operator-kog --version 0.1.9 \
  -n krateo-system --create-namespace \
  --set nova.url="$NOVA_URL"
```

> [!NOTE]
> **Install by OCI reference with an explicit `--version`, as above, and the
> portal Marketplace index does not come into it.** The index does serve the
> current chart: the index URL, its three entries for this chart and why
> `sources` is the load-bearing field are measured and recorded once, in
> [usage](docs/usage.md#install-the-chart). A second copy of that measurement
> lived here until 2026-11-18 — and both copies sat inside the code fence above,
> rendering as shell script with no callout, from #57 onward. One copy is
> enough, and `scripts/validate.py` now fails on the shape that hid them.

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
> The Agent's own text states an `oasgen-provider` version (`0.52.0, or >= 0.54.1` today).
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
is the only source of the version. On every PR, `lint.yaml` (`shellcheck` over
`scripts/` and `hack/`, `kubeconform` over the rendered **built-in** kinds, an
*informational* `helm template` + `helm lint`, plus the shared `lint-docs`),
`validate.yaml` (repo guards and the `get-token.sh` refusals), `smoke.yaml` (a kind
cluster, twelve generated CRDs, every sample applied), `preflight-refs.yaml` and
`security.yml` run. Full runbook in
[docs/release.md](docs/release.md).

## License

Apache-2.0 — see [LICENSE](LICENSE).
