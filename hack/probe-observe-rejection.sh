#!/usr/bin/env bash
#
# When this chart's observe cannot look a resource up, does the controller
# CREATE IT AGAIN?
#
# WHY THIS IS A REAL QUESTION AND NOT A HYPOTHETICAL. rd-flavor.yaml's get verb
# takes its path parameter from the CUSTOM RESOURCE, not from the vendor:
#
#     - action: get
#       path: /flavors/{id}
#       requestFieldMapping:
#         - inPath: id
#           inCustomResource: status.flavor.id
#
# So observe can only issue a GET once `status.flavor.id` is populated, and that
# field is populated from the create response by `populateStatusFields`, which
# `continue`s silently when a declared field is absent from the vendor's payload
# -- no error, no event, no log. Two ways to end up with an empty id therefore
# exist, and one of them needs no misconfiguration at all: a vendor whose create
# response omits `id`.
#
# THIS ALSO EXPLAINS AN OBSERVATION THAT LOOKED LIKE A DEFECT. In the #108 401
# arms the mock refused every request, so no create ever succeeded,
# `status.flavor.id` was never populated, the get verb had no id to substitute,
# and the access log contained POSTs only. That was read as "the controller
# reaches create even though observe was refused". It is simpler than that:
# observe had nothing to look up. The arms below are the ones that can tell the
# two apart, because they let the create SUCCEED first.
#
#   ARM X   create returns 200 with NO `id` in the body.
#           Does the controller POST again, and again?
#           This is the cheap arm and the likely one: it needs only a vendor
#           whose response shape differs from the OAS, which real Nova already
#           does elsewhere (flavor LIST omits ram/vcpus/disk; quota_set returns
#           `id`, not `tenant_id`).
#
#   ARM Y   create returns 200 WITH an id, then every request starts failing.
#           Does the controller POST again?
#           This is "is a rejection on observe read as absence".
#
# WHAT A FINDING WOULD MEAN: a duplicate resource at the vendor for every
# reconcile, invisible from the CR, which is a billing problem rather than an
# aesthetic one.
#
#   hack/probe-observe-rejection.sh <kubeconfig> <context> [namespace]
#   hack/probe-observe-rejection.sh --self-test     # classifier only, no cluster
#
# RUNS ON AN EXISTING CLUSTER. Creates no cluster and deletes none.
set -uo pipefail
export LC_ALL=C

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m  OK: %s\033[0m\n' "$*"; }
bad()  { printf '\033[31m  BAD: %s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mFAIL: %s\033[0m\n' "$*" >&2; exit 1; }
void() { printf '\n\033[33mVOID: %s\033[0m\n' "$*" >&2; exit 3; }

PROBE_UA=probe-observe-control

# ---------------------------------------------------------------------------
# THE CLASSIFIER, kept away from any cluster so it can be self-tested.
#
# Reads attribution records (METHOD PATH STATUS UA) on stdin and prints
#
#     VERDICT=recreated   at least one POST from the controller in this window
#     VERDICT=observed    GETs but no POST -- observe ran and did not re-create
#     VERDICT=silent      no controller request at all
#     VERDICT=other       controller requests, but neither GET nor POST
#
# `silent` IS NOT `observed`. "Zero POSTs because the controller did not
# re-create" and "zero POSTs because the controller never looked" are the same
# integer, and the second is an instrument failure. They must not share a word.
# ---------------------------------------------------------------------------
classify_records() {
  local ua="$1" recs posts gets ctl
  recs=$(cat)
  ctl=$(printf '%s\n' "$recs" | awk -v ua="$ua" 'NF >= 4 && $4 != ua')
  if [ -z "$ctl" ]; then echo "VERDICT=silent"; echo "CTL=0 POST=0 GET=0"; return 0; fi
  posts=$(printf '%s\n' "$ctl" | awk '$1 == "POST"' | grep -c . || true)
  gets=$(printf '%s\n' "$ctl" | awk '$1 == "GET"' | grep -c . || true)
  printf 'CTL=%s POST=%s GET=%s\n' "$(printf '%s\n' "$ctl" | grep -c . || true)" "$posts" "$gets"
  if [ "$posts" -ge 1 ];  then echo "VERDICT=recreated"
  elif [ "$gets" -ge 1 ]; then echo "VERDICT=observed"
  else                         echo "VERDICT=other"; fi
}

verdict_of() { classify_records "$1" | sed -n 's/^VERDICT=//p'; }

self_test() {
  local pass=0 fail_n=0
  t() {
    if [ "$2" = "$3" ]; then printf '  %-56s PASS\n' "$1"; pass=$((pass+1))
    else printf '  %-56s FAIL want=%s got=%s\n' "$1" "$2" "$3"; fail_n=$((fail_n+1)); fi
  }
  # shellcheck disable=SC2016  # single quotes keep awk's $n unexpanded below
  :

  t "a POST from the controller is a re-create" recreated \
    "$(printf 'POST /v2.1/p/flavors 200 Go-http-client/1.1\n' | verdict_of "$PROBE_UA")"
  t "GETs with no POST is observe without re-create" observed \
    "$(printf 'GET /v2.1/p/flavors/x 401 Go-http-client/1.1\nGET /v2.1/p/flavors/x 401 Go-http-client/1.1\n' | verdict_of "$PROBE_UA")"
  t "no controller record is SILENT, never observed" silent \
    "$(printf '' | verdict_of "$PROBE_UA")"

  # THE ARM THAT MAKES THE WINDOW MEAN ANYTHING. The probe's own control
  # request hits the same paths the controller uses, so if it were counted the
  # verdict would be a measurement of this script.
  t "the probe's own POST does not count as a re-create" silent \
    "$(printf 'POST /v2.1/p/flavors 200 %s\n' "$PROBE_UA" | verdict_of "$PROBE_UA")"
  t "probe GET plus controller GET leaves observe" observed \
    "$(printf 'GET /v2.1/p/flavors/x 401 %s\nGET /v2.1/p/flavors/x 401 Go-http-client/1.1\n' "$PROBE_UA" | verdict_of "$PROBE_UA")"

  # A record the parser could not fill must not be read as a controller request.
  t "a short record is ignored rather than counted" silent \
    "$(printf 'GET /v2.1/p/flavors\n' | verdict_of "$PROBE_UA")"
  t "neither GET nor POST is OTHER, not observed" other \
    "$(printf 'DELETE /v2.1/p/flavors/x 401 Go-http-client/1.1\n' | verdict_of "$PROBE_UA")"

  # Counts are reported, so they are asserted.
  t "counts are reported alongside the verdict" "CTL=3 POST=1 GET=2" \
    "$(printf 'POST /a 200 c\nGET /b 401 c\nGET /b 401 c\n' | classify_records "$PROBE_UA" | sed -n 's/^CTL=/CTL=/p')"

  # MUTATION ARM: if the classifier stopped distinguishing POST from GET, the
  # two verdicts above would collapse. Asserted, not described.
  t "recreated and observed are different answers" different \
    "$([ "$(printf 'POST /a 200 c\n' | verdict_of "$PROBE_UA")" != \
         "$(printf 'GET /a 200 c\n'  | verdict_of "$PROBE_UA")" ] && echo different || echo same)"

  printf '\n  %d passed, %d failed\n' "$pass" "$fail_n"
  [ "$fail_n" -eq 0 ] || fail "the classifier is wrong; no cluster result would be readable"
  ok "classifier distinguishes recreated / observed / silent / other"
}

PROJ_X=armx
PROJ_Y=army

# ---------------------------------------------------------------------------
# THE MOCK CONFIGS, AS FUNCTIONS, so the validator below and the cluster deploy
# use the SAME BYTES. A validator that checks a retyped copy of a config is a
# validator of the copy.
#
# log_format SITS OUTSIDE `server {}`, AND THAT IS NOT A STYLE CHOICE. Files in
# conf.d are included inside nginx's `http` context, where log_format is legal;
# inside `server` it is not, and nginx refuses to start:
#
#     [emerg] "log_format" directive is not allowed here in
#             /etc/nginx/conf.d/default.conf:3
#
# This probe's first cluster run died exactly there -- CrashLoopBackOff, a
# readiness probe that could never pass, and a VOID forty seconds in. The config
# had rendered perfectly: every nginx $variable survived the heredoc. It was
# simply not a legal config, and nothing in this repo could say so without a
# cluster. --validate-mock is that something.
# ---------------------------------------------------------------------------
mock_conf() {
  printf '%s\n' \
    "log_format probe '\$remote_addr \"\$request\" \$status \"\$http_user_agent\"';" \
    'server {' \
    '  listen 8080;' \
    '  access_log /dev/stdout probe;' \
    '  default_type application/json;'
  case "$1" in
    noid)
      # A create that SUCCEEDS and returns no id. populateStatusFields continues
      # silently on an absent declared field, so status.flavor.id stays empty and
      # the get verb has no path parameter to substitute.
      printf '%s\n' \
        "  location = /v2.1/${PROJ_X}/flavors {" \
        "    return 200 '{\"flavor\":{\"name\":\"probe-x\",\"ram\":512,\"vcpus\":1,\"disk\":1}}';" \
        '  }' ;;
    ok)
      printf '%s\n' \
        "  location = /v2.1/${PROJ_Y}/flavors {" \
        "    return 200 '{\"flavor\":{\"id\":\"probe-flavor-1\",\"name\":\"probe-y\",\"ram\":512,\"vcpus\":1,\"disk\":1}}';" \
        '  }' \
        "  location = /v2.1/${PROJ_Y}/flavors/probe-flavor-1 {" \
        "    return 200 '{\"flavor\":{\"id\":\"probe-flavor-1\",\"name\":\"probe-y\",\"ram\":512,\"vcpus\":1,\"disk\":1}}';" \
        '  }' ;;
    reject)
      printf '%s\n' \
        '  location / {' \
        "    return 401 '{\"error\":{\"code\":401,\"title\":\"Unauthorized\",\"message\":\"The request you have made requires authentication.\"}}';" \
        '  }' ;;
    *) fail "mock_conf: unknown mock '$1'" ;;
  esac
  [ "$1" = reject ] || printf '%s\n' \
    "  location / { return 404 '{\"itemNotFound\":{\"code\":404,\"message\":\"nope\"}}'; }"
  printf '%s\n' '}'
}

