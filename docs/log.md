---
type: Log
title: openstack-nova-operator-kog — log
description: Curated chronological history of the openstack-nova-operator-kog blueprint — notable changes and design decisions, not a generated changelog.
resource: oci://ghcr.io/krateo-blueprints/charts/openstack-nova-operator-kog
tags: [log, history]
timestamp: 2026-08-11T00:00:00Z
---

# Log

Curated history; release notes live in GitHub Releases.

## 2026-11-28 — the first dry run since `ci/` left the package, and R5 measured

**Part A: the release path, dry-run after #273.** PR #273 (commit `4330ce8`,
merged 2026-11-28 12:06Z) added `chart/.helmignore` excluding `ci/` and
`.helmignore` itself, to keep CI fixtures out of the published package. Three dry
runs earlier the same day — 46127830773 at head `c065681`, 46125049889 at head
`f265575`, 46119254691 at head `0045ba4` — all predate `4330ce8`, so no CI run of
the shared image gate had happened at a head carrying the `.helmignore`. That is
the whole of what was new. The prerequisite itself was checked before #273
merged: the same read of the gate (`cp -r "$d" "$work/$name"` at line 107, values
passed with `-f` at line 174, both outside the chart loader), plus a local
stand-in — with the `.helmignore` in place, `helm template verify <staged> -f
<staged>/ci/render-values.yaml` exited 0 at 1635 lines, and without the `-f` it
still failed on the `nova.url` guard, the discriminating control that makes the
first result mean something. The local `helm template` is a stand-in because it
is not the gate: it does not do the `cp -r`, the `Chart.yaml` stamping, the
`kubeVersion` floor or the image grep, so it could agree with the gate for the
wrong reason. Today's run upgrades "read, plus a local stand-in" to the gate
itself, in CI, on the real head.

That mattered because #263 exists: this chart does not render at its default
values, and the fix was `chart/ci/render-values.yaml`, which the shared image
gate reads by Helm's own `ci/*-values.yaml` convention (the one chart-testing
reads). Had the gate rendered an unpacked `.tgz` rather than the source tree,
excluding `ci/` from the package would have reinstated exactly the state #263
fixed: a chart that cannot render, whose images therefore cannot be checked, and
which the gate correctly refuses to vouch for — so it could never publish.

**Read-verified, not run-verified:** the shared image-gate workflow (`chart-images`) at pin
`88eaff13555233606da5ff3ff3bfed8f534436ce` does `cp -r "$d" "$work/$name"` (line
107) from the checkout and renders that copy with `helm template`, collecting
`-f` arguments from `"$work/$name"/ci/*-values.yaml` (line 166). `.helmignore`
affects only `helm package`, so it cannot starve the gate. Safe by construction —
by reading the file.

**Run-verified:** run 46159190519, a `workflow_dispatch` of `release.yaml` at
`chart_version=0.1.13` and `dry_run=true`, head `4330ce8`. Conclusion: success.
The gate printed `chart: rendering with 1 ci values file(s)`, followed by
`checked 0 image reference(s) across 1 chart(s)`. Jobs: `index-preflight`
success, `verify-images` / `chart-images` success, `release-preflight` /
`preflight` success, `package-and-push` success, `assets` success, `collect`
skipped, `publish-index` skipped — the last two correctly skipped for a dry run.
Packaging printed `Successfully packaged chart and saved it to:
dist/openstack-nova-operator-kog-0.1.13.tgz` and
`openstack-nova-operator-kog-0.1.13.tgz: carries
openstack-nova-operator-kog/compositiondefinition.yaml installing
oci://ghcr.io/krateo-blueprints/charts/openstack-nova-operator-kog 0.1.13`, so the
CompositionDefinition rides inside the package from `chart/` and is stamped. And
`##[notice]dry run: nothing pushed.`

**What that green is NOT.** `checked 0 image reference(s)` means the gate vouched
for nothing about this chart's images. That is honest rather than vacuous — this
chart ships no container image; the provider images are rendered by the oasgen
provider at runtime — but a passing `verify-images` is not evidence about our
images, and nobody should later read it as such.

