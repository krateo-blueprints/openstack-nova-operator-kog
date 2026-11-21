#!/usr/bin/env bash
# Can ComputeVolumeAttachment be enabled? Two questions, both measured.
#
# chart/values.yaml ships `volume_attachment.enabled: false` for one reason,
# named there and in docs/configuration.md: ONE mapping points a path parameter
# at a DOTTED target in spec --
#
#     inPath: volume_id  ->  inCustomResource: spec.volumeAttachment.volumeId
#
# Every proven mapping in this chart points into `status`
# (`inPath: id` -> `status.<envelope>.id`), and rd-quota.yaml's three point at a
# BARE `spec.tenant_id`, which is the self-referential shape oasgen-provider#421
# exempts from #419's suppression. A dotted spec target is neither, and this
# chart has never measured it. It SHOULD behave identically, because a
# requestFieldMapping is a path substitution either way -- and
# docs/configuration.md is mostly a record of reasonable "should"s that were
# wrong.
#
# ARM A -- does the property survive #419's suppression?
#   `spec.volumeAttachment.volumeId` must be present in the CRD the API server
#   SERVES. #419 deleted the very property a self-referential mapping read from;
#   if it also deletes a dotted one, the verbs read a field that does not exist.
#
# ARM B -- is the template SUBSTITUTED or left literal?
#   The request Nova receives must carry the real ids, not `{server_id}` or
#   `{volume_id}`. rd-quota.yaml's measurement is the precedent: on the wire,
#   against a request-logging stand-in, `{tenant_id}` substituted rather than
#   left literal.
#
# Exit codes: 0 both arms pass (the kind can be enabled), 1 a real finding
# (it cannot, as shaped), 3 VOID (nothing was measured).
set -uo pipefail
# LC_ALL=C, for the same reason probe-observe-rejection.sh sets it. This script
# has two `[a-z_]` ranges in the literal-template reader and one `sort -u` over
# request paths, and both are locale-dependent: a range is interpreted by
# collation order, not by ASCII, so under some locales `[a-z]` matches more than
# a-z. probe-extension-passthrough.sh deliberately does NOT set it -- measured:
# it has no character range, no sort and no arithmetic on command output, so
# there it would be cargo.
export LC_ALL=C

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m  OK: %s\033[0m\n' "$*"; }
bad()  { printf '\033[31m  BAD: %s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mFAIL: %s\033[0m\n' "$*" >&2; exit 1; }
void() { printf '\n\033[33mVOID: %s\033[0m\n' "$*" >&2; exit 3; }

PROBE_UA=probe-vam-control
SRV=srv-probe-1
VOL=vol-probe-1
PROJ=p

# mock_conf -- an nginx that LOGS every request and answers the attachment paths.
# log_format sits OUTSIDE `server {}` deliberately: files in conf.d are included
# in nginx's `http` context where it is legal, and inside `server` nginx refuses
# to start. That mistake CrashLoopBackOff'd a sibling probe's first cluster run.
mock_conf() {
  printf '%s\n' \
    "log_format probe '\$msec \$remote_addr \"\$request\" \$status \"\$http_user_agent\"';" \
    'server {' \
    '  listen 8080;' \
    '  access_log /dev/stdout probe;' \
    '  default_type application/json;' \
    "  location = /v2.1/${PROJ}/servers/${SRV}/os-volume_attachments {" \
    '    return 200 '"'"'{"volumeAttachment":{"id":"'"${VOL}"'","volumeId":"'"${VOL}"'","serverId":"'"${SRV}"'","device":"/dev/vdb"}}'"'"';' \
    '  }' \
    "  location = /v2.1/${PROJ}/servers/${SRV}/os-volume_attachments/${VOL} {" \
    '    return 200 '"'"'{"volumeAttachment":{"id":"'"${VOL}"'","volumeId":"'"${VOL}"'","serverId":"'"${SRV}"'","device":"/dev/vdb"}}'"'"';' \
    '  }' \
    '  location / { return 404 '"'"'{"error":"unmatched path -- see the access log"}'"'"'; }' \
    '}'
}

self_test() {
  local pass=0 fail_n=0
  t() {
    if [ "$2" = "$3" ]; then printf '  %-56s PASS\n' "$1"; pass=$((pass+1))
    else printf '  %-56s FAIL want=%s got=%s\n' "$1" "$2" "$3"; fail_n=$((fail_n+1)); fi
  }
  local conf; conf=$(mock_conf)

  t "the mock config has a server block and a listener" "1 1" \
    "$(printf '%s\n' "$conf" | grep -c '^server {' || true) $(printf '%s\n' "$conf" | grep -c 'listen 8080' || true)"
  # log_format BEFORE `server {`, or nginx refuses the whole config.
  t "log_format sits outside the server block" yes \
    "$(printf '%s\n' "$conf" | awk '/^log_format/{lf=NR} /^server \{/{sb=NR} END{print (lf && sb && lf < sb) ? "yes" : "NO"}')"
  t "both attachment paths are served" 2 \
    "$(printf '%s\n' "$conf" | grep -c 'os-volume_attachments' || true)"
  t "an unmatched path 404s rather than 200s" 1 \
    "$(printf '%s\n' "$conf" | grep -c 'location / { return 404' || true)"

  # THE LITERAL-TEMPLATE READER, in both directions. This is the arm that
  # decides arm B, so a planted literal must be seen and a substituted path
  # must not be mistaken for one.
  t "a literal {volume_id} in a path is detected" 1 \
    "$(literal_count 'GET /v2.1/p/servers/srv/os-volume_attachments/%7Bvolume_id%7D 200 ua')"
  t "an unencoded literal brace is detected too" 1 \
    "$(literal_count 'GET /v2.1/p/servers/{server_id}/os-volume_attachments/v1 200 ua')"
  t "a substituted path is NOT read as literal" 0 \
    "$(literal_count "GET /v2.1/p/servers/${SRV}/os-volume_attachments/${VOL} 200 ua")"

  printf '\n  %d passed, %d failed\n' "$pass" "$fail_n"
  [ "$fail_n" -eq 0 ] || fail "the mock config or the literal reader is wrong; no cluster result would be readable"
  ok "mock config loadable in shape, and the literal reader discriminates"
}

# literal_count <text> -- how many lines carry an UNSUBSTITUTED path template.
# Both spellings count: nginx logs the raw request line, and a client may or may
# not percent-encode the braces. grep -c reads to EOF, so no pipefail race.
literal_count() {
  printf '%s\n' "$1" | grep -cE '%7B[a-z_]+%7D|\{[a-z_]+\}' || true
}

[ "${1:-}" = "--self-test" ] && { say "mock config and literal reader (no cluster)"; self_test; exit 0; }

KUBECONFIG_PATH="${1:?usage: probe-volume-attachment-mapping.sh <kubeconfig> <context> [namespace] | --self-test}"
CONTEXT="${2:?usage: probe-volume-attachment-mapping.sh <kubeconfig> <context> [namespace] | --self-test}"
NS="${3:-krateo-system}"
K() { kubectl --kubeconfig "$KUBECONFIG_PATH" --context "$CONTEXT" "$@"; }

# shellcheck source=hack/lib/wait.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/wait.sh"
# shellcheck source=hack/lib/attribute.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/attribute.sh"
if ! command -v wait_for >/dev/null || ! command -v attribute_log >/dev/null; then
  fail "hack/lib/{wait,attribute}.sh were sourced but did not define their
    functions. Every retry and every log read below would read as a failure of
    the thing being measured."
fi

# THE VOLUME ATTACHMENT'S OWN resourceGroup, not the first one in the file.
# `awk '/resourceGroup:/ {print $2; exit}'` returns the right answer today only
# because all seven kinds happen to share one group -- correct by coincidence of
# the current file rather than by construction, and silently wrong the day a kind
# gets its own group.
GROUP=$(python3 -c '
import io, sys, yaml
d = yaml.safe_load(io.open("chart/values.yaml", encoding="utf-8"))
print(d["restdefinitions"]["volume_attachment"].get("resourceGroup", ""))
' 2>/dev/null)
case "${GROUP:-}" in
  *.*) ;;
  *) fail "could not read resourceGroup from chart/values.yaml (got '${GROUP:-<empty>}')." ;;
