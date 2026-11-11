#!/usr/bin/env bash
# Does oasgen re-apply CRDs in a loop when nothing has changed?
#
# THE DISCRIMINATOR IS THE LOG PAIR, NOT resourceVersion. A CRD's
# resourceVersion advances for reasons unrelated to re-application -- status
# subresource writes, other controllers, the apiserver's own bookkeeping -- so
# an advancing resourceVersion is consistent with both a quiet system and a hot
# loop, and cannot separate them. `Applying CRD` / `Applied CRD` pairs in the
# provider's own log are the thing that actually means "it did the work again".
#
# AND A RESTART RE-APPLIES BY DESIGN, which is why restarts are counted rather
# than ignored. A provider that restarted during the window legitimately
# re-applies every CRD on startup. If restarts changed, this window cannot
# separate a regression from correct startup behaviour, so it VOIDs and says
# to re-run -- it does NOT quietly attribute the pairs to the restart, because
# a restart plus a real loop produces the same counts as a restart alone.
#
# RUN THIS BEFORE hack/probe-apikey-rbac.sh, NEVER AFTER. That probe plants and
# deletes a Secret plus one Configuration per kind, and per oasgen-provider#588
# any change to a kind's secretRef Secret-name set restarts that kind's
# controller -- so it replaces every controller pod twice per run and the logs
# this probe counts go with them. The log-head check below would VOID rather
# than report a false count, but a VOID costs a window and the ordering costs
# nothing.
#
# RUNS ON AN EXISTING CLUSTER. Creates no cluster and deletes none, and changes
# nothing: it reads logs and waits.
set -uo pipefail
export LC_ALL=C

KUBECONFIG_PATH="${1:-}"
CONTEXT="${2:-}"
NS="${3:-krateo-system}"
WINDOW="${4:-180}"

bold() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
ok()   { printf '\033[32m  OK: %s\033[0m\n' "$1"; }
bad()  { printf '\033[31m  FOUND: %s\033[0m\n' "$1"; }
say()  { printf '  %s\n' "$1"; }
void() { printf '\n\033[33m  VOID: %s\033[0m\n' "$1"; exit 9; }

K() { kubectl --kubeconfig "$KUBECONFIG_PATH" --context "$CONTEXT" "$@"; }

# --- pure classifier, so --self-test can exercise every branch --------------
# Args: pairs_before pairs_after restarts_before restarts_after head_before head_after
# Order matters: the checks that invalidate the comparison come FIRST, because
# a rotated log or a restart makes the pair counts incomparable and any verdict
# drawn from them would be about the instrument, not the subject.
classify_549() {
  local pb="$1" pa="$2" rb="$3" ra="$4" hb="$5" ha="$6" ab="${7:-1}" aa="${8:-2}"
  if [ "$hb" != "$ha" ]; then
    echo "void:the log's first line changed between samples, so it rotated or was truncated and the counts are not comparable"
    return
  fi
  if [ "$rb" != "$ra" ]; then
    echo "void:the provider restarted during the window ($rb -> $ra). A restart re-applies every CRD by design, and a restart plus a real loop produces the same counts as a restart alone"
    return
  fi
  if [ "$aa" -le "$ab" ]; then
    echo "void:the RestDefinition controller logged nothing new during the window ($ab -> $aa lines), so it may never have run. 'No new pairs' from a provider that never reconciled is not a measurement of its behaviour"
    return
  fi
  if [ "$pb" -eq 0 ]; then
    echo "void:zero Applying/Applied pairs were present even in the baseline, so it is unknown whether this instrument can see a pair at all"
    return
  fi
  if [ "$pa" -gt "$pb" ]; then
    echo "found:$((pa - pb)) new Applying/Applied pair(s) with no restart and nothing changed"
    return
  fi
  if [ "$pa" -lt "$pb" ]; then
    echo "void:the pair count went DOWN ($pb -> $pa) without the log rotating, which should be impossible and means the count is not measuring what it claims"
    return
  fi
  echo "clean:the pair count held at $pb across the window with no restart"
}

