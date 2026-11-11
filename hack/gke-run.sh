#!/usr/bin/env bash
# Create a disposable GKE cluster, run the measurement probes, delete it, and
# VERIFY the deletion.
#
# WHY THIS EXISTS. Diego directed all sessions to stop using local kind
# clusters and use disposable GKE clusters instead, after the shared laptop hit
# load 148. The shape here matches what peers already run rather than being
# invented: 1 x e2-standard-8, 60GB, REGULAR channel, europe-west3-a, in
# operations-dev-krateo-io -- read from keycloak-provider-kog's own cluster.
#
# THE DELETE IS THE DANGEROUS PART, and it is dangerous in two directions.
#
#   A MISSED delete costs real money, every hour, unattended. So the delete is
#   verified by re-listing, and a cluster still present afterwards is a LOUD
#   FAILURE rather than a silent one. "I asked for a delete" and "it is gone"
#   are different claims and only the second is checked.
#
#   An OVER-BROAD delete destroys someone else's work. Four peer clusters live
#   in this project right now. So this script deletes EXACTLY the name it
#   generated, never a filter, never a pattern, and it refuses to delete a name
#   it did not create in this run. That is the same rule as never deleting
#   another session's kind cluster, moved to a place where the blast radius is
#   a cloud project.
#
# NOTHING TOUCHES THE SHARED KUBECONFIG. ~/.kube/config on this machine reaches
# live remote clusters; this writes its own file and passes it explicitly.
#
# DRY RUN IS THE DEFAULT. It prints what it would do and creates nothing, so
# the plan can be read before any spend. Pass --go to actually run.
set -uo pipefail
export LC_ALL=C

PROJECT="${GKE_PROJECT:-operations-dev-krateo-io}"
ZONE="${GKE_ZONE:-europe-west3-a}"
MACHINE="${GKE_MACHINE:-e2-standard-8}"
DISK="${GKE_DISK:-60}"
CHANNEL="${GKE_CHANNEL:-regular}"
NODES=1
OASGEN_VERSION="${GKE_OASGEN_VERSION:-0.60.0}"
TMPOUT=$(mktemp -d)
# A name this run owns. The suffix makes collision with a peer's cluster
# impossible, and the prefix makes the owner obvious in `clusters list`.
RUN_ID="$(date +%H%M%S)-$$"
CLUSTER="nova-kog-${RUN_ID}"
KUBECONFIG_FILE="${TMPDIR:-/tmp}/gke-${CLUSTER}.kubeconfig"
GO=no
[ "${1:-}" = "--go" ] && GO=yes

bold() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
ok()   { printf '\033[32m  OK: %s\033[0m\n' "$1"; }
# shellcheck disable=SC2329  # called from delete_and_verify, which the trap invokes
bad()  { printf '\033[31m  FAIL: %s\033[0m\n' "$1"; }
say()  { printf '  %s\n' "$1"; }
void() { printf '\n\033[33m  VOID: %s\033[0m\n' "$1"; exit 9; }

# Script-scoped: the EXIT trap fires after functions return, and a trap that
# references an out-of-scope name dies under `set -u` -- which here would mean
# the cluster is never deleted. Measured that failure on a Docker container
# earlier today; the stakes are higher now.
CREATED=no

G() { gcloud --project "$PROJECT" "$@"; }
K() { kubectl --kubeconfig "$KUBECONFIG_FILE" --context "$CTX" "$@"; }
H() { helm --kubeconfig "$KUBECONFIG_FILE" --kube-context "$CTX" "$@"; }

# All six RestDefinitions Ready=True. A bounded wait whose TIMEOUT is the
# finding; "not ready yet" is not a measurement of anything.
wait_for_ready() {
  local i n
  for i in $(seq 1 60); do
    n=$(K -n "$NS" get restdefinitions.ogen.krateo.io \
      -o jsonpath='{range .items[*]}{range .status.conditions[?(@.type=="Ready")]}{.status}{"\n"}{end}{end}' 2>/dev/null \
      | grep -c '^True$') || n=0
    [ "${n:-0}" -eq 6 ] && return 0
    sleep 10
  done
  return 1
}

