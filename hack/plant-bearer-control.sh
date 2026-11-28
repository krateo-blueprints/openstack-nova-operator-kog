#!/usr/bin/env bash
# A BEARER document as a POSITIVE CONTROL for apikey-secret-read-arm.yaml.
#
# WHY. At oasgen 0.54.0 the hand-written auth-secret path list covers basic's two
# refs and bearer's one and misses apiKey's, so the CORRECT behaviour of a broken
# path list on an apiKey-only cluster is to record nothing. "oasgen never
# recorded a grant" then cannot tell "the grant path did not run" (setup broken)
# from "the grant path ran and found no apiKey paths" (the 0.54.0 class): both
# print the same string and leave the same empty digest. A bearer document in the
# SAME cluster does get its grant at 0.54.0, so a recorded bearer grant is
# positive proof the grant path RAN, and apiKey's silence becomes a finding.
#
# IT LIVES IN ITS OWN NAMESPACE, deliberately and for two reasons. (1) The apiKey
# probe's corpus enumeration is namespace-scoped (`K -n "$NS" get restdefinitions`),
# so a document elsewhere cannot enter it. (2) That probe's mixed-corpus guard
# reads `status.hasSecuritySchemes`, a boolean equally true for bearer and basic,
# so it does NOT check what its comment says ("every document here must BE
# apiKey"): a non-apiKey document in the SAME namespace would pass it silently
# and corrupt the corpus. Discovered here; the guard is not changed.
#
# RUN IT AFTER the apiKey probe, not before: the probe lists every
# `<Kind>Configuration` CRD cluster-wide and plants an apiKey fixture against
# each, and a bearer kind's CRD would be one of them.
#
# CREDENTIAL-FREE: a placeholder Secret, an unroutable server.
#
# exit: 0 the bearer grant is RECORDED and its controller can read the Secret
#       1 stood up fine, but NO grant was recorded (or can-i said no): the path
#         did not run. Name this to the caller; it is a setup failure.
#       3 could not stand the document up at all (VOID)
#
# usage: plant-bearer-control.sh <kubeconfig> <context>
#        plant-bearer-control.sh --self-test
set -uo pipefail
export LC_ALL=C

BNS=bearer-ctl
GROUP=bearerctl.probe.krateo.io
# A DIGIT in the Kind on purpose: hack/probe-apikey-rbac.sh discovers
# Configuration CRDs with ^[A-Za-z]+Configuration, so this kind can never be
# picked up by it even if this script were run first.
KIND=Bearer2Ctl  # THE DIGIT IS LOAD-BEARING, NOT A TYPO: do not tidy it to BearerCtl (see above)
RDNAME=bearer-ctl
SECRET=bearer-ctl-token

fragment() {
  cat <<'OAS'
openapi: 3.0.3
info:
  title: bearer positive control
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
    bearerAuth:
      type: http
      scheme: bearer
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

# shellcheck source=hack/lib/touch.sh
. "$(dirname "$0")/lib/touch.sh"

# pick_controller <newline-separated `-o name` output> -> the first non-empty line,
# or nothing. An empty selector result stays EMPTY (unread), never a guessed name.
pick_controller() { printf '%s\n' "$1" | grep -m1 . || true; }

# classify <recorded:yes|no|unread> <cani:yes|no|unread> -> recorded | not-recorded | void
classify() {
  case "$1:$2" in
    yes:yes) echo recorded ;;
    unread:*|*:unread) echo void ;;
    *) echo not-recorded ;;
  esac
}

