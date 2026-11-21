#!/usr/bin/env bash
# Does a generated controller report NotReady when its Lease is unreachable?
#
# THIS ARM EXISTS BECAUSE NOTHING ANYWHERE COVERS IT, and the request came from
# the oasgen-provider session with the reason stated plainly: their readiness
# listener (#815) has four Ready-side arms measured on a real fleet and NO
# NEGATIVE CONTROL. A replica waiting for a peer's Lease is READY -- only a Lease
# that cannot be read or written is NotReady -- and FOUR of their documentation
# surfaces asserted the inverse, including values.schema.json, which is what an
# operator reads. The code was always right. The behaviour is correct from
# oasgen 0.85.0; the shipped wording stays inverted until 0.86.0.
#
# So the arm asserts THE CODE'S behaviour, never the schema text at the tag
# under test, and it is built so that it can fail.
#
# TWO INDEPENDENT READINGS, deliberately not one job:
#
#   READING 1  the readiness question, on a MINIMAL planted RestDefinition.
#              Nothing about this chart's auth can break it.
#   READING 2  this chart installed at the same provider version -- do its
#              generated controllers come up, and did the provider record an
#              apiKey secret grant? That is the apiKey-RBAC safe-set reading
#              (docs/configuration.md), and it is reported SEPARATELY so that a
#              chart failure cannot destroy reading 1.
#
# TWO CHANNELS, because they move on timescales two orders of magnitude apart --
# measured from the provider's own chart and runtime by the oasgen session:
#
#   /readyz          503 within ~2s of the Lease becoming unreachable, because
#                    the ownership loop retries every RetryPeriod (2s at
#                    v1.28.1) and `set()` assigns lastErr UNCONDITIONALLY.
#   pod Ready        False only after periodSeconds x failureThreshold, which is
#                    10 x 30 = 300s in the shipped chart.
#
# A ONE-MINUTE SAMPLE OF POD-READY WOULD THEREFORE RECORD A FALSE NEGATIVE on a
# probe that is working perfectly. So the window is DERIVED FROM THE DEPLOYED
# SPEC rather than hardcoded: those two numbers live in a chart that moves
# independently of the code, and a probe that hardcodes someone else's constant
# measures the past.
#
# Exit codes: 0 reading 1 confirmed, 1 a real finding, 3 VOID (nothing measured).
set -uo pipefail
# LC_ALL=C: this script does arithmetic on command output (the derived window,
# status codes, grep counts) and uses bracket ranges, both of which are
# locale-sensitive.
export LC_ALL=C

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m  OK: %s\033[0m\n' "$*"; }
bad()  { printf '\033[31m  BAD: %s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mFAIL: %s\033[0m\n' "$*" >&2; exit 1; }
void() { printf '\n\033[33mVOID: %s\033[0m\n' "$*" >&2; exit 3; }

# ---------------------------------------------------------------------------
# PURE FUNCTIONS. Every one of these decides a verdict, so each is tested below
# in BOTH directions -- a parser that cannot say "no" cannot say "yes" either.
# ---------------------------------------------------------------------------

# derived_window <periodSeconds> <failureThreshold> -- seconds before the kubelet
# flips Ready to False, plus slack. Empty inputs mean the field was absent from
# the deployed spec, and Kubernetes' own defaults then apply (10 and 3): that is
# a different number, and saying which was used is the difference between a
# derived window and a guess.
derived_window() {
  local p="${1:-}" f="${2:-}"
  [ -n "$p" ] || p=10
  [ -n "$f" ] || f=3
  printf '%d' $(( p * f ))
}

# leases_rule_index <role json> -- the index of the rule granting READ/WRITE on
# coordination.k8s.io/leases, or empty.
#
# IT MUST BE THE get/update RULE, NOT MERELY THE FIRST LEASES RULE, and that
# distinction was a real bug here. The provider's generated Role carries TWO
# leases rules, verified in its own asset at 0.84.0:
#
#   verbs: ["create"]                                        <- no resourceNames
#   verbs: ["get","update"]  resourceNames: [<this kind>]    <- scoped
#
# The split is deliberate: `resourceNames` cannot scope a `create`, so a create
# rule carrying one grants nothing and the controller blocks forever with every
# health signal green. For THIS arm the consequence is sharper -- stripping the
# `create` rule leaves an EXISTING Lease readable and renewable, so a controller
# that already holds one keeps working and the arm VOIDs on the can-i check.
# Correct, but a wasted dispatch. The get/update rule is the surgical target.
leases_rule_index() {
  printf '%s' "$1" | python3 -c '
import json, sys
try:
    role = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for i, r in enumerate(role.get("rules") or []):
    groups = r.get("apiGroups") or []
    res = r.get("resources") or []
    verbs = [v.lower() for v in (r.get("verbs") or [])]
    if "coordination.k8s.io" not in groups:
        continue
    if not ("leases" in res or "*" in res):
        continue
    # read or write, never create-only: create alone cannot read a Lease.
    if "get" in verbs or "update" in verbs or "*" in verbs:
        print(i)
        break
' 2>/dev/null
}

# cani_verdict <kubectl auth can-i output> -- yes | no | unreadable.
# `can-i` prints "yes"/"no" on stdout and exits 0/1, and an RBAC lookup that
# ERRORS also prints nothing: three outcomes, not two.
cani_verdict() {
  case "$(printf '%s' "${1:-}" | tr -d '[:space:]')" in
    yes) printf 'yes' ;;
    no)  printf 'no' ;;
    *)   printf 'unreadable' ;;
  esac
}

