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
