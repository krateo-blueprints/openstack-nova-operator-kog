---
type: Configuration
title: openstack-nova-operator-kog — configuration
description: The whole values surface — the per-resource RestDefinition toggles, the Nova endpoint, and the serviceAccount and name overrides — all typed by values.schema.json.
resource: oci://ghcr.io/krateo-blueprints/charts/openstack-nova-operator-kog
tags: [configuration, values, restdefinitions, apikey]
timestamp: 2026-08-11T00:00:00Z
---

# Configuration

Everything is `chart/values.yaml`, fully typed by `chart/values.schema.json`. The
schema requires `nova`, and `nova.url` in particular — the chart hard-fails on an
empty Nova endpoint. When the chart is installed as a Krateo
Composition, the same keys are the spec of the generated `OpenstackNovaOperatorKog` CR
([api](./api.md)).

## RestDefinition toggles (`restdefinitions.*`)

One block per Nova resource. Each emits (when enabled) a ConfigMap holding the OAS
subset plus a `RestDefinition` that oasgen-provider turns into a CRD and controller.

| resource | key | `enabled` default | generated `resourceKind` |
|---|---|---|---|
| Nova Servers (instances) | `restdefinitions.server` | `true` | `Instance` |
| Nova Flavors | `restdefinitions.flavor` | `true` | `ComputeFlavor` |
| Nova Keypairs | `restdefinitions.keypair` | `true` | `ComputeKeypair` |
| Nova Server Groups | `restdefinitions.server_group` | `true` | `ComputeServerGroup` |
| Nova Host Aggregates | `restdefinitions.aggregate` | `true` | `ComputeAggregate` |
| Nova Volume Attachments | `restdefinitions.volume_attachment` | `false` | `ComputeVolumeAttachment` |
| Nova Quota Sets | `restdefinitions.quota` | `true` | `ComputeQuota` |

Every block carries the same three keys:

| key | default | effect |
|---|---|---|
| `enabled` | per table above | emit the ConfigMap + RestDefinition for this resource. |
| `resourceGroup` | `nova.openstack.krateo.io` | the Kubernetes API group of the generated CRD; the GVK becomes `<resourceGroup>/v1alpha1`. |
| `resourceKind` | per table above | the generated CRD Kind. **Prefixed on purpose** — a Kind matching the envelope property name (`Server`, `Flavor`, ...) collides in `crdgen` and yields an invalid schema, so the kinds are `Instance`, `ComputeFlavor`, etc. |

`volume_attachment` is the one kind off by default; `quota` was, until the oasgen
0.52.0 pin. The original reason it was off: Nova's `PUT /os-quota-sets/{tenant_id}` rejects the
`tenant_id` path field leaking into the body (`additionalProperties: false`), which
needs a KOG path-only / excluded-from-body mapping that has not landed yet. See
`chart/assets/quota.yaml`.

## The Nova endpoint (`nova.*`)

| key | default | effect |
|---|---|---|
| `nova.url` | `""` | **THE key input** — the Nova Compute endpoint from your Keystone service catalog, e.g. `https://compute.gra11.cloud.ovh.net/v2.1/<projectId>`. Required; empty fails the install with an explicit message. |

