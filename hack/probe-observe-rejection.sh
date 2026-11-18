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
PROJ_X=armx
PROJ_Y=army

say "deploying the three mocks"
K -n "$NS" apply -f - >/dev/null <<EOF
apiVersion: v1
kind: ConfigMap
metadata: { name: mock-noid-conf, namespace: ${NS} }
data:
  default.conf: |
    server {
      listen 8080;
      log_format probe '\$remote_addr "\$request" \$status "\$http_user_agent"';
      access_log /dev/stdout probe;
      default_type application/json;
      # ARM X: a create that SUCCEEDS and returns no id. populateStatusFields
      # continues silently on an absent declared field, so status.flavor.id
      # stays empty and the get verb has no path parameter to substitute.
      location = /v2.1/${PROJ_X}/flavors {
        return 200 '{"flavor":{"name":"probe-x","ram":512,"vcpus":1,"disk":1}}';
      }
      location / { return 404 '{"itemNotFound":{"code":404,"message":"nope"}}'; }
    }
---
apiVersion: v1
kind: ConfigMap
metadata: { name: mock-ok-conf, namespace: ${NS} }
data:
  default.conf: |
    server {
      listen 8080;
      log_format probe '\$remote_addr "\$request" \$status "\$http_user_agent"';
      access_log /dev/stdout probe;
      default_type application/json;
      location = /v2.1/${PROJ_Y}/flavors {
        return 200 '{"flavor":{"id":"probe-flavor-1","name":"probe-y","ram":512,"vcpus":1,"disk":1}}';
      }
      location = /v2.1/${PROJ_Y}/flavors/probe-flavor-1 {
        return 200 '{"flavor":{"id":"probe-flavor-1","name":"probe-y","ram":512,"vcpus":1,"disk":1}}';
      }
      location / { return 404 '{"itemNotFound":{"code":404,"message":"nope"}}'; }
    }
---
apiVersion: v1
kind: ConfigMap
metadata: { name: mock-reject-conf, namespace: ${NS} }
data:
  default.conf: |
    server {
      listen 8080;
      log_format probe '\$remote_addr "\$request" \$status "\$http_user_agent"';
      access_log /dev/stdout probe;
      default_type application/json;
      location / {
        return 401 '{"error":{"code":401,"title":"Unauthorized","message":"The request you have made requires authentication."}}';
      }
    }
EOF

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
records_from() { K -n "$NS" logs "deploy/mock-$1" --tail=2000 2>/dev/null | attribute_log; }

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

status_id()   { K -n "$NS" get "computeflavor/$1" -o jsonpath='{.status.flavor.id}' 2>/dev/null; }
conditions()  { K -n "$NS" get "computeflavor/$1" -o \
                  jsonpath='{range .status.conditions[*]}type={.type} status={.status} reason={.reason}{"\n"}{end}' 2>/dev/null; }
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
X_ID=$(status_id probe-noid)
if [ -n "${X_ID:-}" ]; then
  void "status.flavor.id on probe-noid is '$X_ID', but mock-noid's create response
    carries no id. The field is populated from somewhere this probe does not
    model, so the arm cannot express the defect it was written for."
fi
ok "status.flavor.id is empty, which is the condition under test"

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
if [ "$ARM_X" = recreated ] || [ "$ARM_Y" = recreated ]; then
  exit 1
fi
exit 0