esac
PLURAL=computevolumeattachments
CFG=probe-vam-cfg

say "mock config and literal reader, before touching the cluster"
self_test

say "preconditions -- each failure says what it means"
K get --raw /readyz > /dev/null 2>&1 \
  || void "cannot reach the API server (kubeconfig=$KUBECONFIG_PATH context=$CONTEXT)."
K get crd restdefinitions.ogen.krateo.io > /dev/null 2>&1 \
  || void "restdefinitions.ogen.krateo.io is absent, so oasgen-provider is not installed."
command -v helm > /dev/null 2>&1 || void "helm is not on PATH, so the chart cannot be installed."
K get ns "$NS" > /dev/null 2>&1 || K create ns "$NS" > /dev/null 2>&1 \
  || void "namespace $NS does not exist and could not be created."
ok "API reachable, oasgen-provider present, helm available"

say "deploying the request-logging mock"
conf_file=$(mktemp)
mock_conf > "$conf_file"
K -n "$NS" create configmap vam-mock-conf \
  --from-file=default.conf="$conf_file" --dry-run=client -o yaml | K apply -f - > /dev/null \
  || void "could not create the mock's ConfigMap."
rm -f "$conf_file"
K apply -f - > /dev/null <<EOF || void "could not apply the mock Deployment/Service."
apiVersion: apps/v1
kind: Deployment
metadata: { name: vam-mock, namespace: ${NS} }
spec:
  replicas: 1
  selector: { matchLabels: { app: vam-mock } }
  template:
    metadata: { labels: { app: vam-mock } }
    spec:
      containers:
        - name: nginx
          image: nginxinc/nginx-unprivileged:1.27-alpine
          ports: [ { containerPort: 8080 } ]
          readinessProbe:
            tcpSocket: { port: 8080 }
            initialDelaySeconds: 1
          volumeMounts:
            - { name: conf, mountPath: /etc/nginx/conf.d }
      volumes:
        - name: conf
          configMap: { name: vam-mock-conf }