# ---------------------------------------------------------------------------
# --validate-mock: do these three configs actually LOAD, and does each answer
# the way its arm needs? Runs real nginx in docker. No cluster.
#
# WHY IT EXISTS: see mock_conf. The first cluster run VOIDed on
# CrashLoopBackOff because log_format sat inside `server {}` -- a config that
# rendered perfectly and was illegal. Ten minutes of cluster build to learn
# something nginx says in one line.
# ---------------------------------------------------------------------------
validate_mock() {
  command -v docker > /dev/null 2>&1 || void "docker is not on PATH, so no config was validated."
  docker info > /dev/null 2>&1 || void "docker is installed but not running, so no config was validated."
  command -v curl > /dev/null 2>&1 || void "curl is not on PATH, so no behaviour could be checked."
  local bad_n=0
  # mock : path to probe : expected code on that path : expected code on /
  for spec in "noid:/v2.1/${PROJ_X}/flavors:200:404" \
              "ok:/v2.1/${PROJ_Y}/flavors/probe-flavor-1:200:404" \
              "reject:/anything:401:401"; do
    local m p want_p want_root d cid port code_p code_root n
    m=${spec%%:*}; spec=${spec#*:}
    p=${spec%%:*}; spec=${spec#*:}
    want_p=${spec%%:*}; want_root=${spec#*:}

    d=$(mktemp -d)
    mock_conf "$m" > "$d/default.conf"

    # THE CONTROL THAT THIS VALIDATOR'S FIRST VERSION DID NOT HAVE, and the
    # reason it is first. `mock_conf` was defined BELOW this function, so the
    # generated file was EMPTY, nginx started happily with no server block, and
    # all three mocks reported "config loads, nginx stays up". A deliberately
    # broken variant passed too -- which is how it was caught. An empty subject
    # agreeing with a correct one is not agreement.
    n=$(wc -c < "$d/default.conf" | tr -d ' ')
    if [ "${n:-0}" -lt 40 ] || ! grep -q 'listen 8080' "$d/default.conf"; then
      bad "mock-$m: the generated config is $n bytes and has no 'listen 8080'.
        Nothing was validated -- nginx would start with no server block and stay
        up, which reads exactly like a pass."
      bad_n=$((bad_n+1)); rm -rf "$d"; continue
    fi

    cid=$(docker run -d --rm -v "$d:/etc/nginx/conf.d:ro" -p 0:8080 \
            nginxinc/nginx-unprivileged:1.27-alpine 2>/dev/null)
    if [ -z "$cid" ]; then
      bad "mock-$m: the container would not start at all."; bad_n=$((bad_n+1)); rm -rf "$d"; continue
    fi
    sleep 2
    # nginx EXITS on an illegal config, so "still running" is the load test.
    if ! docker ps -q --no-trunc | grep -q "$cid"; then
      bad "mock-$m: nginx REFUSED this config --"
      docker logs "$cid" 2>&1 | grep -E 'emerg|error' | sed 's/^/        /'
      bad_n=$((bad_n+1)); rm -rf "$d"; continue
    fi
    port=$(docker port "$cid" 8080/tcp 2>/dev/null | head -1 | sed 's/.*://')
    code_p=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${port}${p}" 2>/dev/null)
    code_root=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${port}/" 2>/dev/null)
    docker rm -f "$cid" > /dev/null 2>&1; rm -rf "$d"

    # BEHAVIOUR, not just loadability. A config that loads and answers the wrong
    # status turns every arm into a measurement of the wrong thing, and 000
    # (nothing listening) must never read as a pass.
    if [ "$code_p" = "$want_p" ] && [ "$code_root" = "$want_root" ]; then
      ok "mock-$m: loads, and answers $code_p on $p and $code_root on /"
    else
      bad "mock-$m: answered $code_p on $p (want $want_p) and $code_root on / (want $want_root)."
      bad_n=$((bad_n+1))
    fi
  done
  if [ "$bad_n" -gt 0 ]; then
    fail "$bad_n of 3 mock configs are wrong. The cluster arms would VOID on
    CrashLoopBackOff ten minutes in, or worse, measure the wrong behaviour."
  fi
  ok "all three mock configs load in real nginx AND answer as their arm needs"
}

if [ "${1:-}" = "--validate-mock" ]; then
  say "mock config validation against real nginx (no cluster)"
  validate_mock
  exit 0
fi

[ "${1:-}" = "--self-test" ] && { say "classifier self-test (no cluster)"; self_test; exit 0; }

KUBECONFIG_PATH="${1:?usage: probe-observe-rejection.sh <kubeconfig> <context> [namespace] | --self-test}"
CONTEXT="${2:?usage: probe-observe-rejection.sh <kubeconfig> <context> [namespace] | --self-test}"
NS="${3:-krateo-system}"
K() { kubectl --kubeconfig "$KUBECONFIG_PATH" --context "$CONTEXT" "$@"; }

[ -f chart/Chart.yaml ] || fail "run me from the repo root"
# shellcheck source=hack/lib/wait.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/wait.sh"
# shellcheck source=hack/lib/attribute.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/attribute.sh"

GROUP=$(awk '/resourceGroup:/ {print $2; exit}' chart/values.yaml 2>/dev/null)
case "${GROUP:-}" in
  *.*) ;;
  *) fail "could not read resourceGroup from chart/values.yaml (got '${GROUP:-<empty>}')." ;;
esac

# The classifier is self-tested BEFORE any cluster work: a cluster result read
# through a broken classifier is worse than no result.
say "classifier self-test, before touching the cluster"
self_test

say "preconditions -- each failure says what it means"
K get --raw /readyz > /dev/null 2>&1 \
  || void "cannot reach the API server (kubeconfig=$KUBECONFIG_PATH context=$CONTEXT). Nothing measured."
for crd in "computeflavors.$GROUP" "computeflavorconfigurations.$GROUP"; do
  K get crd "$crd" > /dev/null 2>&1 \
    || void "CRD $crd is absent, so the chart is not installed in this cluster. Install it first; this is NOT evidence about observe."
done
command -v helm > /dev/null 2>&1 || void "helm is not on PATH, so the arms cannot be installed."
K get ns "$NS" > /dev/null 2>&1 || K create ns "$NS" > /dev/null 2>&1 \
  || void "namespace $NS does not exist and could not be created."
ok "API reachable, both CRDs present, helm available"

# ---------------------------------------------------------------------------
# THREE MOCKS, THREE SERVICES, AND THE REASON IT IS NOT ONE MOCK WITH A SWITCH.
#
# Arm Y needs the vendor to answer a GET successfully and then stop. The obvious
# implementation -- patch one mock's config and restart it -- DESTROYS THE
# ACCESS LOG, which is the only witness this probe has. Patching a Service
# selector keeps the log but puts both arms' traffic in one pod's log, where a
# POST cannot be attributed to a window.
#
# So each behaviour gets its own pod, its own Service and its own log, and the
# arm is selected by which URL the chart is installed with. Separation is then a
# property of the setup rather than of a timestamp comparison.
# ---------------------------------------------------------------------------
say "deploying the three mocks"
for m in noid ok reject; do
  d=$(mktemp -d)
  mock_conf "$m" > "$d/default.conf"
  K -n "$NS" create configmap "mock-${m}-conf" --from-file="$d/default.conf" \
    --dry-run=client -o yaml | K -n "$NS" apply -f - >/dev/null \
    || void "could not create mock-${m}-conf"
  rm -rf "$d"
done

for m in noid ok reject; do
  K -n "$NS" apply -f - >/dev/null <<EOF
apiVersion: apps/v1
kind: Deployment
metadata: { name: mock-${m}, namespace: ${NS} }
spec:
  replicas: 1
  selector: { matchLabels: { app: mock-${m} } }
  template:
    metadata: { labels: { app: mock-${m} } }
    spec:
      containers:
        - name: nginx
          image: nginxinc/nginx-unprivileged:1.27-alpine
          ports: [ { containerPort: 8080 } ]
          volumeMounts: [ { name: conf, mountPath: /etc/nginx/conf.d } ]
          # tcpSocket, not httpGet: a 401 and a 404 are SUCCESSFUL responses for
          # these mocks, so an httpGet probe would have to be told not to treat
          # them as unready. No backticks in this comment -- it sits inside an
          # unquoted heredoc, where they would be command substitution.
          readinessProbe:
            tcpSocket: { port: 8080 }
            initialDelaySeconds: 1
            periodSeconds: 2
            failureThreshold: 30
      volumes: [ { name: conf, configMap: { name: mock-${m}-conf } } ]
---
apiVersion: v1
kind: Service
metadata: { name: mock-${m}, namespace: ${NS} }
spec:
  selector: { app: mock-${m} }
  ports: [ { port: 8080, targetPort: 8080 } ]
EOF
  K -n "$NS" rollout status "deploy/mock-${m}" --timeout=180s >/dev/null \
    || void "mock-${m} never became ready, so its arm was never offered anything."
done
ok "mock-noid, mock-ok and mock-reject are ready, each with its own Service and log"


url_of() { printf 'http://mock-%s.%s.svc.cluster.local:8080/v2.1/%s' "$1" "$NS" "$2"; }

# curl_through <mock> <path> -> sets CODE
curl_through() {
  CODE=$(K -n "$NS" run "probe-curl-$RANDOM" --rm -i --restart=Never --quiet \
           --image=curlimages/curl:8.11.0 -- \
           -s -o /dev/null -w '%{http_code}' -A "$PROBE_UA" \
           "http://mock-$1.${NS}.svc.cluster.local:8080$2" 2>/dev/null \
         | grep -oE '[0-9]{3}' | head -1)
  [ -n "${CODE:-}" ] && [ "$CODE" != "000" ]
}

# EVERY MOCK IS CONFIRMED TO BEHAVE AS DESIGNED BEFORE ANY ARM RUNS. A mock
# that answers differently from its config turns every verdict below into a
# measurement of the wrong thing, and the access-log table would look just as
# authoritative either way.
say "mock behaviour controls -- asserted, not assumed"
curl_through noid "/v2.1/${PROJ_X}/flavors" \
  || void "mock-noid never answered at all (curl 000). Connectivity failure, nothing measured."
[ "$CODE" = "200" ] || void "mock-noid answered $CODE on the create path, expected 200."
curl_through ok "/v2.1/${PROJ_Y}/flavors/probe-flavor-1" \
  || void "mock-ok never answered at all (curl 000)."
[ "$CODE" = "200" ] || void "mock-ok answered $CODE on the get path, expected 200."
curl_through reject "/v2.1/${PROJ_Y}/flavors/probe-flavor-1" \
  || void "mock-reject never answered at all (curl 000)."
[ "$CODE" = "401" ] || void "mock-reject answered $CODE, expected 401."
ok "noid=200-without-id, ok=200-with-id, reject=401, each confirmed through its Service"

# records_from <mock> -> attribution records for that pod's whole log
# EVERY LOG READ IS PINNED TO ONE NAMED POD, not to `deploy/mock-X`.
#
# `kubectl logs deploy/...` resolves the deployment to SOME pod, and immediately
# after a `rollout restart` completes the terminating old pod can still be the
# one it picks. That is what happened on the second run: the empty-log control
# fired with "4 request record(s) after a restart" while the NEW pod's log was
# in fact empty. The control was right to refuse -- an unexplained instrument is
# a reason to stop -- and the cause was this line, not stale traffic.
#
# So the pod names are captured once, after the restart settles, and every
# subsequent read names a pod. A log attributed to "whichever pod kubectl felt
# like" is not an attribution.
POD_NOID=""; POD_OK=""; POD_REJECT=""

mock_pod_var() {
  case "$1" in
    noid)   printf '%s' "$POD_NOID" ;;
    ok)     printf '%s' "$POD_OK" ;;
    reject) printf '%s' "$POD_REJECT" ;;
    *) fail "mock_pod_var: unknown mock '$1'" ;;
  esac
}

