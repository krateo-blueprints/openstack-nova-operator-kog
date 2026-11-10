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
KUBECONFIG_PATH="${1:?usage: probe-401.sh <kubeconfig> <context> [namespace] | --self-test}"
CONTEXT="${2:?}"
NS="${3:-krateo-system}"
GROUP=compute.nova.krateo.io

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
PROBE_OBJS=(computeflavor/probe-401-rejected computeflavor/probe-401-unreachable
            computeflavorconfiguration/probe-401-cfg-rejected
            computeflavorconfiguration/probe-401-cfg-unreachable)
cleanup() {
  say "cleanup -- the probe's objects only; the CLUSTER IS LEFT RUNNING"
  for o in "${PROBE_OBJS[@]}"; do K -n "$NS" delete "$o" --ignore-not-found --timeout=30s >/dev/null 2>&1 || true; done
  K -n "$NS" delete deploy/mock-401 svc/mock-401 cm/mock-401-conf secret/probe-401-token \
      --ignore-not-found --timeout=30s >/dev/null 2>&1 || true
}
trap cleanup EXIT

say "mock-401: refuses every request the way Keystone does"
# QUOTED heredoc: nothing inside is shell-expanded, so nginx's own $variables
# survive intact.
K -n "$NS" apply -f - >/dev/null <<'EOF'
apiVersion: v1
kind: ConfigMap
metadata: { name: mock-401-conf }
data:
  default.conf: |
    log_format probe '$remote_addr "$request" $status';
    server {
      listen 8080;
      access_log /dev/stdout probe;
      location / {
        default_type application/json;
        return 401 '{"error":{"code":401,"title":"Unauthorized","message":"The request you have made requires authentication."}}';
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

say "POSITIVE CONTROL: the mock must actually return 401"
# Without this, "the CR reports a failure" could mean the mock was misconfigured
# and returned 404, or nothing at all -- and the arm would still look like a 401
# measurement. A mock that cannot refuse proves nothing about refusals.
CODE=$(K -n "$NS" run probe-401-curl --rm -i --restart=Never --image=curlimages/curl:8.11.0 -- \
         -s -o /dev/null -w '%{http_code}' "http://mock-401.${NS}.svc.cluster.local:8080/v2.1/flavors" 2>/dev/null \
       | tr -dc '0-9')
if [ "${CODE:-}" != "401" ]; then
  void "the mock answered '${CODE:-<nothing>}', not 401. Everything downstream would be a measurement of the wrong thing."
fi
ok "mock returns 401 to an in-cluster request"

say "the two arms"
# The token is a DUMMY and that is correct here, not a shortcut: the mock
# refuses every request whatever it is sent, so the arm measures how a refusal
# is reported, not whether a real credential works. Nothing credential-shaped
# belongs in a probe that prints what it reads.
K -n "$NS" create secret generic probe-401-token \
  --from-literal=token=dummy-not-a-credential >/dev/null 2>&1 || true

apply_arm() { # apply_arm <name> <baseurl>
  K -n "$NS" apply -f - >/dev/null <<EOF
apiVersion: ${GROUP}/v1alpha1
kind: ComputeFlavorConfiguration
metadata: { name: probe-401-cfg-$1, namespace: ${NS} }
spec:
  authentication:
    apiKey:
      tokenRef: { name: probe-401-token, namespace: ${NS}, key: token }
      header: X-Auth-Token
  server: { url: "$2" }
---
apiVersion: ${GROUP}/v2-1
kind: ComputeFlavor
metadata: { name: probe-401-$1, namespace: ${NS} }
spec:
  configurationRef: { name: probe-401-cfg-$1, namespace: ${NS} }
  flavor: { name: probe-401-$1, ram: 2048, vcpus: 1, disk: 20 }
EOF
}
read_conditions() { # read_conditions <crname> -> type=.. status=.. reason=.. message=..
  K -n "$NS" get "computeflavor/probe-401-$1" -o \
    jsonpath='{range .status.conditions[*]}type={.type} status={.status} reason={.reason} message={.message}
{end}'
}

apply_arm rejected    "http://mock-401.${NS}.svc.cluster.local:8080/v2.1"
apply_arm unreachable "http://nova.invalid/v2.1"
ok "both CRs applied; the controller needs time to reconcile and retry"

# Wait for BOTH to carry at least one condition. A CR with no conditions yet is
# not "no difference" -- compare_arms returns void for that, but waiting first
# turns a timing artifact into a real reading.
_both_have_conditions() {
  local a b
  a=$(read_conditions rejected    | tr -d '[:space:]')
  b=$(read_conditions unreachable | tr -d '[:space:]')
  [ -n "$a" ] && [ -n "$b" ]
}
wait_for "both probe CRs to carry at least one status condition" 40 6 _both_have_conditions \
  || void "neither arm produced conditions within 240s. Nothing to compare -- and note this is a TIMEOUT, not a finding that the two look alike."

say "ARM A -- credential REJECTED (mock returns 401), verbatim"
A_FILE=$(mktemp); read_conditions rejected    | tee "$A_FILE" | sed 's/^/    /'
say "ARM B -- vendor UNREACHABLE (nova.invalid), verbatim"
B_FILE=$(mktemp); read_conditions unreachable | tee "$B_FILE" | sed 's/^/    /'

say "CONTROL -- the comparator must be able to report a difference"
# If this says "same", the verdict below is meaningless whatever it says.
CTRL=$(mktemp); printf 'type=Ready status=False reason=DeliberatelyDifferent message=control\n' > "$CTRL"
CTRL_V=$(compare_arms "$A_FILE" "$CTRL" | sed -n 's/^VERDICT=//p')
[ "$CTRL_V" = different ] \
  || void "the comparator reported '$CTRL_V' against a deliberately different control, so it cannot distinguish anything. The verdict below would be unreadable."
ok "comparator distinguishes a known-different input"

say "VERDICT -- is a rejected credential distinguishable from an unreachable vendor?"
V_OUT=$(compare_arms "$A_FILE" "$B_FILE")
printf '%s\n' "$V_OUT" | sed 's/^/  /'
case "$(printf '%s\n' "$V_OUT" | sed -n 's/^VERDICT=//p')" in
  same)
    bad "CONFIRMED: the two are INDISTINGUISHABLE from the CR."
    bad "docs/configuration.md's claim stands, and so does the consequence --"
    bad "any upgrade measured against an unreachable vendor cannot see a"
    bad "credential-read regression. That is how 0.53.0 passed as clean."
    echo "  OUTCOME=indistinguishable" ;;
  different)
    ok "REFUTED: an operator CAN tell them apart from the CR's conditions."
    ok "Update docs/configuration.md: the caveat on the 0.53.0 measurement is"
    ok "then about something else, and the diff above is the evidence."
    echo "  OUTCOME=distinguishable" ;;
  *)
    void "one or both arms carried no conditions at read time. Nothing measured." ;;
esac
rm -f "$A_FILE" "$B_FILE" "$CTRL"