---
apiVersion: v1
kind: Service
metadata: { name: vam-mock, namespace: ${NS} }
spec:
  selector: { app: vam-mock }
  ports: [ { port: 8080, targetPort: 8080 } ]
EOF
K -n "$NS" rollout status deploy/vam-mock --timeout=180s > /dev/null \
  || void "the mock never became ready, so nothing was ever offered a request."

# ENDPOINTS BEFORE CURL. `rollout status` returning means the pod passed its
# readiness probe; it does NOT mean the Service's endpoints have propagated.
# That gap VOIDed a sibling probe's run 3.4 seconds after "ready".
endpoints_of() {
  K -n "$NS" get endpoints vam-mock \
    -o jsonpath='{range .subsets[*]}{range .addresses[*]}{.ip}{"\n"}{end}{end}' 2>/dev/null \
    | grep -c . || true
}
# shellcheck disable=SC2329  # invoked indirectly, as wait_for's "$@"
_ep_ready() { [ "$(endpoints_of)" -ge 1 ]; }
wait_for "the mock Service to have at least one ready endpoint" 20 3 _ep_ready \
  || void "after 60s the mock Service still has no endpoint (count=$(endpoints_of)).
    The pod passed its readiness probe, so this is endpoint propagation rather
    than nginx. Nothing was measured."
ok "mock ready with $(endpoints_of) endpoint(s)"