# readyz_class <http status> -- ready | notready | unreachable.
readyz_class() {
  case "${1:-}" in
    200)          printf 'ready' ;;
    500|503)      printf 'notready' ;;
    ""|000)       printf 'unreachable' ;;
    *)            printf 'unreachable' ;;
  esac
}

self_test() {
  local pass=0 fail_n=0
  t() {
    if [ "$2" = "$3" ]; then printf '  %-62s PASS\n' "$1"; pass=$((pass+1))
    else printf '  %-62s FAIL want=%s got=%s\n' "$1" "$2" "$3"; fail_n=$((fail_n+1)); fi
  }
  for fn in derived_window leases_rule_index cani_verdict readyz_class; do
    command -v "$fn" > /dev/null 2>&1 \
      || fail "$fn is not defined, so every arm below would compare empty strings
    and pass. That is the vacuous-arm failure this repo has shipped before."
  done

  # THE WINDOW. The shipped chart's 10 x 30 must give 300, and the Kubernetes
  # defaults must give 30 -- a tenfold difference, which is exactly why this is
  # derived and not typed.
  t "the shipped chart's probe gives a 300s window" 300 "$(derived_window 10 30)"
  t "absent fields fall back to k8s defaults, NOT to 300" 30 "$(derived_window '' '')"
  t "a partial spec uses the default for the missing half" 50 "$(derived_window 10 5)"

  # THE LEASES RULE FINDER, both directions.
  local with_leases without_leases second_rule star_role
  with_leases='{"rules":[{"apiGroups":["coordination.k8s.io"],"resources":["leases"],"verbs":["get"]}]}'
  without_leases='{"rules":[{"apiGroups":[""],"resources":["secrets"],"verbs":["get"]}]}'
  second_rule='{"rules":[{"apiGroups":[""],"resources":["secrets"],"verbs":["get"]},{"apiGroups":["coordination.k8s.io"],"resources":["leases"],"verbs":["*"]}]}'
  star_role='{"rules":[{"apiGroups":["coordination.k8s.io"],"resources":["*"],"verbs":["*"]}]}'
  t "a leases rule is found at index 0" 0 "$(leases_rule_index "$with_leases")"
  t "a Role without leases yields NO index" "" "$(leases_rule_index "$without_leases")"
  t "a leases rule at index 1 is found as 1" 1 "$(leases_rule_index "$second_rule")"
  t "a wildcard resources rule counts as granting leases" 0 "$(leases_rule_index "$star_role")"
  t "malformed JSON yields no index rather than crashing" "" "$(leases_rule_index 'not json')"

  # THE REAL CLUSTER SHAPE: the generated Role carries BOTH leases rules, create
  # first. Picking the first match returns the create rule, whose removal leaves
  # an existing Lease readable -- so this arm must prove the function skips it.
  local real_role create_only
  real_role='{"rules":[{"apiGroups":["coordination.k8s.io"],"resources":["leases"],"verbs":["create"]},{"apiGroups":["coordination.k8s.io"],"resources":["leases"],"resourceNames":["things-x-v1"],"verbs":["get","update"]}]}'
  create_only='{"rules":[{"apiGroups":["coordination.k8s.io"],"resources":["leases"],"verbs":["create"]}]}'
  t "with BOTH rules it picks get/update (index 1), not create (0)" 1 \
    "$(leases_rule_index "$real_role")"
  t "a create-ONLY role yields no index: removing it reads nothing" "" \
    "$(leases_rule_index "$create_only")"

  # CAN-I, three outcomes. The third is the one that matters: an RBAC lookup
  # that errors must not read as "no", or a failed mutation check would look
  # like a successful strip.
  t "can-i yes reads as yes" yes "$(cani_verdict 'yes')"
  t "can-i no reads as no" no "$(cani_verdict 'no')"
  t "can-i with trailing newline still reads" no "$(cani_verdict 'no
')"
  t "empty can-i output is UNREADABLE, not no" unreadable "$(cani_verdict '')"
  t "an error string is UNREADABLE, not no" unreadable "$(cani_verdict 'Error from server (Forbidden)')"

  # READYZ, three outcomes. An unreachable endpoint must never read as NotReady:
  # that is the difference between "the listener says not ready" and "there is
  # no listener", which is the whole reason this arm has a precondition.
  t "200 is ready" ready "$(readyz_class 200)"
  t "503 is notready" notready "$(readyz_class 503)"
  t "500 is notready" notready "$(readyz_class 500)"
  t "an empty status is UNREACHABLE, not notready" unreachable "$(readyz_class '')"
  t "000 is UNREACHABLE, not notready" unreachable "$(readyz_class 000)"
  t "404 is UNREACHABLE, not notready" unreachable "$(readyz_class 404)"

  printf '\n  %d passed, %d failed\n' "$pass" "$fail_n"
  [ "$fail_n" -eq 0 ] || fail "a verdict function is wrong, so no cluster reading would be readable"
  ok "the window derivation and all three verdict parsers discriminate in both directions"
}

