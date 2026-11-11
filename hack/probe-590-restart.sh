#!/usr/bin/env bash
# Which changes roll a generated controller, and which must not?
#
# oasgen-provider#588: a generated controller was restarted on EVERY
# deploy.Deploy, so any secretRef Secret-set change rolled it and its logs went
# with it. #590 fixes that by removing the readiness gate on the restart. The
# intended result is a DISCRIMINATION rather than a reduction:
#
#   an rdc.env change          MUST still roll every controller
#   a secretRef Secret-set change  MUST NOT
#
# The maintainer asked specifically for the first half to be checked, because an
# over-correction that stops rolling on an env change is worse than the bug it
# replaces and would otherwise be invisible: a controller quietly running stale
# configuration looks exactly like one that was correctly left alone.
#
# SO THIS PROBE DOES NOT ASSERT A VERSION'S EXPECTED BEHAVIOUR. It reports what
# the two arms did and classifies the pair, which means the same script is
# meaningful before and after the fix and cannot be made to pass by being run
# against the version it was written for.
#
# ONE CLUSTER, TWO ARMS, RE-BASELINED BETWEEN THEM. Arm A necessarily destroys
# the pod identities arm B would be compared against, so the baseline is taken
# twice. Each arm also asserts ITS OWN CHANGE LANDED, because "nothing rolled"
# is the correct response to "nothing changed" and an arm that fails to change
# anything is vacuous rather than passing.
set -uo pipefail
export LC_ALL=C

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=hack/lib/wait.sh
. "$HERE/lib/wait.sh"

KUBECONFIG_PATH="${1:-}"
CONTEXT="${2:-}"
NS="${3:-krateo-system}"
SETTLE="${4:-90}"

bold() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
ok()   { printf '\033[32m  OK: %s\033[0m\n' "$1"; }
bad()  { printf '\033[31m  FOUND: %s\033[0m\n' "$1"; }
say()  { printf '  %s\n' "$1"; }
void() { printf '\n\033[33m  VOID: %s\033[0m\n' "$1"; exit 9; }

K() { kubectl --kubeconfig "$KUBECONFIG_PATH" --context "$CONTEXT" "$@"; }
H() { helm --kubeconfig "$KUBECONFIG_PATH" --kube-context "$CONTEXT" "$@"; }

# --- pure classifier ---------------------------------------------------------
# (a_rolled, b_rolled) -> verdict. Deliberately version-agnostic.
classify_590() {
  case "$1:$2" in
    yes:no)  echo "discriminating:an rdc.env change rolls the controllers and a secretRef Secret-set change does not -- the #590 intent" ;;
    yes:yes) echo "pre-590:both changes roll the controllers, which is the #588 behaviour -- expected at or below 0.58.0" ;;
    no:no)   echo "overcorrected:an rdc.env change does NOT roll the controllers. They are running stale configuration, and that is indistinguishable from being correctly left alone" ;;
    no:yes)  echo "inverted:a secretRef change rolls the controllers but an rdc.env change does not -- the discrimination is backwards" ;;
    *)       echo "void:one of the arms did not produce a yes/no ($1, $2)" ;;
  esac
}

selftest() {
  local fails=0 got want
  bold "--self-test: every pair, and the suite's own coverage"
  while IFS='|' read -r pair want; do
    [ -n "$pair" ] || continue
    got=$(classify_590 "${pair%%:*}" "${pair##*:}"); got="${got%%:*}"
    if [ "$got" = "$want" ]; then say "classify_590($pair) -> $got  ok"
    else say "classify_590($pair) -> $got, wanted $want  *** WRONG ***"; fails=$((fails+1)); fi
  done <<'ROWS'
yes:no|discriminating
yes:yes|pre-590
no:no|overcorrected
no:yes|inverted
yes:maybe|void
unknown:no|void
ROWS
  # The over-correction must NOT be reported as a pass under any pairing, since
  # it is the thing the maintainer most wants caught.
  if [[ "$(classify_590 no no)"  == overcorrected:* ]] && \
     [[ "$(classify_590 no yes)" == inverted:* ]]; then
    say "both no-roll-on-env pairings are findings, not passes  ok"
  else
    say "*** SUITE DEFECT: a missing env roll can read as acceptable ***"; fails=$((fails+1))
  fi
  # And the suite must be able to produce the benign verdict too, or it is
  # simply a function that always complains.
  [[ "$(classify_590 yes no)" == discriminating:* ]] || { say "*** SUITE DEFECT: the intended behaviour is unreachable ***"; fails=$((fails+1)); }
  if [ "$fails" -ne 0 ]; then printf '\n  --- FAIL: %s arm(s) ---\n' "$fails"; return 1; fi
  printf '\n  --- all arms pass ---\n'; return 0
}