MOCK_POD=$(K -n "$NS" get pod -l app=vam-mock -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
[ -n "${MOCK_POD:-}" ] || void "could not pin the mock's pod name; every log read would be a read of nothing."
records() { K -n "$NS" logs "$MOCK_POD" --tail=2000 2>/dev/null | attribute_log; }

# curl_through -- the pod's LOG is read, never an attach. `kubectl run --rm -i`
# streams stdout over an attach, and a curl that finishes before the attach is
# established writes into a stream nobody reads: exit 0, no output, no error.
# Fifteen retries lost that race in a sibling probe, because retrying a race
# does not change the odds.
# shellcheck disable=SC2329  # invoked indirectly, via _mock_answers as wait_for's "$@"
curl_through() {
  local err out rc pod phase i
  err=$(mktemp); pod="probe-vam-curl-$RANDOM"
  K -n "$NS" run "$pod" --restart=Never --attach=false --quiet \
      --image=curlimages/curl:8.11.0 -- \
      -s -o /dev/null -w '%{http_code}' -A "$PROBE_UA" \
      "http://vam-mock.${NS}.svc.cluster.local:8080$1" > /dev/null 2> "$err"
  rc=$?
  if [ "$rc" -eq 0 ]; then
    for i in $(seq 1 20); do
      phase=$(K -n "$NS" get pod "$pod" -o jsonpath='{.status.phase}' 2>/dev/null)
      case "$phase" in Succeeded|Failed) break ;; esac
      sleep 1
    done
    out=$(K -n "$NS" logs "$pod" 2>> "$err")
    K -n "$NS" delete pod "$pod" --wait=false > /dev/null 2>&1
  else
    out=""
  fi
  CODE=""
  [[ $out =~ ([0-9][0-9][0-9]) ]] && CODE="${BASH_REMATCH[1]}"
  if [ -z "${CODE:-}" ]; then
    { printf 'kubectl run exited %s and the pod log held no 3-digit status.\n' "$rc"
      printf 'pod phase reached: %s\n' "${phase:-<never read>}"
      printf 'kubectl stderr: %s\n' "$(tr -d '\n' < "$err" | cut -c1-400)"
    } >&2
  fi
  rm -f "$err"
  [ -n "${CODE:-}" ] && [ "$CODE" != "000" ]
}

# controller_logs -- the controller Deployment is found BY NAME, the way the
# sibling probe does it. There is no `app.kubernetes.io/name=rest-dynamic-controller`
# label on these Deployments: oasgen-provider names one per Kind
# (`<release>-volume-attachment-controller`). A label selector that matches
# nothing prints silence, and silence here is indistinguishable from "the
# controller logged nothing" -- so when no Deployment matches, this says so and
# lists what IS in the namespace.
controller_logs() {
  local dep
  dep=$(K -n "$NS" get deploy -o name 2>/dev/null | grep -m1 'volume-attachment-controller')
  if [ -z "${dep:-}" ]; then
    printf '    no volume-attachment-controller Deployment exists. The namespace holds:\n'
    K -n "$NS" get deploy -o name 2>/dev/null | sed 's/^/      /' || printf '      (could not list Deployments)\n'
    return 0
  fi
  printf '    logs from %s:\n' "$dep"
  K -n "$NS" logs "$dep" --tail=40 2>/dev/null | sed 's/^/      /' \
    || printf '      (the Deployment exists but its logs could not be read)\n'
}

# assert_single_owner -- a second helm release in this namespace would create a
# SECOND RestDefinition for this Kind, and then a request in the mock's log
# cannot be attributed to the controller this probe installed. Two readings,
# because they fail differently: the release count is the cause, the
# per-Kind RestDefinition count is the effect.
assert_single_owner() {
  local releases defs
  releases=$(helm --kubeconfig "$KUBECONFIG_PATH" --kube-context "$CONTEXT" \
    list -n "$NS" -q 2>/dev/null | grep -c . || true)
  defs=$(K -n "$NS" get restdefinition -o jsonpath='{range .items[*]}{.spec.resource.kind}{"\n"}{end}' 2>/dev/null \
    | grep -c '^ComputeVolumeAttachment$' || true)
  printf '    helm releases in %s: %s   RestDefinitions for ComputeVolumeAttachment: %s\n' \
    "$NS" "$releases" "$defs"
  if [ "${defs:-0}" -gt 1 ]; then
    void "$defs RestDefinitions define ComputeVolumeAttachment in $NS ($1). A request
    in the mock's log could come from either controller, so arm B would be
    unattributable. Nothing was measured."
  fi
  [ "${defs:-0}" -eq 1 ] || return 0
}

say "mock behaviour control -- asserted, not assumed"
# shellcheck disable=SC2329  # invoked indirectly, as wait_for's "$@"
_mock_answers() { curl_through "/v2.1/${PROJ}/servers/${SRV}/os-volume_attachments/${VOL}"; }
wait_for "the mock to return any HTTP status to an in-cluster request" 15 6 _mock_answers \
  || void "the mock never returned an HTTP status within 90s. Service endpoints
    = $(endpoints_of); the predicate's last stderr is above and says whether
    kubectl could run the pod at all. Connectivity, not a measurement."