[ "${1:-}" = "--self-test" ] && { say "verdict functions (no cluster)"; self_test; exit 0; }

KUBECONFIG_PATH="${1:?usage: probe-lease-readiness.sh <kubeconfig> <context> [namespace] | --self-test}"
CONTEXT="${2:?usage: probe-lease-readiness.sh <kubeconfig> <context> [namespace] | --self-test}"
NS="${3:-krateo-system}"
K() { kubectl --kubeconfig "$KUBECONFIG_PATH" --context "$CONTEXT" "$@"; }

# shellcheck source=hack/lib/wait.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/wait.sh"
command -v wait_for > /dev/null \
  || fail "hack/lib/wait.sh was sourced but defined nothing; every retry below
    would read as a failure of the thing being measured."

say "verdict functions, before touching the cluster"
self_test

say "preconditions -- each says what its failure means"
K get --raw /readyz > /dev/null 2>&1 \
  || void "cannot reach the API server (kubeconfig=$KUBECONFIG_PATH context=$CONTEXT)."
K get crd restdefinitions.ogen.krateo.io > /dev/null 2>&1 \
  || void "restdefinitions.ogen.krateo.io is absent, so oasgen-provider is not installed."
ok "API reachable, oasgen-provider present"

# TWO DIFFERENT apiGroups, NOT two kinds in one group. The provider names the
# generated Role `{resource}-{apiGroup}-{apiVersion}`, so two RestDefinitions
# sharing all three share one Role -- and stripping it would disable the control
# along with the subject. Distinct kinds alone would probably differ in
# `{resource}`, which is luck rather than design; distinct groups cannot collide.
SUBJ_GROUP=leasesubject.krateo.io
CTRL_GROUP=leasecontrol.krateo.io
SUBJ=lease-subject
CTRL=lease-control
PROBE_UA=probe-lease-control

# fragment -- the smallest OAS that makes oasgen generate a controller. Minimal
# ON PURPOSE: reading 1 must not be able to fail because of anything specific to
# this chart's documents, so nothing here resembles them.
fragment() {
  cat <<'OAS'
openapi: 3.0.3
info:
  title: lease readiness probe
  version: "1"
servers:
  - url: http://unused.invalid/v1
paths:
  /things:
    post:
      operationId: createThing
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/ThingCreate'
      responses:
        '200':
          description: made
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ThingEnvelope'
  /things/{id}:
    get:
      operationId: getThing
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: string
      responses:
        '200':
          description: it
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ThingEnvelope'
    delete:
      operationId: deleteThing
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: string
      responses:
        '204':
          description: gone
components:
  securitySchemes:
    apiKey:
      type: apiKey
      in: header
      name: X-Auth-Token
  schemas:
    ThingSpec:
      type: object
      properties:
        name:
          type: string
    ThingCreate:
      type: object
      properties:
        thing:
          $ref: '#/components/schemas/ThingSpec'
    ThingSummary:
      type: object
      properties:
        id:
          type: string
    ThingEnvelope:
      type: object
      properties:
        thing:
          $ref: '#/components/schemas/ThingSummary'
OAS
}