selftest() {
  local fails=0 got want
  bold "--self-test: every branch of the #549 classifier"
  # pb pa rb ra hb ha | want
  while IFS='|' read -r args want; do
    [ -n "$args" ] || continue
    # shellcheck disable=SC2086  # deliberate word-splitting of the arg list
    got=$(classify_549 $args)
    got="${got%%:*}"
    if [ "$got" = "$want" ]; then
      say "classify_549($args) -> $got  ok"
    else
      say "classify_549($args) -> $got, wanted $want  *** WRONG ***"; fails=$((fails+1))
    fi
  done <<'ROWS'
6 6 0 0 H H 10 20|clean
6 9 0 0 H H 10 20|found
6 6 0 0 H H 10 10|void
6 6 0 0 H H 20 10|void
6 9 0 0 H H 10 10|void
6 6 0 1 H H 10 20|void
6 9 0 1 H H 10 20|void
6 6 0 0 H J 10 20|void
6 9 0 0 H J 10 20|void
0 0 0 0 H H 10 20|void
0 3 0 0 H H 10 20|void
6 4 0 0 H H 10 20|void
ROWS
  # Precedence matters and is the easiest thing to get wrong: a rotated log
  # must VOID even when the pairs look like a finding, and a restart must VOID
  # even when the pairs look clean. Assert the ORDER, not just the outcomes.
  if [[ "$(classify_549 6 9 0 0 H J 10 20)" == void:*rotated* ]]; then
    say "a rotated log VOIDs even when the pairs look like a finding  ok"
  else
    say "*** SUITE DEFECT: rotation does not take precedence over a finding ***"; fails=$((fails+1))
  fi
  if [[ "$(classify_549 6 6 0 1 H H 10 20)" == void:*restarted* ]]; then
    say "a restart VOIDs even when the pairs look clean  ok"
  else
    say "*** SUITE DEFECT: a restart does not take precedence over a clean read ***"; fails=$((fails+1))
  fi
  # And the suite must be able to produce all three verdicts.
  local c f v
  c=$(classify_549 6 6 0 0 H H 10 20); f=$(classify_549 6 9 0 0 H H 10 20); v=$(classify_549 0 0 0 0 H H 10 20)
  if [ "${c%%:*}" = clean ] && [ "${f%%:*}" = found ] && [ "${v%%:*}" = void ]; then
    say "all three verdicts reachable  ok"
  else
    say "*** SUITE DEFECT: not every verdict is reachable ***"; fails=$((fails+1))
  fi
  if [ "$fails" -ne 0 ]; then printf '\n  --- FAIL: %s arm(s) ---\n' "$fails"; return 1; fi
  printf '\n  --- all arms pass ---\n'
  return 0
}

if [ "${1:-}" = "--self-test" ]; then selftest; exit $?; fi

[ -n "$KUBECONFIG_PATH" ] && [ -n "$CONTEXT" ] || {
  echo "usage: probe-549-reapply.sh <kubeconfig> <context> [namespace] [window-seconds]" >&2
  echo "       probe-549-reapply.sh --self-test" >&2
  echo "The context is REQUIRED and never defaulted: the ambient kubeconfig on a" >&2
  echo "shared machine reaches real remote clusters." >&2
  exit 2
}

bold "preconditions"
K version --request-timeout=10s >/dev/null 2>&1 || \
  void "the cluster is not reachable with that kubeconfig/context."
POD=$(K -n "$NS" get pod -l app.kubernetes.io/name=oasgen-provider \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
[ -n "$POD" ] || void "no oasgen-provider pod found in $NS, so there is no log to read."
IMG=$(K -n "$NS" get pod "$POD" -o jsonpath='{.spec.containers[0].image}' 2>/dev/null)
say "pod:   $POD"
say "image: ${IMG:-<unknown>}"

# CONTROL-PLANE STARVATION PREFLIGHT, from openstack-ironic-operator-kog, who
# measured kube-controller-manager at 143 restarts and kube-scheduler at 141
# while etcd and apiserver sat at 3 -- three kind control planes on one 15.6GiB
# VM. The apiserver cannot answer the lease GET inside its deadline, the kcm
# loses leadership and exits 1. Object reads stay CORRECT throughout, so a
# probe that reads state notices nothing; what breaks is DURATIONS and GARBAGE
# COLLECTION, because the GC runs inside the kcm. This probe measures a change
# over a time window, so it is squarely in the affected class.
KSTARVED=$(K -n kube-system get pods -o json 2>/dev/null | python3 -I -c '
import sys, json
try: d=json.load(sys.stdin)
except Exception: print("UNREADABLE"); raise SystemExit
hot=[]
for p in d.get("items",[]):
    for cs in p.get("status",{}).get("containerStatuses",[]) or []:
        if cs.get("restartCount",0) > 5:
            hot.append("%s=%d" % (p["metadata"]["name"], cs["restartCount"]))
print(" ".join(hot) if hot else "NONE")
')
case "$KSTARVED" in
  NONE) say "kube-system restarts: none above 5" ;;
  UNREADABLE) void "could not read kube-system pod restart counts, so control-plane health is unknown. A starved control plane corrupts durations without failing anything." ;;
  *) void "the control plane is restarting: $KSTARVED. Durations and garbage collection are unreliable here, and this probe measures a change over a window. Re-run on a quiet box." ;;