[ "$CODE" = "200" ] || void "the mock answered $CODE on the attachment get path, expected 200.
  Every verdict below would be a measurement of the wrong thing."
ok "the mock answers 200 on the attachment path, and its log now holds this probe's own request"

say "installing the chart with volume_attachment ENABLED"
CHART_DIR=$(mktemp -d)/chart
cp -r chart "$CHART_DIR"
# helm refuses the placeholder, so it is stamped first -- the same staging the
# smoke workflow does.
python3 - "$CHART_DIR" <<'PY'
import io, sys
p = sys.argv[1] + "/Chart.yaml"
s = io.open(p, encoding="utf-8").read().replace("version: CHART_VERSION", "version: 0.0.0-probe")
io.open(p, "w", encoding="utf-8").write(s)
PY
helm --kubeconfig "$KUBECONFIG_PATH" --kube-context "$CONTEXT" \
  upgrade --install nova-vam "$CHART_DIR" -n "$NS" \
  --set nova.url="http://vam-mock.${NS}.svc.cluster.local:8080/v2.1/${PROJ}" \
  --set restdefinitions.volume_attachment.enabled=true \
  --wait --timeout 8m > /tmp/probe-vam-helm.log 2>&1 \
  || { sed 's/^/    /' /tmp/probe-vam-helm.log >&2
       void "helm could not install the chart with the kind enabled. No arm ran."; }
assert_single_owner "after installing with the kind enabled"
ok "chart installed, volume_attachment enabled"

# shellcheck disable=SC2329  # invoked indirectly, as wait_for's "$@"
_crd_ready() { K get crd "${PLURAL}.${GROUP}" > /dev/null 2>&1; }
wait_for "the generated CRD ${PLURAL}.${GROUP} to appear" 40 6 _crd_ready \
  || void "oasgen-provider never generated ${PLURAL}.${GROUP} within 240s, so neither
    arm has anything to read. The RestDefinition's own status follows.
    $(K -n "$NS" get restdefinition nova-vam-volume-attachment -o jsonpath='{.status}' 2>/dev/null)"
ok "the generated CRD exists"

say "ARM A -- does spec.volumeAttachment.volumeId survive #419's suppression?"
crd=$(K get crd "${PLURAL}.${GROUP}" -o json 2>/dev/null)
bytes=$(printf '%s' "$crd" | wc -c | tr -d ' ')
[ "${bytes:-0}" -gt 500 ] || void "the CRD read back only ${bytes} bytes, so every
  property check below would be a check of nothing."