if [ "${1:-}" = "--self-test" ]; then selftest; exit $?; fi

[ -n "$KUBECONFIG_PATH" ] && [ -n "$CONTEXT" ] || {
  echo "usage: probe-590-restart.sh <kubeconfig> <context> [namespace] [settle-seconds]" >&2
  echo "       probe-590-restart.sh --self-test" >&2
  echo "The context is REQUIRED and never defaulted: the ambient kubeconfig on a" >&2
  echo "shared machine reaches real remote clusters." >&2
  exit 2
}

# A fingerprint of every generated controller's pod identity. If any of these
# change, that controller rolled.
_fingerprint() {
  K -n "$NS" get pod -o jsonpath='{range .items[*]}{.metadata.name}{"="}{.metadata.uid}{"\n"}{end}' 2>/dev/null \
    | grep -E '^nova-.*-controller-' | sort
}
_controllers_ready() {
  local want got
  want=$(K -n "$NS" get deploy -o name 2>/dev/null | grep -c 'controller$') || return 1
  [ "${want:-0}" -gt 0 ] || return 1
  got=$(K -n "$NS" get deploy -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.readyReplicas}{"\n"}{end}' 2>/dev/null \
    | awk '/controller /{ if ($2+0 >= 1) n++ } END{print n+0}')
  [ "$got" = "$want" ]
}

bold "preconditions"
K version --request-timeout=10s >/dev/null 2>&1 || void "the cluster is not reachable with that kubeconfig/context."
H status oasgen-provider -n "$NS" >/dev/null 2>&1 || void "no oasgen-provider helm release in $NS, so rdc.env cannot be changed through it."
RUNDIG=$(K -n "$NS" get pod -l app.kubernetes.io/name=oasgen-provider -o jsonpath='{.items[0].status.containerStatuses[0].imageID}' 2>/dev/null)
[ -n "$RUNDIG" ] || void "could not read the running oasgen-provider digest, so the version under test is unknown."
say "running digest: $RUNDIG"
_controllers_ready || void "not every generated controller has a ready replica at baseline. A roll cannot be distinguished from a pod that was already being replaced."
BASE_A=$(_fingerprint)
N_CTRL=$(printf '%s\n' "$BASE_A" | grep -c . )
[ "${N_CTRL:-0}" -gt 0 ] || void "zero generated controller pods found, so there is nothing whose roll could be observed."
ok "baseline: $N_CTRL controller pod(s), all ready"

bold "ARM A -- an rdc.env change MUST roll every controller"
STAMP="probe590-$(date +%s)"
ENV_BEFORE=$(K -n "$NS" get deploy -o jsonpath='{range .items[*]}{.spec.template.spec.containers[0].env}{"\n"}{end}' 2>/dev/null | grep -c 'PROBE_590_STAMP' || true)
if ! H upgrade oasgen-provider oci://ghcr.io/krateo-platformops/charts/oasgen-provider \
      --reuse-values --set-json "rdc.env=[{\"name\":\"PROBE_590_STAMP\",\"value\":\"$STAMP\"}]" \
      -n "$NS" --wait >/dev/null 2>&1; then
  void "the helm upgrade setting rdc.env failed, so arm A never changed anything. A setup failure, not a measurement."
fi
# ASSERT THE CHANGE LANDED. "Nothing rolled" is the correct answer to "nothing
# changed", so an arm that did not change anything is vacuous, not passing.
if ! wait_for "the PROBE_590_STAMP env to appear on a controller" 40 3 \
     bash -c "kubectl --kubeconfig '$KUBECONFIG_PATH' --context '$CONTEXT' -n '$NS' get deploy -o jsonpath='{range .items[*]}{.spec.template.spec.containers[0].env}{\"\\n\"}{end}' 2>/dev/null | grep -q '$STAMP'"; then
  void "rdc.env was set but PROBE_590_STAMP never reached a controller Deployment's spec (it was absent $ENV_BEFORE times before). The arm changed nothing observable, so whether it rolls is untestable here."