cluster_exists() {
  # Echoes yes/no/unknown. "unknown" matters: a listing that FAILED must never
  # be read as "the cluster is gone".
  local out rc
  out=$(G container clusters list --filter="name=$CLUSTER" --format='value(name)' 2>/dev/null); rc=$?
  if [ "$rc" -ne 0 ]; then echo unknown; return; fi
  if printf '%s\n' "$out" | grep -qx "$CLUSTER"; then echo yes; else echo no; fi
}

# shellcheck disable=SC2329  # invoked by the EXIT trap installed below
delete_and_verify() {
  [ "$CREATED" = yes ] || return 0
  bold "DELETE -- exactly $CLUSTER, by name, never a filter"
  G container clusters delete "$CLUSTER" --zone "$ZONE" --quiet >/dev/null 2>&1
  say "delete returned $?; now VERIFYING rather than trusting it"
  local state i
  for i in $(seq 1 60); do
    state=$(cluster_exists)
    case "$state" in
      no)      ok "verified gone after $((i*10))s"; rm -f "$KUBECONFIG_FILE"; return 0 ;;
      unknown) say "could not list clusters on attempt $i -- retrying; an unreadable list is NOT a deletion" ;;
    esac
    sleep 10
  done
  bad "$CLUSTER IS STILL PRESENT (or unlistable) after 600s."
  say "THIS COSTS MONEY EVERY HOUR. Delete it by hand:"
  say "  gcloud --project $PROJECT container clusters delete $CLUSTER --zone $ZONE"
  return 1
}
trap delete_and_verify EXIT

bold "PLAN${GO:+}"
say "project  $PROJECT"
say "zone     $ZONE"
say "cluster  $CLUSTER   (name owned by THIS run; delete targets this exact string)"
say "nodes    $NODES x $MACHINE, ${DISK}GB, channel $CHANNEL"
say "kubecfg  $KUBECONFIG_FILE   (never ~/.kube/config)"
say "oasgen   $OASGEN_VERSION"
say "probes   probe-549-reapply (log-based, FIRST), probe-590-restart,"
say "         probe-apikey-rbac, check-schema-warnings"
say "est cost ~\$0.40-0.48/hour -- MY ESTIMATE, not measured"

if [ "$GO" != yes ]; then
  bold "DRY RUN -- nothing was created"
  say "Re-run with --go to create the cluster and spend money."
  CREATED=no
  exit 0
fi

bold "PRECONDITIONS"
command -v gcloud >/dev/null 2>&1 || void "gcloud is not available."
G container clusters list --format='value(name)' >/dev/null 2>&1 || \
  void "cannot list clusters in $PROJECT, so a delete could not be verified afterwards. Refusing to create something I cannot confirm I removed."
[ "$(cluster_exists)" = "no" ] || void "a cluster named $CLUSTER already exists or is unlistable. Refusing to proceed: this run must own its name outright."
ok "project reachable, clusters listable, name unused"

bold "CREATE"
if ! G container clusters create "$CLUSTER" --zone "$ZONE" --num-nodes "$NODES" \
      --machine-type "$MACHINE" --disk-size "$DISK" --release-channel "$CHANNEL" \
      --no-enable-basic-auth > /tmp/gke-create.log 2>&1; then
  say "gcloud said: $(tail -2 /tmp/gke-create.log | tr '\n' ' ')"
  # It may have created a partial cluster before failing.
  [ "$(cluster_exists)" = "no" ] || CREATED=yes
  void "cluster creation failed. If anything was left behind the EXIT trap will delete and verify it."
fi
CREATED=yes
ok "created $CLUSTER"
KUBECONFIG="$KUBECONFIG_FILE" G container clusters get-credentials "$CLUSTER" --zone "$ZONE" >/dev/null 2>&1 \
  || void "created the cluster but could not fetch credentials into $KUBECONFIG_FILE."
CTX=$(kubectl --kubeconfig "$KUBECONFIG_FILE" config current-context 2>/dev/null)
[ -n "$CTX" ] || void "no context in $KUBECONFIG_FILE."
ok "credentials in $KUBECONFIG_FILE, context $CTX"

