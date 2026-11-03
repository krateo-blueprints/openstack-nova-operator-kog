#!/usr/bin/env bash
#
# Does a credential follow a 302 to a DIFFERENT host, inside a real cluster?
#
# oasgen-provider#379: in every released rest-dynamic-controller, a credentialed
# request's destination host is not validated. `servers[0]` sets the base URL,
# the credential is attached unconditionally, and nothing between checks where
# the request is going. Upstream verified the redirect channel in `httptest`
# overlays. This answers the same question in a pod, which is the gap between
# "verified in a unit test" and "verified in the thing people run".
#
# THIS CHART IS IN THE EXPOSED CLASS BY DOCUMENT SHAPE. All six assets declare
# `apiKey` / `in: header` / `name: X-Auth-Token`, which upstream states is
# exposed regardless of path shape and with no CR write required. Zero of our
# eleven paths start with a parameter, so that separate channel does not apply.
#
# THE DESIGN, and every part of it exists because a previous probe got it wrong:
#
#   mock A  "mocknova"        the declared servers[0] host. Answers POST with a
#                             302 to host B. This is the only thing A does
#                             differently from hack/probe-361.sh's mock.
#   mock B  "redirect-target" a DIFFERENT Service. Logs whether X-Auth-Token
#                             arrived, as PRESENT/ABSENT via an nginx `map`, so
#                             the verdict never prints a credential-shaped
#                             value. B IS THE INSTRUMENT: ground truth comes
#                             from the host that would receive the credential,
#                             never inferred from A's log.
#
# WHY B MUST PROVE IT WAS REACHABLE FIRST. "B logged no credential" from a B
# that was never reachable is indistinguishable from "the controller refused to
# follow the redirect" -- and that is the defect family this repo has found six
# times. So a pre-flight from inside the cluster asserts B answers and logs,
# and a failure there is VOID rather than a clean result. The pre-flight is
# tagged by user-agent so it can be excluded from the verdict: a previous probe
# counted its own curl as the subject's traffic and drew the opposite conclusion
# from the truth.
#
# THE TOKEN IS A DUMMY. `dummy-not-a-credential`, created in-cluster, never a
# real Keystone token. Nothing here contacts OpenStack or any external endpoint:
# both hosts are cluster Services. A probe that needed a real credential to
# answer a question about credential handling would be the wrong probe.
#
# TWO ARMS, AND NEITHER IS A VERDICT ALONE. Since 0.50.0 (#379 part 2) the
# destination host is validated at the request path, so this probe's redirect is
# REFUSED -- and because this mock answers the create WITH the 302, a refusal
# also means the create did not complete. A policy that refused every
# destination would produce identical output here.
#
#   hack/probe-redirect.sh  EXPECT=refused   the off-host hop must be refused
#   hack/probe-361.sh                        a normal create (200, no redirect)
#                                            must still succeed
#
# Run both against the same version. The first shows the hole is closed; the
# second shows the door still opens.
#
# Usage:  EXPECT=refused hack/probe-redirect.sh             # from the repo root
#         CLUSTER=my-name hack/probe-redirect.sh
#
# Exits: 0 verdict reached (read the verdict, it may be either way)
#        3 VOID -- nothing measured, including the load gate and B unreachable
#        1 setup failed
set -euo pipefail

CLUSTER=${CLUSTER:-nova-redirect}
NS=krateo-system
OASGEN_VERSION=${OASGEN_VERSION:-0.49.0}
GROUP=nova.openstack.krateo.io
FAKE_ID=probe-id-0123456789
MOCK_URL="http://mocknova.${NS}.svc.cluster.local:8080/v2.1"
TARGET_HOST="redirect-target.${NS}.svc.cluster.local:8080"

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m  OK: %s\033[0m\n' "$*"; }
bad()  { printf '\033[31m  BAD: %s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mFAIL: %s\033[0m\n' "$*" >&2; exit 1; }
void() { printf '\n\033[33mVOID: %s\033[0m\n' "$*" >&2; exit 3; }

[ -f chart/Chart.yaml ] || fail "run me from the repo root"

# Same gate as hack/probe-361.sh, and validated the same way: an unreadable
# load must land in REFUSE, not fall through to "the gate did not trigger".
L=$(uptime | sed 's/.*load average[s]*: *//' | tr ',' '.' | awk '{print $1}')
LI=${L%%.*}
case "$LI" in
  ''|*[!0-9]*) void "could not read the load average from uptime (got '${L:-<empty>}'). Refusing rather than guessing it is low." ;;