if [ "${1:-}" = "--self-test" ]; then
  fails=0
  t() { if [ "$2" = "$3" ]; then echo "  ok   $1"; else echo "  *** WRONG: $1 (want $2 got $3)"; fails=$((fails+1)); fi; }
  f=$(fragment)
  t "fragment declares a bearer scheme" yes "$(printf '%s' "$f" | grep -q 'scheme: bearer' && echo yes || echo no)"
  t "fragment declares http type" yes "$(printf '%s' "$f" | grep -q 'type: http' && echo yes || echo no)"
  t "fragment declares NO apiKey scheme" no "$(printf '%s' "$f" | grep -qi 'apikey' && echo yes || echo no)"
  t "Kind has a digit so the apiKey probe's discovery regex cannot match it" no \
    "$(printf '%sConfiguration\n' "$KIND" | grep -qE '^[A-Za-z]+Configuration$' && echo yes || echo no)"
  t "recorded + can-i yes -> recorded" recorded "$(classify yes yes)"
  t "recorded but can-i no -> not-recorded" not-recorded "$(classify yes no)"
  t "nothing recorded -> not-recorded, NOT void" not-recorded "$(classify no yes)"
  t "an unread claim -> void" void "$(classify unread yes)"
  t "an unread can-i -> void" void "$(classify yes unread)"
  t "the bearer touch key is a valid annotation key" yes "$(valid_annotation_key bearer-ctl.probe.krateo.io/touch && echo yes || echo no)"
  t "the OLD bearer key (empty segment) is rejected" no "$(valid_annotation_key 'bearer-ctl//touch' && echo yes || echo no)"
  # shellcheck disable=SC2329  # invoked by name through touch_restdefinitions
  _stub_fail() { return 1; }
  touch_restdefinitions _stub_fail ns bearer-ctl.probe.krateo.io/touch rd >/dev/null || true
  # The key LITERAL in this script, not just the validator: reverting the string
  # to an invalid one must fail here.
  # shellcheck disable=SC2016  # the single quotes are a sed program, not a shell expansion
  t "the key this script ACTUALLY passes is valid" yes "$(valid_annotation_key "$(sed -n 's/^touch_restdefinitions K "\$BNS" "\([^"]*\)".*/\1/p' "$0")" && echo yes || echo no)"
  t "a failing annotate counts 0 touched" 0 "$TOUCHED"
  t "pick_controller: nothing selected stays EMPTY (unread)" "" "$(pick_controller "")"
  t "pick_controller: returns what the selector returned, not a name" "deployment.apps/x-controller" "$(pick_controller $'deployment.apps/x-controller\ndeployment.apps/y')"
  if [ "$fails" -ne 0 ]; then echo "--- FAIL: $fails arm(s) ---"; exit 1; fi
  echo "--- all arms pass ---"; exit 0
fi

KUBECONFIG_PATH="${1:-}"; CONTEXT="${2:-}"
[ -n "$KUBECONFIG_PATH" ] && [ -n "$CONTEXT" ] || { echo "usage: plant-bearer-control.sh <kubeconfig> <context>" >&2; exit 2; }
K() { kubectl --kubeconfig "$KUBECONFIG_PATH" --context "$CONTEXT" "$@"; }
void() { echo "VOID (bearer control): $1" >&2; exit 3; }

# shellcheck source=hack/lib/wait.sh
. "$(dirname "$0")/lib/wait.sh"

K create namespace "$BNS" >/dev/null 2>&1 || true
K -n "$BNS" create secret generic "$SECRET" --from-literal=token=placeholder-not-a-credential >/dev/null \
  || void "could not create the fixture Secret"
f=$(mktemp); fragment > "$f"
K -n "$BNS" create configmap "$RDNAME-oas" --from-file=oas.yaml="$f" --dry-run=client -o yaml | K apply -f - >/dev/null \
  || void "could not create the OAS ConfigMap"
rm -f "$f"
K apply -f - >/dev/null <<YAML || void "could not apply the RestDefinition"
apiVersion: ogen.krateo.io/v1alpha1
kind: RestDefinition
metadata: { name: $RDNAME, namespace: $BNS }
spec:
  oasPath: configmap://$BNS/$RDNAME-oas/oas.yaml
  resourceGroup: $GROUP
  resource:
    kind: $KIND
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
YAML
K -n "$BNS" wait restdefinitions.ogen.krateo.io/"$RDNAME" --for=condition=Ready --timeout=300s \
  || { K -n "$BNS" describe restdefinitions.ogen.krateo.io "$RDNAME" | tail -40
       void "the bearer RestDefinition in its own namespace never reached Ready, so no controller exists. (If the provider only watches its own namespace this is why -- and that is a limit of this control, not a finding.)"; }

