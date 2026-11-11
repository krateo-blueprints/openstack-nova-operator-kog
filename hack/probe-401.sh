#!/usr/bin/env bash
#
# Can an operator tell "the credential was rejected" from "the vendor is
# unreachable", reading only the CR?
#
# docs/configuration.md asserts they are INDISTINGUISHABLE from the CR's
# conditions, and that assertion is load-bearing: it is the reason the 0.52.0 ->
# 0.53.0 upgrade measurement was blind. Every CR in that run pointed at
# nova.invalid, so a credential-read break and an unreachable vendor produced the
# same conditions -- and 0.53.0 turned out to break apiKey credential reads. The
# caveat was written at the time and never applied to the conclusion.
#
# So this probe does not ask "what does a 401 look like". It asks whether the
# two look DIFFERENT, which is a comparison, and reports CONFIRMED or REFUTED.
#
# RUNS ON AN EXISTING CLUSTER. It creates no cluster and deletes none: it is
# meant to share the cluster built for the oasgen #549 regression arm, on a
# shared 8-CPU machine where a second kind cluster is not free.
#
#   hack/probe-401.sh <kubeconfig> <context> [namespace]
#   hack/probe-401.sh --self-test        # comparator only, no cluster needed
#
# WHY A THIRD ARM EXISTS. "The two are identical" is also what a broken
# comparator says. So a control compares the 401 conditions against a
# deliberately different state and asserts the comparator reports a DIFFERENCE.
# Without it, CONFIRMED would be unreadable.
set -uo pipefail
export LC_ALL=C

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m  OK: %s\033[0m\n' "$*"; }
bad()  { printf '\033[31m  BAD: %s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mFAIL: %s\033[0m\n' "$*" >&2; exit 1; }
void() { printf '\n\033[33mVOID: %s\033[0m\n' "$*" >&2; exit 3; }

# ---------------------------------------------------------------------------
# The comparator, kept separate from any cluster so it can be self-tested.
#
# NORMALISATION IS THE WHOLE DESIGN PROBLEM. The two arms differ by
# construction in their host and their timestamps, so comparing raw strings
# would report "different" every time and prove nothing -- vacuous in the
# other direction. So: strip timestamps and the two hostnames, compare what is
# left, and PRINT what was stripped so a reader can judge the normalisation
# rather than trust it.
normalise_conditions() {
  sed -E \
    -e 's/[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]+Z?/<TIME>/g' \
    -e 's/mock-401[a-z0-9.-]*(:[0-9]+)?/<HOST>/g' \
    -e 's/nova\.invalid[a-z0-9.-]*(:[0-9]+)?/<HOST>/g' \
    -e 's/[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+(:[0-9]+)?/<ADDR>/g' \
    -e 's/[[:space:]]+$//'
}

# compare_arms <fileA> <fileB> -> prints VERDICT=same|different, rc 0
compare_arms() {
  local a="$1" b="$2" na nb
  na=$(normalise_conditions < "$a")
  nb=$(normalise_conditions < "$b")
  if [ -z "$na" ] || [ -z "$nb" ]; then
    # An empty side is not "the same as the other empty side". Two unread CRs
    # compare equal and would print CONFIRMED, which is this repo's recurring
    # defect: the broken-instrument answer and the reassuring answer agree.
    echo "VERDICT=void"
    echo "  one or both arms produced NO conditions, so nothing was compared."
    return 0
  fi
  if [ "$na" = "$nb" ]; then
    echo "VERDICT=same"
  else
    echo "VERDICT=different"
    echo "--- 401 arm (normalised)"; printf '%s\n' "$na" | sed 's/^/    /'
    echo "--- unreachable arm (normalised)"; printf '%s\n' "$nb" | sed 's/^/    /'
  fi
  return 0
}

self_test() {
  local d; d=$(mktemp -d)
  local pass=0 fail=0
  t() { # t <label> <expected> <actual>
    if [ "$2" = "$3" ]; then printf '  %-54s PASS\n' "$1"; pass=$((pass+1))
    else printf '  %-54s FAIL want=%s got=%s\n' "$1" "$2" "$3"; fail=$((fail+1)); fi
  }
  v() { compare_arms "$1" "$2" | sed -n 's/^VERDICT=//p'; }

  printf 'Ready=False reason=Unavailable msg=Get "http://mock-401.ns.svc:8080/v2.1/flavors": 401\n' > "$d/a"
  printf 'Ready=False reason=Unavailable msg=Get "http://nova.invalid/v2.1/flavors": 401\n'        > "$d/b"
  t "same text, host differs only -> same" same "$(v "$d/a" "$d/b")"

  # Regression arm for a real miss: the host patterns first did NOT absorb the
  # port, so :8080 survived normalisation, the arms read as different, and the
  # probe would have published REFUTED -- a false FINDING, not a false pass.
  # Caught here rather than on a cluster.
  printf 'msg=Get "http://mock-401.ns.svc.cluster.local:8080/v2.1/flavors": 401\n' > "$d/a"
  printf 'msg=Get "http://nova.invalid/v2.1/flavors": 401\n'                       > "$d/b"
  t "host with :port vs host without -> same" same "$(v "$d/a" "$d/b")"

  printf 'Ready=False reason=Unavailable msg=at 2026-11-10T12:00:00Z\n' > "$d/a"
  printf 'Ready=False reason=Unavailable msg=at 2026-11-10T13:30:00Z\n' > "$d/b"
  t "timestamps differ only -> same" same "$(v "$d/a" "$d/b")"

  # THE CONTROL: the comparator must be able to say "different", or "same" above
  # proves nothing. This is the arm whose absence would make CONFIRMED unreadable.
  printf 'Ready=False reason=AuthorizationDenied msg=credential rejected\n' > "$d/a"
  printf 'Ready=False reason=Unavailable msg=no such host\n'               > "$d/b"
  t "genuinely different reasons -> different" different "$(v "$d/a" "$d/b")"

  printf 'Ready=False reason=Unavailable msg=x\n' > "$d/a"
  : > "$d/b"
  t "one arm empty -> void, NOT same" void "$(v "$d/a" "$d/b")"
  : > "$d/a"
  t "both arms empty -> void, NOT same" void "$(v "$d/a" "$d/b")"

  # normalisation must not erase the thing under test
  printf 'reason=AuthorizationDenied\n' > "$d/a"
  printf 'reason=Unavailable\n'         > "$d/b"
  t "normalisation leaves the reason intact" different "$(v "$d/a" "$d/b")"

  printf 'msg=connect to 10.96.0.1:8080 failed\n' > "$d/a"
  printf 'msg=connect to 10.96.7.9:9090 failed\n' > "$d/b"
  t "addresses normalised -> same" same "$(v "$d/a" "$d/b")"

  rm -rf "$d"
  echo
  echo "  $pass passed, $fail failed"
  [ "$fail" -eq 0 ] || return 1
  return 0
}

if [ "${1:-}" = "--self-test" ]; then
  say "comparator self-test (no cluster)"
  self_test || fail "the comparator does not behave as specified"
  ok "comparator is sound: it can say same, different, AND void"
  exit 0
fi

# ---------------------------------------------------------------------------
KUBECONFIG_PATH="${1:?usage: probe-401.sh <kubeconfig> <context> [namespace] [reject-status] | --self-test}"
CONTEXT="${2:?}"
NS="${3:-krateo-system}"
# The rejection status the mock returns. 401 by default, because that is the
# credential-rejection code and the original question. Parameterised because
# oasgen-provider reports that rest-dynamic-controller DOES carry
# customcondition.ReasonAuthorizationDenied -- for 403, via
# stopOnAuthorizationDenied, reached by get/findby/create/update -- and that 401
# is excluded ON PURPOSE: a 401 is what a credential being rotated under us
# looks like, and retrying it is how rotation is tolerated (oasgen#313). That is
# a source read of theirs; running this with 403 turns it into a measurement
# here, and the two answers together are the finding.
REJECT_STATUS="${4:-401}"
case "$REJECT_STATUS" in
  [1-5][0-9][0-9]) : ;;
  *) fail "reject-status must be a three-digit HTTP status (got '$REJECT_STATUS')." ;;