esac
[ "$LI" -lt 15 ] || void "load is $L; the gate is 15. Nothing was measured."
ok "load $L -- under the gate"

say "cluster + oasgen-provider ${OASGEN_VERSION}"
kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
kind create cluster --name "$CLUSTER" --image kindest/node:v1.36.1 >/dev/null
trap 'kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true' EXIT
helm upgrade --install oasgen-provider-crds \
  oci://ghcr.io/krateo-platformops/charts/oasgen-provider-crds \
  --version "$OASGEN_VERSION" -n "$NS" --create-namespace --wait >/dev/null
helm upgrade --install oasgen-provider \
  oci://ghcr.io/krateo-platformops/charts/oasgen-provider \
  --version "$OASGEN_VERSION" -n "$NS" --wait --timeout 10m >/dev/null
ok "oasgen-provider ${OASGEN_VERSION} installed"

say "two hosts: A redirects, B reports what it received"
# QUOTED heredoc throughout: nothing inside is shell-expanded, so nginx's own
# $variables and punctuation are safe. Values are injected by sed afterwards.
# Escaping an unquoted heredoc case by case cost two earlier runs.
cat > /tmp/probe-redirect.yaml <<'EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: mocknova-conf
data:
  default.conf: |
    server {
      listen 8080;
      access_log /dev/stdout;
      default_type application/json;

      # The ONE behaviour under test: the declared host answers the create with
      # a 302 to a DIFFERENT host. Everything else mirrors probe-361's mock.
      location = /v2.1/flavors {
        if ($request_method = POST) {
          return 302 'http://__TARGET_HOST__/v2.1/flavors';
        }
        return 200 '{"flavors":[]}';
      }
      location ~ ^/v2\.1/flavors/.+$ {
        return 200 '{"flavor":{"id":"__FAKE_ID__","name":"probe-redirect","ram":2048,"vcpus":1,"disk":20}}';
      }
      location / { return 404 '{"itemNotFound":{"message":"not found","code":404}}'; }
    }
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: redirect-target-conf
data:
  default.conf: |
    # B is the instrument. PRESENT/ABSENT rather than the value, so a verdict
    # can never print a credential-shaped string even though this token is a
    # dummy. `map` belongs at http level and conf.d/*.conf is included there.
    map $http_x_auth_token $authhdr {
      ""      "ABSENT";
      default "PRESENT";
    }
    log_format groundtruth '$remote_addr "$request" host=$host x_auth_token=$authhdr ua="$http_user_agent"';

    server {
      listen 8080;
      access_log /dev/stdout groundtruth;
      default_type application/json;

      location = /v2.1/flavors {
        return 200 '{"flavor":{"id":"__FAKE_ID__","name":"probe-redirect","ram":2048,"vcpus":1,"disk":20}}';
      }
      location / {
        return 200 '{"note":"B received a request on an unexpected path"}';
      }
    }
EOF