# Exactly ONE pod, or the caller cannot know which log it read.
single_pod() {
  local pods n
  pods=$(K -n "$NS" get pods -l "app=mock-$1" -o name 2>/dev/null)
  n=$(printf '%s\n' "$pods" | grep -c . || true)
  [ "${n:-0}" -eq 1 ] || return 1
  printf '%s' "$pods"
}

records_from() {
  local pod; pod=$(mock_pod_var "$1")
  [ -n "$pod" ] || void "records_from $1 was called before the mock pods were
    pinned. Every count below it would be a count of nothing."
  K -n "$NS" logs "$pod" --tail=2000 2>/dev/null | attribute_log
}

# EVERY ARM'S VERDICT IS A DELTA OVER A LOG
#
# PLACED HERE, BELOW records_from, DELIBERATELY. Written twenty lines higher it
# called records_from before that function existed: `command not found`, an
# empty count, and `[ 0 -eq 0 ]` passing -- a control that cannot fail, which is
# the same mistake that made this file's first mock validator validate an empty
# config. Third instance in one session; the shape is 'a check placed above the
# thing it checks'., so the log's starting state is part
# of the instrument. A second run against pods left over from a first one starts
# with POSTs already in the log: the "wait for the controller to POST at least
# once" gate is satisfied by a STALE record before the new CR is even created,
# the window then opens before the real create, and that create lands inside it
# as a "new" POST -- a false `recreated`. Restarting the mocks is cheaper than
# reasoning about timestamps, and the emptiness is asserted rather than assumed.
say "restarting the mocks so every arm starts from an empty log"
for m in noid ok reject; do
  K -n "$NS" rollout restart "deploy/mock-${m}" >/dev/null 2>&1
