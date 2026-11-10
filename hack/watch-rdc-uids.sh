#!/usr/bin/env bash
# Standing capture of the per-kind rest-dynamic-controller Deployments' identity,
# for oasgen-provider#541.
#
# WHY A WATCHER AND NOT A BEFORE/AFTER SNAPSHOT. #541 only manifests when the new
# provider finds those Deployments ABSENT at its first observation, which takes a
# branch that skips CRD generation while still stamping status.generatorBuild. Why
# they are ever absent is unexplained: a rename is ruled out (the `-controller`
# suffix is identical across 0.52.0-0.56.0) and so is a helm prune (the provider
# renders them at runtime). The deciding datum is their metadata.uid across the
# event -- and nobody has it, because across four runs in two sessions every run
# that captured uids did not see the absence, and the one run that saw the absence
# captured no uids. The natural occurrence needs luck AND preparation at the same
# instant; a watcher left running needs only luck. (The idea is the
# oasgen-provider session's.)
#
# AND IT ANSWERS A QUESTION A SNAPSHOT CANNOT. `--output-watch-events` emits the
# event TYPE, so a DELETED line distinguishes "something deleted them" from "they
# were briefly unobservable". A before/after pair of uids cannot tell those apart:
# both produce two different uids, or two identical ones, with no record of what
# happened in between.
#
# START IT BEFORE THE UPGRADE. Existing objects arrive as initial ADDED events, so
# a watcher started afterwards has no baseline and is worth nothing.
#
# Usage:
#   hack/watch-rdc-uids.sh <kubeconfig> <context> [namespace] [logfile]
#   hack/watch-rdc-uids.sh ~/.kube/config kind-nova krateo-system rdc-uid.log
#
# Append-only; safe to leave running across upgrades, restarts and reconnects.
set -uo pipefail
export LC_ALL=C        # decimal comma otherwise, on an it_IT box

KUBECONFIG_PATH="${1:?usage: watch-rdc-uids.sh <kubeconfig> <context> [ns] [logfile]}"
CONTEXT="${2:?}"
NS="${3:-krateo-system}"
LOG="${4:-rdc-uid.log}"

K() { kubectl --kubeconfig "$KUBECONFIG_PATH" --context "$CONTEXT" "$@"; }

# FAIL LOUDLY RATHER THAN WATCH NOTHING. A watcher that cannot connect produces an
# empty log, and an empty log is indistinguishable from "the event never happened"
# -- which is the whole class of defect this file exists to serve. So the
# preconditions are asserted first, and each one says what its failure means.
K get --raw /readyz > /dev/null 2>&1 || {
  echo "REFUSING: cannot reach the API server with kubeconfig=$KUBECONFIG_PATH context=$CONTEXT." >&2
  echo "  An unreachable cluster yields an EMPTY log, which reads identically to" >&2
  echo "  'the Deployments were never touched'. Fix the connection, do not run blind." >&2
  exit 2
}

n=$(K -n "$NS" get deploy -o name 2>/dev/null | grep -c -- '-controller$')
if [ "${n:-0}" -eq 0 ]; then
  echo "REFUSING: zero *-controller Deployments in namespace $NS right now." >&2
  echo "  Either the chart is not installed, the namespace is wrong, or the" >&2
  echo "  Deployments are ALREADY absent. All three produce a log with no baseline," >&2
  echo "  so there would be nothing to compare a later event against." >&2
  exit 3
fi
echo "# watching $n *-controller Deployment(s) in $NS via context $CONTEXT" | tee -a "$LOG"
echo "# started $(date -u +%Y-%m-%dT%H:%M:%SZ) -- columns: EVENT NAME UID GENERATION RV TS" | tee -a "$LOG"

# `--output-watch-events` wraps each object in a WatchEvent, so the type is at
# `.type` and the object one level down. custom-columns cannot reach a nested
# field; jsonpath can. Verified on kubectl v1.36.2.
#
# `--watch`, NOT `--watch-only`. This is the whole point of the watcher and it is
# easy to get backwards: `--watch-only` means "watch for changes to the requested
# object(s), WITHOUT listing/getting first" -- so it emits no baseline, and the
# baseline uid is precisely the datum being captured. `--watch` lists first, and
# with `--output-watch-events` those existing objects arrive as initial ADDED
# events.
#
# The failure mode is quiet: with `--watch-only` over a set that is not changing,
# the correct output is ZERO LINES. That is indistinguishable from a broken
# pipeline, and it cost three wrong diagnoses here before the flag was read
# properly -- including one isolation run that happened to execute while the
# Deployments were deleted, where zero lines was right for a second, unrelated
# reason.
K -n "$NS" get deploy --output-watch-events --watch \
  -o jsonpath='{.type}{" "}{.object.metadata.name}{" "}{.object.metadata.uid}{" "}{.object.metadata.generation}{" "}{.object.metadata.resourceVersion}{" "}{.object.metadata.creationTimestamp}{"\n"}' \
  2>>"$LOG" \
  | grep --line-buffered -- '-controller ' \
  | while IFS= read -r line; do
      printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$line" >> "$LOG"
    done