The generated controllers call this URL **directly**. There is no proxy in the request
path: the OAS declares an `apiKey` security scheme (`in: header`,
`name: X-Auth-Token`), oasgen-provider turns that into
`spec.authentication.apiKey.{tokenRef,header}` on the generated `<Kind>Configuration`
CRD, and rest-dynamic-controller sets the header on each request itself (mechanism and source lines: [architecture.md](./architecture.md#how-authentication-works-and-why-there-is-no-proxy), verified at `origin/main` by the oasgen-provider session).

The URL must be resolvable and reachable from inside the cluster. For an in-cluster
Nova, the `nova-api` **ClusterIP** remains the safest value — the historic reason (an
nginx `resolver` that could not see `*.svc.cluster.local`) died with the proxy, but an
IP literal still avoids depending on cluster DNS from the controller pod.

## Version floor (security)

Pin **oasgen-provider `0.52.0`, or `0.54.1` or later. NOT `0.53.0` and NOT
`0.54.0`** — both break every kind this chart ships.

> [!CAUTION]
> **`0.53.0` and `0.54.0` cannot read their auth Secret when the Configuration
> uses `apiKey`, and all six of this chart's documents use `apiKey`** — `type:
> apiKey`, `in: header`, `name: X-Auth-Token`, zero bearer. So this is not a
> partial exposure: it is every generated kind.
>
> `0.53.0` carries the breaking change `fix(deploy,restdefinition)!: the
> auth-secret RBAC actually scopes credential access now`
> ([#475](https://github.com/krateo-platformops/oasgen-provider/pull/475)),
> which introduced the collector that gathers credential references. That
> collector never saw an `apiKey` `tokenRef`, fixed by
> [#501](https://github.com/krateo-platformops/oasgen-provider/pull/501).
> `0.54.0`'s own notes say the affected resources *"cannot read their credential
> in any namespace and never reconcile"*.
>
> **The evidence differs per version and is labelled deliberately:**
>
> | version | carries #501 | status |
> |---|---|---|
> | version | collector covers `apiKey` | blanket `secrets` grant | verdict |
> |---|---|---|---|
> | `0.52.0` | **no** (0 mentions) | **present** | **safe, and the pin** — the grant covers the gap |
> | `0.53.0` | **no** (0 mentions) | **removed** | **CONFIRMED broken** |
> | `0.54.0` | **no** (0 mentions) | removed | **CONFIRMED broken** — also measured on live images |
> | `0.54.1` | **yes** (3 mentions) | removed | fixed |
> | `0.55.0` | yes | removed | fixed |
> | `0.56.0` | yes | removed | fixed |
> | `0.60.0` | **yes** (3 mentions) | removed | fixed |
> | `0.69.0` | **yes** (3 mentions) | removed | fixed |
> | `0.74.0` | **yes** (3 mentions) | removed | fixed — **and what the installer ships** |
> | `0.80.0` | **yes** (3 mentions) | removed | fixed — **and what dp303 installed as of 2026-11-21** |
> | `0.84.0` | **yes** (3 mentions) | removed | fixed |
> | `0.85.0` | **yes** (3 mentions) | removed | fixed — **RETRACTED, creates no controllers** |
> | `0.85.1` | **yes** (3 mentions) | removed | fixed — **RETRACTED, creates no controllers** |
> | `0.98.0` | **yes** (3 mentions) | removed | fixed — **pinned by the installer and running on a live cluster (PEER-REPORTED, below)** |
> | `1.0.0` | **yes** (3 mentions) | removed | fixed — **not a compatibility statement (PEER-REPORTED, below)** |
>
> **THE READING NOW COVERS THREE FILES.** `authsecretrbac.go` only *consumes* the
> auth-secret paths. The list is *derived* in `oas2jsonschema/auth_secret_refs.go`
> (one path per `SecretKeySelector` field of every auth-method shape, sorted;
> `authsecretrbac.go:41` is `var authSecretRefPaths = oas2jsonschema.AuthSecretRefPaths()`),
> and that derivation reflects over `BasicAuth`, `BearerAuth` and `APIKeyAuth`,
> which live in a **third** file, `oas2jsonschema/types.go`. Renaming a json tag
> or adding a `SecretKeySelector` field there moves the grant while the deriver's
> sha never does. `hack/measure-apikey-grant.py` now reads all three; a version is
> safe only if all three were measured. Measured 2026-11-28, every read gated on
> `gh`'s exit code **and** a `^package ` line:
>
> ```
> authsecretrbac.go     0.54.0  16010 bytes, 0 apiKey mentions
>                       0.74.0, 0.98.0, 1.0.0  17276 bytes, 3 mentions (identical)
> auth_secret_refs.go   0.54.0  ABSENT (HTTP 404)
>                       0.74.0, 0.98.0, 1.0.0  3714 bytes, sha256 d904185f55f0,
>                       7 apiKey occurrences (6 lines), 1 SecretKeySelector (identical)
> types.go              BasicAuth  {usernameRef, passwordRef}
>                       BearerAuth {tokenRef}   APIKeyAuth {tokenRef}   4 fields
>                       identical at 0.54.0, 0.74.0, 0.98.0 and 1.0.0
>                       raw bytes 24479 / 26213 / 29067 / 29067 (the script prints
>                       decoded characters, 24466 / 26200 / 29054 / 29054: a CONSTANT 13 lower at every tag,
>                       which is a property of the encoding and not of any version)
> ```
>
> **Why each file is controlled differently.** The deriver's control is
> **presence** (absent at `0.54.0`, present at `0.74.0`), not its sha or counts: a
> content assertion would break the day upstream legitimately edits the file.
> `ABSENT` is a fact about the tag and is not "0 mentions". For `types.go` the
> asserted quantity is the **tag set per struct**, never its sha or size: the file
> grew 24479 → 26213 → 29067 bytes while the four tags never moved.
>
> **`0.54.0` agrees with the safe versions on the `types.go` read, which is
> exactly why it cannot control it.** `APIKeyAuth.TokenRef` already existed at
> `0.54.0`; the break was the hand-written path list omitting it, not a missing
> field. So that read is identical at the broken and the fixed versions and cannot
> discriminate them. It is controlled by synthetic fixtures in `--self-test`
> (two refs reads 2, none reads 0 and is distinct from absent, a renamed tag
> reports the new tag, an absent struct reads ABSENT, and plain-string fields
> `Header` and `ValuePrefix` are not matched). A drift in the tag set is reported
> as **CHANGED** (exit 4): the derived paths moved and the grant must be
> re-measured, which is not a verdict that the version is unsafe.
>
> **PEER-REPORTED, NOT MEASURED HERE.** From the installer session, measured by
> them on cluster `krateo-057` on 2026-11-28: the installer pins oasgen-provider
> `0.98.0` **and a live cluster runs it**. All three reads are identical from
> `0.74.0` through `1.0.0`, so the grant the six apiKey documents depend on **is
> intact at the version running in production today**. The gap this closed was
> not only about a future bump: `0.98.0` sat 24 minors outside everything this
> table had measured while it was the version in use. From the oasgen-provider
> session: `1.0.0` is **not** a compatibility statement — zero CRD, chart,
> `values.yaml` or `values.schema.json` changes from `0.99.0`, a nil-contract fix
> in the rest client plus a dependency bump; the only consumer-visible consequence
> is the end of a `<1.0.0` range constraint. `0.91.0` was retracted for a
> reason unrelated to apiKey or RBAC: it published its images and none of its
> charts after a reusable workflow pinned to `@main` gained a required input, so
> the call never started. Use `0.91.1` — **a remedy is a prediction unless someone
> verified it**, and nobody has verified this one here; the `0.85.x` retractions are
> reconciled below.
>
> **A safe reading here is still a READ of three source files, not a run.**
>
> **`apikey-secret-read-arm.yaml` IS A RUN, NOT A READ.** Everything above is from
> reading files. `hack/probe-apikey-rbac.sh` is the careful probe that watches
> generated controllers against a real cluster: it plants its own Secret and
> `<Kind>Configuration`, waits for oasgen to *say* it granted something, and only
> then asks `can-i` of each controller's ServiceAccount (itself gated on `can-i`
> being shown able to answer both yes and no). Its cluster run previously
> happened only through `hack/gke-run.sh`. **The arm is not a new measurement.**
> It makes that probe dispatchable in CI at any version (inputs `oasgen`,
> required, and `negative_control`, default `0.54.0`), on a fresh kind cluster per
> version, and adds the one thing nothing did: a comparison. The subject must read
> clean **and** the control must fail **for the apiKey reason** (`CANNOT read
> Secret`). At the control version only, a synthetic **bearer** document is stood
> up in its own namespace (`hack/plant-bearer-control.sh`), because at `0.54.0`
> the correct behaviour of the broken path list on an apiKey-only cluster is to
> record nothing, and "oasgen never recorded a grant" cannot tell *the grant path
> did not run* from *the grant path ran and found no apiKey paths*. Bearer's
> `tokenRef` is in that list, so a recorded bearer grant proves the path ran.
> **Held** = bearer recorded and apiKey reports `CANNOT read Secret` or `never
> recorded`; bearer not recorded = VOID (setup); bearer recorded and apiKey clean
> = the instrument is in question.
>
> **A limitation of the existing probe, found here and not changed:** the
> mixed-corpus guard in `hack/probe-apikey-rbac.sh` says every document must be
> apiKey but reads `status.hasSecuritySchemes`, a boolean equally true for bearer
> and basic, so a non-apiKey document in the *same* namespace would pass it
> silently. That is why the bearer document lives in a different namespace.
>
> **What it does not say:** one run is one version on one cluster, and a pass is
> not a statement about any other version. It is credential-free, so it shows the
> controller can read the Secret, not that any vendor accepts what is in it.
> **Read-verified, not yet run-verified:** the provider watches all namespaces
> (`go/oasgen-provider/main.go` at `0.54.0`, `0.74.0` and `1.0.0`: the manager's
> `cache.Options` carries only `SyncPeriod`, `DefaultNamespaces` occurs 0 times),
> so the bearer document in its own namespace should be reconciled; if it never
> reaches Ready the control is VOID, a limit of the control and never a finding
> about apiKey. The probe's Configuration-CRD scan is cluster-wide, so the bearer
> Kind carries a digit on purpose to stay out of it. At the time of writing the arm had **not yet been dispatched**.
>
> **`0.53.0` IS BROKEN, AND THIS PAGE USED TO SAY "INFERRED". IT IS NOW
> MEASURED.** An earlier version of this table said nobody had run `0.53.0` and
> that [#500](https://github.com/krateo-platformops/oasgen-provider/issues/500)
> named only `0.54.0`. Both were true; the conclusion no longer needs them,
> because the break is **two independent code facts that have to coincide**, and
> both are readable at the tags:
>
> ```
> go/oasgen-provider/internal/tools/deploy/authsecretrbac.go   case-insensitive "apikey"
>   0.52.0  0        0.53.0  0        0.54.0  0        0.54.1  3
>   0.56.0  3        0.60.0  3        0.69.0  3        0.74.0  3
>
> each read with a BYTE-COUNT CONTROL, because "0 mentions" and "the file was
> not read" are the same number: 13138/16017/16017/16866 bytes at the four
> older tags, 17271/17271/17283/17283 at the four newer ones. And the
> instrument is DISCRIMINATING, not merely consistent -- the same code path
> returns 0 at 0.52.0, 0.53.0 and 0.54.0 with healthy byte counts, so a 3 is a
> fact about the file and not about the reader (measured 2026-11-18).
>
> the cluster-wide ClusterRole with an UNcommented `secrets` resource
>   0.52.0  1 of 4 files, 0 "NO BLANKET" markers
>   0.53.0  0 of 4 files, 3 "NO BLANKET" markers      <- #473/#475 removed it
> ```
>
> **THE REASON THIS PAGE GAVE FOR `0.52.0` BEING SAFE WAS WRONG.** It said
> `0.52.0` "predates the collector entirely". It does not — the collector is
> there, and it is **equally gapped**, with zero `apiKey` mentions. What makes
> `0.52.0` work is the second column: the blanket cluster-wide `secrets` grant
> was still present, so the ServiceAccount already held a superset and the
> collector's gap cost nothing. `#473`/`#475` removed that grant in `0.53.0`,
> which is the moment the latent gap became a failure. The conclusion was right
> and the mechanism was wrong, which is the more dangerous way to be correct:
> anyone reasoning forward from "predates the collector" would conclude that any
> version with a collector is unsafe, and that is not what governs.
>
> Confirmed by the oasgen-provider session from its own source, then
> **re-measured here independently at each tag** rather than relayed — the
> numbers above are this repo's own, and they agree. #501's commit message is
> the primary statement of the mechanism: *"#473/#475 had removed the blanket
> cluster-wide `resources: [\"secrets\"]` ClusterRole grant that was covering
> the gap."*
>
> **TWO RETRACTED VERSIONS KEEP THEIR ROWS DELIBERATELY, AND EACH WAS RETRACTED
> FOR TWO DEFECTS THAT TWO ACCOUNTS NAMED SEPARATELY.** `0.85.0` and `0.85.1` both
> read **safe** on this one measure and both are unusable. Measured in the rdc
> Deployment asset and `values.yaml` of the oasgen-provider chart (upstream files,
> not ones in this repo) at `0.84.0`, `0.85.0`, `0.85.1` and `0.85.2`; a dispatch
> of `apikey-safe-set.yaml` held its controls (`0.54.0` → 0 mentions, `0.74.0` → 3).
>
> **Both defects are the same shape: a long-harmless line met a new default.**
>
> **First, the indentation, which was DORMANT rather than new.** At `0.84.0`
> `- name: metrics` is already at indent 14 where its sibling `- name: http` is at
> 12. It never broke anything because it sits inside
> `{{- if ne (int .Values.rdc.metrics.port) 0 }}` and `metrics.port` defaults to
> `0`, so the block never rendered (peer-reported to date from `0.77.0`; confirmed
> present and dormant at `0.84.0`). `0.85.0` added `- name: health` with the same
> mis-indentation, but `healthProbePort` defaults to `8081`, so it **did** render
> and the asset stopped parsing. What shipped was not a new mistake but a new
> **default** that made an old one reachable; the thing hiding the bug was a
> default, not a check, which is why no test caught it. The `0.85.0` → `0.85.1`
> diff is exactly the `health` and `metrics` entries, six lines, both moved from
> indent 14 to 12.
>
> **Second, the probe (PEER-REPORTED by the oasgen-provider session, confirmed in
> the asset and `values.yaml`).** The unconditional
> `{{- toYaml .Values.rdc.livenessProbe | nindent 12 }}` is **not itself the
> defect**: `0.84.0` emits identically, but the values key is **absent** there, so
> `toYaml nil` renders `null`, which Kubernetes accepts. The defect is the
> **combination**: `0.85.0` introduced `rdc.livenessProbe: {}` as a default
> (alongside a `readinessProbe` of `httpGet /readyz:8081`), so the manifest carried
> `livenessProbe: {}` and the API server refused it ("must specify a handler").
> `0.85.1` kept that default. `0.85.2` keeps the same defaults but changes the
> **template** to `{{- with ... }}`, so an empty value omits the field, and its own
> comment quotes that admission error.
>
> So `0.85.0` had **two** defects and its parse failure **masked** the probe one;
> `0.85.1` fixed only the indentation and stayed unusable for the probe reason.
> **Neither account was wrong** — each named a different defect, and only together
> do they explain both retractions. This page's earlier text was incomplete, not
> wrong.
>
> **Use `0.85.2`. `0.84.0` is the fallback, and strictly worse as a target.**
> `0.85.2` is the first version whose **template** fixes the probe emission
> (measured in the asset), **and it is verified here**: runs `44930009330` and
> `44931943538` measured it twice on a real cluster (see "How a lost Lease
> presents" below), each with a healthy control controller throughout. That
> measurement REQUIRES generated controllers to exist and reconcile, so it is
> evidence of controller creation working at `0.85.2`, from our own CI.
> `0.84.0` is verified clean on both defects (metrics mis-indentation dormant
> behind a `0` default; no `{}` probe default), but the readiness listener does not
> exist there at all, which is why `lease-readiness-arm.yaml` VOIDs at `0.84.0` by
> design. Use it only if you need a version predating the listener.
>
> A retraction does two jobs: recording that a version is bad, which is true
> forever, and recommending a remedy, which is a prediction about a version nobody
> may have installed. The second job has been wrong before on this very entry,
> which once read "fixed in `0.85.1`" before anyone found `0.85.1` could not
> create a controller either. The lesson is to **check a remedy rather than to
> distrust it**; for `0.85.2` the check was made, and it held.
>
> For the helm map-merge behaviour of `readinessProbe: {}` versus `null`, see the
> WARNING under "How a lost Lease presents" below, which is the one copy of that
> fact and the richer one; it is not repeated here so the two cannot drift.
>
> **They keep their rows because an ABSENT row is ambiguous.** A reader who
> measures `0.85.1` themselves gets `3 mentions, safe` and, finding no row here,
> cannot tell whether it was never measured or measured and judged fine. A row
> that says *safe on this measure, retracted for another reason* answers both
> questions and demonstrates the caveat below in the same breath.
>
> **THIS TABLE MEASURES ONE PROPERTY, AND `0.85.0` IS THE REASON TO SAY SO.**
> Every row answers a single question — does the collector grant `apiKey` secret
> access — and a `fixed` verdict is **not** a statement that the version is
> usable. `0.85.0` is `fixed` here and **must not be pinned**: it carries an
> unrelated fatal defect, measured in the provider's own asset rather than taken
> on report.
>
> ```
> assets/rdc/deployment.yaml      `- name: http` sits at indent 12
>   0.84.0   `- name: metrics` at indent 14, but behind
>            `{{- if ne (int .Values.rdc.metrics.port) 0 }}`, and that port
>            defaults to 0 -- so it never renders and 0.84.0 is fine AT THE DEFAULT
>   0.85.0   `- name: health` at indent 14 with NO conditional
> ```
>
> A deeper indent makes the entry a nested list inside the previous item's
> mapping, so the asset does not parse. At `0.85.0` nothing gates it, and the
> provider session measured the consequence on a live cluster: **454 decode
> errors and 0 of 25 RestDefinitions regenerated**, with a fresh install creating
> no controllers either. That cluster reading is theirs; the indentation above is
> this repo's own read of the tagged asset.
>
> **THE RETRACTION LAGGED THE DEFECT BY A WINDOW, AND THE WINDOW IS THE POINT.**
> The provider's published retraction list — the one installer's CI consults to
> refuse a pin — is at
> [krateo-platformops/oasgen-provider](https://github.com/krateo-platformops/oasgen-provider/blob/main/retracted-versions.yaml).
> Measured twice on 2026-11-21, with the entry count as the control that the read
> works:
>
> ```
> ~09:55Z   8 entries? no -- 7: 0.53.0 0.54.0 0.83.0 0.77.0 0.70.0 0.69.0 0.66.0
>           0.85.0 ABSENT from the file entirely (33617 bytes, 444 lines)
> ~10:05Z   8 entries, 0.85.0 FIRST, naming all three charts, with the reason
>           (35859 bytes, 474 lines)
> ```
>
> **So for a period the broken release was public and its marker was not** — the
> defect and its remedy shipped in opposite directions. During that window the
> gate that exists to stop the pin would have passed it, and the list's own
> header is the argument: *"a 'do not pin' sentence in a release note is not a
> control: nothing evaluates it."* The entry landed after this repo raised it,
> which is the only reason the window is measured rather than merely survived.
>
> **`0.80.0`, `0.84.0` AND `0.85.0` WERE ADDED ON 2026-11-21, AND THE REASON IS
> THE SAME ONE AS LAST TIME, WHICH IS WHY IT IS WORTH STATING AGAIN.** The
> `krateo-openstack-blueprint` data-plane test (dp303) does **not** take its
> provider from a blueprint pre-release: it installs **oasgen-provider `0.80.0`
> itself, from OCI, as of 2026-11-21**, so that is the version these KOGs run
> under there. **The attribution is dated deliberately** — if that test's pin
> moves, the measurement below stays true while the reason it was taken would
> not, and an undated claim about someone else's harness is one we cannot keep
> honest from here. This
> table stopped at `0.74.0`, so the version actually in use sat **six minors
> outside everything measured** — the identical shape to the `0.74.0` addition,
> found the same way: a peer corrected an assumption rather than letting it ride.
>
> **`0.80.0` has TWO independent readings, which is better than the set's others
> have.** The source read above, and a live one from the same day's cluster run
> (`44884303626`, this chart installed at `0.80.0`):
>
> ```
> RestDefinition .status at oasgen 0.80.0
>   generatorBuild            0.80.0-6e9c275073cbd3d1e64b44aa907b71f0252dbec8
>   authSecretRBACNamespaces  ["krateo-system"]    <- POPULATED, so a grant happened
>   authSecretDigest          b9a4dc59fdd56edc     <- a real digest, NOT the FNV
>                                                     offset basis (the digest of
>                                                     no input, which is what an
>                                                     empty cluster produces)
> ```
>
> Those two fields are exactly the pair that made this probe's first version
> raise a **false alarm**: on a cluster with no auth Secret and no CRs, oasgen
> correctly grants nothing, and "cannot read a Secret that nobody asked for" was
> reported as a break. Populated here, they are the live half of the claim.
>
> **ONE HONEST NOTE ON THE INSTRUMENT.** The byte counts measured on 2026-11-21
> are exactly **7 lower** than those recorded on 2026-11-18 — on *both* control
> tags (`0.54.0` 16010 vs 16017, `0.74.0` 17276 vs 17283). A tag is immutable, so
> a genuine per-tag difference is impossible; an equal offset across two
> independent tags is a property of **how the bytes are fetched**, not of the
> files. The `apiKey` counts, which is what the verdict rests on, agree exactly.
> Recorded rather than smoothed over, because the next person to re-measure will
> get one of these two numbers and should know the other is not a contradiction.
>
> **`0.54.1` or later is a claim about a set, so every new tag gets measured.**
> `0.60.0`, `0.69.0` and `0.74.0` were added on 2026-11-18 for a specific
> reason: the README documents installing `0.74.0` and this table stopped at
> `0.56.0`, so the version a reader is told to install sat 18 minors outside
> everything that had been measured. The governing fact holds at all three.
> `0.55.0` (tagged 2026-11-08) and `0.56.0` (tagged 2026-11-09) were each checked
> the day they appeared, and both carry #501:
> `git merge-base --is-ancestor b7d33b2 0.55.0^{commit}` reports *ancestor*,
> where `b7d33b2` is #501's merge commit. Both controls were run — the fix
> against itself (must be an ancestor: it is) and the fix against `0.52.0` (must
> **not** be: it is not) — because `--is-ancestor` communicates through its exit
> code, and an exit code is exactly the kind of result that reads the same
> whether it answered the question or never ran.
>
> That check is not ceremony. A patch tag does not have to flow forward: `0.54.1`
> could have been cut from the `0.54.0` line with `0.55.0` branched off `main`
> before #501 landed, which would make `0.55.0` a *higher* version carrying
> *none* of the fix. This repo has already been caught by the mirror image of
> that shape — `0.51.0` was tagged 90 minutes **before** #421 merged, so
> `compare <fix>...0.51.0` returns `behind=6` and the `ComputeQuota` floor is
> `0.52.0` rather than the `0.51.x` the release dates suggest. **Version order
> is not fix order; only ancestry is.**

> [!NOTE]
> **What "exercised" means for this pin, precisely.** CI installs the provider,
> generates all twelve CRDs, reconciles every RestDefinition to `Ready`, and
> server-dry-run applies every shipped sample and the Composition example. It
> points `nova.url` at `nova.invalid`, which is unroutable by design, so **no
> reconciliation against a responding Nova endpoint happens in CI** and no
> credential is ever present. Behaviour that only appears when a vendor API
> answers -- retries, pagination, existence checks, token acceptance -- is
> therefore *not* covered by a green run here. `hack/probe-361.sh` exercises
> that path against an in-cluster mock when a specific question needs it.
>
> **Verified outside CI, and worth distinguishing from the above.** The
> **WHICH REDIRECT THE ARM ACTUALLY EXERCISES, and which two it does not.**
> `hack/probe-redirect.sh` points the chart at
> `mocknova.<ns>.svc.cluster.local:8080` and has it redirect to
> `redirect-target.<ns>.svc.cluster.local:8080`. Those are **siblings**: they
> differ in the first label and share the rest. Measured, not eyeballed — the
> target is not a suffix of the origin, the hosts are unequal, and the ports are
> identical.
>
> That matters in both directions.
>
> It makes the `0.49.0` result **stronger** than first written. Go's `net/http`
> drops `Authorization` when a redirect leaves the original host, so a
> sibling-host hop is one the standard library would have stripped by itself.
> The token arriving anyway is therefore direct evidence that the credential was
> being attached unconditionally at the request path, rather than evidence that
> the stdlib let it through.
>
> It also means the `0.50.0` green is **narrower than "off-host redirects are
> refused"**. The two hops the standard library *permits* are not exercised
> here:
>
> | hop | what `net/http` does | exercised by this arm |
> |---|---|---|
> | sibling host (`a.ns.svc` → `b.ns.svc`) | strips | **yes** |
> | subdomain (`foo` → `sub.foo`) | **permits** | no |
> | port only (`foo:8080` → `foo:9090`) | **permits** (`url.Hostname()` ignores the port) | **yes, measured 2026-11-12** |
>
> The second and third rows are not hypothetical: the oasgen-provider
> maintainers report a measurement table in `authn.go` showing `Bearer <token>`
> **arriving** on exactly those two shapes, under a stdlib comment that
> explicitly permits `foo.com` → `sub.foo.com`. That is their measurement and
> their code, not ours — recorded here because it is the reason the gap in our
> coverage is worth naming rather than a theoretical completeness note.
>
> **The port-only hop has now been measured, and the credential does not cross
> it.** `hack/probe-portonly.sh` puts two ports on one Service, so `:8080`
> answers the create with a `302` to `:9090` on the same host — precisely the
> shape a host check written with `url.Hostname()` would permit. Measured
> 2026-11-12 on a GKE cluster at oasgen `0.61.0`:
>
> ```
> :8080 requests carrying the credential   1
> :9090 requests, with a credential        0
> :9090 requests, without one              0
> ```
>
> and the CR's own condition carries upstream's explanation:
>
> > the create request will not be retried because it cannot succeed as
> > declared: making request: Post `http://…:9090/v2.1/flavors`: **refusing to
> > follow a redirect: refusing to send a credentialed request to `…:9090`: it
> > is not the document's server and is not in the operator's endpoint
> > allowlist (`http://…:8080`)**: endpoint not permitted by the host policy
>
> The allowlist named in that message is the document's own server, so the
> permitted endpoint and the refused one differ **only in the port**.
>
> **What this does NOT establish, and the distinction is the reason the probe
> has two arms.** A policy that refused *every* redirect would produce an
> identical row. So the measured claim is **"the differing-port hop is
> refused"** — not "the policy compares ports". Separating those needs the
> probe's second arm, which points the chart at the host with **no** port
> (defaulting to 80) and hops to the same host, and must be **permitted**.
> That arm is written and **has not been run**: the first arm's refusal is
> terminal (`will not be retried because it cannot succeed as declared`), so
> re-pointing the chart against the same CR produced no second attempt. It now
> applies its own CR; the run has not been repeated. `openstack-ironic` and
> `oasgen-provider` both agree this is the honest summary until it has.
>
> One caveat recorded rather than smoothed: that run's conditions also showed a
> transient RBAC error, because the per-Kind auth-secret Role is provisioned
> from the Configuration's `tokenRef` on reconcile and the probe read conditions
> while it was still propagating. The **per-request attribution** is what makes
> the arm readable anyway — the mock logged a request *carrying the token* on
> `:8080`, which separates "the hop was refused" from "no credential existed
> yet".
>
> The subdomain shape is not naturally reachable through Kubernetes Service DNS,
> so it stays untested here and belongs upstream where `authn`'s own table
> already covers it.
>
> Read the row below as **"a sibling-host hop is refused"**, now joined by **"a
> differing-port hop is refused"** — and still not as a statement about every
> off-host hop.
>
> `0.50.0` host policy was exercised in a kind cluster against responding
> endpoints: `hack/probe-redirect.sh` showed an off-host redirect refused,
> and `hack/probe-361.sh` showed a legitimate same-host create still
> succeeding. Neither is a verdict alone — the first arm's mock answers the
> create *with* the redirect, so a refusal there is also produced by a
> policy that refused everything. Those runs are by hand, load-gated, and
> not part of a green PR.
>
> **A second alternative explanation was not excluded at the time, and is
> worth stating because it is the reassuring one.** A refusal is also what you
> see if the redirecting mock never served the 302 — a regressed `return 200`,
> a `location` that stopped matching, an nginx that rejected the block. The
> probe counted the controller's POSTs to that mock but not what the mock
> answered, so a broken instrument and a working security control produced
> identical output. Closed on 2026-11-12: the probe now reads the mock's own
> access log for a POST answered with a 302 and VOIDs if there is none, with
> the detector re-proven on every run against both a 302 and a 200 line. The
> refusal recorded above has **not** been re-measured under that gate.
>
> **Three states, measured ON THE CREATE PATH, and the one that matters is that
> a 401 is indistinguishable from an unreachable vendor.** Measured 2026-11-11
> by `hack/probe-401.sh` on a kind cluster at the `0.52.0` pin: one
> `ComputeFlavor`, measured twice per run, with the endpoint changed by a
> `helm upgrade` in between, uid unchanged across both reads. Every arm below
> failed at **create** — the reason values are specific to that verb, which the
> first version of this table did not say.
>
> | | 401 (credential rejected) | 403 (authorization denied) | vendor unreachable |
> |---|---|---|---|
> | `Ready` | `False` / **`Creating`** | `False` / **`AuthorizationDenied`** | `False` / **`Creating`** |
> | `Synced` | `False` / `ReconcileError` | **`True` / `ReconcileSuccess`** | `False` / `ReconcileError` |
>
> **401 and unreachable are identical on both conditions, on create.** 403
> differs on both.
>
> **What that run could not rule out.** `probe-401.sh` asserted that its mock
> served the status and that the CR carried the condition — but not that the
> controller had ever *reached* the mock, and its own control request used the
> same path with no distinguishing marker. So the `AuthorizationDenied` row in
> particular rested on attributing a condition to an endpoint nothing proved
> was called, and that condition has other sufficient causes: a missing
> `tokenRef` Secret, or an RBAC denial reaching the same reason. Closed on
> 2026-11-12: the probe marks its own requests, proves on its own traffic that
> the log is readable *before* reading any absence, and VOIDs unless at least
> one request from a caller that is not the probe reached the mock. **The table
> above has not been re-measured under that gate**; it is reported as recorded,
> with the gap named.
>
> **And the "401" column is wider than it looks: at Keystone, a *missing*
> credential is byte-identical to a *rejected* one.** Measured against a real
> Keystone — OpenStack-Helm 2025.1, `keystone-api:5000/v3`, `GET /v3/projects`,
> from an in-cluster curl pod — by the `keycloak-provider-kog` session, not by
> this repo and not against a mock:
>
> | request | status | body |
> |---|---|---|
> | bogus `X-Auth-Token` | `401` | `{"error":{"code":401,"message":"The request you have made requires authentication.","title":"Unauthorized"}}` |
> | **no** `X-Auth-Token` | `401` | **byte-identical to the above** |
> | endpoint that does not resolve | *no HTTP status at all* | curl exit 6 |
>
> So three operator-visible situations collapse into one condition on the create
> path: a wrong token, no token at all, and an unreachable vendor. The transport
> failure is distinguishable from the two 401s at the HTTP layer, but **not**
> through the CR's conditions, per the table above. If a `ComputeFlavor` reports
> `False` / `Creating`, do not read it as "the token is wrong" — check that the
> `tokenRef` Secret exists and has the key, before suspecting its value.
>
> Scope of that measurement, as stated by the session that made it: a
> **malformed** token, not a genuinely **expired** one, and not a valid token
> for the wrong project (Keystone answers that with a 403, which this table's
> 403 column covers from a different direction). Those two remain unmeasured.
>
> **The reason values are verb-dependent; the indistinguishability is not known
> to be.** `openstack-ironic-operator-kog` ran the same status codes against
> Ironic on the **observe** path, every arm gated on the mock's own request
> count, and measured something this table would have mispredicted:
>
> | observed on `get`/observe | `Ready` | `Synced` |
> |---|---|---|
> | 401 | `Unknown` / `ObserveFailed` | `False` / `ReconcileError` |
> | 418 | `Unknown` / `ObserveFailed` | `False` / `ReconcileError` |
> | 403 | `False` / `AuthorizationDenied` | `False` / `ReconcileError` |
>
> So on observe a 401 does not get `Creating` at all, and 401 and 418 are
> byte-identical apart from the integer. `Creating` vs `ObserveFailed` is a
> **verb** distinction.
>
> **What that does and does not do to the claim above.** It does NOT refute it:
> their arms compare 401 against *418*, not against an unreachable vendor, and
> an unreachable vendor on the observe path would plausibly also report
> `ObserveFailed` — in which case 401 and unreachable stay indistinguishable on
> observe too, just under a different reason. **That gap has since been
> measured, and it closes the way the paragraph above guessed.**
> `openstack-ironic-operator-kog` ran unreachable-on-observe with the endpoint
> string never edited — the mock scaled to zero replicas, so the Service still
> resolves and refuses TCP — and with the witness one layer out, because an
> unroutable endpoint produces zero requests at the mock *by construction* and
> "0 calls because unreachable" is the same number as "0 calls because nothing
> looked". The witness was the controller log gaining dial-refused lines against
> a baseline of zero.
>
> | on `get`/observe | `Ready` | reason | `Synced` |
> |---|---|---|---|
> | 401 | `Unknown` | `ObserveFailed` | `False` / `ReconcileError` |
> | unreachable | `Unknown` | `ObserveFailed` | `False` / `ReconcileError` |
>
> Identical `Ready`, identical reason, identical `Synced`; only the message
> differs. So:
>
> | | 401 and unreachable both report |
> |---|---|
> | **create** path | `Creating` |
> | **observe** path | `ObserveFailed` |
>
> **The reason string is verb-dependent. The indistinguishability is not.** The
> finding holds on both verbs — which is the stronger version of it, established
> on two corpora and two verbs. What must still not be done is using the
> create-path reason strings in the table above to recognise a 401 on another
> verb.
>
> One loose end of our own, half resolved statically: in the arms above the mock
> refused **every** request, yet the controller still reached `create`, where a
> refused `get` on Ironic's corpus surfaces as `ObserveFailed`. The first
> hypothesis was that this chart's observe is a `findby` reading a non-200 as
> "absent" and proceeding to create — which would matter, because oasgen warns
> that an identifier absent from a findby response makes findby unmatchable and
> *"the reconciler acts on not-found by creating"*, a documented behaviour
> rather than a chart quirk.
>
> **That hypothesis is dead here, and the reason is structural: no kind in this
> chart declares `findby` at all.** All six declare `create`, `get` and `delete`
> — verified with a control, since "zero findby" is exactly the kind of zero a
> broken pattern produces: `action: get`, `action: create` and `action: delete`
> each occur 6 times, `action: findby` and `action: update` occur 0 times, and
> the only `findby` string anywhere in `chart/` is the explanatory comment in
> `templates/rd-quota.yaml`. So every kind here is addressable by identifier
> through its `get` verb, and the findby-can-never-match class cannot apply.
>
> The remaining hypothesis was that a brand-new CR has no `flavor.id` to
> substitute into the `get` path, so Observe concludes absent without a
> successful lookup and dispatches the create that then took the 401 — which
> would have made the create-path arms correct but reached for a reason
> unrelated to the refusal. **The owner traced it and it is falsified for our
> pin, because that exact defect was already found on this very API and fixed in
> `0.20.0`.** Observe now refuses before building a URL
> (`restclient.go:202`): an unresolved path parameter returns a bare
> `fmt.Errorf`, which `errors.As` cannot match against `*StatusError`, so
> `IsNotFoundError` is false and no create is dispatched — the reconcile
> surfaces an error instead.
>
> The history is worth keeping, because it is the same inference arriving twice:
> the guard's own comment records the failure it was written for as
> `GET /v2.1/flavors/%7Bid%7D` — `{id}` URL-encoded — requested twelve times
> against a live mock
> ([#361](https://github.com/krateo-platformops/oasgen-provider/issues/361)),
> and notes that **Nova answers that URL with `404 itemNotFound`**, which is
> indistinguishable from "this resource does not exist". So the controller would
> have drawn a conclusion about existence from a URL it should never have built.
> We measured on `0.56.0`, 36 minors past the fix. Our create-path arms are
> sound, and the 401 they recorded was reached by a route that had evidence.
>
> The instrumentation is still worth adding and is still ironic's idea — count
> the requests the subject actually made and attribute them per path — because
> this answer came from a source read by the component's owner, which is not a
> thing this repo can do for itself on the next question of the same shape.
>
> **And the 401 case is deliberate, not an omission.** `rest-dynamic-controller`
> does carry a terminal signal — `ReasonAuthorizationDenied`, via
> `stopOnAuthorizationDenied`, reached by **three** callers: `create`, `update`
> and `observe`. An earlier version of this sentence said
> "get/findby/create/update", which is four and wrong: `get` and `findby` sit
> *inside* Observe and inherit its behaviour, which is why a refused `get`
> reports whatever Observe reports. **`delete` deliberately does not use it at
> all**, and that asymmetry is load-bearing rather than an oversight — a refused
> delete leaves the external resource in place, so stopping there would let the
> finalizer be removed and the CR disappear while the thing it represents still
> exists and still costs money
> ([oasgen-provider#261](https://github.com/krateo-platformops/oasgen-provider/issues/261)).
> **401 is excluded from that family on purpose**, because a 401 is
> what a credential being rotated underneath the controller looks like, and
> retrying it is how rotation is tolerated
> ([oasgen-provider#313](https://github.com/krateo-platformops/oasgen-provider/issues/313)).
> Source read by the oasgen-provider session; the 403 and 401 rows above are
> measured here, which is what turns their read into a demonstration that the
> carrier exists and is deliberately not used for 401.
>
> That design is consistent with this chart's own stance: **token refresh is a
> secret manager's job and this chart ships no refresh logic**, so a controller
> that treated a 401 as permanent would break the model the chart depends on.
>
> **`Synced` is NOT a verb-independent terminality signal — do not read it as
> one.** In the measurement above, a 403 on a **create** reported
> `Synced=True`/`ReconcileSuccess` while `Ready` stayed `False`: the controller
> stopped on purpose and recorded the reconcile as successful. It is tempting to
> generalise that into "`Synced=True` with a failing `Ready` means permanent
> refusal", and this document did exactly that for about an hour on
> 2026-11-11 before a second corpus contradicted it.
>
> `openstack-ironic-operator-kog` ran the equivalent arm against Ironic on a
> single `Port` CR with the uid held constant and only the mock's status code
> varying, and measured a 403 on a **get** arriving as
> `Ready=False`/`AuthorizationDenied` with `Synced=False`/`ReconcileError`. Same
> reason, opposite `Synced`. The two runs differ in the verb that was refused —
> mine `create`, theirs `get` — which is the uncontrolled variable in my
> generalisation, not in their measurement.
>
> So what is established is narrower than what I first wrote: **`AuthorizationDenied`
> is reached on two independent corpora**, which is the load-bearing claim, and
> `Synced` accompanies it differently depending on which verb was refused.
>
> **The mechanism, answered by the oasgen-provider session from source at
> `origin/main`, and it is deliberate.** `stopOnAuthorizationDenied`
> (`restResources.go:1058`) returns `(true, nil)` once it has recorded the
> refusal; what differs is what each caller does with that:
>
> | caller | site | returns | so `Synced` is |
> |---|---|---|---|
> | `create` | `restResources.go:1262` | `nil` | `True` / `ReconcileSuccess` |
> | `update` | `restResources.go:1468` | `nil` | `True` / `ReconcileSuccess` |
> | `observe` | `restResources.go:1021` | an **error** | `False` / `ReconcileError` |
>
> **Observe cannot return `nil` without turning a refused read into an attempted
> write.** An `ExternalObservation{}` with no error means "the external resource
> does not exist and is not up to date", which dispatches a CREATE — so a 403 on
> a read would provoke a write. That is why the asymmetry exists, and why it is
> not going to be made uniform.
>
> Stated so a reader can rely on it, in the owner's own bounded wording — they
> withdrew the word "always" when asked, because it had come from reading code
> rather than from anything run:
>
> > A refused request records `Ready=False`/`AuthorizationDenied` on the CR.
> > Whether `Synced` also goes `False` depends on the caller: `create` and
> > `update` return `nil`, so `Synced=True`/`ReconcileSuccess`; Observe (which
> > contains `get` and `findby`) must surface the error, so
> > `Synced=False`/`ReconcileError`. `delete` deliberately does not take this
> > path at all — a refused delete holds its finalizer
> > ([#261](https://github.com/krateo-platformops/oasgen-provider/issues/261)).
> > **Measured on `create` and `observe`; `update` is source-identical to
> > `create` and unexercised.**
>
> That last clause is the load-bearing one, and it is why "always" was the wrong
> word: two of the three callers were measured — `create` here, `observe` on
> Ironic — while `update` is an inference from identical source. The owner
> normalised the two blocks (`restResources.go:1258-1266` against `:1464-1472`,
> verb literal replaced in both) and found them identical, 425 characters each,
> with a non-empty control on both. So `update` cannot diverge without someone
> editing one of them — but nothing has run it. A reader who hits an unexpected
> `update` result now knows they are the first person there, instead of
> believing they have contradicted a measurement.
>
> **`update` IS NOW DECLARED, on exactly one kind, and the reason matters more
> than the count.** This paragraph used to read "no kind here declares `update`
> either", with `findby` and `update` both at 0. `ComputeQuota` now declares
> one, because without it **the quota was never written at all**: Nova's
> `GET /os-quota-sets/{tenant_id}` has no existence check (verified in
> [Nova's quota_sets show at stable/2026.1](https://github.com/openstack/nova/blob/stable/2026.1/nova/api/openstack/compute/quota_sets.py)), so the resource always reads as
> existing and a create-only kind has nothing left to call. See
> `chart/templates/rd-quota.yaml` for the full reasoning. The other five kinds
> still declare none, so `update`-path refusals remain measured on one kind
> rather than none — a boundary that moved, not one that closed.
>
> Which is why the advice is **branch on `reason`, never on `Synced`** — now for
> a statable reason rather than as a hedge. `Synced` carries *did this reconcile
> return an error*, a property of the **caller**. `reason` carries *what did the
> vendor decide*, which is the property you actually want. It is also what
> core-provider's `krateoReady` does, keying on reason and never on message so
> that no component parses another's prose.
>
> The earlier version of this section said `Synced` could not discriminate at
> all. That remains true of **401 vs unreachable**, which is this document's
> subject and is unaffected by any of the above.
>
> **So the gap is not a misclassification. It is an unbounded toleration.** A
> 401 under rotation stops; a 401 from a revoked credential does not — and
> nothing bounds how long a deliberately-tolerated transient may persist before
> it stops counting as progress. `rest-dynamic-controller` already solves this
> exact shape elsewhere, in `latched_absence.go` — which lives in the
> **oasgen-provider** repo, at
> `go/rest-dynamic-controller/internal/controllers/latched_absence.go`, not in
> core-provider; a reader who searches the wrong repo finds nothing and
> concludes the precedent is invented. Its own comment is the
> argument: *the latch cannot tell a create in flight from one that will never
> land — but the two differ in whether they STOP, so a consecutive-repeat
> counter separates them where a single observation cannot.*
>
> **What it costs, for a 401 specifically.** core-provider's `krateoReady` keys
> on the reason alone for a Ready condition that is not `True` (confirmed from
> source by the core-provider session — see [api.md](./api.md)), and `Creating`
> counts as **converging at every version**. So to a Krateo Composition parent, a
> child wedged on a *revoked* credential reports healthy progress indefinitely,
> and reports it identically to a child whose vendor is merely down. A 403 does
> not have this problem: `AuthorizationDenied` is not `Creating`.
>
> **And the reason the `0.52.0` → `0.53.0` upgrade measurement was blind was NOT
> that the conditions cannot distinguish failures.** Nothing in that run
> exercised a credential read at all — `nova.invalid` never answers, so no 401
> or 403 could occur. A weaker and more specific claim than this page used to
> make, and worth the correction: the old version implied the conditions were
> useless, when in fact they distinguish 403 cleanly and are *designed* to merge
> 401 with a transport failure.
>
> Getting that measurement took four runs, and the first three are worth naming
> because each failed differently: a pod named `probe-401-curl` poisoned a
> digit-stripping parse into `401401`; a mock without a readiness probe answered
> `000` because `rollout status` means "started", not "listening"; and a
> two-CR design produced a confident **false** REFUTED whose only discriminator
> was the arms' own object names. The last is why the arms are now sequential.

| version | why it matters |
|---|---|
| `0.19.0` | `apiKey` / `in: header` arrives — below this the chart cannot authenticate at all |
| `0.26.1` | **hard minimum.** Before it, the `krateo.io/connector-verbose` request dump redacted only values it had been told were sensitive, so credential *headers* were logged in cleartext. This chart's credential **is** a header (`X-Auth-Token`), and verbose is exactly what you switch on to debug a 401. Since 0.26.1 credential headers are redacted **by name**, which no longer depends on the value having been registered. **THE TRIGGER IS NARROWER THAN THIS ROW USED TO IMPLY, AND WORSE.** The dump is driven by one read off the reconciled object — `cli.Debug = meta.IsVerbose(c.mg)` in `rest-dynamic-controller`'s `clientset.go` — and nothing propagates the annotation. There is no cluster-wide or RestDefinition-wide switch, so a pre-0.26.1 leak required **annotating the very `Instance` whose credential then appeared in the log**: not a background risk, an action an operator takes deliberately while debugging the resource that is failing. Confirmed from source by the oasgen-provider session after the keycloak-provider-kog session measured that the same annotation on a *RestDefinition* does nothing at all ([oasgen-provider#552](https://github.com/krateo-platformops/oasgen-provider/issues/552)) — so put it on the CR, which is what `chart/samples/test-server.yaml` and the e2e walkthrough do. **AND THE REDACTION IS REQUEST-ONLY, WHICH THIS ROW DID NOT SAY.** As of 2026-11-10, for the versions below `0.57.0` — `0.52.0`, `0.54.0` and `0.56.0`, this chart's pin among them — the accurate statement is *"requests are redacted, responses are not"*: the verbose **response** dump was never redacted ([#566](https://github.com/krateo-platformops/oasgen-provider/issues/566), measured live on `0.56.0` by the keycloak-provider-kog session), and the vendor **error snippet** is logged at `Info` with **no verbose flag at all** ([#567](https://github.com/krateo-platformops/oasgen-provider/issues/567)) — that one is on by default rather than opted into. **NEITHER HAS LANDED. THIS ROW SAID "Both land in `0.57.0`" AND THAT WAS FALSE** — checked 2026-11-18: [#566](https://github.com/krateo-platformops/oasgen-provider/issues/566) and [#567](https://github.com/krateo-platformops/oasgen-provider/issues/567) are **both OPEN**, and `0.57.0` is where the defect was *identified*, not fixed. The installer session read the same number the same way and used it to call its own clusters safe; the error is this cheap to make and it cost one `gh issue view` to find. A fix version that does not exist is worse than no version: it reads as an upper bound and nothing contradicts it. The row is corrected because it read as "verbose is safe", which is a claim about the whole dump and was only ever true of half of it.

**Where the error-snippet path actually is, measured 2026-11-18 with a per-file control**, because an earlier sweep of mine reported it absent and that was a rate-limited fetch reading as an empty file:

```
go/rest-dynamic-controller/internal/controllers/restResources.go
  tag      bytes    control     logVendorBody
  0.52.0    84937   go-source   6
  0.54.0    88497   go-source   6
  0.56.0    96124   go-source   6
  0.74.0   134866   go-source   6
  0.82.0   147102   go-source   6
```

It is in **generic rest-dynamic-controller**, so having no custom provider does not exempt this chart, and it fires on any failed call where the vendor returned a body — at `Info`, with no annotation. `VendorBodySnippet` returns `se.BodySnippet` verbatim and its own doc comment says *"never log a credential at any level"* while the caller logs it unredacted.

**A FLOOR EXISTS AND IT DOES NOT COVER THIS CHART. An earlier version of this
section said "there is no floor to pin here", and that was wrong** — the
oasgen-provider session refuted it and the refutation held when measured.
`0.57.0` is a real boundary. Same file, same per-file control:

```
go/rest-dynamic-controller/internal/tools/client/restclient.go
  tag      bytes    control     redactSnippet   SensitiveValues
  0.52.0   69622    go-source        0                2      <- UNREDACTED
  0.54.0   69622    go-source        0                2      <- UNREDACTED
  0.56.0   69622    go-source        0                2      <- UNREDACTED
  0.57.0   73914    go-source        4                4      <- redaction lands
  0.74.0   79275    go-source        4                4
  0.82.0   79275    go-source        4                4
```

**So on this chart's `0.52.0` pin the vendor error body reaches the log
genuinely unredacted**, and three of us read `validators.go` — which has never
contained the redaction — and each concluded from its absence there that there
was none anywhere.

**AND THE FLOOR IS INSUFFICIENT, WHICH IS WHY #566/#567 STAY OPEN.** From
`0.57.0` the snippet is redacted **by value**, against `SensitiveValues` only,
and that list is appended to exclusively behind a `fromSecret` guard —
`builder.go:355` and `:409`, both verified at `0.82.0`. So it contains
secretRef-resolved values and nothing else.

**This chart's credential is not one of them, and that is checkable here rather
than upstream:** `grep -c secretRef chart/assets/*.yaml` is **0** across all six
documents. The token arrives through the `apiKey` security scheme as
`spec.authentication.apiKey.tokenRef`, not through a verb's field mapping, so it
is never a resolver output and **value-based redaction cannot reach it at any
version, including `main`**.

| | |
|---|---|
| `0.52.0` (this chart's pin) | snippet **unredacted**; every vendor error body reaches the log at `Info` |
| `0.57.0` and above | redacted **by value**, against secretRef-resolved values **only** |
| this chart's credential | an `apiKey` **header**, not a secretRef value — **not covered at any version** |
| still unmeasured | whether Nova or Keystone echo the token in a 4xx body |

**What the oasgen-provider session states and this page has not verified
itself:** four call sites, all reachable on a default install and none gated by
verbosity, error class, content type or size beyond a 2 KiB cap (`get`,
`findby`, `create`, `update`); nothing filtering between `log.Info` and the
platform's log store; and the default level being `Info` because `-debug` only
*lowers* it. Marked as theirs because the distinction between "measured here"
and "told to me by the owner" is the only thing that kept this row from being
wrong a third time.

**The practical consequence is a pin argument, which the earlier wrong version
of this section removed:** moving above `0.57.0` is a real improvement for any
*secretRef*-carrying field, and no improvement at all for this chart's header
credential.

**AND FOR THIS CHART THE WHOLE REDACTION IS INERT, which is the bottom line a
reader needs.** `redactSnippet` takes the value list as its second argument, and
with zero `secretRef` mappings anywhere in these six documents the list is
**empty**, so the call has nothing to match and the snippet passes through
unchanged. The outcome on `0.52.0` and on `0.74.0` is therefore the same *for
this chart* — reached for a different reason than the earlier wrong version of
this section gave, and it is the reason that matters: not "the code is
unchanged" (it changed at `0.57.0`) but "nothing here populates the list the
change reads".

**The fix pattern already exists in this codebase one layer away.** Since
`0.26.1` the verbose **request** dump redacts credential *headers* **by name**,
which is exactly what does not depend on a value having been resolved — the row
above says so. The snippet path redacts by **value** only. So the thing that
would cover an `apiKey` header is already written, for requests, and simply is
not applied here. That is a smaller ask than a new mechanism and it is raised
upstream rather than worked around in this chart: generating a `secretRef` path
for a token the generator correctly emits as a security scheme would be fighting
the generator to satisfy a log filter.

**What this chart's own exposure is, stated as what is known.** The credential travels as a request header, so it is not in a body this chart sends. Whether Nova or Keystone echo anything sensitive in a 4xx body **has not been measured here** — an earlier version of this row asserted flatly that "Nova does not echo `X-Auth-Token`", which is plausible and was never tested. Treat controller logs as carrying whatever Nova puts in an error body until somebody measures it. |
| `0.40.0` | carries the stdlib security work; the `crypto/x509` findings land hardest on rest-dynamic-controller, whose whole job is outbound TLS to Nova. |
| `0.41.0` | Swaps the provider image to distroless/static and bumps `unstructured-runtime` to v1.6.1 (an orphan-delete/finalizer change). Both are the class of change that passes every static check and only fails on a cluster, so this line is documented because 0.41.0 has been **exercised** — a pod starts and reconciles, and this chart's own `apiKey` assertion was re-run against it. A published tag nobody has run does not belong in this table. |

| `0.43.0` | carries three fixes that each matter here: **#325** removes the required `spec.<pathParam>` that made all five of this chart's resource CRs unapplicable; **#322** makes a populated CR object differ from an empty remote `{}` instead of reporting `Synced=True` having never written (this chart is exposed through `spec.server.metadata`, which Nova returns as `{}` when unset); **#308** accepts array request bodies. Also puts `API VERSION` in the default `kubectl get restdefinition` output. |
| `0.48.0` | Adds **#363**: `rest-dynamic-controller` now honours path-level `parameters` (per the spec they apply to every operation in the path item; the inheritance was simply not implemented) **and refuses a request with an unresolved placeholder**. This chart is directly affected: all six OAS assets declare path-level parameters with `required: true`, so before this fix the controller sent `GET /flavors/{id}` literally in the window before `status` held an identifier -- measured here in a mock's access log. Against a real Nova that is a 404, indistinguishable from "this resource does not exist". |
| `0.49.0` | **#367** *can* change generated CRD shape by merging a parameter's constraint into the body schema; this chart is unexposed, verified from the six assets (no parameter carries a constraint) **and** from the generated CRDs -- `hack/crd-shape.py` fingerprints are byte-identical at 0.48.0 and 0.49.0, 212 lines each. **#373** (a findby/list that matched no item no longer counts as proof of absence) is **NOT a path this chart traverses**: all six RestDefinitions declare `create`, `delete` and `get` only, and no `findby`, so there is no list-based existence check here for the fix to reach. An earlier version of this row claimed the opposite; it was wrong. |
| `0.50.0` | **hard minimum for this chart, and the pin it is exercised against.** Below it, a credentialed request's destination host was never validated: the OAS document's `servers[0]` set the base URL, the credential was attached unconditionally, and nothing in between checked where the request was going. So a `302` from the configured Nova endpoint sent the Keystone token to the redirect target — **a host this chart's documents never name**. Measured in a kind cluster on 0.49.0, with the receiving host reporting what it got: `x_auth_token=PRESENT`. Fixed by oasgen-provider [#379](https://github.com/krateo-platformops/oasgen-provider/issues/379) part 2, which validates the destination at the request path. Re-measured on 0.50.0 with the same harness: the hop is refused, and a legitimate same-host create still succeeds. `hack/probe-redirect.sh` and `hack/probe-361.sh` are the two arms; neither is a verdict alone. **The two measurements are not equally strong, and the difference is worth knowing.** The 0.49.0 result is self-proving: a token arriving at the receiving host cannot happen unless the hop happened, so nothing else explains `x_auth_token=PRESENT`. The 0.50.0 result is a refusal, and at the time it was recorded **nothing asserted that the mock had served the 302 at all** — a mock that had stopped redirecting would have produced the identical output, and so would a policy that refused everything (the caveat already stated). The probe gained a gate for the first of those on 2026-11-12: it now reads the redirecting mock's own access log for a POST answered with a 302 and VOIDs if there is none. **A re-run on 0.50.0 would therefore be stronger evidence than the run this row cites, which has not been redone.** No configuration is needed here — all six assets declare one `servers[0]` and zero per-operation overrides, so the permitted set is exactly the operator's `nova.url`. `rdc.endpointAllowlist` is for a deployment whose OpenStack sits behind a redirecting proxy; `rdc.allowAnyHost` disables the redirect check too, which its name does not say. |

| `0.52.0` | **hard minimum while `quota.enabled` is `true`, and the current pin.** Carries **#421** (for [#419](https://github.com/krateo-platformops/oasgen-provider/issues/419)): a `requestFieldMapping` no longer deletes the spec property it reads from. All three `ComputeQuota` verbs map `inPath: tenant_id` → `spec.tenant_id`, and below `0.52.0` that self-reference made the generator delete `spec.tenant_id`, so the verbs read a property that does not exist. Keeping `tenant_id` in the request body kept the property alive but made Nova return 400 (`additionalProperties: false`), so the resource could not be both generated and callable — which is why it shipped disabled. Measured on the same document: `0.50.0` with `tenant_id` removed → `spec.tenant_id` **absent** (CRD resourceVersion 680 → 1647, so it did regenerate); `0.52.0` with it removed → **present and required**, `quota_set` 7 children as the control. On the wire against a request-logging stand-in: `PUT /v2.1/p/os-quota-sets/proj-arm23 body={"quota_set":{...}}` — template substituted, `tenant_id` absent from the body, CR `Ready=True/Available`. Over-reach was checked too and did not happen: the four `status.*`-sourced mappings still produce no `spec.id`, so `ComputeFlavor`/`Instance`/`ComputeAggregate`/`ComputeServerGroup` stay creatable. The quota-disabled fingerprint is **byte-identical** to `0.50.0` (212 lines, 10 CRDs, 211 constrained fields), and our documents contain zero `nullable` and zero `minProperties` so #424 cannot have moved it. Set `quota.enabled: false` and `0.50.0` is sufficient again. |
| `0.56.0` | **not the pin, but MEASURED against this chart.** Fresh renders of all six documents on `0.52.0` and on `0.56.0`, fingerprinted with the same instrument (asserted by the keyword stamp on line 1 of each) and the provider image verified per render: **254 → 260 constrained fields, and the entire difference is `status.observedGeneration type=integer format='int64'` added to all six resource kinds.** Nothing in `spec` changed, so no CR that validates on `0.52.0` can be rejected by `0.56.0`, and **zero new `default=`** — the one keyword whose appearance is unrecoverable, because the API server materialises a default onto existing objects and `kubectl apply` will not prune the result. Cross-checked outside the fingerprint: 0 of 12 served versions declare `observedGeneration` at `0.52.0`, 6 of 12 at `0.56.0`; the github-provider-kog session independently measured 32 of 32 on a fresh `0.56.0` install of a different document set, so the field is not specific to ours. The three fields the provider's own 0.56.0 notes describe (`status.secretRefRBACNamespaces`, `status.secretRefDigest`, `spec…resolver.when`) are **not on the generated kinds at all** — they are declared on `restdefinitions.ogen.krateo.io`, which ships in `oasgen-provider-crds` and is updated by helm directly. That is the real reason the two charts must move together: a lagging CRDs chart means the provider writes status fields its own CRD does not declare, and the API server prunes them. **The pin stays `0.52.0` — this is evidence, not a decision.** What an *upgrade* does, as opposed to a fresh render, is below the table. |
| `0.80.0` | **not the pin, but MEASURED against this chart, 2026-11-17.** The first render of these six documents above `0.56.0`, run credential-free on kind v1.36.1 with the provider image verified running (`ghcr.io/krateo-platformops/oasgen-provider:0.80.0`) and `nova.url` pointed at RFC 2606 `nova.invalid`. **Result: 6 RestDefinitions `Ready=True/Available` + `Synced=True/ReconcileSuccess`, 6 resource CRDs and 6 Configuration CRDs Established, every served resource version has a controller watching it (`hack/assert-served-controlled.py`), the apiKey block unchanged (`tokenRef` 30 and `valuePrefix` 6 occurrences in both fingerprints), 9 sample CRs matching the published GVKs, and all four sample files passing `--dry-run=server`.** Shape diff against the `0.52.0` artifact CI published on `8be9405`, same instrument: **253 → 259 constrained fields, entirely six `status.observedGeneration type=integer format='int64'` lines, one per resource kind.** Nothing in `spec`, no new constraint, **zero new `default=`** — so no CR that validates on the pin can be rejected here. **THE ABSOLUTE COUNTS DO NOT MATCH THE `0.56.0` ROW ABOVE AND THAT IS NOT AN ERROR IN EITHER:** that row measured `254 → 260` before #150 removed `status.server_group.project_id` (the microversion-gated field Nova never returns at 2.1), which is exactly one constrained field. Across chart states only the **deltas** are comparable, and both are `+6` observedGeneration. Get the baseline with `gh run download` from the latest green smoke run rather than re-rendering `0.52.0` — CI uploads it as `crd-shape-$OASGEN_VERSION`. **Seven `status.schemaWarnings` appear, and whether they are NEW is unmeasurable:** the field does not exist below `0.54.0`, so at the pin the condition has nowhere to be reported. Five are the #325 fix describing itself (*"parameter `id` is omitted from the generated spec because verb `get` supplies it with a request-direction fieldMapping"*) and one is `ComputeQuota`'s `identifiers.tenant_id` not appearing in the response, which is expected for that kind and harmless only because no kind here declares a `findby`. The seventh is real and is below the table. **SIX IS THE COUNT AS OF THAT DATE AND IS NOT EDITED HERE.** The chart now declares **seven** kinds: `ComputeVolumeAttachment` was added on 2026-11-19 and ships `enabled: false`, so a default install still renders six and this reading remains exactly reproducible. The seventh was measured separately at `0.80.0` on 2026-11-21 (run `44884303626`): installed with the kind enabled, its generated CRD served, `spec.volumeAttachment.volumeId` present, and its controller issuing a substituted request on the wire. **Two dates, two readings, neither overwriting the other** — a number inside a dated measurement is evidence, and bumping it to match a later chart state is how evidence becomes a claim. **The pin stays `0.52.0` — Diego's call, 2026-11-17, with this measurement in hand.** Moving is now low-risk rather than necessary, and the cost of moving is named: crossing `0.54.0` switches `scripts/check-schema-warnings.py` from SKIP to measuring, so `smoke` goes red on these seven until each is triaged or exempted. |
| `0.82.0` | **not the pin, MEASURED, and the first real run of `hack/render-check.sh`.** Same arms as the `0.80.0` row and the same result: 6/6 RestDefinitions Ready+Synced, 12 CRDs Established, every served version controlled, apiKey block unchanged, and a shape diff against the `0.52.0` baseline of exactly the six `status.observedGeneration` lines (253 → 259). **And the fingerprint is BYTE-IDENTICAL to `0.80.0`** — 17234 bytes each, `diff` empty, with a planted line as the control proving the comparison was not blind. **That confirms a prediction made from code rather than measurement**, which is worth recording because the two agreed: the oasgen-provider session predicted an empty diff from three checks, each able to come back the other way — the plumbing/crdgen pin unchanged at `v1.93.0` across 0.80.0, 0.81.0, 0.82.0 and `main`; 5 of 11 changed generator files all belonging to #805, the 5 being the filter's own control; and #805 touching the create **response** path, which feeds neither `getBaseSchemaForSpec` (the request body) nor `getBaseSchemaForStatus` (`get`/`findby` responses). **The seven `schemaWarnings` are the same seven, including the `202` one** — so #805 is **not** in 0.82.0, consistent with it sitting in a held 0.83.0. **This row used to end "the 202 warning disappearing is therefore the signal that the generator half has landed". THAT WAS WRONG BY CONSTRUCTION** — see the `0.83.0-rc1` row. |
| `0.83.0-rc1` | **MEASURED 2026-11-18, AND THE TAG NO LONGER EXISTS — this row cannot be re-verified.** `git ls-remote --tags` on oasgen-provider returns **zero** `-rc` tags (211 tag refs returned, as the control that the remote answered), so neither `0.83.0-rc1` nor `-rc2` resolves today. The measurements below were taken while they did; they are kept because the *lesson* is version-independent, but anyone re-running them will get "no commit found for the ref" and that is the tag's absence, not a failed measurement. **DO NOT PIN IT: all six controllers come up and reconcile NOTHING.** Every generated Role pairs `create` with `resourceNames` scoped to the Role's own name, which RBAC cannot authorize — the name of an object being created is in the request body, not in the authorization attributes — so the Lease create is denied. The controller does not crash: `unstructured-runtime`'s ownership `select{}` has no timeout and no `os.Exit`, client-go retries forever, and **no workers start**. Measured here, 6 of 6, with every column a reasonable person would check green: `avail=1 ready=1 restarts=0 waiting=-`, each logging *"Waiting for the ownership lease before reconciling"* and never *"Holding the ownership lease; starting workers"*. **THE IN-CLUSTER CONTROL IS THE PROVIDER'S OWN ROLE:** `oasgen-provider-leader-election` carries the same group, resource and `create` verb with `resourceNames=(any)` and works. Same authorizer, same namespace, same moment, saying yes to one shape and no to the other — which rules out an environmental explanation from inside one reading. Fixed upstream in oasgen#814, unreleased; **three signals agree with the broken install** and are each insufficient: pod availability (there is no readiness probe, so Ready means only that the container is running — and the mechanism is worth stating precisely: `rdc.readinessProbe` is **ABSENT** from the provider chart's `values.yaml`, measured at `0.74.0` with its 22 top-level keys as the control, while the provider's own controller Deployment template already pipes that value through `toYaml`. So the rendered `readinessProbe: null` comes from the absence, not from anyone declaring null — a reader hunting for a `null` to change will find nothing to edit. This page said "ships `null`" until 2026-11-18, which was right in effect and wrong in mechanism), restart counts (nothing exits), and `RestDefinition` `Ready=True/Synced=True` — the last being the field a platform surfaces, so the most convincing of the three. Only the log pair disagrees (oasgen#815). **Also, and separately:** a shape diff against the `0.52.0` baseline of exactly the same six `status.observedGeneration` lines as `0.80.0` and `0.82.0` — three releases now agree, so #805 changed nothing about the generated shape, as was predicted from code. **But the seven `schemaWarnings` are still the same seven, the `202` one included.** **THE TRIGGER THIS PAGE RECORDED COULD NEVER HAVE FIRED.** #805 makes a **declared** `successCodes` reach the generator, and this chart declares none — so the warning persists at `0.83.0-rc1` exactly as at `0.82.0`, and would persist at every future release until something declares `202`. It also contradicted the order of operations written beside it — *move the pin, confirm the generator honours it, **then** declare `successCodes`* — which puts the declaration last and therefore makes the warning's disappearance an effect of **our own change**, never a signal arriving from upstream. **The real confirmation is an experiment:** declare `successCodes: [202]` on the server create verb in a staged copy, render at `0.83.0-rc1`, and check the warning clears. If it does not, the generator half is broken and the declaration must not ship. Declaring it against this chart's `0.52.0` pin would be the stored-and-ignored state described below, since the field is present in the CRD at `0.52.0` and honoured at runtime while the generator ignores it. |
| `0.83.0-rc2` | **MEASURED 2026-11-18. The Lease RBAC defect is FIXED, and the rc1→rc2 pair is the most useful thing this page records about reading health.** All six controllers available, zero restarts, and **each holding its own Lease — read from the OBJECT**, not from a log line: `nova-aggregate-controller` holds `computeaggregates-nova.openstack.krateo.io-v1alpha1`, and so on for all six, each `holderIdentity` naming a pod of that controller. **The first reading of this row said "holding their ownership lease" on the strength of a log message emitted ONCE at startup** — caught only because the capture began at pod start, where a peer looking two minutes later got nothing on an install that was definitely broken. The arm now reads the Lease and prints the one-shot count as corroboration only (*6 of 6 logs still carried it*). **AND THE LEASE NAME IS NOT DERIVABLE FROM THE CONTROLLER NAME:** `nova-server-controller` holds `instances-nova.openstack.krateo.io-v1alpha1`, because the name follows the plural RESOURCE. Together with three observed API versions all receiving a `v1alpha1` name, that is why the check reads `-lease-name` off the Deployment rather than deriving it — provider-given-not-derived is exactly what oasgen#806 changed, so a derivation would re-encode the shape that broke. `render-check.sh` exits 0. Each generated Role now carries **two** rules — `resourceNames=(any) verbs=create` plus `resourceNames=<own name> verbs=get,update` — which is what rules out *"the chart swap did not re-render the Role"*, a failure that looks identical to the fix not working and which the run passing would **not** have distinguished. Still narrower than the provider's own `oasgen-provider-leader-election` on `list`/`watch`/`patch`, deliberately: `LeaseLock` issues only Get, Create and Update. Controls held: **7** `schemaWarnings` including the `202` one (the chart was unmodified, so that is the expected reading), and a fingerprint of `253 → 259` with the same six `status.observedGeneration` lines — now identical across `0.80.0`, `0.82.0`, `rc1` and `rc2`. **THE PAIR, WHICH IS WHY THIS ROW MATTERS BEYOND THIS VERSION:** `rc1` → `avail 1, ready 1, restarts 0, waiting -` → **FAIL**, alive and reconciling nothing. `rc2` → `avail 1, ready 1, restarts 0, waiting -` → **PASS**, holding its lease. **Four pod signals, identical values, opposite truths.** Those four are not weak, they are *jointly insufficient* — no combination of them separates the two states, and `RestDefinition Ready=True/Synced=True` reads the same in both as well. Only the ownership log line differs. It is also the positive control for the check that does the distinguishing: the arm failed on `rc1` and passes on `rc2`, which two earlier versions of that arm could not have shown, because they could only ever say yes. Upstream: oasgen#814 (merged, confirmed on four independent installs), oasgen#815 (the silence). **Pinnability is a separate question** — a 31-minor jump from `0.52.0` is its own decision, and nothing here argues against the tag. |
| retractions | **[retracted-versions.yaml](https://github.com/krateo-platformops/oasgen-provider/blob/main/retracted-versions.yaml) in the oasgen-provider repo is the authoritative list, not release notes.** It retracts **0.66.0, 0.69.0, 0.70.0 and 0.77.0**, and its `use:` field names the most-*verified* safe version rather than the newest (`0.73.0` as of 2026-11-16). **Exactly two of the four apply to this chart, and the other two do not** — filtered against our shape (six documents, zero `findby`, create/delete/get only, apiKey auth) by the oasgen-provider session on request. **0.69.0 and 0.70.0 apply in full:** both were retracted for a *measured* duplicate create that fires *"on any kind that declares no `findby` verb"*, and all six kinds here declare none. **0.66.0 does not:** its defect is a kind declaring **no create verb** writing `external-create-pending` about five times a second, and all six kinds here declare create. **0.77.0 does not:** its reason names a **findby-addressed** kind failing to *reach* `CreateOutcomeUnknown`, and a zero-`findby` kind reaches that state by the other road — its existence check cannot answer at all. That last one is recorded because it is the opposite of what the shape would suggest: having no `findby` is usually the exposure here, and for 0.77.0 it is the exemption. **Nothing else between `0.56.0` and `0.82.0` is breaking for this shape.** Read that file before naming a version to anyone. |

### A create declared only under `202` is not extracted (the seventh warning)

**RESOLVED UPSTREAM AND MEASURED HERE, 2026-11-18. The fix works, and this chart
cannot use it yet.**

[oasgen#805](https://github.com/krateo-platformops/oasgen-provider/pull/805)
makes a verb's declared `successCodes` reach the generator, UNIONed with the
default `{200, 201}` rather than replacing it — so declaring `202` cannot cost
`200`/`201`. Staged on this chart's server create verb (uncommitted) and
rendered at `0.83.0-rc1`:

```
BEFORE  nova-server  warnings=2   ... + "no suitable response schema found for action 'create'"
AFTER   nova-server  warnings=1   ... the by-design `id` omission alone
TOTAL   7 -> 6 schemaWarning(s). The other five kinds: unchanged, one each.
```

**The five unchanged kinds are the control.** The declaration did exactly one
thing, on exactly the verb it was declared for. A warning clearing elsewhere, or
a new one appearing, would have been the interesting result instead.

**REPRODUCED ON `0.83.0-rc2`, and that run is the cleanest this chart has ever
measured:**

```
6 schemaWarning(s), the 202 one ABSENT
every kind at exactly 1:   aggregate, flavor, keypair, quota, server, server-group
  5 are "parameter omitted ... requestFieldMapping"  -- this chart's design
  1 is ComputeQuota's identifiers.tenant_id          -- dormant, no findby declared
all six controllers available, zero restarts, HOLDING their ownership lease
render-check.sh exit 0
```

So six is the floor for this chart as it stands: five by-design omissions and one
dormant quota warning, with nothing left that represents a defect.

**AND THE ACQUISITION ARM STILL PASSED WITH THE DECLARATION PRESENT**, which is
the control that matters for running two changes at once — a new release and a
staged declaration. If the declaration had disturbed the Lease machinery the arm
would have caught it, so the two are orthogonal rather than merely both-green.

**AND IT IS SHAPE-NEUTRAL, which is the part that had to be measured rather than
reasoned about.** The generated CRD fingerprint with the declaration present is
**identical** to the fingerprint without it — the same `253 → 259` and the same
six `status.observedGeneration` lines against the `0.52.0` baseline, 17 lines of
diff output on each side, compared with a both-sides-non-empty control after a
first attempt compared against an empty side and nearly read that as a
difference.

**THE GENERAL ANSWER IS PER-VERB, AND IT IS NOT "NO CHANGE".** Traced by the
oasgen-provider session through the generator: `getBaseSchemaForSpec` reads the
create **request body**, and `getBaseSchemaForStatus` reads `get`/`findby`
responses **only**, so a create verb's response reaches exactly one consumer —
the comparison that emits this warning. Declare `successCodes` on a **`get` or
`findby`** verb instead and it *does* feed the status schema, which is
consumer-visible CRD shape. Same field, same mechanism, different verb,
different consequence: if a future experiment stages it on a read verb, expect a
diff and do not read it as a regression.

> [!WARNING]
> **DO NOT DECLARE IT AT THIS CHART'S `0.52.0` PIN.** The field is present in
> the CRD at `0.52.0` (line 1738) and rest-dynamic-controller honours it at
> runtime, while the generator ignores it — so it would be **stored and obeyed
> by half the system**, which reads as configured and is worse than absent.
> `0.83.0-rc1` itself is not pinnable either (see its row above: six controllers
> up, none reconciling). The order is: a release carrying #805 **and** #814,
> then the pin, then this declaration.

**DECISION, Diego, 2026-11-18: declare it when the pin next moves anyway — do
NOT move the pin for this.** The reasoning is what the measurement showed about
cost rather than about the fix:

```
status fields     populated from the `get` response, not the create one  -> unaffected
create response   not extractable, so the COMPARISON is skipped
net cost today    one lost generator-side validation + one allowlist entry
```

So nothing is broken while this stands; what is lost is a check. Pin moves in
this repo are driven by things with teeth — the `ComputeQuota` floor, the apiKey
RBAC break, a retraction — and this does not join that list on its own merits.
The allowlist entry for it in `scripts/check-schema-warnings.py` carries the
order of operations, so **whoever moves the pin next is told to declare it
then** rather than having to rediscover why it was left out.


On `0.80.0` the `server` RestDefinition alone reports:

> validation error at : could not extract schema for action 'create' to compare:
> **no suitable response schema found for action 'create'**

**The cause is the response status code, and the six documents are their own
control set:**

| document | create responds | warning |
|---|---|---|
| `aggregate` | `'200'` | none |
| `flavor` | `'200'` | none |
| `keypair` | `'201'` | none |
| `quota` (PUT) | `'200'` | none |
| `server-group` | `'200'` | none |
| **`server`** | **`'202'`** | **this one** |

Five silent controls against one warning, one-to-one with the status code. The
generator hardcoded `SuccessCodes: []int{200, 201}`
(`oas2jsonschema/types.go:23`), so a create declared only under `'202'` was
never looked up. Deletes declared at `'202'` (`flavor`, `keypair`, `quota`)
produce nothing, so it is specific to the create action.

**`chart/assets/server.yaml` is correct and must not be changed.** Nova's
`POST /servers` returns `202 Accepted`; declaring `'200'` to silence a generator
warning would make the document lie about the vendor. Fixed upstream as
oasgen-provider **#805**, which makes the extractor iterate
`config.SuccessCodes` **union** `verb.SuccessCodes`.

**It does not reach status, so no CR is affected.** The status schema is built
from the `get` or `findby` action, never from `create`
(`extractor.go:140`, `:149`); `create`'s schema feeds only the comparison, which
is what *"to compare"* means. At runtime `populateStatusFields` projects from the
**response body** using `identifiers` and `additionalStatusFields` and consults
no generated schema at all. `status.server.status` and
`status.server.addresses` populate from Nova's real response regardless.

**DO NOT ADD `successCodes: [202]` YET, and the reason is not the obvious one.**
The field is **already in the CRD at the pin** — verified by reading the shipped
[ogen.krateo.io_restdefinitions.yaml](https://github.com/krateo-platformops/oasgen-provider/blob/main/go/oasgen-provider/crds/ogen.krateo.io_restdefinitions.yaml) at three tags: present at `0.52.0`
line 1738, `0.54.0` line 1738 and `0.82.0` line 2198, with a description that
names 202 explicitly. It is **stored, not pruned**, which is worse than pruned:
`kubectl get restdefinition -o yaml` would show the declaration and nothing
would contradict an operator who concluded the async create was configured.
And `rest-dynamic-controller` has honoured the field at runtime since before the
pin (`client/builder/builder.go:38,85,132` at `0.52.0`; the same three sites at
`:38,93,140` on `0.82.0`), so declaring it today would be **half-working** —
reconciler accepting the 202 while the generator kept warning.

**Order of operations:** move the pin, then **confirm the generator honours it**
(a render where this warning is gone), then declare. Checking that the CRD
serves the field is *not* that confirmation — it passes at every tag above and
proves nothing.

### What an upgrade does to an existing installation

**It depends entirely on which branch the upgrade takes, and both branches have
now been measured.** An earlier version of this section said an already-served
version's schema is "not rewritten". **That is false** — it is rewritten, on the
healthy path.

Two runs, same chart, same `0.52.0` → `0.56.0` upgrade of both charts, differing
only in how busy the machine was:

```
                       RUN 1  (load ~30)        RUN 2  (load ~10)
Dynamic Controller
  not deployed yet     29                       0
  already deployed     12                       24
Creating RestDefinition 6                       0
Generator build changed 0                       6
Regenerating CRD        0                       6
Updating RestDefinition 0                       6
RDC Deployment uid      not captured            UNCHANGED on 6 of 6 (gen 2 -> 4)
status.generatorBuild   <absent> -> 0.56.0-…    <absent> -> 0.56.0-…
observedGeneration      0 of 12 -> 0 of 12      0 of 12 -> 6 of 12
                        SCHEMA STALE            SCHEMA UPDATED IN PLACE
```

**So "served versions are immutable" is not the rule.** On the drift path the
provider regenerates and the new field lands on the existing CRD. The damage in
[#541](https://github.com/krateo-platformops/oasgen-provider/issues/541) is
specifically that **the other branch skips generation**:

`Observe` returns early when the per-kind `rest-dynamic-controller` Deployment is
**not found**, and never reaches the check that decides whether to regenerate.
`Create` runs instead, finds the CRD already present, skips generation, and
stamps `status.generatorBuild` anyway — consuming a one-shot activation, because
that check returns true unconditionally only while the stored build is empty.

**The trap, which survives both runs:** after the Create path,
`status.generatorBuild` reads `0.56.0` while the served schema is still the one
`0.52.0` generated. Anything treating that field as "this schema is current" gets
a confident wrong answer.

**What is still not known.** Why the Deployments were missing in run 1. Run 2 was
the run meant to answer it — uids captured before any forcing — but **the
phenomenon did not recur**, so its "uid unchanged on 6 of 6" says only that
nothing deletes them on a healthy upgrade. It is not an answer about the case
where they went missing. A rename is ruled out (the `-controller` suffix is
identical across `0.52.0`–`0.56.0`) and so is a helm prune (the provider renders
them at runtime). The deciding measurement has not been taken.

**And run 2 suggests the trigger is load-correlated**, which is a weaker and more
awkward claim than version-dependence: the same upgrade on the same versions took
opposite branches at load ~30 and load ~10. Two runs is not a correlation, and
this page does not claim one. It claims the branch is not determined by the
versions involved — which the github-provider-kog session's `0.48.0` → `0.56.0`
upgrade also shows, having found the Deployments present and regenerated 32 of 32.

**Recovery and detection are in
[docs/release.md](./release.md#bumping-the-oasgen-provider-pin)**, next to the pin
bump that would cause it. The recovery there is now **measured**: a cluster was
forced into the broken state by deleting the six `*-controller` Deployments
before the upgrade — which takes the `Create` branch and leaves
`observedGeneration` on 0 of 12 while `generatorBuild` reads `0.56.0` — and
clearing the stamp took it to **6 of 12**, with the stamp rewritten after the
render. **The trigger is also now reproducible on demand**, which it was not
when this page first described it.

And the damage does not repair itself unaided: with the Deployments restored to
6/6 **before** the measurement, five 60-second rounds showed **no CRD write at
all** and `observedGeneration` still 0 of 12. Not "re-rendered to the same
bytes" — nothing wrote the CRD. That is five minutes, not forever, and the state
was forced rather than naturally occurring; both limits are stated on the
release page.

Neither `0.41.0`, `0.43.0`, `0.48.0`, `0.49.0`, `0.50.0` nor `0.52.0` changes the derived resource `apiVersion`:
both pin `plumbing v1.14.2` and their `internal/tools/render/` trees are
byte-identical, so resource CRs stay at `v2-1`.

Neither `0.40.0` nor `0.41.0` changes it either: both pin
`plumbing v1.14.2` and their `internal/tools/render/` trees are identical, so
resource CRs stay at `v2-1` (see [api.md](./api.md)).

> **Neither image has a shell.** `rest-dynamic-controller` has been
> distroless since 0.20.0 and `oasgen-provider` joins it at 0.41.0, so
> `kubectl exec -- env` or `-- sh` finds nothing in either pod. This is the
> kind of thing you discover mid-incident, so: read configuration with
> `kubectl get ... -o jsonpath` or `kubectl set env --list`, read behaviour
> with `kubectl logs` (plus `krateo.io/connector-verbose`, but only on
> >= 0.26.1 — see the row above), and when you genuinely need a shell in the
> pod's namespaces use `kubectl debug --target`.

Note that `https://charts.krateo.io` still serves appVersion `0.10.0`, which is
below every line of this table. Install from the OCI charts under
`ghcr.io/krateo-platformops/charts/` instead.


> [!IMPORTANT]
> **Three rows say "hard minimum", for three independent reasons, and the
> effective floor is the highest of them: `0.52.0`.** Below `0.26.1` the verbose
> request dump wrote credential *headers* to pod logs in cleartext; below
> `0.50.0` a credentialed request's destination host was never validated; below
> `0.52.0` this chart's enabled `ComputeQuota` is unusable, because the
> generator deleted the very `spec.tenant_id` its three verbs read
> (oasgen-provider#419). None subsumes another — they are different defects in
> different code — and a deployment on `0.49.0` is unaffected by the first and
> exposed to the second. All three are kept in the table because each explains a
> different thing a reader may be trying to understand about an older pin.
>
> The third is the only one that is **this chart's own doing**: it became a floor
> the moment `quota.enabled` flipped to `true`. Set `quota.enabled: false` and
> `0.50.0` is sufficient again.

### Installing this pin onto a cluster that already has a provider

**The six install snippets in this repo are written as if the cluster were
empty, and four of them point at a cluster the reader did not just create.**
`restdefinitions.ogen.krateo.io` is **cluster-scoped and singleton**: there is
exactly one of it per cluster, shared by every KOG installed there. So
`helm upgrade --install oasgen-provider-crds --version 0.52.0` is not a local
act. If the cluster is managed by the Krateo installer, that command names an
older build of an object other operators depend on.

**The divergence is real and current.** `krateo-platformops/installer` at
release **`0.3.673`** pins both `oasgen-provider` and `oasgen-provider-crd` at
**`0.74.0`** (read from [component-pins.yaml at that tag](https://github.com/krateo-platformops/installer/blob/0.3.673/chart/files/component-pins.yaml),
not from its default branch). This repo pins `0.52.0` and enforces it in CI, for the
reason in the table above. Neither number is wrong; they answer different
questions. What is wrong is an install snippet that assumes they agree.

**What a downgrade of that CRD would prune, measured 2026-11-18.** Both
`oasgen-provider-crds` charts pulled from GHCR and rendered with
`--kube-version 1.36.0` (they refuse to render below that floor, so a missing
`--kube-version` fails as a chart error rather than an empty result), then every
field path walked out of `openAPIV3Schema`:

```
0.52.0                                     224 field paths
0.74.0                                     263 field paths
present at 0.74.0, absent at 0.52.0         39
present at 0.52.0, absent at 0.74.0          0   <- control: a clean superset
```

The reverse direction being **zero** is what makes this a pure loss rather than
a rename. The 39, grouped:

| what | paths | what stops working |
|---|---|---|
| `verbsDescription[].correlation.observe.pagination` (both `pageNumber` and `continuationToken`) | 27 | a paginated observe silently reads only the first page |
| `verbsDescription[].correlation.observe.responseTransform` | 4 | a declared response transform is dropped |
| `verbsDescription[].fieldMapping[].resolver.when` | 2 | a conditional resolver fires unconditionally |
| `verbsDescription[].pagination.continuationToken` (`maxPages`, `response.hasMorePath`) | 2 | an unbounded page walk |
| `status.schemaWarnings`, `status.generatorBuild`, `status.secretRefDigest`, `status.secretRefRBACNamespaces` | 4 | the fields this repo's own checks read |

Pruning is **silent with a green apply** — the same mechanism recorded for
`identifiersMatchPolicy` elsewhere on this page. Nothing warns the owner of
another KOG that their pagination declaration has stopped existing.

**`successCodes` is NOT in that list**, and that absence is the control that
earned the method trust: it is present in the CRD at `0.52.0` (line 1738), which
is exactly what was established when an earlier claim here said the opposite.
A method that reported it missing would have been reporting its own failure.

> [!IMPORTANT]
> **Run this before any of this repo's `oasgen-provider` install snippets:**
>
> ```bash
> kubectl get deploy -n krateo-system \
>   -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.template.spec.containers[*].image}{"\n"}{end}' \
>   | grep -i oasgen
> ```
>
> **Empty output** means no provider is installed: run the snippet as written.
>
> **A tag at or above `0.52.0`** means the cluster already satisfies this
> chart's floor. **Do not run the snippet** — this chart needs a provider at or
> above `0.52.0`, not one at exactly `0.52.0`, so an existing newer provider is
> already correct and re-installing can only lower it. Skip to installing this
> chart.
>
> **A tag below `0.52.0`** is the only case where an upgrade is both needed and
> safe, and it is still shared: raising it affects every KOG in that cluster.
>
> The **image tag** is the authoritative version. `appVersion` is uniformly
> stale across several charts in this org and must not be used for this.

**Measured on one cluster, 2026-11-18, by the core-provider session**, after
this section first said it was unmeasured. On an installer-managed cluster the
CRD's own annotations read:

```
restdefinitions.ogen.krateo.io
  meta.helm.sh/release-name       oasgen-provider-crd      <- SINGULAR
  meta.helm.sh/release-namespace  krateo-system
  helm.sh/resource-policy         keep
```

with a control, so the reading means something: `compositiondefinitions.core.krateo.io`
on the same cluster reads release `installer`.

**So the plural name this repo documents is a DIFFERENT release from the one that
owns the CRD, and Helm 3 refuses it on ownership metadata.** That is the safe,
puzzling branch: the reader gets an error they did not expect from a chart they
did not choose, and the cluster is unharmed.

> [!WARNING]
> **The difference between safe and catastrophic is one letter.**
> `helm upgrade --install oasgen-provider-crd` — **singular** — is an upgrade of
> the owning release, and it would replace the live CRD with `0.52.0`'s 224
> paths. No ownership error, no warning, 39 paths gone from every RestDefinition
> in the cluster. The plural form in this repo's snippets is refused; the
> singular form is not.
>
> `helm.sh/resource-policy: keep` does not prevent this. Helm reads it from the
> release's stored manifest rather than the live object, so it is not the
> protection it appears to be (established by the oasgen-provider session).
>
> **`--take-ownership`** (helm >= 3.17) also bypasses the refusal, adopting the
> CRD even under the plural name. Do not reach for it to make an ownership error
> go away.

**What that measurement does NOT establish.** It is **one cluster**. From
core-provider's own code, a composition-installed chart's release name is the
composition's `krateo.io/release-name` label if set; failing that an existing
release adopted by its recorded identity; failing that a minted
`<composition-name>-<5 chars of its UID>`. `oasgen-provider-crd` with no suffix
is **not** a minted name, so on that cluster it came from the installer's own
install path — and how the installer names it elsewhere is the installer's to
confirm. Which is why the preflight below is a per-cluster check rather than a
rule, and why it says "do not run the snippet" rather than "the snippet will
fail".

## `keypair.type` was microversion 2.2 and we speak 2.1

**The first defect this chart's artifacts were caught in against real Nova**,
from the openstack-as-a-service session's dp303 run on 2026-11-18. Fixed by
removing the field from the create request schema; the durable half is
`microversion_findings` in
[hack/check-restdefinition-shape.py](../hack/check-restdefinition-shape.py).

```
request   POST /v2.1/<project>/os-keypairs
          {"keypair":{"name":...,"public_key":"ssh-rsa ...","type":"ssh"}}
answer    HTTP/1.1 400
          X-Openstack-Nova-Api-Version: 2.1
          Invalid input for field/attribute keypair ...
          Additional properties are not allowed ('type' was unexpected)
CR        Ready=False/Creating   Synced=False/ReconcileError, indefinitely
```

Keypair `type` exists on create only from compute microversion **2.2**. This
chart sends **no microversion header on any path**, so Nova serves 2.1 and
refuses the body. Every request body in all six documents was audited:
`keypair.type` was the only property above 2.1, and server-group correctly uses
the 2.1 `policies` form rather than the 2.64 `policy` + `rules` pair.

**Two things the controller's verbose log showed that the CR did not**, both
read from the run's own log (sha256-verified, credential-scrubbed, 476 lines):

**1. A permanently-rejected create retries forever, six times per resync.** The
400 timestamps:

```
10:21:49  :50  :52  :57  10:22:05  10:22:21      <- 1s, 2s, 5s, 8s, 16s
10:24:47  :48  :50  :55  10:25:03  10:25:19      <- the same burst, 3 min later
10:27:47  :48  :50  :55  10:28:03  10:28:19      <- and again
```

Eighteen rejected POSTs in under seven minutes, and the three-minute gap is the
controller's `resyncInterval` of 180s. There is no terminal state for a body the
vendor will never accept, so the cost of this defect is roughly 2,880 rejected
requests per day per CR for as long as the CR exists.

**2. The vendor's explanation is deliberately kept off the resource.** Verbatim
from the controller:

```
"msg":"Vendor rejected the request; its explanation follows and is
       deliberately NOT on the resource"
```

So a user sees `ReconcileError` and no indication that Nova said
`'type' was unexpected`. **That is why this needed a live run to find:** the
information that explains the failure never reaches Kubernetes at all, and the
only place it appears is the controller's log with verbose enabled. (Verbose is
safe at this chart's floor — the credential-header leak was below `0.26.1` and
we pin well above it.)

## A create that returns no `id` leaks the resource — silently at `0.52.0`, reported at `0.74.0`

**Measured 2026-11-18 on a real cluster, rest-dynamic-controller `0.52.0`**
(`build 0.52.0-5e4f7707831e59dc63d3adb74ae07ab58b5a7c52`). Reproduce with
[hack/probe-observe-rejection.sh](../hack/probe-observe-rejection.sh).

`rd-flavor.yaml`'s `get` **and** `delete` verbs both take their path parameter
from the custom resource, not from the vendor:

```yaml
- action: get
  path: /flavors/{id}
  requestFieldMapping:
    - inPath: id
      inCustomResource: status.flavor.id
```

`status.flavor.id` is filled from the create response by `populateStatusFields`,
which **`continue`s silently when a declared field is absent** from the vendor's
payload — no error, no event, no log. So a vendor that answers a create with
`200` and a body lacking `id` leaves both verbs with no parameter to substitute.
This is not hypothetical: real Nova already differs from this OAS elsewhere
(flavor LIST omits `ram`/`vcpus`/`disk`; `quota_set` returns `id`, not
`tenant_id`).

What that produces, with a mock whose create returns
`200 {"flavor":{"name":"probe-x","ram":512,"vcpus":1,"disk":1}}`:

```
status.flavor   {"disk":1,"name":"probe-x","ram":512,"vcpus":1}   parsed, no id
conditions      Ready=True   reason=Available
                Synced=True  reason=ReconcileSuccess
annotations     krateo.io/external-create-pending:   10:29:18Z
                krateo.io/external-create-succeeded: 10:29:18Z

observe         "External resource is up to date"      x2, 179.9s apart
                ZERO HTTP requests at the vendor in a 200s window (CTL 1 -> 1)
delete          kubectl delete -> rc 0, CR gone
                ZERO DELETE requests at the vendor
                "External resource is not addressable, releasing finalizer
                 without calling the API"
```

**The CR reports `Ready=True reason=Available` for a resource the controller
cannot see and will never look at again.** Then `kubectl delete` succeeds, the CR
disappears, and the resource stays at the vendor with nothing in Kubernetes
recording that it exists.

**The control for the zero that matters.** "Zero DELETEs from the controller" and
"this mock does not log DELETEs" are the same integer. After the CR was gone the
probe sent its own DELETE to the same mock under its own User-Agent and the
access log showed **1**. So the mock records DELETEs when it receives them, and
the zero is an absence rather than a blind instrument.

**What is NOT wrong here.** There is **no duplicate-create loop** — that was the
hypothesis this probe was written to test and it is refuted. The controller
reconciles (twice in 200s, matching its logged `resyncInterval` of 180s) and
issues no request at all. Each of the three behaviours is defensible alone:
skipping an absent declared field, reporting "up to date" when there is nothing
addressable to GET, and releasing a finalizer rather than hanging forever. It is
the combination that loses a resource.

**Why this is reportable rather than ours to work around.** The controller
*knows* a create succeeded — `krateo.io/external-create-succeeded` is set — so
the information needed to tell "created and now unreachable" from "never
created" is present at the moment the decision is made. Raised with
oasgen-provider on 2026-11-18, bounded explicitly to `0.52.0`: their current line
is `0.83.1` with six commits unreleased, including a change to how a create's
outcome is classified, and **this has not been measured there.**

### Measured again at `0.74.0`, the version the installer ships — and it is no longer silent

**Run 44351381915, oasgen-provider `0.74.0`, 200s windows, on a GitHub runner.**
The leak is still a leak. What changed is that the controller now says so, and
the earlier entry's word "silently" is true only at the floor.

```
conditions      Ready=False   reason=NoIdentifierRecorded     (0.52.0: Ready=True/Available)
                Synced=False  reason=ReconcileError
status.flavor   {"disk":1,"name":"probe-x","ram":512,"vcpus":1}    still no id

observe          16 reconciles in 200s, ZERO HTTP requests at the vendor
                "Refusing to assume up-to-date: no identifier was ever
                 recorded"                                          x12
delete          kubectl delete -> rc 0, CR gone
                ZERO DELETE requests at the vendor
                "External resource may survive this deletion: a create
                 succeeded but the resource could not be addressed or
                 re-derived"
                identifiers: flavor.id=<unset>, flavor.name=<unset>
                unresolvedPathParams: id
```

**Two Warning events, and the wording is the deliverable:**

```
Warning CannotObserveExternalResource
  the create reported success but no identifier was recorded in status, so the
  ComputeFlavor cannot be addressed; this kind declares a get verb but no
  findby, so it cannot be re-found either. The external resource MAY EXIST and
  be unreferenced -- do not delete this resource to clear the condition,
  because that would orphan it

Warning OrphanedExternalResourcePossible
  released ComputeFlavor/probe-noid in namespace "krateo-system" while its
  external resource may still exist: the create succeeded but the resource
  could not be addressed (flavor.id=<unset>, flavor.name=<unset>) and findby
  could not re-derive it. Check the external system for an object matching
  these identifiers
```

**So the resource is abandoned AND recoverable.** An operator can find it from
the identifiers in that event, and the first event explicitly warns against the
instinct that would make it worse. At `0.52.0` the same sequence produced
`Ready=True reason=Available` and no events at all — a reader on a platform
cluster gets materially better behaviour than the block above describes.

> **DO NOT ALERT ON THE EVENT `reason`. IT IS NOT A SELECTOR FOR THIS.** The
> block above prints `CannotObserveExternalResource` as the signal of a possible
> orphan, and on its own it is nothing of the kind.
>
> That reason is declared in **unstructured-runtime**, not in
> rest-dynamic-controller — measured: 1 occurrence in the runtime's
> `pkg/controller/worker.go`, and **0 across all 441 `.go` files** of
> oasgen-provider `0.84.0` (441 and 139 extracted file counts are the controls
> that those zeros are readings). At every runtime version this chart can run it
> has exactly **one** use site, and that site is a catch-all:
>
> ```
> v1.18.0  (oasgen <= 0.74.0)   worker.go:982    1 use site
> v1.22.0  (oasgen 0.75.0+)     worker.go:999    1 use site
>
>   `if actionErr != nil` after excluding a status conflict -- so ANY observe
>   failure lands there: a transport error, a 500, an unresolvable path, and
>   the no-identifier case above, all under one reason.
> ```
>
> **The message distinguishes them; the reason does not, and the reason is the
> machine-consumed half.** Match on the message text (`no identifier was
> recorded in status`), or on `OrphanedExternalResourcePossible`, which is
> emitted only on the delete path.
>
> **THE BOUNDARY, STATED AS A BOUNDARY** — because "no version can reach it" is
> a claim about today that goes false the moment the floor moves, without anyone
> touching this file.
>
> `unstructured-runtime` **v1.28.0** adds a second use site for the *same*
> reason: a resource absent while `krateo.io/management-policy` asks for
> observe-only — not an error at all, and the one case nobody would want to page
> on. **v1.28.1** gives that case a reason of its own,
> `ExternalResourceAbsentUnderPolicy`. Measured 2026-11-21 with a known-present
> symbol as a per-read control, so "absent" cannot be "I searched an empty tree":
>
> ```
> unstructured-runtime   ExternalResourceAbsentUnderPolicy   .go files   CONTROL
>   v1.27.1              0 occurrences                       136         28
>   v1.28.0              0 occurrences                       139         29
>   v1.28.1              3 occurrences                       139         29
>
> and the pin lives in rest-dynamic-controller, NOT in oasgen-provider:
>   go/rest-dynamic-controller/go.mod  @0.74.0  v1.18.0   (the installer's pin)
>                                      @0.84.0  v1.27.1
>                                      @0.85.0  v1.28.1
>   go/oasgen-provider/go.mod          @all     no unstructured-runtime line
> ```
>
> **So below oasgen `0.85.0` the distinct reason does not exist; from `0.85.0` it
> does.** That changes nothing about the advice above, and the reason is worth
> being precise about: the new reason separates the *policy* case, while the
> no-identifier case still shares `worker.go`'s single catch-all with every other
> observe failure. **The message remains the way to identify the leak either
> way** — what `0.85.0` removes is one unrelated case that used to land in the
> same bucket.
>
> That the dependency sits in `rest-dynamic-controller` rather than
> `oasgen-provider` is not trivia: RDC is the binary that reconciles these CRs,
> so a runtime bump reaches this chart's behaviour through RDC's `go.mod` and not
> through the generator's.

**Both controls held, because both zeros matter.** mock-noid logs DELETEs when it
receives them (the probe sent its own and the log showed it), so "zero DELETEs
from the controller" is an absence. And 198 events were readable in the
namespace, so "no Warning events" would have been an absence rather than an
unreadable event stream — without that second control those two are the same
observation.

**STATE OF THE INSTRUMENT, said plainly.** The `0.52.0` block above was measured
by hand during a run that itself reported VOID. The `0.74.0` block is the
probe's own end-to-end verdict, obtained. Getting there cost four VOIDs, every
one an instrument defect rather than a result:

| VOID | cause |
|---|---|
| 44334008550 | the mock behaviour controls were single-shot against a just-started Service; `rollout status` returning is not endpoint propagation |
| 44342938736 | the chart was installed twice under two release names, so the CR was reconciled by a controller pointed at `nova.invalid` |
| 44347239443 | `curl_through` sent kubectl's stderr to `/dev/null`, so a 90s timeout could not tell "curl reached nothing" from "kubectl never ran the pod" |
| 44351381915 | the `#648` attribution control looked for probe traffic that the probe's own mock restart deletes — and it VOIDed the run, discarding arm Y |

### Why there is no `findby`, decided rather than overlooked

The orphan above exists for one reason: with no `findby` verb the identifier can
never be re-derived, so a create whose response omitted it is unrecoverable by
the controller. Five of the six kinds *could* declare one — `/flavors`,
`/os-aggregates`, `/os-keypairs`, `/os-server-groups` and `/servers` all exist as
collection endpoints; `quota` is a per-project singleton with none.

**Asked and answered on 2026-11-18: do not add one.** The reasons, so this is not
re-opened as an oversight:

* **The loss is reported, not silent, at every version from `0.74.0`.** Two
  Warning events name `flavor.id` and `flavor.name` and tell the operator not to
  delete the CR to clear the condition. That is "reported loss needing manual
  cleanup", not "silent loss".
* **Declaring a `findby` would move this chart INTO a defect class it is
  currently exempt from.** oasgen-provider retracted `0.77.0` for a defect whose
  precondition is a *findby-addressed* kind failing to reach
  `CreateOutcomeUnknown`. Having no `findby` is the exemption there, which
  inverts how the rest of that retraction list reads.
* **It would add a collection LIST on every observe where the id is absent**, on
  every kind, forever — against a vendor whose flavor LIST is already known to
  omit `ram`/`vcpus`/`disk`, so `name` would be the only practical match key.
* The chart is green at its pin and the pin is deliberate.

**What would reopen it:** an orphan actually observed in a deployment, or
upstream making the identifier recoverable without a `findby`. Not a tidier
reading of the same evidence.

### Arm Y: a rejected observe is NOT read as absence

**Measured in run 44356631228, same cluster, same 200s window.** The chart was
pointed at a mock that answers the get path with `401` *after* a create that
returned an id:

```
METHOD  PATH                               STATUS  COUNT
GET     /v2.1/army/flavors/probe-flavor-1  401     x11
CTL=11  POST=0  GET=11

type=Ready    status=Unknown reason=ObserveFailed
type=Synced   status=False   reason=ReconcileError
type=Observed status=True    reason=ResponseObtained
```

**Eleven rejected GETs and ZERO POSTs.** A rejection on observe is not read as
absence; the controller does not re-create. This was the hypothesis the probe
was written to test, and it is refuted at `0.74.0`.

`Ready=Unknown` rather than `False` is the right shape: the controller is not
asserting a negative it cannot know. And the two failure modes stay
**distinguishable from the CR alone** — a rejected observe gives
`Ready=Unknown/ObserveFailed` with `Observed=True/ResponseObtained`, while a lost
identifier gives `Ready=False/NoIdentifierRecorded`. They do not collapse into
one reading, which is worth having measured because it is the kind of property
that quietly stops being true.

### `#648`: the observe after a successful create waits for the resync

**Measured in run 44364522140, oasgen `0.74.0`.** A create that returns an id
does **not** schedule its own observe — the first GET arrives with the next
reconcile.

```
t0  controller POST at mock-ok      1795025224.885
t1  controller GET  at mock-ok      1795025377.960
    t1 - t0                         153.1s   against a LOGGED resync of 180s
    t1 - external-create-succeeded  154.0s   (annotation 2026-11-18T18:07:04Z)

Ready reason samples                25 Creating, 0 Available, over 26 samples
VERDICT                             waits-for-resync
```

**The second figure is the one that makes this a diagnosis rather than an
observation.** 153.1s to the first GET says "slow". `t1 − succeeded = 154.0s`
says "not scheduled": the create's own success stamp lands about a second before
its POST is logged, so the whole gap is in observe scheduling and nowhere inside
the create.

**And the resource sits in `Creating` the entire time** — 25 of 26 samples, zero
reading `Available`. The reason is sampled rather than read once, because a
condition holds only its current value: once Ready flips there is nothing left
to say it was ever `Creating`.

**The threshold is half the resync interval read from the controller's own
startup log, not a hardcoded 180.** A peer measured the cycle running
start-to-start at 177.8s then 180.0s against that logged value, so a fixed
comparison would be a coin flip at the boundary.

**Bounded forward by code, not by more runs.** The oasgen-provider session
measured `0` enqueue/requeue calls inside `handleCreate` at both
`unstructured-runtime` `v1.18.0` (which `0.74.0` pins) and `v1.28.1` (current
`main`), with the extraction line counts and an elsewhere-in-file occurrence
count as controls. So `CreateWithResult` did not fix it and the behaviour is
unchanged across that range — **their measurement, relayed, and the controls are
why it is worth relaying.**

**One line of that run's output was wrong and is not evidence.** It printed
`t2-t0  -1s (Ready=True, ...)`, and a `Ready=True` cannot precede its own create
by a second. The label hardcoded `Ready=True` whatever the status was; the
condition was `False/Creating`, so the figure was the transition *into* Creating.
Fixed so the status is read. **The verdict never depended on it.**

**Nothing in this probe is unmeasured now.** All four arms have answers, and
getting the last one cost seven dispatches with five VOIDs — every VOID this
probe's own instrument rather than the behaviour under test. The causes are
listed in the table above because each is a distinct class and each now has a
self-test arm: single-shot controls against a just-started Service; the chart
installed twice under two release names; kubectl's stderr discarded so a timeout
could not name its own failure; a control looking for traffic the probe's own
mock restart deletes; and `kubectl run --rm -i` losing its output when the
container exits before the attach establishes.

**The last one is the generalisable lesson: retrying a race does not change the
odds.** Fifteen retries lost the same attach race, and the readiness loop added
for the first VOID could not help, because that loop answers "not ready yet"
while this failure is "finished, and nobody was listening". The pod's log
survives the container's exit; the attach does not.

## `ComputeVolumeAttachment` ships disabled, and why it is the one kind that cannot orphan

Added for the Ceph/Rook storage acceptance test. It attaches an existing Cinder
volume to an existing server:

```
create  POST   /servers/{server_id}/os-volume_attachments
get     GET    /servers/{server_id}/os-volume_attachments/{volume_id}
delete  DELETE /servers/{server_id}/os-volume_attachments/{volume_id}
```

**Both path parameters come from `spec`, and that is the whole point.** Every
other kind in this chart takes its `get` parameter from
`status.<envelope>.id`, filled from the create response — which is why a create
that answers `200` without the identifier leaves the resource unaddressable and
unrecoverable, measured above. Nova addresses an attachment by the **volume id
the caller supplied**, so nothing this kind needs ever has to come back over the
wire, and there is no created-but-unaddressable state for it to reach.

The identifier is bare `volumeId` rather than dotted, for the reason
`rd-quota.yaml` records: a dotted `volumeAttachment.volumeId` would make the
identifier depend on the response again. Observe substitutes it into the `get`
path instead of matching it against a payload. oasgen ≥ 0.54.0 will warn
*"status field 'volumeId' not found in response"* — correct, and harmless only
because this kind declares no `findby`. **It becomes a real defect the moment it
gains one.**

### The dotted spec mapping is MEASURED, and it works

One mapping — `inPath: volume_id` → `spec.volumeAttachment.volumeId` — points a
path parameter at a **dotted target in `spec`**. Every proven mapping in this
chart points into `status` (`inPath: id` → `status.<envelope>.id`), and
`rd-quota.yaml`'s three point at a **bare** `spec.tenant_id`, which is the
self-referential shape oasgen-provider#421 exempts. A dotted spec target is
neither, so it was the one shape here that had never been measured. It now has
been — twice, at **two provider versions**, on a real cluster against a
request-logging stand-in: run `44880829168` at oasgen **0.74.0** (what the
Krateo installer ships) and run `44884303626` at **0.80.0** (what
`krateo-openstack-blueprint`'s deployment path installs for itself). **Identical
in every field, including both `schemaWarnings`:**

```
ARM A  spec properties   ['configurationRef', 'server_id', 'volumeAttachment']
       volumeAttachment  ['device', 'volumeId']
       -> spec.volumeAttachment.volumeId SURVIVED #419's suppression, and
          spec.server_id was created by the self-referential mapping as #421 intends

ARM B  GET /v2.1/p/servers/srv-probe-1/os-volume_attachments/vol-probe-1  200
       controller requests 1   carrying the real ids 1   literal templates 0
       -> {volume_id} and {server_id} both SUBSTITUTED on the wire
```

**The controls are what make those results rather than observations.** ARM A
refuses to conclude unless the CRD's spec carries some property, so "volumeId
absent" can never be "the schema is empty". ARM B lays down a request from its
own User-Agent first, so a controller request count of zero is an absence rather
than an unread log — and the two agents are distinguishable in the access log
(`probe-vam-control` versus `Go-http-client/1.1`). A single RestDefinition owned
the Kind, so the request is attributable. `hack/probe-volume-attachment-mapping.sh`
re-runs the whole reading at any provider version, and that is not decoration:
the first run measured `0.74.0` because that is this chart's documented pin, and
a peer then pointed out that **the deployment which actually runs these KOGs
installs `0.80.0` itself** — six minors away. The reading was re-taken there
rather than assumed to carry across. It did carry, which is the outcome worth
having measured rather than hoped for.

**The provider confirmed it from the other direction**, in the RestDefinition's
own `schemaWarnings`:

> parameter `"volume_id"` is omitted from the generated spec because verb
> `"get"` supplies it with a request-direction fieldMapping (inPath, from
> `"spec.volumeAttachment.volumeId"`), so it is not the author's to set.

That is why `server_id` appears as a spec property and `volume_id` does not: one
mapping is **self-referential and creates** its property, the other **reads** a
property the request body schema already declares. Two shapes that look alike in
the template behave differently, and the warning says so explicitly.

The second warning — *"status field 'volumeId' not found in response, defaulting
to string"* — arrived exactly as the template comment predicted in writing
before the run, and is harmless for the same stated reason: no `findby`.

### So why is it still `enabled: false`?

**The volume-attachment kind and boot-from-volume share one blocker, and it is
not ours.** Both need a real Cinder volume, and the deployment that could supply
one is blocked on
[krateo-openstack-blueprint#308](https://github.com/krateo-blueprints/krateo-openstack-blueprint/issues/308)
(no Cinder backend until its storage phase lands). The boot-from-volume case is
tracked in
[#431](https://github.com/krateo-blueprints/krateo-openstack-blueprint/issues/431),
which closes only on this repo's confirmed receipt of the reading.

**Because the mapping was measured and the kind was not exercised.** The stand-in
answers `200` unconditionally, so no real Cinder volume had ever been attached.

**That reason no longer holds: the kind is now exercised end to end** against
real Cinder on Ceph RBD — reported by the openstack-as-a-service session on
2026-11-26 at nova-kog 0.1.11, and **repeated on a second, fresh cluster** on
2026-11-28 at 0.1.12 with all four views agreeing again. Second-hand, not run
here. One reading can be a cluster's accident; two on different clusters cannot,
which is the difference between "it worked" and "it works". The earlier such
reading so far; recorded because it is the evidence this entry was waiting for.
Each link in their chain was checked against a different system rather than one
tool's opinion: the CR reached `Ready` with `status.volumeAttachment.device`
`/dev/vdb`, Nova listed the volume on the server at that same device, Cinder
showed it `in-use` against that server, libvirt's domain carried it as an RBD
disk, and a marker written to `/dev/vdb` inside the guest over SSH read back
after the guest's page cache was dropped — with the vendor reporting *exactly*
one attachment, not "at least one". So `{volume_id}` substitutes from
`spec.volumeAttachment.volumeId` on a live vendor, and `get` addresses the
attachment by volume id; that second half had never been measured anywhere.

So `false` is now **waiting on a decision rather than on evidence**, and those
are different things. Enabling a kind adds a CRD and a per-kind controller
Deployment to every install of this chart — a footprint change for every
consumer, which is not a documentation edit's to make.
Nothing is pending: set `restdefinitions.volume_attachment.enabled=true` and the
definition renders — measured both ways, 6 RestDefinitions with it off and 7
with it on, `helm lint` clean in both.

> [!IMPORTANT]
> **Through 0.1.10 that was true of `helm` only, and the blueprint path could not
> reach it at all.** `chart/values.schema.json` listed six kinds while
> `values.yaml` defined seven, and core-provider generates the Composition CRD
> from that schema, so a Composition setting
> `spec.restdefinitions.volume_attachment` was rejected by the API server with
> `strict decoding error: unknown field`. Measured live on GKE by the
> openstack-as-a-service session's #308 run, which had to tear the cluster down.
> `helm lint` could never have caught it: it reads `values.yaml`, not the schema.
> The kinds the schema omitted were exactly the ones shipped off by default,
> because a kind nobody enables is one nobody writes into a Composition.
> **Fixed in 0.1.11**, whose published chart carries all seven keys in both
> files (verified by pulling it, not from the release's green).
> `verify_schema_covers_every_values_key` in `scripts/validate.py` now compares
> the two files in both directions, defaults included.

### Fields deliberately absent

`tag` (microversion 2.49 on this body) and `delete_on_termination` (2.79). This
chart sends no microversion header, so Nova serves 2.1 and refuses a body
carrying either, with `Additional properties are not allowed` —
`verify_no_request_field_above_microversion_baseline` keeps them out.

## How a lost Lease presents: `/readyz` goes 503, then the pod goes NotReady

Each generated controller holds a Kubernetes **coordination Lease** for leader
election. If it loses access to that Lease — an RBAC change, a webhook, an
apiserver it cannot reach — **that controller stops reconciling its CRs.** The CRs
do not report an error of their own: they simply stop changing.

**Measured twice on a real cluster at oasgen-provider `0.85.2`** (runs
`44930009330` and `44931943538`, both `conclusion=success`, each with a second
controller as a healthy control throughout):

> [!WARNING]
> **`503` and *unreachable* are different findings.** Everything above is the
> `503` case: a reachable endpoint refusing. **An endpoint that cannot be reached
> at all usually means the listener was disabled**, not that a Lease was lost.
>
> `REST_CONTROLLER_HEALTH_PROBE_PORT: "0"` disables it — and in that configuration
> `rdc.readinessProbe` must be set to **`null`**, not `{}`. **Helm merges maps**,
> so `{}` leaves the chart's `httpGet` in place, the probe keeps polling a port
> nothing binds, and **every pod goes NotReady for a reason that has nothing to do
> with the Lease.** An operator in that state sees the same symptoms this section
> describes and would look for an RBAC problem that does not exist.

**Three things carry the name "the readiness path or port", and only one of them is
fixed.** Derive the other two from the deployed object, exactly as with the window:

| | where it lives | changeable? |
|---|---|---|
| the **server's** route | hardcoded in the provider's `health.go` | **no** |
| the **probe's** request path | `rdc.readinessProbe.httpGet.path` | **yes**, a chart value |
| the **probe's** port | `rdc.readinessProbe.httpGet.port`, matching `rdc.healthProbePort` | **yes**, a chart value |

Read from the published chart at `0.85.2`, with a deliberately absent key as the
control: `httpGet.path: /readyz`, `httpGet.port: 8081`, `healthProbePort: 8081`.
**So `8081` and `/readyz` below are a reading of the default install, not
properties of the controller.** Change the probe's path and every pod goes
NotReady against a server answering perfectly well on `/readyz` — the probe asks
for something that was never served.

| what you can observe | when |
|---|---|
| `GET /readyz` on port **8081** returns **503** | within **30s** of the Lease becoming unreachable |
| the Pod's `Ready` condition flips to `False` | within the **derived window**, ~**300s** |
| the Pod is still `Ready=True` while `/readyz` is already 503 | for the first ~30s — this is the `failureThreshold` working, not a bug |
| `/readyz` returns to **200** and reconciliation resumes | on its own, within ~10s of access being restored, **no restart needed** |

Neither the ~300s window nor the port is a constant to memorise. The window is
`periodSeconds × failureThreshold` read off the deployed object, `10 × 30` at
`0.85.2`; the port is `rdc.healthProbePort`, `8081` by default. **Derive both
rather than assume them** — a future provider release may ship
different numbers, and the arm that produced this table derives it on every run:

```bash
P=.spec.template.spec.containers[0].readinessProbe
kubectl -n krateo-system get deploy <controller> \
  -o jsonpath="{$P.periodSeconds}{'\t'}{$P.failureThreshold}{'\n'}"
```

**Why the delay is deliberate.** A transient Lease blip must not evict a working
controller; a persistent one must. That is what the threshold buys, and the gap
between the two signals is where it is visible: at ~30 seconds the endpoint is
already refusing while the kubelet has not yet acted.

**What to check first if CRs stop changing and nothing looks wrong:**

```bash
kubectl -n krateo-system get pods -l app.kubernetes.io/name=<controller>
kubectl -n krateo-system exec deploy/<controller> -- wget -qO- localhost:8081/readyz
kubectl auth can-i get leases.coordination.k8s.io/<lease-name> \
  -n krateo-system --as=system:serviceaccount:krateo-system:<controller-sa>
```

> [!IMPORTANT]
> The `can-i` must **name the lease**. The provider scopes each controller's
> Lease rule with `resourceNames`, so the unnamed question — `can-i get
> leases.coordination.k8s.io` — asks "can you get *any* lease?" and answers **no**
> for a grant that is present and working. Read the name from the Role:
> `kubectl -n krateo-system get role <controller-sa> -o jsonpath='{.rules[*].resourceNames[*]}'`.

**See also the sibling trap, and it is worse because the error misdirects:**
`kubectl apply` cannot modify *any* RBAC this provider generates. Those Roles are
created by its controller, so `kubectl` never wrote the
`kubectl.kubernetes.io/last-applied-configuration` annotation that client-side
apply requires — and the failure names that annotation, so a reader goes looking
at permissions. `kubectl edit`, a JSON patch, or `kubectl apply --server-side`
all work. Documented upstream as
[oasgen-provider#872](https://github.com/krateo-platformops/oasgen-provider/issues/872)
(*"kubectl apply cannot modify any RBAC this provider generates"*, closed — the
remedy is in their create-markers runbook), which is the place to check if the
behaviour changes.

**This is the provider's behaviour, not this chart's**, and it is recorded here
because the consequence is the user's: a silently stalled controller is otherwise
indistinguishable from a broken one. `hack/probe-lease-readiness.sh` re-measures
it on demand; `.github/workflows/lease-readiness-arm.yaml` dispatches it, and the
trigger is our provider pin moving rather than a schedule.

## Authentication and token rotation

The Keystone token lives in a Secret referenced by each `<Kind>Configuration` CR:

```yaml
spec:
  authentication:
    apiKey:
      tokenRef:
        name: nova-token
        namespace: krateo-system
        key: token
      header: X-Auth-Token
```

Two properties of that shape are worth knowing:

- **The Secret holds the raw token value only** — no prefix, no `Bearer `. The wire
  format is kept out of the Secret deliberately, so whatever mints the token never has
  to know about HTTP framing. (`valuePrefix` exists upstream for APIs that declare
  `apiKey` but want a prefix; for Keystone it stays empty.)
- **`header` is a real field, not a hidden constant.** oasgen-provider defaults it from
  the OAS scheme name, but surfaces it on the CR so the value it derived is visible when
  you are debugging a 401.

**Keystone tokens expire (~1h on OVH), and keeping that Secret fresh is a
secret-manager's job.** Point External Secrets Operator, Vault Agent, or your
platform's equivalent at the Secret and let it own minting and rotation. This chart
ships no CronJob, no sidecar and no refresh logic by design — rotation is a solved
problem that does not belong in an operator package. `scripts/get-token.sh --secret`
remains a convenience for bootstrapping a local demo, not a production path.

## Overrides

| key | default | effect |
|---|---|---|
| `nameOverride` | `""` | override the chart name used in resource names. |
| `fullnameOverride` | `""` | override the fully-qualified app name used in resource names. |

**There is no `serviceAccount` key, and this section used to document one.**
`serviceAccount.create` and `serviceAccount.name` were declared in
`values.yaml` and `values.schema.json` and read by **no template** — measured at
0 references against `restdefinitions` 24, `nameOverride` 2,
`fullnameOverride` 2 and `nova` 1 as controls. They were removed in #154, and
this table was left behind by that PR: for about an hour the chart documented a
knob it no longer declared, which is the same defect the PR was fixing, one
layer out. Removed here.

This chart deploys no workload of its own — it emits ConfigMaps and
RestDefinitions, and `oasgen-provider` runs one `rest-dynamic-controller` per
RestDefinition under **its own** ServiceAccount. To set the controllers'
ServiceAccount, use the `oasgen-provider` chart's values, not this one's.