ARM_A=$(printf '%s' "$crd" | python3 -c '
import json, sys
crd = json.load(sys.stdin)
vers = crd["spec"]["versions"]
served = [v["name"] for v in vers if v.get("served")]
out = {"served": served}
for v in vers:
    props = (v.get("schema", {}).get("openAPIV3Schema", {})
              .get("properties", {}).get("spec", {}).get("properties", {}) or {})
    out.setdefault("spec_props", {})[v["name"]] = sorted(props)
    va = (props.get("volumeAttachment", {}).get("properties") or {})
    out.setdefault("va_props", {})[v["name"]] = sorted(va)
print(json.dumps(out))
' 2>/dev/null)
[ -n "${ARM_A:-}" ] || void "could not parse the CRD's schema; arm A measured nothing."
printf '    served versions : %s\n' "$(printf '%s' "$ARM_A" | python3 -c 'import json,sys; print(json.load(sys.stdin)["served"])')"
printf '    spec properties : %s\n' "$(printf '%s' "$ARM_A" | python3 -c 'import json,sys; print(json.load(sys.stdin)["spec_props"])')"
printf '    volumeAttachment: %s\n' "$(printf '%s' "$ARM_A" | python3 -c 'import json,sys; print(json.load(sys.stdin)["va_props"])')"

A_VERDICT=$(printf '%s' "$ARM_A" | python3 -c '
import json, sys
d = json.load(sys.stdin)
# CONTROL: the spec must carry SOMETHING, or "volumeId absent" is "schema empty".
any_props = any(v for v in d["spec_props"].values())
has_vol = any("volumeId" in v for v in d["va_props"].values())
has_srv = any("server_id" in v for v in d["spec_props"].values())
if not any_props:
    print("void")
elif has_vol and has_srv:
    print("both")
elif has_vol:
    print("vol-only")
elif has_srv:
    print("srv-only")
else:
    print("neither")
')
case "$A_VERDICT" in
  void)
    void "the CRD's spec has NO properties at all, so 'volumeId absent' would be
      'the schema is empty'. Nothing is concluded about the suppression." ;;
  both)
    ok "ARM A = BOTH PRESENT. spec.volumeAttachment.volumeId survived, and the
    self-referential spec.server_id was created by the path parameter as #421
    intends. The dotted spec target is not suppressed." ;;
  vol-only)
    bad "ARM A = spec.volumeAttachment.volumeId is present but spec.server_id is
    NOT. The dotted target survived and the self-referential one did not, which
    is the opposite of what #421 promises and needs reporting upstream." ;;
  srv-only)
    bad "ARM A = spec.server_id is present but spec.volumeAttachment.volumeId is
    GONE. #419's suppression reaches a DOTTED spec target as well, so the get and
    delete verbs read a property the CRD does not declare. This kind cannot be
    enabled as shaped: the volume id must move to a bare, self-referentially
    mapped spec property." ;;
  *)
    bad "ARM A = NEITHER property is present, although the spec carries others.
    Both mappings are being suppressed; the kind cannot be enabled as shaped." ;;
esac

say "ARM B -- is {volume_id} substituted on the wire, or left literal?"
PROBE_REQS=$(records | awk -v ua="$PROBE_UA" '$4 == ua' | grep -c . || true)
if [ "${PROBE_REQS:-0}" -lt 1 ]; then
  void "the mock's log holds 0 requests from this probe's own User-Agent, although
    the behaviour control above made one. Either the log rotated or the parser is
    not matching it -- and with no probe traffic present, separating controller
    requests from mine is untested, so any path read below is unattributable."
fi
ok "the log holds $PROBE_REQS request(s) from this probe, so excluding them is a filter that is exercised"