done
for m in noid ok reject; do
  K -n "$NS" rollout status "deploy/mock-${m}" --timeout=180s >/dev/null \
    || void "mock-${m} did not come back after a restart."
  # Wait for the OLD pod to be gone, not merely for the new one to be ready --
  # that gap is what made the previous run read a terminating pod's log.
  # shellcheck disable=SC2329  # invoked indirectly, by wait_for
  _one_pod() { single_pod "$m" > /dev/null; }
  wait_for "exactly one mock-${m} pod to remain after the restart" 30 4 _one_pod \
    || void "mock-${m} still has more than one pod after 120s, so a log read
    could not be attributed to either."
  case "$m" in
    noid)   POD_NOID=$(single_pod noid) ;;
    ok)     POD_OK=$(single_pod ok) ;;
    reject) POD_REJECT=$(single_pod reject) ;;
  esac
  n=$(records_from "$m" | grep -c . || true)
  [ "${n:-0}" -eq 0 ] \
    || void "mock-${m}'s log already holds $n request record(s) after a restart,
    so no delta measured against it is attributable to this run."
done
printf '    pinned pods: %s %s %s\n' "$POD_NOID" "$POD_OK" "$POD_REJECT"
ok "all three mock logs are empty and pinned to one pod each"

# THE CHART CARRIES `version: CHART_VERSION` UNTIL RELEASE, so helm refuses it
# unstamped -- `chart.metadata.version "CHART_VERSION" is invalid`. Staged here
# rather than required of the caller: a probe that depends on an undocumented
# prior step reports "helm could not install" for a reason the reader cannot
# see, and every arm below would VOID with the cause one layer away. Caught
# before the first run by checking `helm template` against the working tree.
CHART_PATH=$(mktemp -d)/chart
mkdir -p "$CHART_PATH"
cp -R chart/. "$CHART_PATH"/
sed "s/CHART_VERSION/0.0.0-probeobserve/g" chart/Chart.yaml > "$CHART_PATH/Chart.yaml"
if grep -q CHART_VERSION "$CHART_PATH/Chart.yaml"; then
  fail "failed to stamp CHART_VERSION; helm would refuse the chart and the error
    would point at helm rather than at this line."