CFGV=$(K get crd -o jsonpath="{range .items[?(@.spec.names.kind==\"${KIND}Configuration\")]}{.spec.versions[0].name}{end}" 2>/dev/null)
[ -n "$CFGV" ] || void "no ${KIND}Configuration CRD is served"
K apply -f - >/dev/null <<YAML || void "the ${KIND}Configuration was refused"
apiVersion: $GROUP/$CFGV
kind: ${KIND}Configuration
metadata: { name: bearer-ctl-cfg, namespace: $BNS }
spec:
  authentication:
    bearer:
      tokenRef: { name: $SECRET, namespace: $BNS, key: token }
YAML
# The key used to be `bearer-ctl//touch` (an empty segment, invalid): the annotate
# failed silently on every run. Now valid, and a failure is SAID, not discarded.
touch_restdefinitions K "$BNS" "bearer-ctl.probe.krateo.io/touch" "$RDNAME" || true
if [ "$TOUCHED" -eq 1 ]; then echo "  touched the bearer RestDefinition to force a reconcile"
else echo "  TOUCH FAILED ($TOUCH_ERR): the wait below is doing the work alone"; fi

# shellcheck disable=SC2329  # invoked by name through wait_for
recorded_now() {
  K -n "$BNS" get restdefinitions.ogen.krateo.io "$RDNAME" -o jsonpath='{.status.authSecretRBACNamespaces}' 2>/dev/null | grep -q "$BNS"
}
REC=yes
wait_for "oasgen to record the BEARER grant for $BNS" 100 3 recorded_now || REC=no

CANI=unread
# SELECTED BY LABEL, NOT BY NAME. The first version grepped the plural out of the
# Deployment names and nothing matched: the generated Deployment is
# `<RestDefinition name>-controller` (per the oasgen-provider session reading
# restdefinition.go:601 / deploy.go:27 -- second-hand), with no plural anywhere.
# The name is NOT asserted here either: the label the asset sets is the stable
# contract, and what comes back is READ and printed. An empty answer stays `unread`.
DEPS=$(K -n "$BNS" get deploy -l app.kubernetes.io/component=controller -o name 2>/dev/null || true)
echo "  controller Deployment(s) in $BNS by label app.kubernetes.io/component=controller: ${DEPS:-<none>}"
DEP=$(pick_controller "$DEPS")
if [ -n "$DEP" ]; then
  SA=$(K -n "$BNS" get "$DEP" -o jsonpath='{.spec.template.spec.serviceAccountName}' 2>/dev/null)
  [ -n "$SA" ] || SA=default
  # Same shape as the apiKey probe's check: the NAMED form, because the grant is resourceNames-scoped.
  POS=$(K auth can-i get "secret/$SECRET" -n "$BNS" 2>/dev/null)
  ans=$(K auth can-i get "secret/$SECRET" -n "$BNS" --as "system:serviceaccount:$BNS:$SA" 2>/dev/null)
  case "$POS:$ans" in yes:yes) CANI=yes ;; yes:no) CANI=no ;; esac
  echo "  controller $DEP (sa=$SA) can-i get secret/$SECRET: $ans (my own identity: $POS)"
else
  echo "  no Deployment carries app.kubernetes.io/component=controller in $BNS, so can-i was not asked (unread)"
fi
echo "  bearer grant recorded: $REC; controller can-i: $CANI"
case "$(classify "$REC" "$CANI")" in
  recorded) echo "BEARER GRANT RECORDED AND READABLE -- the grant path RAN in this cluster"; exit 0 ;;
  not-recorded) echo "BEARER GRANT NOT RECORDED -- the grant path did not run here, so an apiKey silence is NOT attributable" >&2; exit 1 ;;
  *) void "the bearer grant or can-i could not be read" ;;
esac