**The instrument nearly produced a false pass.** `gh run view --log` contains
each script's TEXT as well as its output, so the full 1610-line log holds 42
`::error::` and 4 `did not render`, every one of them script text. The first
filter was `grep -v $'\033\[36;1m'`, which the local grep (ugrep) parsed as a
bracket expression and rejected with "error at position 5" — zero lines of
output, which made every "must be 0" check pass. The reassuring answer and the
broken-instrument answer were the same string. The working filter uses `grep -vF`
on the literal escape prefix and leaves 918 of 1610 lines, with two controls: a
known-real line survives it (count 1), and `echo "::error::` counts 0 in the
filtered log while `::error::` counts 42 in the full one.

**Part B: R5 readiness, measured rather than inferred.** acmp reported
(**peer-reported**, 2026-11-28) that the R5 warn-to-fail flip rides in
krateo-blueprints/charts#72, which they merge on 2026-12-02, tagging from that
merge commit — and that the tag number is explicitly not promised: it is "the
first charts tag cut after #72 merges, planned as 1.15.0", because 1.14.0 was
announced as carrying this flip and shipped without it.

That is the reason the watcher changed. A watcher keyed to the literal tag
`1.15.0` is wrong in both directions at once — silent forever if the tag is cut
as 1.16.0, and firing on a 1.15.0 that does not carry the flip — and both
failures look exactly like "not yet". It now keys on the checker's behaviour at
the dereferenced commit: any new bare-semver tag above 1.14.1 triggers a
dereference, a fetch of `.github/workflows/repository-standard.yaml` at that
commit, and a count of `fail("R5"` against `warn("R5"`. A control guards the read:
every version of that file contains `fail(` calls, so a count of zero means the
fetch or decode broke and the watcher prints VOID rather than a reassuring "not
yet".

Baseline at charts 1.14.1, dereferenced to commit
`dfd2fde56249748002d2ce46613279c3ea2ff5b5`: `fail("R5"` = 0 and `warn("R5"` = 6,
with 14 total `fail(` in the file as the control. Per-rule fail counts there:
R1=2, R2=2, R3=4, R4=1, R6=2, R7=1, R8=1 — and no R5, which is what makes R5
advisory today.

Our readiness, with the checker's own regexes run against
`.github/workflows/release.yaml`: a caller of release-oci True, `index_channel`
present True, `packages: write` present True, `chart_version` present True,
`workflow_dispatch` present True, and as a control a regex that must be false —
`index_channel: operators` — False. Separately, `calls()` in the checker is
anchored to `^\s*-?\s*uses:\s*['"]?<path>@`, so `release.yaml`'s nine mentions
of publish-chart in comments cannot trip the "calls publish-chart beside
release-oci" condition; no workflow of ours has a `uses:` line naming
publish-chart, release-preflight or chart-push, and no release-tag workflow exists.
All seven R5 conditions pass today, so the flip will not turn our build red. That
is a measurement at this pin; it will be re-measured when the pin moves, not
assumed to still hold.

## 2026-11 — a boot-from-volume Instance could never report anything

`server.image` was listed in the Instance RestDefinition's
`additionalStatusFields` and declared in the OAS as an object. Nova returns
`image` as an **object** for a server booted from an image and as the **empty
string** for one booted from a volume — its own source says so
(`nova/api/openstack/compute/views/servers.py`, `_get_image` at `stable/2026.1`:
the else branch is `return ""`). A CRD status schema is structural and a property
has exactly one type, so a volume-backed Instance's status write was refused and
`observe` failed forever:

```
observe failed: ... status.server.image: Invalid value: "string":
server.image in body must be of type object: "string"
```

Measured on a live cluster: the server itself booted and ran correctly — ACTIVE
on KVM, console login prompt, answering ping and SSH, metadata reporting its own
uuid — while the CR sat at `Ready=Unknown/ObserveFailed` for 900 s with
`server.id` as the only status field ever written. **Nova was fine; only the
reporting was broken**, which is the worst shape for this defect: the resource a
user asked for exists and works, and nothing watching the CR can tell.

**Fixed by dropping `server.image` from the status fields, not by retyping it.**
Declaring it a string would move the same failure onto image-backed servers, the
majority case, and a structural schema cannot express both. The field stays in
the `get` response schema, where it is true. Nothing in this repository read
`status.server.image` — checked repo-wide before removing it — and the other five
status fields were checked against the same Nova source: `flavor` is an object on
every path, `addresses` is an object, and `status`, `tenant_id` and `created` are
always strings. `verify_no_polymorphic_status_fields` in `scripts/validate.py`
now refuses to let the field back in, because the fix is the absence of a line
and an absence is what a later reader restores in good faith.