fi
ok "staged a stamped chart at $CHART_PATH"

install_at() {
  helm --kubeconfig "$KUBECONFIG_PATH" --kube-context "$CONTEXT" \
    upgrade --install nova-probe "$CHART_PATH" -n "$NS" \
    --set nova.url="$1" --wait --timeout 8m > /tmp/probe-observe-helm.log 2>&1 \
    || { sed 's/^/    /' /tmp/probe-observe-helm.log >&2
         void "helm could not install the chart pointing at $1. No arm ran."; }
}

WINDOW="${PROBE_WINDOW:-120}"
CFG=probe-observe-cfg
K -n "$NS" create secret generic probe-observe-token \
  --from-literal=token=probe-token-not-a-credential >/dev/null 2>&1 || true

# apply_cr <name> -> applies the Configuration and one ComputeFlavor
apply_cr() {
  local man; man=$(mktemp)
  cat > "$man" <<EOF
apiVersion: ${GROUP}/v1alpha1
kind: ComputeFlavorConfiguration
metadata: { name: ${CFG}, namespace: ${NS} }
spec:
  authentication:
    apiKey:
      tokenRef: { name: probe-observe-token, namespace: ${NS}, key: token }
      header: X-Auth-Token
---
apiVersion: ${GROUP}/v2-1
kind: ComputeFlavor
metadata: { name: $1, namespace: ${NS} }
spec:
  configurationRef: { name: ${CFG}, namespace: ${NS} }
  flavor: { name: $1, ram: 512, vcpus: 1, disk: 1 }
EOF
  # The apply's exit status is read. An unchecked apply is how an earlier probe
  # in this repo reported success over two BadRequests.
  if ! K -n "$NS" apply -f "$man" > /tmp/probe-observe-apply.log 2>&1; then
    sed 's/^/    /' /tmp/probe-observe-apply.log >&2
    echo "    --- the manifest it refused ---" >&2
    sed 's/^/    /' "$man" >&2
    rm -f "$man"
    void "the probe's own CRs did not apply, so no arm ran."
  fi
  rm -f "$man"
}

