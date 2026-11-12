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
# NS was REFERENCED AND NEVER ASSIGNED, and `set -u` killed the run after the
# cluster was already created and billing. The dry run could not catch it
# because the dry run exits BEFORE the install block -- so "dry run clean"
# described the plan and none of the working code.
NS="${GKE_NS:-krateo-system}"
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

DRY=no
G() { if [ "$DRY" = yes ]; then echo "[dry] gcloud $*"; return 0; fi
      gcloud --project "$PROJECT" "$@"; }
K() { if [ "$DRY" = yes ]; then echo "[dry] kubectl $*"; return 0; fi
      kubectl --kubeconfig "$KUBECONFIG_FILE" --context "$CTX" "$@"; }
H() { if [ "$DRY" = yes ]; then echo "[dry] helm $*"; return 0; fi
      helm --kubeconfig "$KUBECONFIG_FILE" --kube-context "$CTX" "$@"; }

# All six RestDefinitions Ready=True. A bounded wait whose TIMEOUT is the
# finding; "not ready yet" is not a measurement of anything.
run_probe() {
  local label="$1"; shift
  if [ "$DRY" = yes ]; then say "[dry] would run: $label ($1 ...)"; RESULTS="$RESULTS\n  $label: [dry]"; return 0; fi
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
  if [ "$DRY" = yes ]; then return 0; fi
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
# THE PLAN LISTS WHAT THE SCRIPT WILL ACTUALLY RUN, derived from the script's
# own run_probe lines rather than restated beside them. A hand-maintained list
# drifts the moment a probe is added -- it did: two arms were wired in while
# this block still announced four, so the plan a reader approves would have
# described a different run from the one that happens. "The dry run described
# the PLAN, not the script" already cost a cluster in this repo once.
_PLAN_PROBES=$(grep -oE '^run_probe +[A-Za-z0-9._-]+' "${BASH_SOURCE[0]}" | awk '{print $2}' | tr '\n' ' ')
say "probes   ${_PLAN_PROBES% }"
say "         (derived from this script's own run_probe lines, not restated)"
[ -n "${_PLAN_PROBES// /}" ] || void "could not derive the probe list from this script, so the
    PLAN below would understate or overstate what runs. Refusing to print a plan
    I cannot source from the script itself."
say "est cost ~\$0.40-0.48/hour -- MY ESTIMATE, not measured"

if [ "$GO" != yes ]; then
  # THE DRY RUN NOW TRAVERSES EVERY LINE. It used to `exit 0` here, so it
  # exercised the plan and none of the working code -- and an `NS` that was
  # referenced but never assigned killed a real run AFTER the cluster was
  # created and billing. "Dry run clean" described the plan, which is a scope
  # narrower than what the words imply.
  #
  # So instead of returning early, the externals become echoes and the rest of
  # the script runs under `set -u`. An unbound variable, a typo in a command
  # name, or a function used before definition now fails here, free.
  bold "DRY RUN -- externals stubbed, every line still executed"
  DRY=yes
fi

# EVERY FUNCTION THIS SCRIPT DEPENDS ON MUST EXIST, checked by NAME rather than
# by calling it. Found by mutation-testing the dry run: short-circuiting a
# branch in dry mode means the call inside it is never evaluated, so a typo'd
# function name passed cleanly -- `wait_for_redy` gave rc=0. The paths the dry
# run skips are precisely the paths it cannot check, so the names are asserted
# separately. `declare -F` proves the name resolves without running the body,
# which matters because wait_for_ready would otherwise sleep for ten minutes
# against stubs.
for _fn in cluster_exists delete_and_verify wait_for_ready run_probe G K H; do
  declare -F "$_fn" >/dev/null || void "the function '$_fn' is not declared, so a call to it would fail at runtime. This is a typo or a deleted definition, not a cluster problem."
done
ok "all 7 required functions are declared"

bold "PRECONDITIONS"
command -v gcloud >/dev/null 2>&1 || void "gcloud is not available."
G container clusters list --format='value(name)' >/dev/null 2>&1 || \
  void "cannot list clusters in $PROJECT, so a delete could not be verified afterwards. Refusing to create something I cannot confirm I removed."
if [ "$DRY" = yes ]; then say "[dry] skipping the name-collision check: cluster_exists cannot be evaluated without a project"
elif [ "$(cluster_exists)" != "no" ]; then void "a cluster named $CLUSTER already exists or is unlistable. Refusing to proceed: this run must own its name outright."
fi
ok "project reachable, clusters listable, name unused"

# THE IMAGE MUST EXIST BEFORE A CLUSTER IS BOUGHT, and this check exists because
# the alternative was measured today: oasgen-provider 0.61.0 was TAGGED and its
# GitHub release published at 08:23:38Z while
# `docker manifest inspect ghcr.io/.../oasgen-provider:0.61.0` answered
# "manifest unknown" -- the image build had not finished. Without this gate the
# run would create a billing cluster, helm-install with `--wait --timeout 10m`,
# sit through an ImagePullBackOff for ten minutes, VOID, and delete. Ten minutes
# of cluster time to learn something one registry query answers in a second.
#
# A TAG IS NOT AN IMAGE, and the trigger for a measurement run is the IMAGE.
#
# WITH A POSITIVE CONTROL, because "this version is missing" and "the registry
# is unreachable" are the same silence otherwise -- and the second would make
# every version look missing, including ones that are fine. The control is a
# version this repo has already measured against, so a failure of the control
# is a network or auth problem rather than a statement about OASGEN_VERSION.
IMG=ghcr.io/krateo-platformops/oasgen-provider
# THE CONTROL MUST NOT BE THE SUBJECT. If it is, "the control answered too" is
# the same query twice and proves nothing about reachability -- the control's
# whole job is to fail differently from the subject. So it is the documented
# chart pin, swapped when the subject happens to be that pin.
IMG_CONTROL=0.52.0
[ "$OASGEN_VERSION" = "$IMG_CONTROL" ] && IMG_CONTROL=0.60.0
# THE DRY RUN DOES RUN THESE, deliberately, and my first version skipped them.
# They are READ-ONLY registry queries: nothing is created, nothing is billed,
# nothing on the shared project is touched. Skipping them made the gate
# unverifiable without `--go`, and `--go` cannot be used to test it, because a
# gate that WRONGLY passes proceeds to `clusters create` -- and killing the
# local process does not cancel an in-flight create, nor fire the EXIT trap
# that would delete it. So the only safe way to exercise this gate was to make
# the safe mode exercise it.
#
# `--dry-run GKE_OASGEN_VERSION=<absent version>` now VOIDs, which is the test.
if true; then
  command -v docker >/dev/null 2>&1 || void "docker is not available, and the provider image cannot be verified without it. Refusing to create a cluster for an image that may not exist."
  if ! docker manifest inspect "$IMG:$IMG_CONTROL" >/dev/null 2>&1; then
    void "the registry did not answer for the CONTROL version $IMG_CONTROL either, so this is
    a registry, network or auth failure rather than a statement about
    $OASGEN_VERSION. Nothing was created. Refusing to guess which."
  fi
  if ! docker manifest inspect "$IMG:$OASGEN_VERSION" >/dev/null 2>&1; then
    void "oasgen-provider $OASGEN_VERSION has NO image in the registry, although the control
    version $IMG_CONTROL does -- so the registry is reachable and this version is
    genuinely absent. A git tag and a GitHub release are not an image: the build
    may still be running. Nothing was created and nothing was billed. Re-run when
    \`docker manifest inspect $IMG:$OASGEN_VERSION\` succeeds."
  fi
  ok "image $IMG:$OASGEN_VERSION exists in the registry (independent control $IMG_CONTROL answered too)"
fi

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
if [ "$DRY" = yes ]; then CTX="dry-run-context"
else
  CTX=$(kubectl --kubeconfig "$KUBECONFIG_FILE" config current-context 2>/dev/null)
  [ -n "$CTX" ] || void "no context in $KUBECONFIG_FILE."
fi
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
if [ "$DRY" = yes ]; then say "[dry] skipping the running-digest assertion"
elif ./hack/assert-running-image.sh "$KUBECONFIG_FILE" "$CTX" "$NS" \
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

if [ "$DRY" = yes ]; then say "[dry] skipping the Ready wait"
elif ! wait_for_ready; then
  void "the six RestDefinitions never all reached Ready, so nothing below describes a working install."
fi
ok "all 6 RestDefinitions Ready"

bold "MEASURE -- log-based probe FIRST, per hack/probe-590-restart.sh's own note"
# probe-apikey-rbac and probe-590 both churn the controllers (oasgen #507 and
# #588), and probe-549 counts lines in a controller log. Order is load-bearing.
RESULTS=""
run_probe 549-reapply   ./hack/probe-549-reapply.sh "$KUBECONFIG_FILE" "$CTX" "$NS" 180
run_probe 590-restart   ./hack/probe-590-restart.sh "$KUBECONFIG_FILE" "$CTX" "$NS" 90
run_probe apikey-rbac   ./hack/probe-apikey-rbac.sh "$KUBECONFIG_FILE" "$CTX" "$NS"
run_probe schemawarn    python3 scripts/check-schema-warnings.py "$KUBECONFIG_FILE" "$CTX" "$NS" "$OASGEN_VERSION"

# THE TWO BANKED ARMS, AND THEY GO LAST ON PURPOSE. Both re-point the chart at
# their own in-cluster mock with `helm upgrade --set nova.url=...`, which
# re-renders the OAS ConfigMaps and makes every RestDefinition regenerate. The
# four probes above read state that regeneration would disturb -- schemawarn in
# particular reads `.status.schemaWarnings`, which is written at generation
# time. Running these first would make the earlier readings a measurement of
# whatever the last re-point left behind.
#
# WITHOUT THIS WIRING THE RUN WOULD HAVE REPORTED SUCCESS HAVING NEVER
# EXERCISED THEM: a summary listing four green probes, while the two arms the
# cluster was bought for sat unrun on disk. Found by reading the runner's probe
# list rather than assuming the arms were in it.
run_probe portonly      ./hack/probe-portonly.sh "$KUBECONFIG_FILE" "$CTX" "$NS"
run_probe delete-548    ./hack/probe-548-delete.sh "$KUBECONFIG_FILE" "$CTX" "$NS"

bold "SUMMARY"
printf '%b\n' "$RESULTS"
say "full output under $TMPOUT"
say "measured on $OASGEN_VERSION, running digest reported above"
rm -rf "$STAGE"
exit 0
