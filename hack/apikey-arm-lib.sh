#!/usr/bin/env bash
# Helpers for .github/workflows/apikey-secret-read-arm.yaml, kept in a file so the
# parts that went wrong on the FIRST dispatch can be exercised offline.
#
# THE FIRST DISPATCH (run 46164705298) DIED WITH `exit code 9` AND NO MESSAGE, for
# three reasons, each of which a self-test over the CLASSIFIER could not see:
#
#   1. ERREXIT. GitHub runs `run:` steps as `bash -e {0}` and `set -uo pipefail`
#      does not clear -e, so `probe ... > out; M_RC=$?` died AT the probe's own
#      non-zero exit and the classifier was never reached for any interesting
#      case. `shell: bash` does not help (it expands to -eo pipefail). The fix is
#      the `|| rc=$?` guard, and run_capture below is the one place it lives.
#   2. NO WAIT. The probe ran 0.7s after `helm install` and VOIDed on a
#      precondition that is true 0.7s after any install. wait_ready fixes that,
#      and a wait that times out names WHICH wait.
#   3. THE EVIDENCE WAS NEVER PRINTED. run_capture prints the output inside a
#      collapsible group, always, success included.
#
# The self-test runs run_capture under `bash -e` against stub probes, which is the
# test that was missing: the classifier arms all passed while nothing tested that
# the classifier gets CALLED.
#
# usage: source hack/apikey-arm-lib.sh      (defines functions only)
#        bash hack/apikey-arm-lib.sh --self-test
export LC_ALL=C
# shellcheck source=hack/lib/wait.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/wait.sh"

# run_capture <outfile> <cmd...> -> sets RUN_RC; ALWAYS returns 0, so a caller
# under `bash -e` survives a non-zero command. Prints the output in a group.
run_capture() {
  local out="$1"; shift
  RUN_RC=0
  "$@" > "$out" 2>&1 || RUN_RC=$?
  echo "::group::$1 (rc=$RUN_RC)"
  cat "$out"
  echo "::endgroup::"
  return 0
}

# probe_label <rc> <logfile> -> a label. The probe's own vocabulary: rc 1 carries
# three different messages, which are different defects.
probe_label() {
  local rc="$1" out="$2"
  if [ "$rc" = 0 ]; then echo clean
  elif [ "$rc" = 1 ] && grep -q "CANNOT read Secret" "$out"; then echo found:apikey-reason
  elif [ "$rc" = 1 ] && grep -q "never recorded" "$out"; then echo found:grant-path-did-not-run
  elif [ "$rc" = 1 ]; then echo found:other
  elif [ "$rc" = 9 ]; then echo void
  else echo "unexpected-rc-$rc"; fi
}

# wait_ready <context> <namespace> -> 0 when oasgen and every RestDefinition are
# up; otherwise 1 with a message NAMING the wait that failed. Never a finding.
wait_ready() {
  local ctx="$1" ns="$2" rd=restdefinitions.ogen.krateo.io
  K() { kubectl --context "$ctx" "$@"; }
  kubectl --context "$ctx" -n "$ns" rollout status deploy/oasgen-provider --timeout=180s \
    || { echo "WAIT FAILED: the oasgen-provider rollout never completed"; return 1; }
  # shellcheck disable=SC2329  # invoked by name through wait_for
  have_crd() { K get crd "$rd"; }
  wait_for "the $rd CRD to exist" 60 3 have_crd || { echo "WAIT FAILED: the provider never created $rd"; return 1; }
  K wait --for=condition=Established "crd/$rd" --timeout=180s \
    || { echo "WAIT FAILED: $rd never became Established"; return 1; }
  # shellcheck disable=SC2329  # invoked by name through wait_for
  have_rds() { [ "$(K -n "$ns" get "$rd" --no-headers 2>/dev/null | wc -l | tr -d ' ')" -ge 1 ]; }
  wait_for "at least one RestDefinition in $ns" 60 3 have_rds \
    || { echo "WAIT FAILED: no RestDefinition appeared in $ns"; return 1; }
  K -n "$ns" wait "$rd" --all --for=condition=Ready --timeout=420s \
    || { echo "WAIT FAILED: the RestDefinitions never all reached Ready"; K -n "$ns" get "$rd" -o wide || true; return 1; }
  return 0
}

if [ "${1:-}" = "--self-test" ]; then
  self="$(cd "$(dirname "$0")" && pwd)/apikey-arm-lib.sh"
  d=$(mktemp -d); fails=0
  t() { if [ "$2" = "$3" ]; then echo "  ok   $1"; else echo "  *** WRONG: $1 (want '$2' got '$3')"; fails=$((fails+1)); fi; }
  printf '#!/bin/sh\necho "CANNOT read Secret/x -- the 0.54.0 class"\nexit 1\n' > "$d/p1"
  printf '#!/bin/sh\necho "oasgen never recorded an authSecretRBAC grant"\nexit 1\n' > "$d/p1b"
  printf '#!/bin/sh\necho "VOID: zero RestDefinitions"\nexit 9\n' > "$d/p9"
  printf '#!/bin/sh\necho fine\nexit 0\n' > "$d/p0"
  printf '#!/bin/sh\necho boom\nexit 1\n' > "$d/p1c"
  # THE TEST THAT WAS MISSING: run each stub UNDER `bash -e`, the way GitHub runs a
  # step, and require that the line AFTER run_capture executes and sees the rc.
  for row in "p0:0:clean" "p1:1:found:apikey-reason" "p1b:1:found:grant-path-did-not-run" \
             "p1c:1:found:other" "p9:9:void"; do
    IFS=: read -r stub rc label <<<"$row"
    got=$(bash -e -c ". '$self'; run_capture '$d/out' sh '$d/$stub' >/dev/null; echo \"\$RUN_RC \$(probe_label \"\$RUN_RC\" '$d/out')\"" 2>&1 | tail -1)
    t "under bash -e, $stub (exit $rc) is CLASSIFIED, not fatal" "$rc $label" "$got"
  done
  # And the evidence is printed, in a group, on success as well as failure.
  shown=$(bash -e -c ". '$self'; run_capture '$d/out' sh '$d/p0'" 2>&1)
  t "the output is printed inside a group on SUCCESS" yes \
    "$(printf '%s' "$shown" | grep -q '::group::' && printf '%s' "$shown" | grep -q '^fine$' && printf '%s' "$shown" | grep -q '::endgroup::' && echo yes || echo no)"
  shown=$(bash -e -c ". '$self'; run_capture '$d/out' sh '$d/p9'" 2>&1)
  t "the output is printed on FAILURE too" yes "$(printf '%s' "$shown" | grep -q 'zero RestDefinitions' && echo yes || echo no)"
  t "an unexpected rc is labelled, not swallowed" unexpected-rc-5 "$(probe_label 5 "$d/out")"
  rm -rf "$d"
  if [ "$fails" -ne 0 ]; then echo "--- FAIL: $fails arm(s) ---"; exit 1; fi
  echo "--- all arms pass ---"; exit 0
fi