esac
# DERIVED, NOT TYPED. This line read `compute.nova.krateo.io` when written --
# wrong; the chart's group is `nova.openstack.krateo.io`, which is what
# hack/probe-361.sh and hack/probe-redirect.sh use and what values.yaml says.
# The consequence had it run: the CRD precondition below would have found
# `computeflavors.compute.nova.krateo.io` absent and VOIDed with "the chart is
# not installed in this cluster" -- a confident, wrong cause, in a probe written
# to avoid exactly that. The comparator self-test could not catch it because it
# never touches GROUP. So read it from the chart instead of restating it.
GROUP=$(awk '/resourceGroup:/ {print $2; exit}' chart/values.yaml 2>/dev/null)
case "$GROUP" in
  *.*) : ;;
  *) fail "could not read resourceGroup from chart/values.yaml (got '${GROUP:-<empty>}').
    Refusing rather than guessing: a wrong group makes every CRD look absent,
    which this script would otherwise report as 'the chart is not installed'." ;;
esac

# NEVER the ambient context: ~/.kube/config on this machine reaches real remote
# GKE clusters, and this probe applies CRs and deletes Deployments.
K() { kubectl --kubeconfig "$KUBECONFIG_PATH" --context "$CONTEXT" "$@"; }

[ -f chart/Chart.yaml ] || fail "run me from the repo root"
# shellcheck source=hack/lib/wait.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/wait.sh"

