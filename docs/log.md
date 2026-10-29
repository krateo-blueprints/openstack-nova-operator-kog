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

## 2026-10 — auth bridge removed, native apiKey auth

The `nova-auth-bridge` nginx proxy was deleted. It had rewritten
`Authorization: Bearer <t>` into `X-Auth-Token: <t>` because `oasgen-provider`
then accepted only `http/basic` and `http/bearer`; `apiKey` / `in: header` has
been supported since 0.19.0, so the OAS now declares `X-Auth-Token` directly and
`rest-dynamic-controller` sets it. `authBridge.*` was replaced by a single
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
