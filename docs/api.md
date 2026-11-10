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

> **Do not hardcode this version — read it off the object, and treat it as served only when `Ready` is `True`.** Verified by the core-provider session on 2026-11-10: those status fields name the CRD the apiserver **accepted**, not one it is **serving**. `RefreshCompositionDefinitionStatus` (`helpers/status/status.go:68-79`) writes them, and the code says so itself at `compositiondefinitions.go:1252-1260` ([#269](https://github.com/krateo-platformops/core-provider/issues/269)) — those fields "were correct before this change and the API they name could still 404". What makes "the API answers" true is the **`Ready=True`/`Available` gate on `crdclient.IsReady(crd)`** (Established), present from `2.13.46` and so in the `.68`/`.71` this page prefers. **The window I worried about does not exist:** during a bump `Observe` returns before the refresh, so the fields keep naming the previous, still-created GVK rather than an intended-but-uncreated one. core-provider
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
>
> **And that holds at every tag since.** The core-provider session walked
> `2.13.44`–`2.13.80` with a go/ast probe: `crdclient.IsReady` is absent at `.44`
> and `.45` — which reproduces the 0/0/1 we recorded, and is the probe's own
> calibration — then present in all 35 tags from `.46` to `.80`, sitting in
> `Observe` exactly 12 lines before `SetConditions(rtv1.Available())` at every
> one of them, with exactly one call of each per tag. So the `.46` pin in
> `smoke.yaml` and `released.yaml` still means what it says.
>
> **These measurements are of each tag's SOURCE. Tagged and released are
> different claims, and a source-level probe cannot tell them apart.** The probe
> reads `git show <tag>:<file>`, which answers whatever is in git whether or not
> the images were ever built and pushed. When these measurements were taken,
> `2.13.80`'s release workflow was still running and its artifacts had not been
> checked — so the upper end of the range was measured but **not installable**.
> The core-provider session flagged that limit on its own measurement before this
> page could print it wrong, and has since confirmed `2.13.80` **is** released:
> the workflow completed, the three images on ghcr carry revision `98e967c5`
> (`2.13.79` carries `55f23ff9`) and the three charts are at `2.13.80`, whose only
> change from `.79` is a logging fix. **Keep the distinction when reading any
> future row here**: a tag existing in git is not a version you can run.
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
> predicate, `servesVersion`, which **first appears in 2.13.63**. Before that, a
> CRD version bump passes a gate that cannot fail.
>
> Earlier versions of this page said "between 2.13.60 and 2.13.65" from two
> measurements of ours — absent at `.60`, present at `.65`. That bracketed the
> truth without naming it. The core-provider session has since walked every tag
> from `2.13.44` to `2.13.81` with a **go/ast probe rather than grep**, recording
> which function each call sits in: absent through `.62`, present from `.63`, as
> `servesVersion(gvr.Version)` passed as the watcher predicate in
> `ApplyOrUpdateCRD` at line 280 at every tag `.63`–`.81`.
>
> **And it is consulted, not merely present** — a distinction worth insisting on,
> since presence is the cheaper thing to measure and the two get conflated. The
> branch runs after `kube.Apply` of a CRD that already existed and was mutated
> (a version appended, or a column backfilled); an unmutated CRD returns earlier,
> so no wait is needed there, and the create path keeps `IsReady` deliberately.
> The surrounding 61 lines are byte-identical by md5 from `.63` to `.80`, so the
> branch has not moved (source again — see the tagged-versus-released note above;
> `.80`'s artifacts have since been verified). Stated scope, theirs: branch structure and position after
> `Apply` were read, not runtime behaviour at each tag.
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
> On core-provider ≥ 2.13.63 the update path waits until the version is actually
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
> | **2.13.77** | **Avoid. Go straight to `2.13.78` or later if you go past `.71` at all.** It moves every local composition controller into core-provider's own namespace ([#313](https://github.com/krateo-platformops/core-provider/issues/313)) and **that migration is one-way** — dropping back below `.77` afterwards means deleting the moved Deployments by hand. It *also* tried, on undeploy, to delete a **retired** controller's authn `clientconfig` Secret (`cdc-<ns>-<sa>-clientconfig`) **without the RBAC to do so**, which **wedged retirements and definition deletes** ([#416](https://github.com/krateo-platformops/core-provider/issues/416)); `2.13.78` stopped touching it. **Corrected 2026-11-10 by the authn session: an earlier version of this row said `.77` *deleted* that Secret.** It did not — the delete failed for lack of permission, so the symptom is a stuck undeploy, not a controller losing its credential. The blast radius is per-controller: only the retired controller's `cdc-<ns>-<sa>` mapping and Secret are named, and since authn 0.40.0 the Secret is owned by its mapping and garbage-collected with it, so nothing needs delete rights on Secrets. Even had one been deleted, a **live** controller recovers by itself — its next `/serviceaccount/login` re-mints and writes it synchronously. **What this row does NOT claim:** that an undeploy already wedged under `.77` resumes on its own after upgrading to `.78`. That is core-provider's to confirm and **remains unclaimed by them too**: by code, a `.77` undeploy wedged on the forbidden delete returned the error and was **requeued**, so each retry after the roll runs `.78`'s path without that delete — making it **expected by code (the error is requeued, not recorded terminally) and not observed on a wedged object**. They also note they did not re-verify authn's half of this row. |
> | `2.13.72`–`2.13.76` | **Unaudited for regressions** the way `.64`/`.65` were checked. Not known-bad; not known-good. The three properties this page actually relies on **have** been measured in the *source* of `.72`–`.81`, so this means "nobody has looked for anything else", not "nobody has looked". |
> | `2.13.79`–`2.13.81` | Released. `.79` adds opt-in delegation, a node-local OTLP default and [#414](https://github.com/krateo-platformops/core-provider/issues/414) dry run; `.80` is a logging fix; `.81` bounds helm operations at 15m per hook ([#410](https://github.com/krateo-platformops/core-provider/issues/410)) and sweeps the Delete-path RBAC. **That sweep is NOT bindings-only — and my saying so was an error I imported from a mention count rather than a scope read — but it is still not related to the `.77` Secret story above**, which is worth saying because the two read as adjacent: confirmed from source at `2.13.80` and `main` by the core-provider session, [#414](https://github.com/krateo-platformops/core-provider/issues/414) deletes only RoleBindings and ClusterRoleBindings whose subjects are all ServiceAccounts, plus unreferenced non-Helm Roles, and [#426](https://github.com/krateo-platformops/core-provider/issues/426) adds that same sweep to the Delete path. **Corrected 2026-11-10 by the core-provider session, who named their own phrasing as the source:** `RemoveDanglingControllerBindings` (`tools/deploy/danglingrbac.go:195-249`, same code at `.79`–`.82`) deletes the dangling RoleBindings/ClusterRoleBindings **and then, for each Role or ClusterRole one of them referenced, deletes that role too** when no remaining binding references it and it carries no helm release annotation. Accurate form: **"dangling controller bindings, and the roles only they referenced — never Secrets, never authn mappings"**. **And it removes nothing by default:** deletion requires `danglingControllerRBAC.remove` to be true, and the chart ships `false`, which only logs a dry run. **Confirmed at the `2.13.81` tag itself** by the core-provider session (`git show 2.13.81:…`, not `.80` and not `main`): `danglingrbac.go` has exactly two `Delete` calls — one on RoleBindings/ClusterRoleBindings, one on Roles/ClusterRoles that no remaining binding references and Helm does not own — and the subject filter admits a binding only when **every** subject is a ServiceAccount. Mentions of `secret|authn|serviceaccountmapping` in those files: **0** — and the control I previously quoted for that zero is **withdrawn by its author**: `rolebinding` counting 10 was a *mention* count, not a proof of scope. The zero stands on the code read above, not on that ratio. And for the `.77` hazard specifically, lines mentioning both `clientconfig` and `Delete` number **1** at `.81` — a comment reading `// NOT deleted here: authn's clientconfig Secret for this identity` — against **4** at `.77`, which had the delete. That contrast is the control that makes the 1 readable.

> **But the Delete path is not only the sweep, and this is the distinction to carry away.** Undeploy *does* delete the controller's authn **mapping**, via `deleteAuthnServiceAccountMapping` — deliberately **in every version since tag `2.3.0`** — the core-provider session placed it: present at `2.3.0`, absent at `2.2.0`, called unconditionally from undeploy (`deploy.go:739-741` as of `.82`), and a no-op when the mapping or the authn CRD is absent — and **not** as part of #414's sweep. authn's `clientconfig` Secret then disappears **by garbage collection**, through the ownerReference authn 0.40.0 puts on it. **That half is authn's claim, not core-provider's**, who state they have not verified it. So "core-provider does not delete the Secret" is true and is not the same as "nothing removes it": the mapping is removed on purpose and the Secret follows. #416 was never about that path — it was `.77` reaching for the Secret *directly*, without the rights to do it.

> **`2.13.82` is published** (verified here against `ghcr.io/krateo-platformops/charts/core-provider`: 89 tags, newest `2.13.82`, no `.83`). Its only change to the sweep is a tighter subject check ([#433](https://github.com/krateo-platformops/core-provider/issues/433)), reported by the core-provider session. **This page said `.81` was the newest published tag earlier the same day, and that was true when written** — a reminder that "newest" is a standing assertion about the future, like any `>=`, and goes stale by the hour rather than by the release. The `.68`/`.71` preference is unchanged: nothing above has been audited for the three properties this page rests on. The core-provider session reports the three properties unchanged through `.81` and **declines to recommend any of them over `.68`/`.71`**, on the explicit ground that "runs on one cluster" is not the property this table rests on. This page keeps the `.68`/`.71` preference. |
>
> **`2.13.81` is the newest published tag** — verified here on 2026-11-10 against
> `ghcr.io/krateo-platformops/charts/core-provider` (88 tags, max `2.13.81`, no
> `.82`). Note the registry path: a first probe of the dead org — the same name
> with the hyphen removed — answered with 118 tags topping out at `1.0.1` and no
> `2.13.x` at all, which reads exactly like "those versions do not exist". The
> dead org returns **more** tags than the live one, so volume is not evidence of
> being the right repository. Always probe the hyphenated path CI itself uses.
>
> This paragraph cannot name that org, and the reason is the second half of the
> lesson. `lint-docs` (org-level,
> `krateo-platformops/.github/.github/workflows/lint-docs.yaml@main`) bans the
> dead org's name as a literal in any doc, and it **failed this very commit** —
> a guard cannot distinguish documenting a trap from falling into it, and
> should not try. So the repository already held a guard against the exact
> mistake above. It did not help, because it gates committed files and the
> mistake was an ad-hoc `curl` in a shell. **A guard protects the artifact, not
> the operator**; the measurement that goes into a doc is made before the doc
> exists.
>
> **Past `.81`, property 3 must be re-measured before it is relied on:**
> `childhealth.go` changed on `main` in
> [#428](https://github.com/krateo-platformops/core-provider/issues/428) and
> [#432](https://github.com/krateo-platformops/core-provider/issues/432), both
> landing *after* `.81`. The byte-identical finding below is a statement about
> `.71` vs the `55f23ff` snapshot, not about whatever `main` is now.
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
