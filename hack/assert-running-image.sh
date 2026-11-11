#!/usr/bin/env bash
# Is the pod running the image I asked for? Accept EVERY correct digest form.
#
# WHY THIS IS NOT A ONE-LINE COMPARISON. A published tag resolves to an OCI
# image INDEX, and that index has several digests which are all correct answers
# to "what is this image":
#
#   the index digest            what `docker buildx imagetools inspect` reports
#   each platform's manifest    what a runtime may record after pulling one
#   attestation manifests       platform unknown/unknown; never runnable
#
# A container runtime may record the index it resolved OR the platform manifest
# it actually pulled, and which one depends on the runtime. So a check that
# accepts ONE form and rejects the others reports a version mismatch on a
# correctly-deployed pod -- which is the worst failure available to a
# verification step, because it is alarming AND names the wrong cause.
#
# MEASURED, 2026-11-11, oasgen-provider:0.59.0:
#   index                 sha256:51c45ccb4303...  (matches what the publisher announced)
#   linux/amd64           sha256:4697307a82ed...
#   linux/arm64           sha256:e666fdda4efb...
#   unknown/unknown x2    attestations
# A peer nearly reported the amd64 platform digest to the publisher as a
# contradiction of their announced value. Both were right; the word "the
# digest" was wrong.
#
# THE SET IS DERIVED FROM THE REGISTRY, NEVER HARDCODED. A hardcoded set rejects
# correct forms the moment the publisher's build tooling changes, and it would
# then be this script reporting a mismatch rather than the cluster having one.
#
# ATTESTATIONS ARE DELIBERATELY EXCLUDED. No runtime records one as a running
# image, so admitting them would widen the accept set for no gain -- and a
# wider accept set is a weaker check.
set -uo pipefail
export LC_ALL=C

ok()   { printf '\033[32m  OK: %s\033[0m\n' "$1"; }
bad()  { printf '\033[31m  MISMATCH: %s\033[0m\n' "$1"; }
say()  { printf '  %s\n' "$1"; }
void() { printf '\033[33m  VOID: %s\033[0m\n' "$1"; exit 9; }

# --- pure classifier, so --self-test needs no network ------------------------
# Args: running_digest, then the acceptable set as "label=digest" pairs.
# Echoes "match:<label>" or "nomatch:".
classify_digest() {
  local running="$1"; shift
  local pair
  for pair in "$@"; do
    [ -n "$pair" ] || continue
    if [ "${pair#*=}" = "$running" ]; then echo "match:${pair%%=*}"; return; fi
  done
  echo "nomatch:"
}

