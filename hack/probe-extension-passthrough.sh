#!/usr/bin/env bash
# Does an author-supplied x-kubernetes-* extension reach the generated CRD?
#
# WHY THIS EXISTS. docs/configuration.md and chart/assets/server.yaml both state
# that `x-kubernetes-validations` does NOT survive oasgen-provider, which is why
# this chart documents a Nova 400 instead of expressing "exactly one of imageRef
# or block_device_mapping_v2" as a CEL rule. That statement is currently a CODE
# READ -- measured end to end, but a read:
#
#   oasgen 0.52.0   Schema.Extensions has NO population site; the only
#                   assignment in the tree is the clone copying an existing map
#   oasgen 0.74.0   libopenapi_adapter.go assigns allowedExtensions(...), a
#                   CLOSED two-key allowlist: x-kubernetes-list-type and
#                   x-kubernetes-list-map-keys
#   plumbing        crdgen WOULD carry a rule (passthroughExtensions, v1.14.2),
#                   so the gate is oasgen's allowlist, not the generator
#
# This probe measures it AT THE POINT OF USE instead: it plants both extensions
# in one OAS fragment and reads the CRD the API server actually serves.
#
# THE POSITIVE CONTROL IS THE WHOLE DESIGN, and it is the reviewer's idea. A run
# that reported "no CEL in the CRD" would read identically if the RestDefinition
# never generated, if the fragment never loaded, or if this script asked the
# wrong question. So the fragment also carries `x-kubernetes-list-type` on a
# SIBLING field -- an extension that IS on the allowlist. The expected result is
# therefore asymmetric:
#
#   x-kubernetes-validations   ABSENT   <- the finding
#   x-kubernetes-list-type     PRESENT  <- the control that makes the finding readable
#
# If both are absent, nothing was measured. If both are present, the claim in
# docs/configuration.md is wrong and the Nova-400 documentation should be
# replaced by a CEL rule.
#
# Exit codes: 0 the claim holds, 1 the claim is REFUTED, 3 VOID (no measurement).
set -uo pipefail

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m  OK: %s\033[0m\n' "$*"; }
bad()  { printf '\033[31m  BAD: %s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mFAIL: %s\033[0m\n' "$*" >&2; exit 1; }
void() { printf '\n\033[33mVOID: %s\033[0m\n' "$*" >&2; exit 3; }

GROUP=ext.probe.krateo.io
KIND=ExtProbe
PLURAL=extprobes

# The fragment, as a function so --self-test can check its shape with no cluster.
fragment() {
  cat <<'OAS'
openapi: 3.0.3
info:
  title: extension passthrough probe
  version: "1.0"
servers:
  - url: https://ext.invalid/v1
components:
  securitySchemes:
    apiKey:
      type: apiKey
      in: header
      name: X-Auth-Token
  schemas:
    ExtProbeSpec:
      type: object
      required:
        - name
      properties:
        name:
          type: string
        # THE SUBJECT. On the allowlist at neither version, per the code read.
        pair:
          type: object
          x-kubernetes-validations:
            - rule: "has(self.a) != has(self.b)"
              message: "exactly one of a or b"
          properties:
            a:
              type: string
            b:
              type: string
        # THE POSITIVE CONTROL. x-kubernetes-list-type IS on the 0.74.0
        # allowlist, so it must appear in the served CRD. Its absence means the
        # run measured nothing rather than that the subject is absent.
        tags:
          type: array
          x-kubernetes-list-type: atomic
          items:
            type: string
    ExtProbeCreateRequest:
      type: object
      required:
        - extprobe
      properties:
        extprobe:
          $ref: '#/components/schemas/ExtProbeSpec'
    ExtProbeSummary:
      type: object
      properties:
        id:
          type: string
    ExtProbeEnvelope:
      type: object
      properties:
        extprobe:
          $ref: '#/components/schemas/ExtProbeSummary'
paths:
  /extprobes:
    post:
      operationId: createExtProbe
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/ExtProbeCreateRequest'
      responses:
        '200':
          description: created
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ExtProbeEnvelope'
  /extprobes/{id}:
    get:
      operationId: getExtProbe
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: string
      responses:
        '200':
          description: one
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ExtProbeEnvelope'
    delete:
      operationId: deleteExtProbe
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: string
      responses:
        '204':
          description: gone
OAS
}

self_test() {
  local pass=0 fail_n=0
  t() {
    if [ "$2" = "$3" ]; then printf '  %-54s PASS\n' "$1"; pass=$((pass+1))
    else printf '  %-54s FAIL want=%s got=%s\n' "$1" "$2" "$3"; fail_n=$((fail_n+1)); fi
  }
  local frag; frag=$(fragment)

  # THE FRAGMENT MUST CARRY BOTH EXTENSIONS, or the run is asymmetric by
  # accident rather than by design. This is the arm that keeps the control real.
  t "the fragment plants the SUBJECT (x-kubernetes-validations)" 1 \
    "$(printf '%s\n' "$frag" | grep -cE '^[[:space:]]*x-kubernetes-validations:' || true)"
  t "the fragment plants the CONTROL (x-kubernetes-list-type)" 1 \
    "$(printf '%s\n' "$frag" | grep -cE '^[[:space:]]*x-kubernetes-list-type:' || true)"
  t "the fragment parses as YAML" ok \
    "$(printf '%s\n' "$frag" | python3 -c 'import sys,yaml; yaml.safe_load(sys.stdin); print("ok")' 2>/dev/null || echo BROKEN)"
  t "it declares exactly the apiKey scheme this provider needs" 1 \
    "$(printf '%s\n' "$frag" | grep -c '^    apiKey:' || true)"

  # The CRD reader must distinguish present from absent. Both directions, on
  # the same function the real run uses.
  local with without
  with=$(printf 'spec:\n  versions:\n  - schema:\n      openAPIV3Schema:\n        x-kubernetes-validations: []\n')
  without=$(printf 'spec:\n  versions:\n  - schema:\n      openAPIV3Schema:\n        type: object\n')
  t "the CRD reader sees a planted rule" 1 \
    "$(count_in "$with" '^[[:space:]]*x-kubernetes-validations:')"
  t "and does not invent one" 0 \
    "$(count_in "$without" '^[[:space:]]*x-kubernetes-validations:')"
  # A DESCRIPTION MENTIONING THE KEY MUST NOT COUNT. This is the arm the
  # fragment check above failed on first: it counted a comment.
  t "a description naming the extension is not a declaration" 0 \
    "$(count_in "$(printf '        description: see x-kubernetes-validations\n')" '^[[:space:]]*x-kubernetes-validations:')"

  printf '\n  %d passed, %d failed\n' "$pass" "$fail_n"
  [ "$fail_n" -eq 0 ] || fail "the probe's own fragment or reader is wrong; no cluster result would be readable"
  ok "fragment carries both extensions and the reader discriminates"
}

# count_in <text> <needle> -- no pipe from an unbounded producer, and no
# early-exit consumer: grep -c reads to EOF and cannot race under pipefail.
count_in() { printf '%s\n' "$1" | grep -cE "$2" || true; }

[ "${1:-}" = "--self-test" ] && { say "fragment and reader self-test (no cluster)"; self_test; exit 0; }

KUBECONFIG_PATH="${1:?usage: probe-extension-passthrough.sh <kubeconfig> <context> [namespace] | --self-test}"
CONTEXT="${2:?usage: probe-extension-passthrough.sh <kubeconfig> <context> [namespace] | --self-test}"
NS="${3:-krateo-system}"
K() { kubectl --kubeconfig "$KUBECONFIG_PATH" --context "$CONTEXT" "$@"; }

# shellcheck source=hack/lib/wait.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/wait.sh"
command -v wait_for >/dev/null \
  || fail "hack/lib/wait.sh was sourced but did not define wait_for; every retry
    below would read as a failure of the thing being waited for."

say "fragment and reader self-test, before touching the cluster"
self_test

say "preconditions"
K get --raw /readyz > /dev/null 2>&1 \
  || void "cannot reach the API server (kubeconfig=$KUBECONFIG_PATH context=$CONTEXT)."
K get crd restdefinitions.ogen.krateo.io > /dev/null 2>&1 \
  || void "restdefinitions.ogen.krateo.io is absent, so oasgen-provider is not installed."
K get ns "$NS" > /dev/null 2>&1 || K create ns "$NS" > /dev/null 2>&1 \
  || void "namespace $NS does not exist and could not be created."
ok "API reachable, oasgen-provider CRD present"

say "planting the fragment"
frag_file=$(mktemp)
fragment > "$frag_file"
K -n "$NS" create configmap ext-probe-oas \
  --from-file=ext-probe.yaml="$frag_file" --dry-run=client -o yaml | K apply -f - > /dev/null \
  || void "could not create the ConfigMap holding the fragment."
K apply -f - > /dev/null <<EOF || void "could not apply the probe RestDefinition."
apiVersion: ogen.krateo.io/v1alpha1
kind: RestDefinition
metadata:
  name: ext-probe
  namespace: ${NS}
spec:
  oasPath: configmap://${NS}/ext-probe-oas/ext-probe.yaml
  resourceGroup: ${GROUP}
  resource:
    kind: ${KIND}
    identifiers:
      - extprobe.id
    verbsDescription:
      - action: create
        method: POST
        path: /extprobes
      - action: get
        method: GET
        path: /extprobes/{id}
        requestFieldMapping:
          - inPath: id
            inCustomResource: status.extprobe.id
      - action: delete
        method: DELETE
        path: /extprobes/{id}
        requestFieldMapping:
          - inPath: id
            inCustomResource: status.extprobe.id
EOF
ok "ConfigMap and RestDefinition applied"

# shellcheck disable=SC2329  # invoked indirectly, as wait_for's "$@"
_crd_exists() { K get crd "${PLURAL}.${GROUP}" > /dev/null 2>&1; }
wait_for "the generated CRD ${PLURAL}.${GROUP} to appear" 40 6 _crd_exists \
  || void "oasgen-provider never generated ${PLURAL}.${GROUP} within 240s. Nothing
    was measured about extensions -- this is the provider not generating, which
    reads identically to an extension being dropped if you only count strings.
    The RestDefinition's own conditions follow.
    $(K -n "$NS" get restdefinition ext-probe -o jsonpath='{.status}' 2>/dev/null)"
ok "the CRD exists, so there is something to read"

say "reading the CRD the API server serves"
crd=$(K get crd "${PLURAL}.${GROUP}" -o yaml 2>/dev/null)
bytes=$(printf '%s' "$crd" | wc -c | tr -d ' ')
[ "${bytes:-0}" -gt 500 ] || void "the CRD read back only ${bytes} bytes, so the
  counts below would be counts of nothing."
subject=$(count_in "$crd" '^[[:space:]]*x-kubernetes-validations:')
control=$(count_in "$crd" '^[[:space:]]*x-kubernetes-list-type:')
printf '    CRD read: %s bytes\n' "$bytes"
printf '    x-kubernetes-validations (SUBJECT): %s\n' "$subject"
printf '    x-kubernetes-list-type   (CONTROL): %s\n' "$control"

if [ "${control:-0}" -lt 1 ]; then
  void "the positive control is ABSENT: x-kubernetes-list-type did not reach the
    CRD either, although it is on oasgen 0.74.0's allowlist. So this run cannot
    tell 'the subject was dropped' from 'no extension reached the CRD at all' --
    which is the only reading that makes the subject's absence a finding.
    Nothing is concluded."
fi
ok "the control extension reached the CRD ($control occurrence(s)), so an absent subject means something"

if [ "${subject:-0}" -ge 1 ]; then
  bad "REFUTED: x-kubernetes-validations DID reach the CRD ($subject
    occurrence(s)), while docs/configuration.md and chart/assets/server.yaml
    state that it does not. The Nova-400 documentation on the server asset
    should be replaced by a real CEL rule, and the code read that concluded
    otherwise needs re-examining."
  exit 1
fi

ok "CONFIRMED at the point of use: the planted CEL rule is absent from the served
    CRD while the allowlisted list-type extension is present. The claim in
    docs/configuration.md holds, measured rather than read."
exit 0