# THE RESOURCE NAME IS FULLY QUALIFIED, AND THAT IS NOT PEDANTRY. `computeflavor`
# is AMBIGUOUS in this cluster -- it prefix-matches both `computeflavors` and
# `computeflavorconfigurations`, so kubectl answers
#
#     error: you must specify only one resource
#
# on stderr and nothing on stdout. The first version of this probe read
# status.flavor.id through that ambiguous name with 2>/dev/null, got an empty
# string, and announced "status.flavor.id is empty, which is the condition under
# test". The field WAS empty, so the arm reached the right answer for the wrong
# reason -- and would have said the same thing about a CR that did not exist at
# all. The broken-instrument answer and the reassuring answer were the same
# empty string, again.
RES="computeflavors.${GROUP}"

cr_exists() { K -n "$NS" get "$RES" "$1" -o name > /dev/null 2>&1; }
status_id()   { K -n "$NS" get "$RES" "$1" -o jsonpath='{.status.flavor.id}' 2>/dev/null; }
conditions()  { K -n "$NS" get "$RES" "$1" -o \
                  jsonpath='{range .status.conditions[*]}type={.type} status={.status} reason={.reason}{"\n"}{end}' 2>/dev/null; }
status_keys() { K -n "$NS" get "$RES" "$1" -o jsonpath='{.status.flavor}' 2>/dev/null; }

# THE WINDOW MUST OUTLAST THE CONTROLLER'S RESYNC, read from the controller's
# own startup log rather than guessed. rest-dynamic-controller 0.52.0 logs
# `"resyncInterval":180000000000` -- 180 SECONDS -- and a 120s window saw
# exactly one reconcile, so the first run VOIDed on its own liveness control.
# Confirmed twice: the log field, and two observe events 179.9s apart.
resync_seconds() {
  local dep ns
  dep=$(K -n "$NS" get deploy -o name 2>/dev/null | grep -m1 'flavor-controller')
  [ -n "$dep" ] || return 1
  ns=$(K -n "$NS" logs "$dep" --tail=300 2>/dev/null \
       | sed -n 's/.*"resyncInterval":\([0-9][0-9]*\).*/\1/p' | head -1)
  [ -n "$ns" ] || return 1
  echo $(( ns / 1000000000 ))
}
count_method() { records_from "$1" | awk -v ua="$PROBE_UA" -v m="$2" '$4 != ua && $1 == m' | grep -c . || true; }
count_ctl()    { records_from "$1" | awk -v ua="$PROBE_UA" '$4 != ua' | grep -c . || true; }

# ===========================================================================
say "ARM X -- the create succeeds and returns no id"
# ===========================================================================
install_at "$(url_of noid "$PROJ_X")"
apply_cr probe-noid

# shellcheck disable=SC2329  # invoked indirectly, by wait_for
_x_posted() { [ "$(count_method noid POST)" -ge 1 ]; }
wait_for "the controller to POST to mock-noid at least once" 40 6 _x_posted \
  || void "no controller POST ever reached mock-noid within 240s. The controller
    never created anything, so nothing can be said about whether it re-creates.
    That is a timeout or an install problem, NOT a clean result."

# THE CONTROL THAT DECIDES WHETHER THIS ARM MEANS ANYTHING. The arm is about an
# EMPTY status.flavor.id. If the field is populated, nginx returned an id after
# all and the window below measures the healthy path.
cr_exists probe-noid \
  || void "the CR probe-noid does not exist, so every field read below would be
    empty for a reason that has nothing to do with the vendor's response."
X_ID=$(status_id probe-noid)
if [ -n "${X_ID:-}" ]; then
  void "status.flavor.id on probe-noid is '$X_ID', but mock-noid's create response
    carries no id. The field is populated from somewhere this probe does not
    model, so the arm cannot express the defect it was written for."
fi
ok "status.flavor.id is empty, which is the condition under test"

RESYNC=$(resync_seconds)
if [ -z "${RESYNC:-}" ]; then
  void "could not read resyncInterval from the flavor controller's log, so there
    is no way to know whether a ${WINDOW}s window is long enough to contain a
    second reconcile. Guessing it is would make any 'no re-create' answer
    unreadable."
fi
if [ "$WINDOW" -le "$RESYNC" ]; then
  void "the window is ${WINDOW}s and the controller's resyncInterval is
    ${RESYNC}s, so at most one reconcile can land inside it. 'No new POSTs'
    would mean 'no second reconcile happened', which is a statement about this
    probe. Set PROBE_WINDOW above ${RESYNC}."