# The CR's apiVersion is READ from the CRD rather than assumed: this chart's two
# halves are served at DIFFERENT versions (v2-1 for the resource, v1alpha1 for
# the Configuration) and nothing warns until apply.
RES_VER=$(printf '%s' "$crd" | python3 -c '
import json,sys
vs=[v["name"] for v in json.load(sys.stdin)["spec"]["versions"] if v.get("served")]
print(vs[0] if vs else "")')
CFG_CRD=computevolumeattachmentconfigurations.${GROUP}
CFG_VER=$(K get crd "$CFG_CRD" -o json 2>/dev/null | python3 -c '
import json,sys
try:
    vs=[v["name"] for v in json.load(sys.stdin)["spec"]["versions"] if v.get("served")]
    print(vs[0] if vs else "")
except Exception:
    print("")')
printf '    resource apiVersion      : %s/%s\n' "$GROUP" "${RES_VER:-<unread>}"
printf '    configuration apiVersion : %s/%s\n' "$GROUP" "${CFG_VER:-<unread>}"
[ -n "${RES_VER:-}" ] && [ -n "${CFG_VER:-}" ] \
  || void "could not read a served version for both halves of the pair
    (resource='${RES_VER:-}' configuration='${CFG_VER:-}'). Applying a CR with a
    guessed apiVersion is how this repo has produced green applies that did
    nothing."

K -n "$NS" create secret generic probe-vam-token \
  --from-literal=token=probe-not-a-real-token --dry-run=client -o yaml | K apply -f - > /dev/null \
  || void "could not create the probe's token Secret."

man=$(mktemp)
cat > "$man" <<EOF
apiVersion: ${GROUP}/${CFG_VER}
kind: ComputeVolumeAttachmentConfiguration
metadata: { name: ${CFG}, namespace: ${NS} }
spec:
  authentication:
    apiKey:
      tokenRef: { name: probe-vam-token, namespace: ${NS}, key: token }
      header: X-Auth-Token
---
apiVersion: ${GROUP}/${RES_VER}
kind: ComputeVolumeAttachment
metadata: { name: probe-vam, namespace: ${NS} }
spec:
  configurationRef: { name: ${CFG}, namespace: ${NS} }
  server_id: ${SRV}
  volumeAttachment:
    volumeId: ${VOL}
EOF
if ! K -n "$NS" apply -f "$man" > /tmp/probe-vam-apply.log 2>&1; then
  sed 's/^/    /' /tmp/probe-vam-apply.log >&2
  echo "    --- the manifest it refused ---" >&2
  sed 's/^/    /' "$man" >&2
  rm -f "$man"
  void "the probe's own CRs did not apply, so arm B never ran. If the rejection
    names an unknown field, that is arm A's finding arriving through a different
    door and should be read with it."
fi
rm -f "$man"
ok "the attachment CR applied at ${GROUP}/${RES_VER}"

# shellcheck disable=SC2329  # invoked indirectly, as wait_for's "$@"
_ctl_hit() { [ "$(records | awk -v ua="$PROBE_UA" '$4 != ua' | grep -c . || true)" -ge 1 ]; }
if ! wait_for "the controller to send its first request to the mock" 40 6 _ctl_hit; then
  bad "ARM B = NO CONTROLLER REQUEST within 240s. The CR applied and the mock is
    reachable (this probe's own request is in the same log), so the controller
    either never reconciled or failed before the request. That is UNMEASURED
    rather than 'left literal' -- the controller log follows."
  controller_logs
  ARM_B=unmeasured
else
  printf '    %-7s %-62s %s\n' METHOD PATH STATUS
  records | awk -v ua="$PROBE_UA" '$4 != ua { printf "    %-7s %-62s %s\n", $1, $2, $3 }' | sort -u
  CTL_PATHS=$(records | awk -v ua="$PROBE_UA" '$4 != ua { print $2 }')
  LITERALS=$(literal_count "$CTL_PATHS")
  SUBBED=$(printf '%s\n' "$CTL_PATHS" | grep -c "os-volume_attachments" || true)
  WITH_IDS=$(printf '%s\n' "$CTL_PATHS" | grep -c "${SRV}" || true)
  printf '    controller requests: %s   carrying %s: %s   literal templates: %s\n' \
    "$SUBBED" "$SRV" "$WITH_IDS" "$LITERALS"
  if [ "${LITERALS:-0}" -gt 0 ]; then
    ARM_B=literal
    bad "ARM B = TEMPLATE LEFT LITERAL. $LITERALS request path(s) still carry an
    unsubstituted {name} template, so the mapping did not substitute. The kind
    cannot be enabled as shaped."
  elif [ "${WITH_IDS:-0}" -lt 1 ]; then
    ARM_B=unmeasured
    bad "ARM B = the controller sent $SUBBED attachment request(s) but none carries
    '${SRV}'. Neither substituted nor literal, so nothing is concluded; read the
    path table above."
  else
    ARM_B=substituted
    ok "ARM B = SUBSTITUTED. $WITH_IDS request path(s) carry the real ids and 0
    carry a literal template, measured on the wire against a request-logging
    stand-in -- the same instrument that settled rd-quota.yaml."
  fi
fi

say "VERDICT"
printf '    ARM A (spec.volumeAttachment.volumeId survives) %s\n' "$A_VERDICT"
printf '    ARM B ({volume_id} substituted on the wire)     %s\n' "${ARM_B:-unmeasured}"
cat <<'NOTE'

    WHAT A PASS IS WORTH. Both arms passing means the two things
    docs/configuration.md names as gating `volume_attachment.enabled: true` have
    been measured once, on one cluster, at the provider version this run used.
    It does not make the kind exercised: no real Cinder volume was attached, and
    the mock answers 200 unconditionally.

    CLEANUP IS NOT AUTOMATIC. The CR, the Configuration, the Secret, the mock and
    the `nova-vam` release are left in place so a failure can be read afterwards.
NOTE

if [ "$A_VERDICT" = "both" ] && [ "${ARM_B:-}" = "substituted" ]; then
  ok "BOTH ARMS PASS: the two conditions gating volume_attachment.enabled are met."
  exit 0
fi
exit 1