# Derive index + platform digests for a reference. Attestations excluded.
derive_forms() {
  local ref="$1" idx plats
  idx=$(docker buildx imagetools inspect "$ref" --format '{{.Manifest.Digest}}' 2>/dev/null)
  plats=$(docker manifest inspect "$ref" 2>/dev/null | python3 -I -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: raise SystemExit(1)
for m in d.get("manifests",[]):
    p=m.get("platform",{}) or {}
    osn,arch=p.get("os",""),p.get("architecture","")
    if osn=="unknown" or arch=="unknown": continue      # attestation, never run
    print("%s/%s=%s" % (osn,arch,m.get("digest","")))
')
  [ -n "$idx" ] && printf 'index=%s\n' "$idx"
  [ -n "$plats" ] && printf '%s\n' "$plats"
  [ -n "$idx" ] || [ -n "$plats" ]
}

selftest() {
  local fails=0 got
  printf '\n\033[1m== --self-test: the classifier, on a fixture (no network)\033[0m\n'
  local SET=("index=sha256:aaa" "linux/amd64=sha256:bbb" "linux/arm64=sha256:ccc")
  # EMPTY is a sentinel. The row for an empty running digest was previously
  # written as a literal empty field and SKIPPED by the guard below -- a row
  # that never ran, in the suite for a check whose whole job is to not accept
  # the wrong thing. An empty imageID is a real input: it is what a pod reports
  # before its container has started.
  while IFS='|' read -r running want; do
    [ -n "$running" ] || continue
    [ "$running" = EMPTY ] && running=""
    got=$(classify_digest "$running" "${SET[@]}")
    if [ "$got" = "$want" ]; then say "classify_digest($running) -> $got  ok"
    else say "classify_digest($running) -> $got, wanted $want  *** WRONG ***"; fails=$((fails+1)); fi
  done <<'ROWS'
sha256:aaa|match:index
sha256:bbb|match:linux/amd64
sha256:ccc|match:linux/arm64
sha256:zzz|nomatch:
EMPTY|nomatch:
ROWS
  # EVERY form in the set must be accepted. A check that takes the index and
  # rejects the platform manifests is the defect this file exists for, and it
  # would pass a suite that only ever tries the index.
  local p
  for p in "${SET[@]}"; do
    [ "$(classify_digest "${p#*=}" "${SET[@]}")" = "match:${p%%=*}" ] || {
      say "*** SUITE DEFECT: form ${p%%=*} is not accepted ***"; fails=$((fails+1)); }
  done
  say "all ${#SET[@]} forms in the set are accepted  ok"
  # And an empty set must not accept anything, or a failed derivation would
  # silently pass everything.
  if [ "$(classify_digest sha256:aaa)" = "nomatch:" ]; then
    say "an EMPTY acceptable set matches nothing  ok"
  else
    say "*** SUITE DEFECT: an empty set accepted a digest ***"; fails=$((fails+1))
  fi
  if [ "$fails" -ne 0 ]; then printf '\n  --- FAIL: %s arm(s) ---\n' "$fails"; return 1; fi
  printf '\n  --- all arms pass ---\n'; return 0
}

case "${1:-}" in
  --self-test) selftest; exit $? ;;
  --forms)
    REF="${2:-}"
    [ -n "$REF" ] || { echo "usage: assert-running-image.sh --forms <image-ref>" >&2; exit 2; }
    command -v docker >/dev/null 2>&1 || void "docker is needed to read the registry."
    printf '\n\033[1m== acceptable digest forms for %s\033[0m\n' "$REF"
    FORMS=$(derive_forms "$REF") || void "could not derive any digest form for $REF. The registry was not readable, so an absent match below would be about the network rather than the pod."
    printf '%s\n' "$FORMS" | sed 's/^/  /'
    exit 0 ;;
esac

KUBECONFIG_PATH="${1:-}"; CONTEXT="${2:-}"; NS="${3:-}"; SELECTOR="${4:-}"; REF="${5:-}"
[ -n "$KUBECONFIG_PATH" ] && [ -n "$CONTEXT" ] && [ -n "$NS" ] && [ -n "$SELECTOR" ] && [ -n "$REF" ] || {
  cat >&2 <<'USAGE'
usage: assert-running-image.sh <kubeconfig> <context> <ns> <pod-selector> <image-ref>
       assert-running-image.sh --forms <image-ref>
       assert-running-image.sh --self-test
The context is REQUIRED and never defaulted: the ambient kubeconfig on a shared
machine reaches real remote clusters.
USAGE
  exit 2
}
K() { kubectl --kubeconfig "$KUBECONFIG_PATH" --context "$CONTEXT" "$@"; }

printf '\n\033[1m== derive the acceptable set from the registry\033[0m\n'
mapfile -t FORMS < <(derive_forms "$REF")
[ "${#FORMS[@]}" -gt 0 ] || void "could not derive any digest form for $REF, so a mismatch below would describe the network rather than the pod."
printf '%s\n' "${FORMS[@]}" | sed 's/^/  /'

printf '\n\033[1m== read what is RUNNING\033[0m\n'
RUNNING=$(K -n "$NS" get pod -l "$SELECTOR" \
  -o jsonpath='{range .items[*]}{.status.containerStatuses[0].imageID}{"\n"}{end}' 2>/dev/null \
  | sed 's|.*@||' | grep . | sort -u)
N=$(printf '%s\n' "$RUNNING" | grep -c .)
[ "${N:-0}" -gt 0 ] || void "no pod matching '$SELECTOR' reported an imageID, so nothing was verified."
[ "$N" = "1" ] || void "pods are running $N DIFFERENT digests, so there is no single running image. Mid-rollout is not a version."
say "running: $RUNNING"

printf '\n\033[1m== VERDICT\033[0m\n'
RES=$(classify_digest "$RUNNING" "${FORMS[@]}")
if [ "${RES%%:*}" = "match" ]; then
  ok "the running digest is the ${RES#*:} form of $REF"
  say "Reported WHICH form matched on purpose: 'it matched' without naming the form"
  say "would hide a check that accepted something unexpected."
  exit 0
fi
bad "the running digest matches NO correct form of $REF."
say "This is not a form-labelling problem -- every correct form was derived from"
say "the registry and offered. The pod is running a different image."
exit 1