fi
ok "window ${WINDOW}s > resyncInterval ${RESYNC}s, so at least one further reconcile is due"

X_N0=$(count_method noid POST); X_C0=$(count_ctl noid)
say "ARM X window: ${WINDOW}s, starting at POST=$X_N0 CTL=$X_C0"
sleep "$WINDOW"
X_N1=$(count_method noid POST); X_C1=$(count_ctl noid)
X_DPOST=$((X_N1 - X_N0)); X_DCTL=$((X_C1 - X_C0))

printf '    %-7s %-34s %-6s %s\n' METHOD PATH STATUS COUNT
records_from noid | awk -v ua="$PROBE_UA" '$4 != ua { print $1, $2, $3 }' | sort | uniq -c | sort -rn |
  while read -r n m pa st; do printf '    %-7s %-34s %-6s x%s\n' "$m" "$pa" "$st" "$n"; done

# LIVENESS BEFORE INTERPRETATION. "No new POSTs" and "the controller did nothing
# at all in this window" are the same integer, and only the first is a result.
if [ "$X_DCTL" -lt 1 ]; then
  void "the controller made NO request of any kind to mock-noid during the
    ${WINDOW}s window (CTL $X_C0 -> $X_C1). A delta of 0 new POSTs is therefore
    not evidence that it does not re-create -- it is evidence that nothing was
    reconciling. Raise PROBE_WINDOW or check the controller is running."
fi
if [ "$X_DPOST" -ge 1 ]; then
  bad "ARM X = RECREATED. $X_DPOST new POST(s) in ${WINDOW}s after the first
    create, with status.flavor.id empty throughout. Each one is another resource
    at the vendor, and the CR shows nothing."
  ARM_X=recreated
else
  ok "ARM X = no re-create. $X_DCTL controller request(s) in the window, 0 of them POST."
  ARM_X=stable
fi
conditions probe-noid | sed 's/^/    /'

# ===========================================================================
say "ARM Z -- can a CR whose id never populated be DELETED, and is the vendor told?"
# ===========================================================================
# THIS ARM EXISTS BECAUSE ARM X'S ANSWER TURNED OUT TO BE THE LESS INTERESTING
# HALF. Measured 2026-11-18 on rest-dynamic-controller 0.52.0: an id-less CR is
# NOT re-created -- and it is also never observed and never deleted at the
# vendor, while every surface reports success. The controller says so itself:
#
#     "External resource is up to date"                        (observe, no GET)
#     "External resource is not addressable, releasing
#      finalizer without calling the API"                      (delete)
#
# and the CR carries Ready=True/Available, Synced=True/ReconcileSuccess plus
# `krateo.io/external-create-succeeded`. So the operator KNOWS a create
# succeeded and still treats the resource as unaddressable, with nothing in
# Kubernetes recording that a resource was abandoned.
#
# WHICH FIELDS DID POPULATE is printed, because it is the discriminator: if
# status.flavor carries name/ram/vcpus/disk and not id, the response WAS parsed
# and only the declared identifier was missing. A wholly empty status would mean
# something else entirely.
say "what DID populate from the create response:"
printf '    status.flavor = %s\n' "$(status_keys probe-noid)"
conditions probe-noid | sed 's/^/    /'

# THE CONTROL FIRST, AND IT IS THE WHOLE ARM. "Zero DELETEs from the controller"
# and "this mock does not log DELETEs" are the same integer. So the probe sends
# one DELETE of its own, under its own User-Agent, and asserts it appears --
# before any absence is read as a finding.
Z_OWN_BEFORE=$(records_from noid | awk -v ua="$PROBE_UA" '$4 == ua && $1 == "DELETE"' | grep -c . || true)
K -n "$NS" run "probe-del-$RANDOM" --rm -i --restart=Never --quiet \
  --image=curlimages/curl:8.11.0 -- \
  -s -o /dev/null -X DELETE -A "$PROBE_UA" \
  "http://mock-noid.${NS}.svc.cluster.local:8080/v2.1/${PROJ_X}/flavors/control" >/dev/null 2>&1
# shellcheck disable=SC2329  # invoked indirectly, by wait_for
_z_own_logged() { [ "$(records_from noid | awk -v ua="$PROBE_UA" '$4 == ua && $1 == "DELETE"' | grep -c . || true)" -gt "$Z_OWN_BEFORE" ]; }
wait_for "the probe's own DELETE to appear in mock-noid's log" 10 3 _z_own_logged \
  || void "the probe sent a DELETE to mock-noid and its own log does not show one.
    A count of the controller's DELETEs would measure this instrument, so no
    absence below is readable."
ok "mock-noid logs DELETEs when it receives them, so a zero from the controller is an absence"

Z_D0=$(count_method noid DELETE)
K -n "$NS" delete "$RES" probe-noid --timeout=90s > /tmp/probe-observe-del.log 2>&1
Z_DEL_RC=$?
sed 's/^/    /' /tmp/probe-observe-del.log
Z_D1=$(count_method noid DELETE)
Z_DELTA=$((Z_D1 - Z_D0))
printf '    controller DELETEs at the vendor: %s -> %s (delta %s)\n' "$Z_D0" "$Z_D1" "$Z_DELTA"
K -n "$NS" logs "$(K -n "$NS" get deploy -o name | grep -m1 flavor-controller)" --tail=80 2>/dev/null \
  | grep -o '"msg":"[^"]*"' | tail -4 | sed 's/^/    controller said /'