fi
ok "the env change reached the controller Deployments"
sleep "$SETTLE"
AFTER_A=$(_fingerprint)
if [ "$AFTER_A" != "$BASE_A" ]; then A_ROLLED=yes; ok "controllers rolled on the env change"
else A_ROLLED=no; bad "controllers did NOT roll on the env change"; fi

bold "ARM B -- a secretRef Secret-set change MUST NOT roll them"
_controllers_ready || void "controllers are not all ready after arm A, so arm B's baseline is unusable."
BASE_B=$(_fingerprint)
SEC=probe590-token
CFG=probe590-cfg
cleanup() {
  K -n "$NS" delete secret "$SEC" --ignore-not-found >/dev/null 2>&1 || true
  for row in "${CFG_KINDS[@]:-}"; do
    [ -n "$row" ] || continue
    K -n "$NS" delete "${row%%$'\t'*}" "$CFG" --ignore-not-found >/dev/null 2>&1 || true
  done
  H upgrade oasgen-provider oci://ghcr.io/krateo-platformops/charts/oasgen-provider \
    --reuse-values --set-json 'rdc.env=[]' -n "$NS" --wait >/dev/null 2>&1 || true
}
mapfile -t CFG_KINDS < <(K get crd -o jsonpath='{range .items[*]}{.spec.names.kind}{"\t"}{.spec.group}{"\t"}{.spec.versions[0].name}{"\n"}{end}' 2>/dev/null | grep -E '^[A-Za-z]+Configuration\t')
trap cleanup EXIT
[ "${#CFG_KINDS[@]}" -gt 0 ] || void "no <Kind>Configuration CRD is served, so no secretRef set can be changed."
K -n "$NS" create secret generic "$SEC" --from-literal=token=placeholder-not-a-credential >/dev/null 2>&1 || true
K -n "$NS" get secret "$SEC" >/dev/null 2>&1 || void "could not create arm B's Secret, so its change never landed."
row="${CFG_KINDS[0]}"; ck="${row%%$'\t'*}"; rest="${row#*$'\t'}"; cg="${rest%%$'\t'*}"; cv="${rest##*$'\t'}"
MAN=$(mktemp)
cat > "$MAN" <<EOF
apiVersion: $cg/$cv
kind: $ck
metadata: { name: $CFG, namespace: $NS }
spec:
  authentication:
    apiKey:
      tokenRef: { name: $SEC, namespace: $NS, key: token }
      header: X-Auth-Token
EOF
if ! K apply -f "$MAN" >/dev/null 2>&1; then rm -f "$MAN"; void "could not apply arm B's $ck, so its secretRef change never landed."; fi
rm -f "$MAN"
ok "arm B's change landed: $ck/$CFG references Secret/$SEC"
sleep "$SETTLE"
AFTER_B=$(_fingerprint)
if [ "$AFTER_B" != "$BASE_B" ]; then B_ROLLED=yes; say "controllers rolled on the secretRef change"
else B_ROLLED=no; say "controllers did not roll on the secretRef change"; fi

bold "VERDICT"
RESULT=$(classify_590 "$A_ROLLED" "$B_ROLLED")
WHY="${RESULT#*:}"
say "arm A (rdc.env) rolled: $A_ROLLED    arm B (secretRef) rolled: $B_ROLLED"
case "${RESULT%%:*}" in
  discriminating) ok "$WHY"; say "Measured on $RUNDIG."; exit 0 ;;
  pre-590)        ok "$WHY"; say "Measured on $RUNDIG. Not a defect below 0.59.0."; exit 0 ;;
  overcorrected)  bad "$WHY"; say "Measured on $RUNDIG. This is the over-correction the maintainer asked to have caught."; exit 1 ;;
  inverted)       bad "$WHY"; say "Measured on $RUNDIG."; exit 1 ;;
  void)           void "$WHY" ;;
esac
