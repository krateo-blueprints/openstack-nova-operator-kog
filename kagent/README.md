# kagent Agent: `nova-kog-expert`

A [kagent](https://kagent.dev) Agent CRD that turns an LLM into a
subject-matter expert on this blueprint:

- The single-layer Krateo KOG packaging (oasgen-provider +
  rest-dynamic-controller) that generates five `<Kind>` /
  `<Kind>Configuration` CRD pairs — `Instance`, `ComputeFlavor`,
  `ComputeKeypair`, `ComputeServerGroup`, `ComputeAggregate` — from the
  Nova OAS subsets in `chart/assets/`, plus a disabled `ComputeQuota`.
- Native **apiKey** authentication: the OAS declares
  `apiKey / in: header / name: X-Auth-Token`, oasgen-provider (pin 0.52.0; 0.26.1 is a hard floor)
  turns it into `spec.authentication.apiKey.{tokenRef,header}`, and
  rest-dynamic-controller sets the header itself. There is no proxy in
  the request path.
- Why **token rotation belongs to a secret-manager** (External Secrets
  Operator, Vault Agent) and not to this chart.
- The bring-up bugs hit against OVH Public Cloud — the narrative is in
  [`docs/medium-article.md`](../docs/medium-article.md) (which now
  carries a superseded banner for its auth-bridge material), and the
  current design is in
  [`docs/architecture.md`](../docs/architecture.md).

The system prompt embeds those bugs (unresolvable endpoint URL, the
Kind-vs-envelope-property crdgen collision behind both `Server`→`Instance`
and the `Compute*` prefix, the fictional `BearerAuth` sample, OVH
`openrc` `/v3` + domain quirks, root-vs-envelope identifier extraction),
a triage recipe for stuck CRs, and the upstream **org hyphen trap**:
`krateo-platformops` is live, the unhyphenated `krateoplatformops` is
abandoned but still resolves.

## Prerequisites

### 1. Install kagent

Pulled directly from the GHCR OCI registry — no `helm repo add` needed.
Verified working on kagent v0.9.6.

```bash
kubectl create ns kagent --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install kagent-crds \
  oci://ghcr.io/kagent-dev/kagent/helm/kagent-crds \
  --version 0.9.6 --namespace kagent --wait --timeout 5m

helm upgrade --install kagent \
  oci://ghcr.io/kagent-dev/kagent/helm/kagent \
  --version 0.9.6 --namespace kagent \
  --set providers.default=anthropic \
  --set providers.anthropic.model=claude-sonnet-4-6 \
  --timeout 10m
```

Swap `providers.default` for `openAI`, `gemini`, `azureOpenAI`, or
`ollama` if you want a non-Anthropic auto-`ModelConfig`. The chart
auto-creates a `ModelConfig` named `default-model-config` matching
whichever provider you pick.

### 2. ModelConfig: Gemini on Vertex AI (what this repo ships)

The Agent here references a `ModelConfig` named **`vertex-gemini`** of
kind `GeminiVertexAI` (see
[`modelconfig-vertex-gemini.yaml`](./modelconfig-vertex-gemini.yaml)).
We use Vertex rather than the chart's auto-created provider so the
agent runs on GCP-managed Gemini with service-account auth.

> **Prefer Workload Identity to a downloaded key.** `roles/aiplatform.user`
> is enough to spend money on Vertex AI, and a downloaded JSON key is a
> long-lived bearer credential with no expiry and no device binding: anyone
> who obtains the file can bill your project until you notice. On GKE, bind
> the Kubernetes service account to a Google one with Workload Identity
> Federation and skip the key entirely — kagent needs
> `GOOGLE_APPLICATION_CREDENTIALS` only when there is a key file to point at,
> and ADC resolves the federated identity without one. The downloaded-key
> path below still works and is documented because not every cluster is on
> GKE, but it is the fallback, not the default.

**One-time GCP prep:**

1. Enable the Vertex AI API on your GCP project.
2. Create a service account (e.g. `kagent-vertex`) with role
   **`roles/aiplatform.user`** (displayed as "Agent Platform User" in
   the current GCP UI — same role ID, recent rebrand). Grant nothing
   broader: this role cannot read your buckets or your other projects,
   and that containment is the only thing limiting a leaked key's blast
   radius to one API's bill.
3. *(Recommended)* Set up Workload Identity Federation for the `kagent`
   namespace's service account and **stop here** — steps 4–5 and the
   secret below are only for clusters that cannot use it.
4. Otherwise, download a JSON key for the service account.
5. Note where it landed. You are going to delete it in a moment.

**Create the secret** kagent will mount as `/creds/key.json`:

```bash
kubectl -n kagent create secret generic kagent-vertex \
  --from-file=key.json=$HOME/Downloads/<your-sa-key>.json

# Then remove the local copy -- the cluster has it now, and the file in
# ~/Downloads is the copy that gets backed up, synced, shared or committed.
# shred is GNU, rm -P is BSD/macOS; the last fallback just guarantees removal.
shred -u $HOME/Downloads/<your-sa-key>.json 2>/dev/null \
  || rm -P $HOME/Downloads/<your-sa-key>.json 2>/dev/null \
  || rm -f  $HOME/Downloads/<your-sa-key>.json
```

**Key hygiene, briefly, because this is the credential people lose:**

- **Never commit it.** It is a JSON blob with a `private_key` field; it does
  not look like a password and it is easy to paste into a values file, a
  Dockerfile, a bug report or a chat. This repo's `.gitignore` now refuses the
  common names (`key.json`, `*service-account*.json`, `*-sa.json`,
  `*credentials*.json`, `*.pem`), which is a seatbelt and not a boundary — it
  cannot know a filename you invent, and it does not apply to the paste.
- **Delete the download** once the Secret exists, as above. A key you cannot
  find is a key you cannot rotate.
- **Rotate on any doubt.** `gcloud iam service-accounts keys list
  --iam-account=<sa>` shows every key and its creation date; a key you do
  not recognise is an incident, and a key older than you remember creating
  is worth replacing on principle.
- **Delete the service account** when you tear the demo down. An unused SA
  with a live key is the one nobody is watching.

**Edit and apply the `ModelConfig`**, setting `projectID` and `location`
to your GCP project + region:

```bash
$EDITOR kagent/modelconfig-vertex-gemini.yaml   # set projectID and location
kubectl apply -f kagent/modelconfig-vertex-gemini.yaml
```

The kagent controller's translator auto-injects
`GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`,
`GOOGLE_GENAI_USE_VERTEXAI=true`, and
`GOOGLE_APPLICATION_CREDENTIALS=/creds/key.json` into the agent pod, and
mounts the `kagent-vertex` secret as a volume at `/creds/`. No further
auth wiring required.

#### Alternative providers

| `spec.provider`     | secret name        | secret key            | notes                          |
|---------------------|--------------------|-----------------------|--------------------------------|
| `Anthropic`         | `kagent-anthropic` | `ANTHROPIC_API_KEY`   | direct Anthropic API           |
| `OpenAI`            | `kagent-openai`    | `OPENAI_API_KEY`      |                                |
| `Gemini`            | `kagent-gemini`    | `GOOGLE_API_KEY`      | AI Studio (not Vertex)         |
| `GeminiVertexAI`    | `kagent-vertex`    | `key.json` (file)     | this repo                      |
| `AnthropicVertexAI` | same shape         | `key.json` (file)     | Claude via Vertex Model Garden |

Edit `spec.declarative.modelConfig` in
[`agent-nova-expert.yaml`](./agent-nova-expert.yaml) if you swap
providers.

### 3. Built-in `kagent-tool-server`

Shipped automatically by the kagent helm chart as a `RemoteMCPServer`.
Provides the `k8s_*` tool family the Agent uses. No extra setup.

## Apply

```bash
kubectl apply -f kagent/agent-nova-expert.yaml
kubectl -n kagent get agent nova-kog-expert
```

Then open the kagent UI (or the A2A endpoint) and ask one of the example
prompts under `spec.declarative.a2aConfig.skills`:

- "How does an `Instance` CR become a real OpenStack VM?"
- "Why does my OVH openrc 401 on Keystone?"
- "My CRD failed to install with an empty-type `spec` error — what happened?"
- "Instance is `Ready=False` with no status — what now?"

## What the agent can do

- Read any of the five `<Kind>` / `<Kind>Configuration` CRs, the
  generated CRDs and the `RestDefinition`s, and explain why each piece
  exists.
- Tail `rest-dynamic-controller` pod logs when diagnosing reconciliation
  hangs.
- Patch narrow status fields (e.g. `status.server.id` to recover a VM
  orphaned by Bug #5) — with confirmation.
- Cite `docs/architecture.md`, `docs/configuration.md`, `docs/e2e.md`
  and the chart templates verbatim where relevant — and treat the
  articles as history, not current design.
- State plainly that there is **no GPU/accelerator support**:
  `ComputeFlavor` exposes no `extra_specs` and `ComputeAggregate` no
  metadata, which are the two places Nova gates accelerators.

## What the agent will not do

- Curl Nova or Keystone directly, or mint/rotate tokens — that is the
  secret-manager's job, or `scripts/get-token.sh` on the user's laptop
  with their OpenStack
  credentials.
- Recommend `kubectl delete instance <name>` when `status.server.id` is
  empty (would orphan the VM). It walks you through patching the id
  first.
- Mock or invent Nova state — it reads it from the live `Instance` CR
  via `k8s_get_resource_yaml`.