say "preconditions -- each failure says what it means"
K get --raw /readyz > /dev/null 2>&1 \
  || void "cannot reach the API server (kubeconfig=$KUBECONFIG_PATH context=$CONTEXT). Nothing measured."
for crd in "computeflavors.$GROUP" "computeflavorconfigurations.$GROUP"; do
  K get crd "$crd" > /dev/null 2>&1 \
    || void "CRD $crd is absent: the chart is not installed in this cluster, so no CR can be applied. This is NOT evidence about 401 handling."
done
ok "API reachable and both halves of the pair are served"

# The probe's own objects, so cleanup never touches the shared cluster itself.
PROBE_OBJS=(computeflavor/probe-401 computeflavorconfiguration/probe-401-cfg)
cleanup() {
  say "cleanup -- the probe's objects only; the CLUSTER IS LEFT RUNNING"
  for o in "${PROBE_OBJS[@]}"; do K -n "$NS" delete "$o" --ignore-not-found --timeout=30s >/dev/null 2>&1 || true; done
  K -n "$NS" delete deploy/mock-401 svc/mock-401 cm/mock-401-conf secret/probe-401-token \
      --ignore-not-found --timeout=30s >/dev/null 2>&1 || true
}
trap cleanup EXIT

say "mock-401: refuses every request with $REJECT_STATUS, the way Keystone does"
# QUOTED heredoc: nothing inside is shell-expanded, so nginx's own $variables
# survive intact.
# UNQUOTED heredoc now, so ${REJECT_STATUS} expands -- which means nginx's own
# $variables must be escaped as \$ or the shell eats them. That is the trade and
# it is worth stating: a quoted heredoc protects nginx's syntax, an unquoted one
# lets the status through, and getting it wrong silently produces an nginx config
# with empty log fields rather than an error.
K -n "$NS" apply -f - >/dev/null <<EOF
apiVersion: v1
kind: ConfigMap
metadata: { name: mock-401-conf }
data:
  default.conf: |
    log_format probe '\$remote_addr "\$request" \$status';
    server {
      listen 8080;
      access_log /dev/stdout probe;
      location / {
        default_type application/json;
        return ${REJECT_STATUS} '{"error":{"code":${REJECT_STATUS},"title":"Rejected","message":"The request you have made was rejected."}}';
      }
    }
EOF
K -n "$NS" apply -f - >/dev/null <<EOF
apiVersion: apps/v1
kind: Deployment
metadata: { name: mock-401, namespace: ${NS} }
spec:
  replicas: 1
  selector: { matchLabels: { app: mock-401 } }
  template:
    metadata: { labels: { app: mock-401 } }
    spec:
      containers:
        - name: nginx
          image: nginxinc/nginx-unprivileged:1.27-alpine
          ports: [ { containerPort: 8080 } ]
          volumeMounts: [ { name: conf, mountPath: /etc/nginx/conf.d } ]
          # WITHOUT THIS, a rollout-status wait means "the container started",
          # not "nginx is listening" -- measured 2026-11-11: the positive
          # control got curl code 000 (no connection) immediately after a
          # successful rollout, on a machine at load 49. A 401 is a SUCCESSFUL
          # probe response here, so an httpGet probe would have to be told not
          # to treat it as unready; tcpSocket avoids the question.
          # No backticks in this comment: it sits inside an UNQUOTED heredoc,
          # so backticks would be command substitution run by the shell.
          # shellcheck caught that, at default severity, which my -S warning
          # habit would have missed.
          readinessProbe:
            tcpSocket: { port: 8080 }
            initialDelaySeconds: 1
            periodSeconds: 2
            failureThreshold: 30
      volumes: [ { name: conf, configMap: { name: mock-401-conf } } ]
