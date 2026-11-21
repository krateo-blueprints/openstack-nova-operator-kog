---
type: ExampleIndex
title: openstack-nova-operator-kog — examples
description: Index of the runnable examples under examples/ and the worked CRs shipped in chart/samples/.
resource: oci://ghcr.io/krateo-blueprints/charts/openstack-nova-operator-kog
tags: [examples, composition, samples]
timestamp: 2026-08-11T00:00:00Z
---

# Examples

## Under `examples/`

- [examples/nova-composition](../examples/nova-composition/README.md) — install the
  blueprint as a Krateo Composition: an `OpenstackNovaOperatorKog` CR that deploys the
  toggled Nova `RestDefinition`s through the Krateo control
  plane. It doubles as the reference for the derived Composition GVK
  (`composition.krateo.io/v<chart-version>`, kind `OpenstackNovaOperatorKog`) —
  read the exact value from `.status.apiVersion` on the CompositionDefinition
  rather than copying it, since it tracks the chart version down to the patch.

## Worked CRs in `chart/samples/`

These are applied after the operator packaging is installed, to drive real Nova
resources ([usage](./usage.md)):

- `chart/samples/nova-auth.yaml` — the `InstanceConfiguration` that references the
  `nova-token` Keystone-token Secret.
- `chart/samples/test-server.yaml` — a sample `Instance` CR (a Nova server); edit
  `flavorRef` / `imageRef` / network UUID for your project.
- `chart/samples/extra-resources.yaml` — sample `ComputeFlavor` / `ComputeKeypair` /
  `ComputeServerGroup` / `ComputeAggregate` CRs with their `<Kind>Configuration`
  objects.

## Full walkthroughs

- [quickstart](./quickstart.md) — install to a VM in Horizon, the short path.
- [e2e](./e2e.md) — the complete OVH Public Cloud walkthrough on a `kind` cluster.
- [e2e-krateo-openstack](./e2e-krateo-openstack.md) — the same against a self-hosted
  Krateo-blueprint OpenStack, with screenshots.

## A reconciled `Instance` from a real Nova

**The first end-to-end artifact in this repo that is not a mock.** Everything
else here is rendered, planted or answered by an nginx stand-in. This is one
object from a live OpenStack: an instance created, booted `ACTIVE` on a KVM
node, read back, and deleted.

**Provenance, because a measurement without it is an anecdote.** Measured by the
`krateo-openstack-blueprint` data-plane test (dp303) run 3 on **2026-11-21**,
blueprint `0.3.74-main.gc16973f`, **nova-kog `0.1.9`**, **oasgen-provider
`0.80.0`** (image asserted), on GKE. The readings and the scrub are theirs; the
annotations below are this repo's.

```yaml
apiVersion: nova.openstack.krateo.io/v2-1
kind: Instance
metadata:
  annotations:
    # krateo.io/connector-verbose: "true" WAS SET IN THE CAPTURE AND IS REMOVED
    # HERE DELIBERATELY. See the note below -- do not copy it into a cluster you
    # care about.
    krateo.io/external-create-pending: "2026-11-21T09:34:57Z"
    krateo.io/external-create-succeeded: "2026-11-21T09:34:58Z"
  creationTimestamp: "2026-11-21T09:34:56Z"
  finalizers:
  - composition.krateo.io/finalizer
  generation: 1
  labels:
    dp303-run: "11210833"
    krateo.io/oas-version: v2-1
  name: dp303-11210833-vm
  namespace: openstack
spec:
  configurationRef:
    name: dp303-instance
    namespace: openstack
  server:
    flavorRef: e512bd04-f049-4e4f-8d18-63fb90273e2e
    imageRef: 83aa263b-9463-472a-b930-b21e471efd1a
    key_name: dp303-11210833-key
    metadata:
      dp303-run: "11210833"
      managed-by: krateo-kog
    name: dp303-11210833-vm
    networks:
    - uuid: 71dcfbe5-c172-453d-b28d-55fad31142b4
    security_groups:
    - name: dp303-11210833-sg
status:
  conditions:
  - lastTransitionTime: "2026-11-21T09:37:53Z"
    message: ""
    reason: Available
    status: "True"
    type: Ready
  - lastTransitionTime: "2026-11-21T09:34:58Z"
    message: ""
    reason: ReconcileSuccess
    status: "True"
    type: Synced
  - lastTransitionTime: "2026-11-21T09:37:53Z"
    message: ""
    reason: ResponseObtained
    status: "True"
    type: Observed
  server:
    addresses:
      dp303-11210833-net:
      - addr: 192.168.233.15
        version: 4
    created: "2026-11-21T09:34:58Z"
    flavor:
      id: e512bd04-f049-4e4f-8d18-63fb90273e2e
    id: f9c270f5-a647-4803-96fc-ef3181e827a8
    image:
      id: 83aa263b-9463-472a-b930-b21e471efd1a
    name: dp303-11210833-vm
    status: ACTIVE
    tenant_id: 1445d085f68343feb7eb5f1fd1e28b58
```

