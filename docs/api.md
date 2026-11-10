---
type: API
title: openstack-nova-operator-kog — API
description: The contract this blueprint exposes — the CompositionDefinition that registers it, the generated OpenstackNovaOperatorKog Composition CRD and how its GVK is derived, and the per-resource Nova CRD kinds (Instance, ComputeFlavor, ...) with their Configuration objects.
resource: oci://ghcr.io/krateo-blueprints/charts/openstack-nova-operator-kog
tags: [api, compositiondefinition, crd, instance, restdefinition]
timestamp: 2026-08-11T00:00:00Z
---

# API

This blueprint has two API surfaces: the **Composition** surface (how Krateo installs
the chart) and the **generated Nova** surface (the CRDs oasgen-provider derives from
the chart's `RestDefinition`s).

## The `CompositionDefinition` (registration)

`compositiondefinition.yaml` registers the blueprint with Krateo. It is a
`core.krateo.io/v1alpha1` `CompositionDefinition` pointing at the published chart:

```yaml
apiVersion: core.krateo.io/v1alpha1
kind: CompositionDefinition
metadata:
  name: openstack-nova-operator-kog
  namespace: krateo-system
spec:
  chart:
    url: oci://ghcr.io/krateo-blueprints/charts/openstack-nova-operator-kog
    version: "0.1.0"
```

| field | meaning |
|---|---|
| `spec.chart.url` | the OCI location of the packaged chart. Resolves only once a release workflow has pushed the chart to the `charts/` namespace. |
| `spec.chart.version` | the published chart version; must match a released `Chart.yaml` `version`. |

## The generated `OpenstackNovaOperatorKog` CRD

Applying the `CompositionDefinition` makes core-provider generate a Composition CRD
from the chart's `values.schema.json`. The GVK is derived as:

- **group** — `composition.krateo.io`
- **version** — `"v"` + the **full** chart version with dots replaced by dashes
  (`"v" + strings.ReplaceAll(chartVersion, ".", "-")`). Note this includes the
  patch: chart `0.1.3` → `v0-1-3`, and a patch bump therefore changes the served
  apiVersion. Stale versions are pruned once no Composition still uses them, so an
  older apiVersion will not stay addressable as a fallback.
- **kind** — PascalCase of the CompositionDefinition `metadata.name`:
  `openstack-nova-operator-kog` → `OpenstackNovaOperatorKog`

A CR of that kind carries the chart's values as its spec. **No literal apiVersion is
correct here**: `chart/Chart.yaml` carries `version: CHART_VERSION`, substituted at
release time, so at no commit in this repo does a correct literal exist to write.
Substitute the value you read back:

> **Do not hardcode this version — read it off the object.** core-provider
> publishes the GVK it actually created on the CompositionDefinition's status:
>
> ```bash
> # Wait on the CONDITION first: an unresolvable chart sets Ready=False with
> # reason Unavailable and the registry error in the message, so this fails
> # fast and says why. Polling for a non-empty field explains nothing.
> kubectl wait compositiondefinition/openstack-nova-operator-kog -n krateo-system \
>   --for=condition=Ready --timeout=300s
>
>
> **`Ready`/`Available` on a CompositionDefinition did NOT mean the generated CRD
> answers — BELOW 2.13.46. At our documented floor and above, it is gated.**
> Measured in `internal/controllers/compositiondefinitions/compositiondefinitions.go`,
> with `rtv1.Available()` in the same file as the control so a zero is an answer
> rather than a failed read:
>
> ```
> tag        crdclient.IsReady(crd)     rtv1.Available()  (control)
> 2.13.44              0                        1
> 2.13.45              0                        1
> 2.13.46              1                        1
> ```
>
> At 2.13.46 this sits immediately before `SetConditions(Available())`:
>
> ```go
> if !crdclient.IsReady(crd) {
>     cr.SetConditions(rtv1.Creating().
>         WithMessage("crd for '…' is accepted but not yet Established"))
>     return
> }
> ```
>
> It arrived in `5830e94` — **the same commit as the drain-wait RBAC fix**, which
> is why both of this chart's 2.13.46 floor reasons land on one tag.
>
> An earlier version of this note claimed `IsReady` had "zero callers". That
> citation was wrong twice over and is corrected here: it counted the wrong call
> site (`crd.go`'s install/update watchers, which exist at every tag and were
> never what #269 was about), and the issue's own evidence —
> `deployment.go:85:// if IsReady(dep) {` — is a commented reference to a
> **different** `IsReady` taking a `*appsv1.Deployment`. The defect was real
> below .46; only the evidence was mismatched.
>
> **So on a pin at or above 2.13.46 you do not need this gate for first install.**
> It still costs nothing, and the real reason to keep it is below:
>
> ```bash
> kubectl wait --for=condition=Established \
>   crd/$(kubectl get compositiondefinition openstack-nova-operator-kog \
>           -n krateo-system -o jsonpath='{.status.resource}').composition.krateo.io \
>   --timeout=300s
> ```
>
> **THE HAZARD THAT SURVIVES THE .46 FIX IS AN UPGRADE, AND `Established` CANNOT
> COVER IT.** On the update path the gate is *vacuous*: the CRD already exists,
> so `Established` is already `True` and never goes back to `False` across a
> version bump — it returns immediately and attests nothing.
> core-provider's own comment says exactly this, and the fix is a different
> predicate, `servesVersion`, which first appears between 2.13.60 and 2.13.65
> (measured: absent at .60, present at .65). Before that, a CRD version bump
> passes a gate that cannot fail.
>
> This matters here specifically because oasgen-provider 0.53.0 carries
> `feat(restdefinition): re-render a CRD when the generator that made it was
> upgraded` (#460), so a provider bump *is* an update-path event. If you are
> bumping, check the version is actually **served**, not merely Established:
>
> ```bash
> kubectl get crd "$CRD" -o jsonpath='{range .spec.versions[*]}{.name}{"="}{.served}{"\n"}{end}'
> ```
>
> On core-provider ≥ 2.13.65 the update path waits until the version is actually
> served, so this check has a truthful answer to read rather than an
> `Established` that cannot fail (`a3b1956`, `crd.go:265`). That is the reason to
> prefer **2.13.68 or 2.13.71** over the 2.13.46 floor: .46 fixes first install,
> not upgrade.
>
> **NOT "or later", and this page used to say exactly that.** A floor is a claim
> about every version above it, and that set grew two members this page would not
> have wanted:
>
> | version | why it matters |
> |---|---|
> | **2.13.77** | **Avoid. Go straight to `2.13.78` or later if you go past `.71` at all.** It moves every local composition controller into core-provider's own namespace ([#313](https://github.com/krateo-platformops/core-provider/issues/313)) and **that migration is one-way** — dropping back below `.77` afterwards means deleting the moved Deployments by hand. It *also* deleted authn's per-controller `clientconfig` Secret on undeploy ([#416](https://github.com/krateo-platformops/core-provider/issues/416)), which `2.13.78` removed. |
> | `2.13.72`–`2.13.76` | **Unaudited.** Nobody has checked these for regressions the way `.64`/`.65` were checked. Not known-bad; not known-good either. |
>
> Reported by the core-provider session on 2026-11-10, measured at
> `kp/main 55f23ff`. The parts this page relies on are **unchanged**: the reason
> classification in `childhealth.go` is byte-identical between `2.13.71` and
> `main` — with a control, because an empty diff is also what a broken instrument
> produces: the same command reports 231 added and 16 removed lines between
> `2.13.60` and `2.13.71`. `servesVersion` is still present (4 non-test
> references at `main`).
>
> This is the same defect as oasgen-provider#314 for RestDefinitions, in a
> different component: a readiness signal that attests the write rather than the
> service. For a Composition *instance* the distinction IS carried — gate on
> `.status.conditions[Ready].reason`, where `ZeroEvidence` (`Status: Unknown`)
> means "not evaluated" and cannot be mistaken for healthy.
> kubectl get compositiondefinition openstack-nova-operator-kog -n krateo-system \
>   -o jsonpath='{.status.apiVersion}'   # e.g. v0-1-3 for chart 0.1.3
> kubectl get compositiondefinition openstack-nova-operator-kog -n krateo-system \
>   -o jsonpath='{.status.kind}'         # served too - no need to read the CRD
> kubectl get compositiondefinition openstack-nova-operator-kog -n krateo-system \
>   -o jsonpath='{.status.resource}'     # the plural, which you cannot reliably compute
> ```
>
> The plural genuinely is not derivable: core-provider resolves it through
> `plumbing/kubeutil/plurals`, not a string rule. One counterexample is enough
> to show why `strings.ToLower(kind)+"s"` is a guess rather than a rule — a
> kind like `Kaas` pluralises to `kaas`, not `kaass` — and a wrong plural
> fails silently, by matching nothing, rather than by erroring.
>
> These fields are populated once the CompositionDefinition reconciles
> successfully; they are absent while the Composition CRD does not yet exist.

```yaml
apiVersion: composition.krateo.io/<APIVERSION>   # substitute, do not hardcode
kind: OpenstackNovaOperatorKog
metadata:
  name: openstack-nova-operator-kog
  namespace: openstack
spec:
  nova:
    enabled: true
    upstreamUrl: https://compute.gra11.cloud.ovh.net/v2.1/<projectId>
  restdefinitions:
    server:
      enabled: true
    flavor:
      enabled: true
    keypair:
      enabled: false
    server_group:
      enabled: false
    aggregate:
      enabled: false
    quota:
      enabled: false
```

The full spec schema is `chart/values.schema.json`, documented field by field in
[configuration](./configuration.md). Applying this CR installs the operator packaging
(the toggled RestDefinitions); it does not create individual Nova resources.

## The generated Nova CRDs

Each enabled `restdefinitions.<name>` block produces a `RestDefinition`
(`ogen.krateo.io/v1alpha1`) that oasgen-provider turns into a CRD in group
`nova.openstack.krateo.io`. **The resource CRDs are served at `v2-1`, derived
from the OAS `info.version`; only the `<Kind>Configuration` halves are
`v1alpha1`.** An earlier version of this line said `v1alpha1` for both, which
would send a reader to a version the API server does not serve for these kinds
-- measured on 0.52.0, all twelve CRDs, in [configuration](./configuration.md):

| resource | Kind | Nova API |
|---|---|---|
| server | `Instance` | Servers (compute instances) |
| flavor | `ComputeFlavor` | Flavors |
| keypair | `ComputeKeypair` | Keypairs |
| server_group | `ComputeServerGroup` | Server Groups |
| aggregate | `ComputeAggregate` | Host Aggregates |
| quota | `ComputeQuota` | Quota Sets (Nova admin-only; 403 on a project-scoped credential) |

### Authentication: the `<Kind>Configuration` object

Because each OAS declares an `apiKey` security scheme (`in: header`,
`name: X-Auth-Token`), oasgen-provider also generates a companion
`<Kind>Configuration` CRD per resource (`InstanceConfiguration`,
`ComputeFlavorConfiguration`, ...). It carries the Keystone token reference; a
resource CR points at it via `spec.configurationRef`:

```yaml
apiVersion: nova.openstack.krateo.io/v1alpha1
kind: InstanceConfiguration
metadata:
  name: nova-config
  namespace: krateo-system
spec:
  authentication:
    apiKey:
      tokenRef:
        name: nova-token
        namespace: krateo-system
        key: token
      header: X-Auth-Token
```

### An `Instance` CR

The request body is envelope-wrapped, so the spec has a `server` block (hence the kind
is `Instance`, not `Server`):

```yaml
apiVersion: nova.openstack.krateo.io/v2-1
kind: Instance
metadata:
  name: kog-demo-1
  namespace: krateo-system
spec:
  configurationRef:
    name: nova-config
    namespace: krateo-system
  server:
    name: kog-demo-1
    flavorRef: "b2-7"
    imageRef: "00000000-0000-0000-0000-000000000000"
    networks:
      - uuid: "00000000-0000-0000-0000-000000000000"
```

Worked CRs for every kind live in `chart/samples/` (`nova-auth.yaml`,
`test-server.yaml`, `extra-resources.yaml`); see [examples](./examples.md).