---
apiVersion: v1
kind: Service
metadata: { name: mock-401, namespace: ${NS} }
spec:
  selector: { app: mock-401 }
  ports: [ { port: 8080, targetPort: 8080 } ]
EOF
K -n "$NS" rollout status deploy/mock-401 --timeout=180s >/dev/null \
  || void "mock-401 never became ready, so nothing was offered a 401."

say "POSITIVE CONTROL: the mock must actually return $REJECT_STATUS"
# Without this, "the CR reports a failure" could mean the mock was misconfigured
# and returned 404, or nothing at all -- and the arm would still look like a 401
# measurement. A mock that cannot refuse proves nothing about refusals.
# THIS PARSE HAS THREE TRAPS AND THE FIRST VERSION HIT ONE. Measured on a live
# cluster 2026-11-11:
#   1. the pod was named `probe-401-curl`, so `tr -dc '0-9'` concatenated the
#      HTTP code with THE DIGITS IN THE POD'S OWN NAME -> "401401", and the
#      control correctly VOIDed on a value it did not understand;
#   2. the obvious repair -- take the LAST 3-digit token -- is a FALSE PASS:
#      against a mock returning 500 the digits were "000401401" and the last
#      token is 401, i.e. exactly the answer being looked for. Verified by
#      standing up a 500 mock on purpose;
#   3. even with --quiet and a digitless name the code can appear TWICE
#      ("500500"), because kubectl both attaches and re-reads the pod log.
# So: digitless pod name, --quiet to drop kubectl's own chatter, and the FIRST
# 3-digit token. And then a SECOND, INDEPENDENT instrument has to agree.
# RETRIED, because 000 and 404 are different failures. curl reports 000 when it
# never got an HTTP response at all -- DNS, or nginx not yet bound -- and that is
# a transient worth waiting out; any real status code is an answer worth judging
# immediately. Collapsing the two would turn a slow cluster into "the mock
# answered wrongly".
_mock_answers() {
  CODE=$(K -n "$NS" run probe-cred-check --rm -i --quiet --restart=Never \
           --image=curlimages/curl:8.11.0 -- \
           -s -o /dev/null -w '%{http_code}' "http://mock-401.${NS}.svc.cluster.local:8080/v2.1/flavors" 2>/dev/null \
         | grep -oE '[0-9]{3}' | head -1)
  [ -n "${CODE:-}" ] && [ "$CODE" != "000" ]
}
wait_for "mock-401 to return any HTTP status to an in-cluster request" 15 6 _mock_answers \
  || void "the mock never returned an HTTP status at all (curl code 000 throughout).
    That is a connectivity failure, NOT the mock answering wrongly, and nothing
    downstream was measured."
if [ "${CODE:-}" != "$REJECT_STATUS" ]; then
  void "the mock answered '${CODE:-<nothing>}', not $REJECT_STATUS. Everything downstream would be a measurement of the wrong thing."
fi
# Independent confirmation from the mock's own access log. Two instruments that
# must agree: curl reports what it received, nginx reports what it served. If
# only one says 401 the mock is not understood, and an unexplained instrument is
# a reason to stop rather than a detail to note.
MOCKLOG=$(K -n "$NS" logs deploy/mock-401 --tail=50 2>/dev/null | grep -c "\" $REJECT_STATUS\$" || true)
if [ "${MOCKLOG:-0}" -lt 1 ]; then
  void "curl reported $REJECT_STATUS but the mock's own access log shows no
    $REJECT_STATUS-served line.
    Two instruments disagree about what just happened, so neither can be trusted
    for the comparison below."