> [!CAUTION]
> **`krateo.io/connector-verbose: "true"` was present in the capture and is
> removed above.** It was a test-harness setting, and it is not one to copy.
> This chart's credential is a header (`X-Auth-Token`), and the accurate
> statement for every version including the `0.80.0` this ran on is
> **"requests are redacted, responses are not"**:
> [#566](https://github.com/krateo-platformops/oasgen-provider/issues/566) (the
> response dump is written unredacted) and
> [#567](https://github.com/krateo-platformops/oasgen-provider/issues/567) (the
> vendor error snippet is logged at `Info` with no verbose flag at all) were
> **both still OPEN when re-checked on 2026-11-21**, with a closed issue used as
> the control so "OPEN" is a reading rather than what the query always prints. A
> server create response can carry `adminPass`. `docs/configuration.md` has the
> full history of that row, including the two times it claimed a fix version
> that did not exist.

### What the object itself proves about timing

The annotations and conditions are a clock, and they close a question this repo
could previously only answer against a stand-in:

```
09:34:56Z  creationTimestamp
09:34:57Z  external-create-pending      the create is about to be issued
09:34:58Z  external-create-succeeded    +1s   -- Nova accepted it
09:37:53Z  Ready=Available, Observed    +175s -- the FIRST observe landed here
```

**175 seconds between a successful create and the first observe, against a
logged resync of 180s.** A create does **not** schedule its own observe; the
first `GET` arrives with the next reconcile
([oasgen-provider#648](https://github.com/krateo-platformops/oasgen-provider/issues/648)).

This repo measured the same mechanism in-cluster at **`0.74.0`** against an
nginx stand-in — `t1 - t0 = 153.1s` against the same 180s resync, run
`44364522140`, written up in `docs/configuration.md`. **That measurement could
not have established this on its own:** a mock that answers instantly cannot
distinguish *"the controller waits for the resync"* from *"my mock is fast"*.
The live reading at a different provider version is what makes it a property.

### And the thin-status window, captured from outside

Two readings of the same object, which is what `docs/configuration.md` means
when it says an empty status field is evidence of nothing:

```
+3s    .status 300 B   server: {id: ...}                        reason: Pending
+352s  .status 731 B   addresses, created, flavor.id, image.id,
                       name, status: ACTIVE, tenant_id          reason: Available
```

`populateStatusFields` skips an absent declared field **silently** — no error,
no event, no log — so a thin `.status` on a not-yet-`Ready` CR says nothing
about whether the field will ever arrive. Their harness also diffed the two
readings for **empty `.status.server` fields and found none**, and nothing
present at +3s was gone at +352s.

`tenant_id` is populated rather than `project_id`: that is the 2.1 field name,
which is consistent with this chart sending no microversion header at all.

### What this one artifact does not show

One run, one flavor, one image, one network. It is **not** boot-from-volume —
`imageRef` is set and there is no `block_device_mapping_v2` — so the capability
added in `0.1.10` is still unexercised against a real Nova.

**That gap is tracked rather than merely admitted:**
[krateo-openstack-blueprint#431](https://github.com/krateo-blueprints/krateo-openstack-blueprint/issues/431)
records the exact CR shape to run (no `imageRef`, `boot_index: 0`,
`delete_on_termination: false`), what will be reported (the raw `.status` early
and at `Ready`, whether `Ready` arrives at all, Nova's refusal if there is one,
and that teardown leaves the volume and no server), and closes only on this
repo's confirmed receipt. **It is blocked on
[#308](https://github.com/krateo-blueprints/krateo-openstack-blueprint/issues/308)**
— that install has no Cinder backend until its storage phase lands.

**A pointer rather than a date, deliberately.** "Unexercised as of 2026-11-21"
is a claim a reader cannot check and that rots silently; an open issue with a
blocking dependency is one they can open and see the state of. The same reason
the orphan-reason note above names a version boundary instead of describing the
present moment.

Teardown was
measured in the same run: the CR deleted in 5–11s with **0 same-named servers
left in Nova**, which is the arm no mock can close.