# plant <rd-name> <Kind> <apiGroup>
plant() {
  local f; f=$(mktemp)
  fragment > "$f"
  K -n "$NS" create configmap "$1-oas" --from-file=oas.yaml="$f" \
    --dry-run=client -o yaml | K apply -f - > /dev/null \
    || void "could not create the ConfigMap for $1."
  rm -f "$f"
  K apply -f - > /dev/null <<EOF || void "could not apply RestDefinition $1."
apiVersion: ogen.krateo.io/v1alpha1
kind: RestDefinition
metadata: { name: $1, namespace: ${NS} }
spec:
  oasPath: configmap://${NS}/$1-oas/oas.yaml
  resourceGroup: $3
  resource:
    kind: $2
    identifiers:
      - thing.id
    verbsDescription:
      - action: create
        method: POST
        path: /things
      - action: get
        method: GET
        path: /things/{id}
        requestFieldMapping:
          - inPath: id
            inCustomResource: status.thing.id
      - action: delete
        method: DELETE
        path: /things/{id}
        requestFieldMapping:
          - inPath: id
            inCustomResource: status.thing.id
EOF
}

# deploy_for <rd-name> -- the generated controller Deployment, found BY NAME.
# There is no rest-dynamic-controller label on these Deployments; a label
# selector that matches nothing prints silence, and silence here is
# indistinguishable from "the controller is not there".
deploy_for() {
  K -n "$NS" get deploy -o name 2>/dev/null | grep -m1 "$1" || true
}

say "planting two minimal RestDefinitions: one SUBJECT, one CONTROL"
plant "$SUBJ" LeaseProbeSubject "$SUBJ_GROUP"
plant "$CTRL" LeaseProbeControl "$CTRL_GROUP"
# shellcheck disable=SC2329  # invoked indirectly, as wait_for's "$@"
_both_deploys() { [ -n "$(deploy_for "$SUBJ")" ] && [ -n "$(deploy_for "$CTRL")" ]; }
wait_for "both generated controller Deployments to appear" 40 6 _both_deploys \
  || void "after 240s there is no Deployment for one or both planted definitions.
    subject='$(deploy_for "$SUBJ")' control='$(deploy_for "$CTRL")'.
    Without two controllers there is no control, and an arm that cannot be
    contradicted is not worth running."
SUBJ_DEP=$(deploy_for "$SUBJ"); CTRL_DEP=$(deploy_for "$CTRL")
ok "subject=${SUBJ_DEP} control=${CTRL_DEP}"