fi
ok "mock returns $REJECT_STATUS to an in-cluster request, confirmed by curl AND by the mock's access log"

say "ONE CR, TWO ENDPOINTS -- the arms are sequential, not parallel"
# WHY SEQUENTIAL, AND THIS IS THE WHOLE CORRECTION. The first version applied
# two Configuration CRs with different names, one per endpoint. Two things were
# wrong with that, and together they produced a confident FALSE FINDING
# ("REFUTED: an operator CAN tell them apart") on 2026-11-11:
#
#   1. `ComputeFlavorConfiguration.spec` has exactly ONE field, `authentication`.
#      There is no per-CR endpoint. The URL comes from the OAS `servers[0]`,
#      rendered from `nova.url` at INSTALL time -- so the distinction being
#      measured cannot be expressed per-CR at all. Both applies failed with
#      `unknown field "spec.server"`, and the script reported success because it
#      discarded the exit status.
#   2. The two arms therefore differed ONLY in their object names, and the
#      comparator duly reported a difference. A comparison whose arms differ by
#      construction always reports a difference, and that difference reads as a
#      finding.
#
# So: ONE CR with ONE name, measured twice, with the endpoint changed by a helm
# upgrade in between. The arms then differ in exactly the variable under test.
CFG=probe-401-cfg
CR=probe-401

K -n "$NS" create secret generic probe-401-token \
  --from-literal=token=dummy-not-a-credential >/dev/null 2>&1 || true

read_conditions() {
  K -n "$NS" get "computeflavor/$CR" -o \
    jsonpath='{range .status.conditions[*]}type={.type} status={.status} reason={.reason} message={.message}
{end}' 2>/dev/null
}
read_uid() { K -n "$NS" get "computeflavor/$CR" -o jsonpath='{.metadata.uid}' 2>/dev/null; }
read_rv()  { K -n "$NS" get "computeflavor/$CR" -o jsonpath='{.metadata.resourceVersion}' 2>/dev/null; }

point_chart_at() { # point_chart_at <url>
  helm --kubeconfig "$KUBECONFIG_PATH" --kube-context "$CONTEXT" upgrade --install nova "$CHART_PATH" \
    -n "$NS" --set nova.url="$1" --wait --timeout 8m > /tmp/probe401-helm.log 2>&1 \
    || { sed 's/^/    /' /tmp/probe401-helm.log >&2
         void "helm upgrade to $1 failed; nothing was measured at that endpoint."; }
}

# The chart carries `version: CHART_VERSION` until release, so helm refuses it
# unstamped. Stage a stamped copy here rather than requiring the caller to have
# done it -- a probe that depends on an undocumented prior step is a probe that
# reports "helm upgrade failed" for a reason the reader cannot see.
CHART_PATH=$(mktemp -d)/chart
mkdir -p "$CHART_PATH"
cp -R chart/. "$CHART_PATH"/
sed "s/CHART_VERSION/0.0.0-probe401/g" chart/Chart.yaml > "$CHART_PATH/Chart.yaml"
grep -q CHART_VERSION "$CHART_PATH/Chart.yaml" \
  && fail "failed to stamp CHART_VERSION; helm would refuse the chart and the
    error would point at helm rather than at this line."
ok "staged a stamped chart at $CHART_PATH"

MOCK_URL="http://mock-401.${NS}.svc.cluster.local:8080/v2.1"
UNREACHABLE_URL="https://nova.invalid/v2.1"

say "ARM A -- point the chart at the rejecting mock ($REJECT_STATUS)"
point_chart_at "$MOCK_URL"
# EXIT STATUS CHECKED. The first version piped this to /dev/null and ignored it,
# then printed "both CRs applied" over two BadRequests.
# Written to a file, then applied as a separate command. `apply -f - <<EOF || {`
# cannot parse: the brace group opens before the heredoc body is consumed, so
# its closing brace never arrives. Separating them also makes the manifest
# inspectable after a failure, which is what a reader wants at that moment.
CR_MANIFEST=$(mktemp)
cat > "$CR_MANIFEST" <<EOF
apiVersion: ${GROUP}/v1alpha1
kind: ComputeFlavorConfiguration
metadata: { name: ${CFG}, namespace: ${NS} }
spec:
  authentication:
    apiKey:
      tokenRef: { name: probe-401-token, namespace: ${NS}, key: token }
      header: X-Auth-Token