if cr_exists probe-noid; then
  ARM_Z=hung
  bad "ARM Z = the CR did not finish deleting (rc=$Z_DEL_RC). It is waiting on a
    finalizer, which is a VISIBLE failure -- the operator is not pretending."
elif [ "$Z_DELTA" -ge 1 ]; then
  ARM_Z=deleted
  ok "ARM Z = the CR is gone AND $Z_DELTA DELETE(s) reached the vendor."
else
  ARM_Z=leaked
  bad "ARM Z = SILENT LEAK. The CR is gone, rc=$Z_DEL_RC, and ZERO DELETEs
    reached the vendor -- with the control above proving this mock logs them.
    A resource that was really created is abandoned, and nothing in Kubernetes
    records it."
fi

# ===========================================================================
say "ARM Y -- the create succeeds WITH an id, then every request is rejected"
# ===========================================================================
install_at "$(url_of ok "$PROJ_Y")"
apply_cr probe-okid

# shellcheck disable=SC2329  # invoked indirectly, by wait_for
_y_has_id() { [ -n "$(status_id probe-okid)" ]; }
wait_for "status.flavor.id on probe-okid to be populated from the create response" 40 6 _y_has_id \
  || void "status.flavor.id never populated at mock-ok, although its create
    response carries one. Without it the get verb has no path parameter, so arm
    Y would be measuring arm X again -- the very confusion this probe exists to
    end."
Y_ID=$(status_id probe-okid)
ok "status.flavor.id = '$Y_ID', so observe now has something to look up"

# ATTRIBUTION BY CONSTRUCTION. mock-reject has its own pod and its own log, and
# nothing has addressed it yet. Asserted rather than assumed: if traffic is
# already there, no POST found later can be attributed to the post-rejection
# window.
Y_PRE=$(count_ctl reject)
if [ "$Y_PRE" -ne 0 ]; then
  void "mock-reject's log already holds $Y_PRE controller request(s) before the
    chart was pointed at it. The window below would not be attributable."
fi
ok "mock-reject's log holds 0 controller requests, so its whole log is the window"

install_at "$(url_of reject "$PROJ_Y")"
# shellcheck disable=SC2329  # invoked indirectly, by wait_for
_y_reached() { [ "$(count_ctl reject)" -ge 1 ]; }
wait_for "the controller to reach mock-reject after the re-point" 40 6 _y_reached \
  || void "the controller never reached mock-reject within 240s of the chart
    being pointed at it. Either the new URL has not taken effect or nothing is
    reconciling; either way the silence below is about this probe."
say "ARM Y window: ${WINDOW}s"
sleep "$WINDOW"

printf '    %-7s %-34s %-6s %s\n' METHOD PATH STATUS COUNT
records_from reject | awk -v ua="$PROBE_UA" '$4 != ua { print $1, $2, $3 }' | sort | uniq -c | sort -rn |
  while read -r n m pa st; do printf '    %-7s %-34s %-6s x%s\n' "$m" "$pa" "$st" "$n"; done

Y_CLASS=$(records_from reject | classify_records "$PROBE_UA")
printf '    %s\n' "$Y_CLASS"
ARM_Y=$(printf '%s\n' "$Y_CLASS" | sed -n 's/^VERDICT=//p')
case "$ARM_Y" in
  recreated) bad "ARM Y = RECREATED. The controller POSTed again after its GET was
    rejected, so a rejection on observe is read as absence." ;;
  observed)  ok "ARM Y = observe propagated. GETs were rejected and no POST followed." ;;
  silent)    void "ARM Y is SILENT: no controller request in the window although one
    arrived before it. Nothing measured." ;;
  *)         bad "ARM Y = $ARM_Y -- neither GET nor POST. Read the table above." ;;
esac
conditions probe-okid | sed 's/^/    /'

say "VERDICT"
printf '    ARM X (create returns no id)        %s\n' "$ARM_X"
printf '    ARM Z (delete with no id)           %s\n' "$ARM_Z"
printf '    ARM Y (observe rejected after id)   %s\n' "$ARM_Y"
printf '    window per arm                      %ss\n' "$WINDOW"
cat <<'NOTE'

    WHAT A "stable" OR "observed" ANSWER IS WORTH. It is an absence over one
    window on one cluster, not a proof. Lengthen it with PROBE_WINDOW and the
    claim strengthens; it never becomes a guarantee. A "recreated" answer, by
    contrast, is positive evidence and needs no window argument.

    CLEANUP IS NOT AUTOMATIC. Both CRs are left in place deliberately: deleting
    probe-okid while the chart points at mock-reject sends the delete verb into
    a 401 and the CR will hang on its finalizer, which is this chart's known and
    deliberate behaviour. To clean up, point the chart back at mock-ok first:

      helm upgrade --install nova-probe ./chart -n <ns> --set nova.url=<mock-ok url>
      kubectl -n <ns> delete computeflavor probe-okid probe-noid
      helm uninstall nova-probe -n <ns>
NOTE
# Written as an `if`, not as `A || B && exit`: that form parses as
# `(A || B) && exit` and happens to be right here, which is exactly why it is
# the wrong thing to leave in a script -- SC2015 has already been earned twice
# in this repo's probes.
if [ "$ARM_X" = recreated ] || [ "$ARM_Y" = recreated ] || [ "$ARM_Z" = leaked ]; then
  exit 1
fi
exit 0