bold "INSTALL -- mirroring smoke.yaml's sequence, not inventing one"
# smoke.yaml proves this order works on a real cluster every PR. Deviating from
# it here would mean debugging an install on a billing cluster.
STAGE=$(mktemp -d)
cp -R ./chart "$STAGE/chart"
sed "s/CHART_VERSION/0.0.0-gke/g" ./chart/Chart.yaml > "$STAGE/chart/Chart.yaml"
if grep -q CHART_VERSION "$STAGE/chart/Chart.yaml"; then
  rm -rf "$STAGE"; void "the chart version stamp did not take, so helm would refuse the chart. Nothing was measured."
fi
ok "staged a stamped chart at $STAGE/chart"

H upgrade --install oasgen-provider-crds oci://ghcr.io/krateo-platformops/charts/oasgen-provider-crds \
  --version "$OASGEN_VERSION" -n "$NS" --create-namespace --wait >/dev/null 2>&1 \
  || void "installing oasgen-provider-crds $OASGEN_VERSION failed."
H upgrade --install oasgen-provider oci://ghcr.io/krateo-platformops/charts/oasgen-provider \
  --version "$OASGEN_VERSION" -n "$NS" --wait --timeout 10m >/dev/null 2>&1 \
  || void "installing oasgen-provider $OASGEN_VERSION failed."
ok "oasgen-provider $OASGEN_VERSION installed"

# REPORT THE RUNNING DIGEST, not the requested tag -- measured today: right
# after an upgrade the Deployment says the new tag while the pod still carries
# the old digest.
if ./hack/assert-running-image.sh "$KUBECONFIG_FILE" "$CTX" "$NS" \
     app.kubernetes.io/name=oasgen-provider \
     "ghcr.io/krateo-platformops/oasgen-provider:$OASGEN_VERSION" > "$TMPOUT/digest.txt" 2>&1; then
  sed -n 's/^  OK: /  /p' "$TMPOUT/digest.txt" | sed 's/^/  /'
else
  say "digest check said:"; sed 's/^/    /' "$TMPOUT/digest.txt"
  void "the running image is not a form of $OASGEN_VERSION, so every measurement below would be attributed to the wrong version."
fi

H upgrade --install nova "$STAGE/chart" -n "$NS" \
  --set nova.url=https://nova.invalid/v2.1 --wait --timeout 5m >/dev/null 2>&1 \
  || void "installing this chart failed."
ok "chart installed against an unroutable endpoint"

if ! wait_for_ready; then
  void "the six RestDefinitions never all reached Ready, so nothing below describes a working install."
fi
ok "all 6 RestDefinitions Ready"

bold "MEASURE -- log-based probe FIRST, per hack/probe-590-restart.sh's own note"
# probe-apikey-rbac and probe-590 both churn the controllers (oasgen #507 and
# #588), and probe-549 counts lines in a controller log. Order is load-bearing.
RESULTS=""
run_probe() {
  local label="$1"; shift
  bold "PROBE: $label"
  if "$@" > "$TMPOUT/$label.txt" 2>&1; then
    tail -4 "$TMPOUT/$label.txt" | sed 's/^/    /'
    RESULTS="$RESULTS\n  $label: rc=0"
  else
    local rc=$?
    tail -8 "$TMPOUT/$label.txt" | sed 's/^/    /'
    RESULTS="$RESULTS\n  $label: rc=$rc"
  fi
}
run_probe 549-reapply   ./hack/probe-549-reapply.sh "$KUBECONFIG_FILE" "$CTX" "$NS" 180
run_probe 590-restart   ./hack/probe-590-restart.sh "$KUBECONFIG_FILE" "$CTX" "$NS" 90
run_probe apikey-rbac   ./hack/probe-apikey-rbac.sh "$KUBECONFIG_FILE" "$CTX" "$NS"
run_probe schemawarn    python3 scripts/check-schema-warnings.py "$KUBECONFIG_FILE" "$CTX" "$NS" "$OASGEN_VERSION"

bold "SUMMARY"
printf '%b\n' "$RESULTS"
say "full output under $TMPOUT"
say "measured on $OASGEN_VERSION, running digest reported above"
rm -rf "$STAGE"
exit 0
