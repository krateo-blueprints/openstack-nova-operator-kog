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