say "PRECONDITION -- is the readiness listener wired at this provider version?"
# This must fail DIFFERENTLY from the subject. If the listener is absent,
# /readyz returns nothing, and "no 503" would otherwise be reported as "readiness
# did not fire" -- a finding against a feature that is simply not present.
# Measured: oasgen 0.84.0 ships NO rdc readinessProbe at all (the key does not
# exist in values.yaml); 0.85.0 is the first version that wires it.
PROBE_JSON=$(K -n "$NS" get "$SUBJ_DEP" -o json 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
c = (d["spec"]["template"]["spec"]["containers"] or [{}])[0]
rp = c.get("readinessProbe") or {}
hg = rp.get("httpGet") or {}
print(json.dumps({"path": hg.get("path", ""), "port": hg.get("port", ""),
                  "period": rp.get("periodSeconds", ""),
                  "threshold": rp.get("failureThreshold", ""),
                  "has": bool(rp)}))
' 2>/dev/null)
[ -n "${PROBE_JSON:-}" ] || void "could not read the subject Deployment's spec at all."
RZ_PATH=$(printf '%s' "$PROBE_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["path"])')
RZ_PORT=$(printf '%s' "$PROBE_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["port"])')
RZ_PERIOD=$(printf '%s' "$PROBE_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["period"])')
RZ_THRESH=$(printf '%s' "$PROBE_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["threshold"])')
printf '    readinessProbe: path=%s port=%s periodSeconds=%s failureThreshold=%s\n' \
  "${RZ_PATH:-<none>}" "${RZ_PORT:-<none>}" "${RZ_PERIOD:-<default>}" "${RZ_THRESH:-<default>}"
if [ -z "${RZ_PATH:-}" ] || [ -z "${RZ_PORT:-}" ]; then
  void "the generated controller declares NO readinessProbe httpGet at this
    provider version, so there is no listener to read. Measured at oasgen
    0.84.0: the rdc readinessProbe key does not exist in values.yaml at all, and
    0.85.0 is the first version that wires it. This is NOT a finding about
    readiness -- it is the absence of the feature, and the two must never be
    reported as the same thing."
fi
WINDOW=$(derived_window "$RZ_PERIOD" "$RZ_THRESH")
ok "the listener is wired; the pod-Ready window DERIVES to ${WINDOW}s (not hardcoded)"

sa_of() {
  K -n "$NS" get "$1" -o jsonpath='{.spec.template.spec.serviceAccountName}' 2>/dev/null
}

# roles_with_leases <sa> -- every Role/ClusterRole bound to this ServiceAccount
# that grants coordination.k8s.io/leases, as "kind/name" lines.
roles_with_leases() {
  local sa="$1"
  K get rolebindings,clusterrolebindings -A -o json 2>/dev/null | python3 -c '
import json, sys
sa = sys.argv[1]; ns = sys.argv[2]
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for item in data.get("items", []):
    subs = item.get("subjects") or []
    if not any(s.get("kind") == "ServiceAccount" and s.get("name") == sa
               and s.get("namespace", ns) == ns for s in subs):
        continue
    r = item.get("roleRef") or {}
    print("%s/%s" % (r.get("kind", ""), r.get("name", "")))
' "$sa" "$NS" 2>/dev/null | sort -u
}

role_json() {
  case "$1" in
    ClusterRole/*) K get clusterrole "${1#ClusterRole/}" -o json 2>/dev/null ;;
    Role/*)        K -n "$NS" get role "${1#Role/}" -o json 2>/dev/null ;;
    *)             printf '' ;;
  esac
}

can_i_leases() {
  cani_verdict "$(K auth can-i get leases.coordination.k8s.io -n "$NS" \
    --as="system:serviceaccount:${NS}:$1" 2>/dev/null)"
}

# readyz_of <deploy> -- the pod's own /readyz, read from a curl pod against the
# pod IP. The POD LOG is read, never an attach: a curl that finishes before the
# attach is established writes into a stream nobody reads, and retrying a race
# does not change the odds.
readyz_of() {
  local ip pod out rc err phase i
  ip=$(K -n "$NS" get pod -l "$(K -n "$NS" get "$1" -o jsonpath='{.spec.selector.matchLabels}' 2>/dev/null \
        | python3 -c 'import json,sys; d=json.load(sys.stdin); print(",".join("%s=%s"%kv for kv in d.items()))' 2>/dev/null)" \
        -o jsonpath='{.items[0].status.podIP}' 2>/dev/null)
  if [ -z "${ip:-}" ]; then printf ''; return 1; fi
  err=$(mktemp); pod="probe-rz-$RANDOM"
  K -n "$NS" run "$pod" --restart=Never --attach=false --quiet \
      --image=curlimages/curl:8.11.0 -- \
      -s -o /dev/null -w '%{http_code}' -A "$PROBE_UA" \
      "http://${ip}:${RZ_PORT}${RZ_PATH}" > /dev/null 2> "$err"
  rc=$?
  if [ "$rc" -eq 0 ]; then
    for i in $(seq 1 20); do
      phase=$(K -n "$NS" get pod "$pod" -o jsonpath='{.status.phase}' 2>/dev/null)
      case "$phase" in Succeeded|Failed) break ;; esac
      sleep 1
    done
    out=$(K -n "$NS" logs "$pod" 2>> "$err")
    K -n "$NS" delete pod "$pod" --wait=false > /dev/null 2>&1
  else out=""; fi
  rm -f "$err"
  local code=""
  [[ $out =~ ([0-9][0-9][0-9]) ]] && code="${BASH_REMATCH[1]}"
  printf '%s' "$code"
}

pod_ready_of() {
  K -n "$NS" get pod -l "$(K -n "$NS" get "$1" -o jsonpath='{.spec.selector.matchLabels}' 2>/dev/null \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(",".join("%s=%s"%kv for kv in d.items()))' 2>/dev/null)" \
    -o jsonpath='{.items[0].status.conditions[?(@.type=="Ready")].status}' 2>/dev/null
}

say "PRECONDITION -- can the Lease grant be removed from the SUBJECT ALONE?"
SUBJ_SA=$(sa_of "$SUBJ_DEP"); CTRL_SA=$(sa_of "$CTRL_DEP")
printf '    subject SA=%s   control SA=%s\n' "${SUBJ_SA:-<unread>}" "${CTRL_SA:-<unread>}"
[ -n "${SUBJ_SA:-}" ] && [ -n "${CTRL_SA:-}" ] \
  || void "could not read one or both ServiceAccount names, so nothing can be
    stripped and nothing would be measured."
if [ "$SUBJ_SA" = "$CTRL_SA" ]; then
  void "both controllers share the ServiceAccount '$SUBJ_SA'. Stripping its Lease
    grant would break the CONTROL as well, and an arm whose control fails with
    its subject measures nothing. Not a finding about readiness."
fi
mapfile -t SUBJ_ROLES < <(roles_with_leases "$SUBJ_SA")
mapfile -t CTRL_ROLES < <(roles_with_leases "$CTRL_SA")
printf '    roles bound to the subject: %s\n' "${SUBJ_ROLES[*]:-<none>}"
printf '    roles bound to the control: %s\n' "${CTRL_ROLES[*]:-<none>}"
TARGET=""
for r in "${SUBJ_ROLES[@]:-}"; do
  [ -n "$r" ] || continue
  idx=$(leases_rule_index "$(role_json "$r")")
  [ -n "${idx:-}" ] || continue
  shared=no
  for c in "${CTRL_ROLES[@]:-}"; do [ "$c" = "$r" ] && shared=yes; done
  if [ "$shared" = yes ]; then
    void "the only Lease-granting role bound to the subject ($r) is ALSO bound to
      the control, so removing that rule would disable both. The control exists
      precisely so that 'the subject went NotReady' can be told from 'something
      broke the cluster' -- without it this run would be unreadable."
  fi
  TARGET="$r"; TARGET_IDX="$idx"; break
done
[ -n "${TARGET:-}" ] \
  || void "no Lease-granting Role is bound to the subject's ServiceAccount, so
    there is nothing to remove. Either the generated controller does not take a
    Lease at this version, or it reads one it was never granted -- both are
    interesting, and neither is the measurement this arm claims to make."
ok "will remove rule ${TARGET_IDX} of ${TARGET}, bound to the subject only"

say "BASELINE -- both controllers healthy before anything is broken"
# shellcheck disable=SC2329  # invoked indirectly, as wait_for's "$@"
_both_ready() { [ "$(pod_ready_of "$SUBJ_DEP")" = "True" ] && [ "$(pod_ready_of "$CTRL_DEP")" = "True" ]; }
wait_for "both controller pods to report Ready=True" 40 6 _both_ready \
  || void "the controllers never both became Ready, so there is no healthy
    baseline to break. subject=$(pod_ready_of "$SUBJ_DEP") control=$(pod_ready_of "$CTRL_DEP")."
B_SUBJ_RZ=$(readyz_class "$(readyz_of "$SUBJ_DEP")")
B_CTRL_RZ=$(readyz_class "$(readyz_of "$CTRL_DEP")")
B_CANI=$(can_i_leases "$SUBJ_SA")
printf '    subject /readyz=%s   control /readyz=%s   subject can-i get leases=%s\n' \
  "$B_SUBJ_RZ" "$B_CTRL_RZ" "$B_CANI"
[ "$B_SUBJ_RZ" = ready ] && [ "$B_CTRL_RZ" = ready ] \
  || void "a baseline /readyz is not 'ready' (subject=$B_SUBJ_RZ control=$B_CTRL_RZ).
    Breaking something that is already broken measures nothing."
[ "$B_CANI" = yes ] \
  || void "the subject's ServiceAccount cannot get Leases even BEFORE the strip
    (can-i=$B_CANI). There is nothing to take away."
ok "baseline healthy on both channels, and the grant is present to remove"

say "THE MUTATION -- remove the Lease rule from the subject's role"
SAVED=$(mktemp)
role_json "$TARGET" > "$SAVED"
[ -s "$SAVED" ] || void "could not save $TARGET before mutating it. A test that
  breaks something it cannot restore leaves residue the next reader cannot tell
  from a real failure."
case "$TARGET" in
  ClusterRole/*) K patch clusterrole "${TARGET#ClusterRole/}" --type=json \
                   -p "[{\"op\":\"remove\",\"path\":\"/rules/${TARGET_IDX}\"}]" > /dev/null 2>&1 ;;
  Role/*)        K -n "$NS" patch role "${TARGET#Role/}" --type=json \
                   -p "[{\"op\":\"remove\",\"path\":\"/rules/${TARGET_IDX}\"}]" > /dev/null 2>&1 ;;
esac
PATCH_RC=$?

restore() {
  # Restore from the SAVED object, and prove it took. Anything else leaves the
  # cluster in a state the next run would misread.
  K apply -f "$SAVED" > /dev/null 2>&1
  local v; v=$(can_i_leases "$SUBJ_SA")
  printf '    restore: can-i get leases = %s\n' "$v"
  [ "$v" = yes ] || printf '\033[31m    RESTORE DID NOT TAKE -- the role is still stripped\033[0m\n'
}
trap restore EXIT

[ "$PATCH_RC" -eq 0 ] || void "the patch removing rule ${TARGET_IDX} of ${TARGET} failed,
  so nothing was broken and nothing is being measured."

# THE MUTATION CHECK, which is the arm that makes the rest trustworthy. If the
# strip did not take, "/readyz stayed 200" would be reported as a readiness
# defect when in fact nothing was ever taken away.
# shellcheck disable=SC2329  # invoked indirectly, as wait_for's "$@"
_cani_no() { [ "$(can_i_leases "$SUBJ_SA")" = no ]; }
wait_for "the subject's Lease permission to disappear" 10 3 _cani_no \
  || void "after 30s the subject's ServiceAccount STILL reports can-i get
    leases=$(can_i_leases "$SUBJ_SA"). The mutation did not take, so any
    readiness reading below would be a reading of an unbroken system."
ok "the grant is gone: can-i get leases = no for the subject, and only the subject"
CTRL_CANI=$(can_i_leases "$CTRL_SA")
[ "$CTRL_CANI" = yes ] \
  || void "the CONTROL's Lease permission also changed (can-i=$CTRL_CANI). The
    mutation was not isolated, so subject and control can no longer disagree."
ok "the control still holds its grant (can-i=yes), so the two can disagree"

say "CHANNEL 1 -- /readyz, which should flip in seconds"
# ~2s expected: the ownership loop retries every RetryPeriod (2s at v1.28.1) and
# set() assigns lastErr unconditionally, so one failed read is enough. 30s of
# slack, because a fast expectation that fails intermittently is worse than a
# slow one that does not.
# shellcheck disable=SC2329  # invoked indirectly, as wait_for's "$@"
_subj_notready() { [ "$(readyz_class "$(readyz_of "$SUBJ_DEP")")" = notready ]; }
if wait_for "the subject's /readyz to return 503" 10 3 _subj_notready; then
  RZ_VERDICT=notready
  ok "CHANNEL 1 = /readyz reports NOT READY within 30s of the Lease becoming unreachable"
else
  LAST=$(readyz_class "$(readyz_of "$SUBJ_DEP")")
  RZ_VERDICT="$LAST"
  if [ "$LAST" = ready ]; then
    bad "CHANNEL 1 = /readyz STILL REPORTS READY 30s after the Lease grant was
    removed, with can-i confirming the grant is gone and the control unaffected.
    That is the negative arm failing -- the condition nothing anywhere covers."
  else
    bad "CHANNEL 1 = /readyz is $LAST, which is neither ready nor a 503. An
    unreachable listener is not evidence about readiness; read the pod's events."
  fi
fi
CTRL_RZ_MID=$(readyz_class "$(readyz_of "$CTRL_DEP")")
if [ "$CTRL_RZ_MID" = ready ]; then
  ok "the control's /readyz is still ready, so channel 1 is attributable"
else
  bad "the CONTROL's /readyz is $CTRL_RZ_MID -- something broke beyond the subject,
    and channel 1 cannot be attributed to the Lease strip."
fi

say "CHANNEL 2 -- the pod's Ready condition, which should lag by ~${WINDOW}s"
# THE TWO-TIMESCALE READING. /readyz flipping while the pod is still Ready is the
# failureThreshold DOING ITS JOB, not a disagreement -- and recording it as a
# third state says more than either channel alone.
MID_POD=$(pod_ready_of "$SUBJ_DEP")
printf '    at ~30s: /readyz=%s   pod Ready=%s   (derived window %ss)\n' \
  "$RZ_VERDICT" "${MID_POD:-<unread>}" "$WINDOW"
if [ "$RZ_VERDICT" = notready ] && [ "${MID_POD:-}" = "True" ]; then
  ok "THE THRESHOLD IS WORKING: /readyz is already 503 while the pod is still
    Ready. A one-minute sample of pod-Ready alone would have called this a
    FALSE NEGATIVE on a correctly behaving probe."
fi
# shellcheck disable=SC2329  # invoked indirectly, as wait_for's "$@"
_pod_notready() { [ "$(pod_ready_of "$SUBJ_DEP")" = "False" ]; }
SLACK=$(( WINDOW / 4 + 30 ))
TRIES=$(( (WINDOW + SLACK) / 10 + 1 ))
if wait_for "the subject pod's Ready condition to become False (up to $((WINDOW + SLACK))s)" "$TRIES" 10 _pod_notready; then
  POD_VERDICT=notready
  ok "CHANNEL 2 = the pod went NotReady within the DERIVED window, so the kubelet
    consumed the listener's answer"
else
  POD_VERDICT=$(pod_ready_of "$SUBJ_DEP")
  bad "CHANNEL 2 = the pod is still Ready=${POD_VERDICT:-<unread>} after
    $((WINDOW + SLACK))s, which exceeds periodSeconds x failureThreshold. If
    channel 1 reported 503, the listener works and the kubelet is not consuming
    it; those are different defects and this row says which."
fi
CTRL_POD=$(pod_ready_of "$CTRL_DEP")
if [ "${CTRL_POD:-}" = "True" ]; then
  ok "the control pod stayed Ready=True throughout, which is what makes the
    subject's transition attributable to the Lease strip"
else
  bad "the CONTROL pod is Ready=${CTRL_POD:-<unread>}. Both moved, so this run
    measures 'something broke' rather than 'an unreachable Lease is reported'."
fi

say "RESTORE -- and prove it took, because residue is indistinguishable from a defect"
restore
trap - EXIT
# shellcheck disable=SC2329  # invoked indirectly, as wait_for's "$@"
_subj_recovered() { [ "$(readyz_class "$(readyz_of "$SUBJ_DEP")")" = ready ]; }
if wait_for "the subject's /readyz to return 200 again" 12 5 _subj_recovered; then
  ok "RESTORE = /readyz recovered, which also confirms lastErr is not sticky:
    a successful Lease read clears it"
  RESTORE_VERDICT=recovered
else
  RESTORE_VERDICT=stuck
  bad "RESTORE = /readyz did not return to 200 within 60s of the grant being put
    back. Either the restore did not take (can-i above says) or the error IS
    sticky, which would be a finding in its own right."
fi

say "READING 2 -- does THIS CHART come up at the same provider version?"
# SEPARATE ON PURPOSE. This is the apiKey-RBAC safe-set question
# (docs/configuration.md), and it must not be able to void reading 1: a chart
# that cannot install says nothing about a readiness listener.
R2=unmeasured
CHART_DIR=$(mktemp -d)/chart
cp -r chart "$CHART_DIR" 2>/dev/null || true
if [ -d "$CHART_DIR" ]; then
  python3 - "$CHART_DIR" <<'PY'
import io, sys
p = sys.argv[1] + "/Chart.yaml"
s = io.open(p, encoding="utf-8").read().replace("version: CHART_VERSION", "version: 0.0.0-probe")
io.open(p, "w", encoding="utf-8").write(s)
PY
  if helm --kubeconfig "$KUBECONFIG_PATH" --kube-context "$CONTEXT" \
       upgrade --install nova-lr "$CHART_DIR" -n "$NS" \
       --set nova.url="http://unused.invalid/v2.1/p" \
       --wait --timeout 8m > /tmp/probe-lr-helm.log 2>&1; then
    NS_GRANTED=$(K -n "$NS" get restdefinition -o json 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("unreadable"); sys.exit(0)
items = [i for i in d.get("items", []) if i["metadata"]["name"].startswith("nova-lr")]
granted = sum(1 for i in items if (i.get("status") or {}).get("authSecretRBACNamespaces"))
digests = {(i.get("status") or {}).get("authSecretDigest", "") for i in items}
print("%d/%d granted; digests=%s" % (granted, len(items), sorted(d for d in digests if d)[:2]))
')
    printf '    %s\n' "$NS_GRANTED"
    R2=installed
    ok "READING 2 = the chart installed and its RestDefinitions report their auth
    grant state above. A populated authSecretRBACNamespaces with a digest that is
    not the FNV offset basis is the live half of the apiKey-RBAC claim."
  else
    R2=failed
    tail -15 /tmp/probe-lr-helm.log | sed 's/^/      /'
    bad "READING 2 = the chart did NOT install at this provider version. That is a
    finding about this chart, reported separately, and it does NOT affect
    reading 1 above -- which ran on a minimal definition for exactly this reason."
  fi
fi

say "VERDICT"
printf '    provider version under test  %s\n' "${OASGEN_VERSION:-<not passed>}"
printf '    derived pod-Ready window     %ss  (periodSeconds=%s x failureThreshold=%s)\n' \
  "$WINDOW" "${RZ_PERIOD:-default}" "${RZ_THRESH:-default}"
printf '    CHANNEL 1  /readyz           %s\n' "$RZ_VERDICT"
printf '    CHANNEL 2  pod Ready         %s\n' "${POD_VERDICT:-unmeasured}"
printf '    RESTORE                      %s\n' "${RESTORE_VERDICT:-unmeasured}"
printf '    READING 2  this chart        %s\n' "$R2"
cat <<'NOTE'

    WHAT THIS DOES NOT SHOW. One cluster, one provider version, one strip. It
    says nothing about a replica waiting for a peer's Lease -- that arm is Ready
    by design and is measured on a real fleet elsewhere. This is the negative
    half: a Lease that cannot be read must be reported.

    AND IT ASSERTS THE CODE, NOT THE DOCS. At the version under test the
    provider's own values.schema.json may still describe the inverse behaviour
    (inverted at and below 0.85.0, corrected from 0.86.0). The schema text was
    not consulted.
NOTE

if [ "$RZ_VERDICT" = notready ] && [ "${POD_VERDICT:-}" = notready ] \
   && [ "${RESTORE_VERDICT:-}" = recovered ] && [ "${CTRL_POD:-}" = "True" ]; then
  ok "READING 1 CONFIRMED on both channels, with the control healthy throughout
    and the restore proven."
  exit 0
fi
exit 1