> [!IMPORTANT]
> **On an existing installation, verify that the CRD actually re-rendered.** This
> fix changes the generated schema, and an upgrade can stamp
> `status.generatorBuild` while skipping generation entirely
> ([oasgen-provider#541](https://github.com/krateo-platformops/oasgen-provider/issues/541)),
> leaving the old schema served and the field still rejected. Check that
> `status.server.image` is gone from
> `kubectl get crd instances.nova.openstack.krateo.io -o json` rather than
> trusting the reported build.

**Verified live on 0.1.12, with the control.** A fresh GKE install (core-provider
2.13.135) created both kinds of Instance and both reached `Ready`:

| | `Ready` | `.status.server` |
|---|---|---|
| image-backed | True/Available at 2026-11-28T11:01:24Z | `id`, `name`, `status: ACTIVE`, `created`, `flavor`, `tenant_id`, `addresses` — **no `image`** |
| boot-from-volume | True/Available at 11:01:54Z | `id`, `status: ACTIVE`; CR address matches the server's port |

Both also reported `Synced True/ReconcileSuccess` and `Observed
True/ResponseObtained`, and neither carried `server.image` anywhere.

**And the same run found a second scenario the defect broke, which the original
report did not cover: observation after a REBUILD.** A `server rebuild
--reimage-boot-volume` at microversion 2.93 left the CR reporting
`.status.server.status: ACTIVE` with `Ready True/Available` on 0.1.12 — and that
check **failed on 0.1.11**. So the unobservable window was not limited to a
volume-backed server's creation; it reopened every time such a server was
rebuilt. The rebuild itself was confirmed real rather than a no-op by two
independent readings: the marker written to the boot volume's root filesystem was
erased, and Cinder had recreated the RBD image under the same name (new internal
id, new create time). The attached data volume and its own marker survived.

**The image-backed arm is the one that matters here, and it is why this entry can
say the fix is settled rather than merely applied.** The defect was in the
volume-backed path, but image-backed servers were the ones actually *populating*
`server.image` — so dropping the field could have repaired the rare case by
breaking the common one, and nothing in CI could have told the difference. A pass
on the fixed case alone would have been the reassuring half of a two-sided
question. (Measured by the openstack-as-a-service session; second-hand, and the
only reading so far.)

**What the same run showed about the blast radius, which the defect report alone
did not.** The broken field cost *reporting only*, never an orphan: the Instance
CR deleted cleanly **although it was never `Ready`**, and the server went with
it. Teardown in dependency order left nothing behind — the attachment CR's
deletion removed Nova's attachment, the second volume was `available` 0 s later,
and the boot volume, created with `delete_on_termination: false`, **survived the
server** and was then removed by its own CR. Exactly one of each object existed
in OpenStack throughout. So an operator who hit this defect had a server they
could still delete through Kubernetes; what they could not do was learn anything
about it from the CR.

**Rebuild survives the attachment, out of band.** A `server rebuild
--reimage-boot-volume` (which OSC requires for a volume-backed server, and which
needs microversion **2.93**) returned the server to ACTIVE with no duplicate, the
attachment CR still `Ready` with the same volume and `/dev/vdb`, and a marker
written to the data volume before the rebuild still readable after. Worth
recording because this chart is 2.1 everywhere by design and sends no
microversion header: a *declarative* rebuild verb here would need far more than a
new path, so rebuild stays an out-of-band operation.

Both readings are second-hand (the openstack-as-a-service session's #308 run B),
and one gap is recorded as a gap rather than a pass: Cinder's side of teardown
was measured, the removal of the RBD images from Ceph was not.

## 2026-11 — the two install paths had diverged, and the schema was the fork

`chart/values.schema.json` listed six `restdefinitions` keys while
`chart/values.yaml` defined seven. core-provider generates the Composition CRD
from the schema and the API server decodes strictly, so through **0.1.10**
`helm install --set restdefinitions.volume_attachment.enabled=true` enabled the
kind and a Composition could not express the field at all — it was rejected with
`strict decoding error: unknown field "spec.restdefinitions.volume_attachment"`.
Found by a live run on GKE, not by CI: `helm lint` reads `values.yaml` and never
reads the schema. **The kinds the schema omitted were exactly the ones shipped
disabled**, which is the mechanism rather than a coincidence — a kind nobody
enables is a kind nobody writes into a Composition, so the omission stays
invisible until the first consumer wants it.

Fixed in **0.1.11**, which also corrects a second drift the same sweep found:
`quota.enabled` was `true` in `values.yaml` and `false` in the schema, stale
since the kind was unparked at the oasgen 0.52.0 pin.

**That second drift was a behavioural fork, not a documentation error.** Measured
on a live cluster after the fix (nova-kog 0.1.11, core-provider 2.13.130): a
Composition setting only `restdefinitions.volume_attachment` had **all seven**
keys stored by the API server, each carrying `enabled` plus `resourceGroup` and
`resourceKind` from the schema. The CRD's defaults materialise absent objects
whole. It follows — by inference from that reading, since 0.1.10 was not
re-installed — that a Composition install at 0.1.10 got `ComputeQuota` **off**
regardless of `values.yaml`, while every helm install got it on.

So **upgrading a Composition-installed cluster to 0.1.11 adds `ComputeQuota`**.
That is not a new decision: enabling quota was decided at the 0.52.0 pin and
`values.yaml` has said `true` ever since. The fix propagates that decision to the
install path which had silently diverged from it.

`verify_schema_covers_every_values_key` in `scripts/validate.py` now compares the
two files in both directions, defaults included, so neither kind of drift can
return.

## 2026-10 — auth bridge removed, native apiKey auth

The `nova-auth-bridge` nginx proxy was deleted. It had rewritten
`Authorization: Bearer <t>` into `X-Auth-Token: <t>` because `oasgen-provider`
then accepted only `http/basic` and `http/bearer`; `apiKey` / `in: header` has
been supported since 0.19.0, so the OAS now declares `X-Auth-Token` directly and
`rest-dynamic-controller` sets it (mechanism and source lines: [architecture.md](./architecture.md#how-authentication-works-and-why-there-is-no-proxy), verified at `origin/main` by the oasgen-provider session). `authBridge.*` was replaced by a single
required `nova.url`. Keystone token rotation is explicitly a secret-manager's
responsibility — the chart ships no CronJob, sidecar or refresh logic.

## 2026-08-11 — Documentation Standard adoption

The repo adopts the Krateo Documentation Standard (OKF): the invariant docs bundle
(`index`/`overview`/`usage`/`configuration`/`api`/`examples`/`release`/`log` +
`llms.txt`), a runnable `examples/nova-composition`, and the shared `lint-docs` check
wired into a new `lint.yaml`. The pre-existing deep-dive docs (`architecture`,
`quickstart`, `e2e`, `e2e-krateo-openstack`, and the two Medium write-ups) gain OKF
frontmatter and are indexed from `index.md`. README is rewritten to the six-section
standard shape. Part of krateo-platformops/installer#52.

## Initial packaging

The blueprint ships whole: the KOG chart (per-resource OAS ConfigMaps and
`RestDefinition`s for Servers → `Instance`, Flavors → `ComputeFlavor`, Keypairs →
`ComputeKeypair`, Server Groups → `ComputeServerGroup`, Host Aggregates →
`ComputeAggregate`, and Quota Sets → `ComputeQuota` off by default), the
`nova-auth-bridge` nginx proxy, worked sample CRs, and the `get-token.sh` Keystone
fetcher. Two decisions worth keeping:

- **The kinds are prefixed** (`Instance`, `ComputeFlavor`, ... not `Server`,
  `Flavor`). Each Nova request body is envelope-wrapped, so the generated spec carries
  a property named after the resource; a Kind matching it collides in `crdgen` and
  yields an invalid CRD. Prefixing the kinds sidesteps the collision.
- **The auth bridge exists because KOG only speaks `http/basic` and `http/bearer`.**
  Nova authenticates with `X-Auth-Token`, so a stateless nginx proxy rewrites the
  header. It is interim: when upstream KOG grows `apiKey` support the bridge can be
  removed.

Validated against OVH Public Cloud as the managed OpenStack target and against a
self-hosted Krateo-blueprint OpenStack (see `docs/e2e.md` and
`docs/e2e-krateo-openstack.md`).