esac
# A FRESH CLUSTER HAS ZERO RESTARTS BY CONSTRUCTION, so the check above proves
# nothing on a new build. ironic's live control does: delete a Deployment and
# time how long its ReplicaSet survives. The GC runs in the kcm, so a healthy
# cluster collects in about a second and a starved one does not collect at all.
K -n "$NS" create deployment probe-549-gc --image=registry.k8s.io/pause:3.9 --replicas=1 >/dev/null 2>&1 || true
if K -n "$NS" get deployment probe-549-gc >/dev/null 2>&1; then
  K -n "$NS" delete deployment probe-549-gc --wait=false >/dev/null 2>&1 || true
  GCOK=no
  for _ in $(seq 1 15); do
    if [ "$(K -n "$NS" get rs -l app=probe-549-gc --no-headers 2>/dev/null | wc -l | tr -d ' ')" = "0" ]; then
      GCOK=yes; break
    fi
    sleep 1
  done
  K -n "$NS" delete rs -l app=probe-549-gc --ignore-not-found >/dev/null 2>&1 || true
  [ "$GCOK" = yes ] || void "a deleted Deployment's ReplicaSet survived 15s. Garbage collection runs inside kube-controller-manager, so this is control-plane starvation -- durations measured here would be properties of the load, not of the provider."
  ok "live control: GC collected a deleted Deployment's ReplicaSet within 15s"
else
  say "could not create the GC control Deployment; skipping the live starvation control"
fi
[ -n "$IMG" ] || void "could not read the pod's image, so the version under test is unknown."

read_counts() {
  # Emits "pairs restarts head" on one line, or returns non-zero. The log read
  # is checked rather than discarded: an unreadable log and a quiet log are the
  # same empty string otherwise.
  local log pairs restarts head
  log=$(K -n "$NS" logs "$POD" --tail=-1 2>/dev/null) || return 1
  [ -n "$log" ] || return 1
  pairs=$(printf '%s\n' "$log" | grep -cE 'Applying CRD|Applied CRD') || pairs=0
  # ACTIVITY WITNESS. Without this, "no new pairs" is indistinguishable from
  # "the provider never ran" -- and under control-plane starvation on a loaded
  # box, never running is a real possibility that breaks nothing visible. Any
  # line from the RestDefinition controller proves it was awake.
  activity=$(printf '%s\n' "$log" | grep -cF 'managed/restdefinition.ogen.krateo.io') || activity=0
  head=$(printf '%s\n' "$log" | head -1 | cksum | awk '{print $1}')
  restarts=$(K -n "$NS" get pod "$POD" -o jsonpath='{.status.containerStatuses[0].restartCount}' 2>/dev/null) || return 1
  [ -n "$restarts" ] || return 1
  printf '%s %s %s %s\n' "$pairs" "$restarts" "$head" "$activity"
}

bold "BASELINE"
B=$(read_counts) || void "could not read the provider log and restart count. An unreadable log and a quiet log are the same empty string, so this is not a measurement of zero."
read -r PB RB HB AB <<<"$B"
say "pairs=$PB restarts=$RB log-head-cksum=$HB controller-log-lines=$AB"
[ "$PB" -gt 0 ] || void "zero Applying/Applied pairs in the baseline. Either this provider has not applied a CRD since it started, or the phrases this probe greps for are not the phrases it logs -- and those read identically. Install the chart first, or check the log vocabulary."
ok "the instrument can see pairs: $PB in the baseline"

bold "WINDOW -- ${WINDOW}s with nothing changed"
say "changing nothing and waiting. Any pair appearing now had no trigger."
sleep "$WINDOW"

bold "AFTER"
A=$(read_counts) || void "could not re-read the provider log after the window."
read -r PA RA HA AA <<<"$A"
say "pairs=$PA restarts=$RA log-head-cksum=$HA controller-log-lines=$AA"

bold "VERDICT"
RESULT=$(classify_549 "$PB" "$PA" "$RB" "$RA" "$HB" "$HA" "$AB" "$AA")
WHY="${RESULT#*:}"
case "${RESULT%%:*}" in
  clean) ok "$WHY"; say "A measurement of ${IMG}, over ${WINDOW}s, and of nothing else."; exit 0 ;;
  found) bad "$WHY"; say "Nothing changed during the window and the provider did not restart, so these"; say "re-applications had no trigger. This is the #549 shape."; exit 1 ;;
  void)  void "$WHY" ;;
esac