---
apiVersion: ${GROUP}/v2-1
kind: ComputeFlavor
metadata: { name: ${CR}, namespace: ${NS} }
spec:
  configurationRef: { name: ${CFG}, namespace: ${NS} }
  flavor: { name: ${CR}, ram: 2048, vcpus: 1, disk: 20 }
EOF
# EXIT STATUS CHECKED. The first version piped this to /dev/null and ignored it,
# then printed "both CRs applied" over two BadRequests.
if ! K -n "$NS" apply -f "$CR_MANIFEST" > /tmp/probe401-apply.log 2>&1; then
  sed 's/^/    /' /tmp/probe401-apply.log >&2
  echo "    --- the manifest it refused ---" >&2
  sed 's/^/    /' "$CR_MANIFEST" >&2
  void "the probe's own CRs did not apply, so no arm was measured. The manifest
    above is wrong, not the cluster -- and an unchecked apply is how the previous
    run reported success over two BadRequests."
fi
ok "applied, and the apply's exit status was read"

_has_conditions() { [ -n "$(read_conditions | tr -d '[:space:]')" ]; }
wait_for "the CR to carry at least one status condition at the rejecting endpoint" 40 6 _has_conditions \
  || void "the CR never produced a condition at the mock endpoint within 240s.
    That is a TIMEOUT, not a finding that the two endpoints look alike."

UID_A=$(read_uid); RV_A=$(read_rv)
A_FILE=$(mktemp); read_conditions | tee "$A_FILE" | sed 's/^/    /'

say "ARM B -- same CR, repoint the chart at an unroutable endpoint"
point_chart_at "$UNREACHABLE_URL"
# The discriminator that the upgrade actually reached this object: its
# resourceVersion must advance past what arm A saw. Without it, arm B could be a
# re-read of arm A's status and the two would agree for the wrong reason.
_rv_advanced() { local rv; rv=$(read_rv); [ -n "$rv" ] && [ "$rv" != "$RV_A" ]; }
wait_for "the CR to be written again after the endpoint change (resourceVersion past $RV_A)" 40 6 _rv_advanced \
  || void "the CR was never rewritten after the helm upgrade, so arm B would be a
    re-read of arm A. Nothing was measured at the second endpoint."

UID_B=$(read_uid)
B_FILE=$(mktemp); read_conditions | tee "$B_FILE" | sed 's/^/    /'

say "CONTROLS"
[ -n "$UID_A" ] && [ "$UID_A" = "$UID_B" ] \
  || void "the object changed identity between arms (uid $UID_A -> $UID_B), so
    this is not one CR measured twice. Nothing comparable was captured."
ok "same object across both arms (uid unchanged)"
CTRL=$(mktemp); printf 'type=Ready status=False reason=DeliberatelyDifferent message=control\n' > "$CTRL"
CTRL_V=$(compare_arms "$A_FILE" "$CTRL" | sed -n 's/^VERDICT=//p')
[ "$CTRL_V" = different ] \
  || void "the comparator reported '$CTRL_V' against a deliberately different control."
ok "comparator can report a difference"

say "VERDICT -- is a rejected credential distinguishable from an unreachable vendor?"
V_OUT=$(compare_arms "$A_FILE" "$B_FILE")
printf '%s\n' "$V_OUT" | sed 's/^/  /'
case "$(printf '%s\n' "$V_OUT" | sed -n 's/^VERDICT=//p')" in
  same)
    bad "CONFIRMED: INDISTINGUISHABLE from the CR once the host is normalised away."
    bad "docs/configuration.md's claim stands, and so does the consequence: any"
    bad "upgrade measured against an unreachable vendor cannot see a"
    bad "credential-read regression. That is how 0.53.0 passed as clean."
    echo "  OUTCOME=indistinguishable" ;;
  different)
    ok "REFUTED: the conditions differ beyond the host string."
    ok "The diff above is the evidence; docs/configuration.md needs updating."
    echo "  OUTCOME=distinguishable" ;;
  *)
    void "one or both arms carried no conditions. Nothing measured." ;;
esac
rm -f "$A_FILE" "$B_FILE" "$CTRL"