# Both Deployments share one hardened spec; only name and ConfigMap differ.
for pair in "mocknova:mocknova-conf" "redirect-target:redirect-target-conf"; do
  name=${pair%%:*}; conf=${pair##*:}
  cat >> /tmp/probe-redirect.yaml <<EOF
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${name}
spec:
  replicas: 1
  selector: { matchLabels: { app: ${name} } }
  template:
    metadata: { labels: { app: ${name} } }
    spec:
      securityContext:
        runAsNonRoot: true
        runAsUser: 101
        runAsGroup: 101
        seccompProfile: { type: RuntimeDefault }
      containers:
        - name: nginx
          image: nginxinc/nginx-unprivileged:1.27-alpine
          ports: [ { containerPort: 8080 } ]
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: false
            capabilities: { drop: ["ALL"] }
          resources:
            requests: { cpu: 10m, memory: 24Mi }
            limits:   { cpu: 200m, memory: 96Mi }
          volumeMounts:
            - { name: conf, mountPath: /etc/nginx/conf.d }
      volumes:
        - name: conf
          configMap: { name: ${conf} }
---
apiVersion: v1
kind: Service
metadata:
  name: ${name}
spec:
  selector: { app: ${name} }
  ports: [ { port: 8080, targetPort: 8080 } ]
EOF
done

# NOT `sed -i'' -e ...`: in shell that is a single argument `-i`, and BSD sed
# (macOS) then takes the NEXT argument as the backup suffix, eating `-e` and
# failing with "sed: -e: No such file or directory". GNU sed accepts it, so the
# form works on CI and breaks on the machine this is usually run from. Writing
# to a new file and moving it is portable and needs no suffix at all.
sed -e "s|__TARGET_HOST__|${TARGET_HOST}|g" -e "s|__FAKE_ID__|${FAKE_ID}|g" \
  /tmp/probe-redirect.yaml > /tmp/probe-redirect.rendered.yaml
mv /tmp/probe-redirect.rendered.yaml /tmp/probe-redirect.yaml
# The placeholders must be GONE. A surviving __TARGET_HOST__ would make A
# redirect to a literal hostname that does not resolve, and the probe would
# report "the controller did not follow the redirect" -- a false negative that
# looks exactly like the safe result.
if grep -aq '__TARGET_HOST__\|__FAKE_ID__' /tmp/probe-redirect.yaml; then
  fail "a placeholder survived substitution; refusing to run with a manifest
  that would produce a false 'did not follow' verdict"
fi
kubectl -n "$NS" apply -f /tmp/probe-redirect.yaml >/dev/null
kubectl -n "$NS" rollout status deploy/mocknova --timeout=180s >/dev/null
kubectl -n "$NS" rollout status deploy/redirect-target --timeout=180s >/dev/null
ok "A (mocknova) and B (redirect-target) are up"

say "PRECONDITION: B must be reachable, must log, AND must detect a header"
# The header value is assembled from a variable rather than written inline next
# to the header name. gitleaks' `curl-auth-header` rule flagged the inline form
# and was RIGHT to: a credential-shaped literal beside an auth header name in a
# committed script is exactly the pattern that is a real leak in another file.
# This value is inert by construction, but the shape is the hazard, so the shape
# goes rather than the detection being suppressed.
PREFLIGHT_HEADER_NAME="X-Auth-Token"
PREFLIGHT_SENTINEL="preflight-sentinel-not-a-credential"
# Tagged user-agent so the verdict can exclude it. A previous probe counted its
# own pre-flight as the subject's traffic and reached the opposite conclusion.
kubectl -n "$NS" run preflight --rm -i --restart=Never --image=curlimages/curl:8.11.0 -- \
  -s -o /dev/null -A "probe-preflight" \
  -H "${PREFLIGHT_HEADER_NAME}: ${PREFLIGHT_SENTINEL}" \
  "http://${TARGET_HOST}/v2.1/flavors" >/dev/null 2>&1 || true
sleep 3
PRE_LINE=$(kubectl -n "$NS" logs deploy/redirect-target --tail=-1 2>/dev/null \
             | grep -a 'probe-preflight' || true)
if [ -z "$PRE_LINE" ]; then
  void "B did not log the pre-flight, so B cannot be used as ground truth. 'No credential reached B' would be indistinguishable from 'B was never reachable'."
fi
# THE CONTROL THAT WAS MISSING. Reachability alone is not enough: if B's
# `map $http_x_auth_token` were broken it would log every request as ABSENT, and
# then ABSENT in the real test would mean nothing. The pre-flight deliberately
# SENDS the header, so B must report PRESENT here. A detector that cannot detect
# a header it was just sent cannot be trusted to report its absence.
if ! printf '%s\n' "$PRE_LINE" | grep -aq 'x_auth_token=PRESENT'; then
  void "B logged the pre-flight but did NOT report the header it was sent:
    $PRE_LINE
  So B's detector does not detect, and a later ABSENT would be meaningless."
fi
ok "B is reachable, logging, AND proven to detect a header it is sent --"
ok "so a later ABSENT is a measurement rather than a broken instrument"

say "chart pointed at A, and a ComputeFlavor applied"
STAGE=$(mktemp -d); cp -R ./chart "$STAGE/chart"
sed 's/CHART_VERSION/0.0.0-ci/g' chart/Chart.yaml > "$STAGE/chart/Chart.yaml"
helm upgrade --install nova "$STAGE/chart" -n "$NS" --set nova.url="$MOCK_URL" --wait --timeout 5m >/dev/null
rm -rf "$STAGE"
for _ in $(seq 1 60); do kubectl get crd "computeflavors.$GROUP" >/dev/null 2>&1 && break; sleep 5; done
kubectl wait --for=condition=Established "crd/computeflavors.$GROUP" --timeout=180s >/dev/null
kubectl -n "$NS" create secret generic nova-token \
  --from-literal=token=dummy-not-a-credential >/dev/null
kubectl apply -f - >/dev/null <<EOF
apiVersion: ${GROUP}/v1alpha1
kind: ComputeFlavorConfiguration
metadata: { name: nova-config, namespace: ${NS} }
spec:
  authentication:
    apiKey:
      tokenRef: { name: nova-token, namespace: ${NS}, key: token }
      header: X-Auth-Token
---
apiVersion: ${GROUP}/v2-1
kind: ComputeFlavor
metadata: { name: probe-redirect, namespace: ${NS} }
spec:
  configurationRef: { name: nova-config, namespace: ${NS} }
  flavor: { name: probe-redirect, ram: 2048, vcpus: 1, disk: 20 }
EOF
ok "applied; observing for 240s"
sleep 240

say "A's log -- RAW, IN ORDER. Did the controller get the 302 at all?"
A_LOG=$(kubectl -n "$NS" logs deploy/mocknova --tail=-1 2>/dev/null || true)
printf '%s\n' "$A_LOG" | grep -a 'HTTP/1' | sed 's/^/    /' || true

say "B's log -- GROUND TRUTH. Subject traffic only."
B_RAW=$(kubectl -n "$NS" logs deploy/redirect-target --tail=-1 2>/dev/null || true)
printf '%s\n' "$B_RAW" | grep -a 'HTTP/1' | sed 's/^/    /' || true
B_SUBJ=$(printf '%s\n' "$B_RAW" | grep -a 'HTTP/1' | grep -av 'probe-preflight' || true)

say "VERDICT on #379's redirect channel"
A_POST=$(printf '%s\n' "$A_LOG" | grep -ac '"POST /v2.1/flavors' || true)
if [ "${A_POST:-0}" -eq 0 ]; then
  void "the controller never POSTed to A, so it was never offered a redirect. Nothing about #379 was measured."
fi
echo "  POSTs to A (the 302 was served):        $A_POST"

# WHAT HAPPENED, stated before any judgement about it. The two are separated on
# purpose: the facts are the same on every version, and only the EXPECTATION
# changes. Conflating them is how a probe starts reporting what it hoped for.
if [ -z "$B_SUBJ" ]; then
  OUTCOME=refused
  ok "B received NO subject request: the controller did NOT carry the request to"
  ok "another host. B was proven reachable AND proven to detect a header above,"
  ok "so this silence is a measurement rather than an absent one."
elif printf '%s\n' "$B_SUBJ" | grep -aq 'x_auth_token=PRESENT'; then
  OUTCOME=followed_with_credential
  echo "  subject requests that reached B:"
  printf '%s\n' "$B_SUBJ" | sed 's/^/      /'
  bad "THE CREDENTIAL FOLLOWED THE REDIRECT TO A DIFFERENT HOST."
  bad "B is not the host named in servers[0], and it received the apiKey header."
else
  OUTCOME=followed_without_credential
  echo "  subject requests that reached B:"
  printf '%s\n' "$B_SUBJ" | sed 's/^/      /'
  ok "the controller followed the redirect but did NOT carry the credential."
fi
echo "  OUTCOME=$OUTCOME"

# NOW the judgement, which depends entirely on which version is under test.
#   EXPECT=refused   oasgen/RDC >= 0.50.0, where #379 part 2 validates the
#                    destination host at the request path. A followed request is
#                    a REGRESSION and exits non-zero.
#   EXPECT=report    (default) no expectation -- print and exit 0. This is how
#                    the 0.49.0 run was done, where the follow WAS the finding.
case "${EXPECT:-report}" in
  refused)
    if [ "$OUTCOME" = "refused" ]; then
      ok "EXPECT=refused and the request was refused: #379 part 2 holds here."
      echo
      bad "THIS ARM ALONE IS NOT ENOUGH. This mock answers the create with the"
      bad "302 itself, so a refusal also means the create did not complete. A"
      bad "policy that refused EVERYTHING would produce this identical output."
      bad "Run hack/probe-361.sh against the same version: its mock answers the"
      bad "create with 200 and no redirect, so it shows a legitimate call still"
      bad "succeeds. Neither arm is a verdict on its own."
      exit 0
    fi
    bad "EXPECT=refused but OUTCOME=$OUTCOME -- the destination host was NOT"
    bad "validated. On >= 0.50.0 that is a REGRESSION of #379 part 2."
    exit 1
    ;;
  report)
    echo "  EXPECT=report: no pass/fail asserted. Set EXPECT=refused on >= 0.50.0."
    exit 0
    ;;
  *)
    fail "unknown EXPECT='${EXPECT}'. Use 'refused' or 'report'."
    ;;
esac
