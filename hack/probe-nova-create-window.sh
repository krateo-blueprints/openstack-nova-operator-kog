#!/usr/bin/env bash
# The Nova create-window arm's orchestrator: ONE real, BILLABLE server create by
# default (the hard ceiling is five, for someone who deliberately wants
# replicates), observed from two sides.
#
# *** PARKED. THIS ARM IS NOT RUNNABLE TODAY. ***
# Whether CI holds a cloud credential is unsettled, and that is Diego's decision,
# not a gap in this instrument. Without the secrets it VOIDs at its first step,
# naming the missing secret; that is not a skip and not a pass. When a credential
# exists the path is scripts/get-token.sh, which needs OS_USER_DOMAIN_NAME=Default
# (the openrc's own value 401s; this script forces it). Every run is a REAL create
# on someone's cloud and costs money. See docs/e2e.md "The Nova create-window arm".
#
# WHY ONE DRAW SUFFICES. The verdict is a per-PASS classification of the
# controller's own log (hack/nova-create-window.py classify_pass), not a sample:
# a hint pass is single-object, off the resync grid, and waited >= the hint; a
# starved pass shares one queuedAt or sits on the grid. That is decisive per pass,
# so there is no distribution to build.
#
#   Q1  does a freshly-created Nova server ever answer a GET with 404 before it
#       answers with BUILD?                       (hack/nova-create-window.py poll)
#   Q2  does the Krateo CR's Ready go True -> not-True around a create?
#                                                 (hack/capture-watch-stream.py, vendored)
#
# THE QUESTION IS THE openstack-ironic-operator-kog SESSION'S. Ironic's
# POST /v1/nodes is immediately consistent -- 201 and the node is GETtable at
# once -- so their lab physically cannot produce the window. Nova returns 202 with
# the server in BUILD, so this one can. See docs/e2e.md "The Nova create-window
# arm".
#
# THE LIMITS ARE ENFORCED HERE, NOT BY CONVENTION. Each create is money.
#   * MAX_DRAWS is a readonly constant, and the loop is bounded by it, so an input
#     cannot raise it. An input asking for more is REFUSED (rc 64) before any
#     cluster or cloud call, never clamped: a silently-clamped 6 would read as a
#     6-draw result.
#   * `draw_cap_ok` is checked immediately before every apply, against a counter
#     written to run.json BEFORE the apply. The count is incremented before money
#     is spent, not after.
#   * SEQUENTIAL: draw n+1 does not start until the vendor, READ, shows no server
#     under this run's prefix. Parallel creates share resync ticks, so their draws
#     are correlated and prove far less than their count suggests.
#   * After each draw the vendor is read for the number of servers under the
#     prefix. More than one means a duplicate create (the lost-create-outcome
#     hazard in docs/e2e.md); an UNREAD count is treated as the same: stop
#     spending rather than spend blind. That is recorded as cap_breach and the run
#     FAILS.
#   * TEARDOWN IS PROVEN BY READING THE VENDOR (hack/nova-create-window.py
#     teardown), not by a delete call returning. A run that cannot prove it is a
#     FAILED run (rc 4), never a pass with a warning. The CRs are deleted FIRST
#     and the controller scaled to zero BEFORE any direct vendor DELETE: deleting a
#     server out from under a live CR makes the controller observe "gone" and
#     CREATE ANOTHER ONE.
#
# THE CREDENTIAL. Repository secrets, read by scripts/get-token.sh. The openrc's
# own OS_USER_DOMAIN_NAME (ovhcloud-emea) 401s against OVH; get-token needs
# `Default`, which is forced below. ABSENT credential => VOID (rc 2) with a message
# saying so. Never a pass, never a silent skip.
#
# Modes:   --self-test
#          check-credential
#          check-inputs        DRAWS / OASGEN_VERSION / WINDOW_S, refused before any cluster is built
#          nova-url            print the Nova endpoint from the Keystone catalog
#          run <outdir>        needs a cluster with this chart installed, kubectl
#          teardown <outdir>   idempotent; the workflow also calls it under always()
# Exit:    0 completed (the VERDICT is `analyse`'s, not this script's)
#          2 VOID   4 teardown/cap failure   64 usage
set -uo pipefail
export LC_ALL=C

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PY="$HERE/nova-create-window.py"
READER="$HERE/capture-watch-stream.py"      # vendored from ironic, see its header
FLAPWATCH="$HERE/watch-condition-flap.py"    # vendored from ironic, see its header
# shellcheck source=hack/lib/wait.sh
. "$HERE/lib/wait.sh"
# shellcheck source=hack/lib/self-test-guard.sh
. "$HERE/lib/self-test-guard.sh"

readonly MAX_DRAWS=5
NS="${NS:-krateo-system}"
CR_RESOURCE="${CR_RESOURCE:-instances.nova.openstack.krateo.io}"
WINDOW_S="${WINDOW_S:-240}"          # must exceed the 180s resync (asserted)
SYNCED_WAIT_S="${SYNCED_WAIT_S:-300}"

die()  { printf '%s\n' "$*" >&2; exit "${2:-64}"; }
void() { printf 'VOID: %s\n' "$*" >&2; exit 2; }

# ---------------------------------------------------------------------------
# PURE FUNCTIONS. Each decides something about money or a verdict, and each is
# exercised in both directions by --self-test.
# ---------------------------------------------------------------------------

# validate_inputs <draws> <oasgen> -> 0, or 64 with the reason on stderr.
validate_inputs() {
  local draws="$1" ver="$2"
  case "$draws" in
    ''|*[!0-9]*) echo "REFUSED: draws='$draws' is not a whole number" >&2; return 64 ;;
  esac
  if [ "$draws" -lt 1 ]; then
    echo "REFUSED: draws=$draws; at least 1 is needed, and 3 usable ones to report" >&2; return 64
  fi
  if [ "$draws" -gt "$MAX_DRAWS" ]; then
    echo "REFUSED: draws=$draws exceeds the cap of $MAX_DRAWS. Each create is BILLABLE on a real cloud; this is refused, never clamped." >&2
    return 64
  fi
  if ! printf '%s' "$ver" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
    echo "REFUSED: oasgen='$ver' is not ONE x.y.z version. One oasgen version per dispatch." >&2
    return 64
  fi
  return 0
}

# draw_cap_ok <draws_started> -> 0 if another create may be issued.
draw_cap_ok() {
  [ "${1:-$MAX_DRAWS}" -lt "$MAX_DRAWS" ]
}

# credential_state -> prints "ok" or "absent: <what>" ; rc 0 / 2.
credential_state() {
  local miss="" v
  if [ -n "${OS_APPLICATION_CREDENTIAL_ID:-}${OS_APPLICATION_CREDENTIAL_SECRET:-}" ]; then
    for v in OS_APPLICATION_CREDENTIAL_ID OS_APPLICATION_CREDENTIAL_SECRET; do
      [ -n "${!v:-}" ] || miss="$miss $v"
    done
  else
    for v in OS_USERNAME OS_PASSWORD OS_PROJECT_ID; do
      [ -n "${!v:-}" ] || miss="$miss $v"
    done
  fi
  for v in NOVA_ARM_FLAVOR NOVA_ARM_IMAGE NOVA_ARM_NETWORK; do
    [ -n "${!v:-}" ] || miss="$miss $v"
  done
  if [ -n "$miss" ]; then
    echo "absent:$miss"
    return 2
  fi
  echo ok
}

# server_prefix <run_id> / server_name <run_id> <n>. The trailing hyphen keeps
# run 12's prefix from matching run 123's servers.
server_prefix() { printf 'nova-window-%s-' "$1"; }
server_name()   { printf '%s%s' "$(server_prefix "$1")" "$2"; }

# host_sample [loadavg file] -> one JSON line. Reads /proc/loadavg; on a host
# without it prints load1 as absent rather than 0 (UNREAD is not clean).
host_sample() {
  local f="${1:-/proc/loadavg}" l1 l5 l15 np
  np="$(nproc 2>/dev/null || echo 0)"
  if [ -r "$f" ] && read -r l1 l5 l15 _ < "$f" && [ -n "$l1" ]; then
    printf '{"wall":"%s","load1":%s,"load5":%s,"load15":%s,"nproc":%s}\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$l1" "$l5" "$l15" "$np"
  else
    printf '{"wall":"%s","load1_unread":true,"nproc":%s}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$np"
  fi
}

# ---------------------------------------------------------------------------
# THE ARM. Needs a cluster, kubectl, python3 and the cloud. NOT exercised by
# --self-test: the watch/fifo/jsonpath choreography below runs first in CI.
# ---------------------------------------------------------------------------
get_token() {
  # Forced, not defaulted: the openrc's own value (ovhcloud-emea) 401s.
  export OS_USER_DOMAIN_NAME=Default
  local out rc=0 line
  out="$(bash "$ROOT/scripts/get-token.sh" --upstream)" || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "get-token.sh failed rc=$rc (output withheld: it can carry a credential)" >&2
    return 1
  fi
  NOVA_TOKEN=""; NOVA_URL=""
  while IFS= read -r line; do
    case "$line" in
      TOKEN=*)    NOVA_TOKEN="${line#TOKEN=}" ;;
      NOVA_URL=*) NOVA_URL="${line#NOVA_URL=}" ;;
    esac
  done <<< "$out"
  [ -n "$NOVA_TOKEN" ] && [ -n "$NOVA_URL" ] || { echo "get-token.sh returned no token or no Nova URL" >&2; return 1; }
  [ -z "${GITHUB_ACTIONS:-}" ] || echo "::add-mask::$NOVA_TOKEN"
  export NOVA_TOKEN NOVA_URL
}

put_secret() {
  # No pipe: a pipeline's status is its LAST command's, so `create | apply` would
  # read as success when the render failed and a stale token stayed in place.
  local f y rc=0
  f="$(mktemp)"; y="$(mktemp)"; chmod 600 "$f" "$y"
  printf '%s' "$NOVA_TOKEN" > "$f"
  kubectl -n "$NS" create secret generic nova-token --from-file=token="$f" \
    --dry-run=client -o yaml > "$y" && kubectl apply -f "$y" >/dev/null || rc=$?
  rm -f "$f" "$y"
  return "$rc"
}

apply_configuration() {
  kubectl apply -f - <<EOF
apiVersion: nova.openstack.krateo.io/v1alpha1
kind: InstanceConfiguration
metadata:
  name: nova-config
  namespace: $NS
spec:
  authentication:
    apiKey:
      tokenRef: {name: nova-token, namespace: $NS, key: token}
      header: X-Auth-Token
EOF
}

apply_instance() {
  local name="$1"
  kubectl apply -f - <<EOF
apiVersion: nova.openstack.krateo.io/v2-1
kind: Instance
metadata:
  name: $name
  namespace: $NS
spec:
  configurationRef: {name: nova-config, namespace: $NS}
  server:
    name: $name
    flavorRef: "$NOVA_ARM_FLAVOR"
    imageRef: "$NOVA_ARM_IMAGE"
    networks:
      - uuid: "$NOVA_ARM_NETWORK"
    metadata:
      managed-by: nova-create-window-arm
EOF
}

cond_field() { # <name> <type> <status|reason>
  kubectl -n "$NS" get "$CR_RESOURCE" "$1" \
    -o "jsonpath={.status.conditions[?(@.type==\"$2\")].$3}" 2>/dev/null
}

# resync_seconds -> the controller's OWN resyncInterval, read from its startup line
# (`"resyncInterval":180000000000` is nanoseconds), or empty when it is not there.
# Read BEFORE the create so each draw's expected window is stated before it is
# paid for. Empty is UNREAD and is reported as such; it is never defaulted to 180.
resync_seconds() {
  local dep ns
  dep="$(kubectl -n "$NS" get deploy -o name 2>/dev/null | grep -m1 -i instance)" || return 1
  [ -n "$dep" ] || return 1
  ns="$(kubectl -n "$NS" logs "$dep" --tail=-1 2>/dev/null \
        | sed -n 's/.*"resyncInterval":\([0-9][0-9]*\).*/\1/p' | head -1)"
  [ -n "$ns" ] || return 1
  echo $(( ns / 1000000000 ))
}

# _cleanup: runs on EXIT, INT and TERM once the run can have created anything, so
# an interrupted run still tears down. Idempotent: the arm's normal path also
# calls teardown_run, and the second call must not repeat it.
_cleanup() {
  local rc=$?
  [ "${TORN:-0}" = 1 ] && return "$rc"
  TORN=1
  teardown_run "$OUT" || { echo "cleanup: TEARDOWN UNPROVEN" >&2; exit 4; }
  return "$rc"
}

prefix_empty() { # predicate for wait_for: the vendor, READ, holds none under the prefix
  local n
  n="$(python3 "$PY" count --nova "$NOVA_URL" --prefix "$PREFIX")" || return 1
  [ "$n" = 0 ]
}

stop_controller() {
  # Without this, a direct vendor DELETE under a live CR is answered by the
  # controller CREATING A REPLACEMENT.
  local d
  for d in $(kubectl -n "$NS" get deploy -o name 2>/dev/null | grep -i instance || true); do
    kubectl -n "$NS" scale "$d" --replicas=0 >/dev/null 2>&1 || true
  done
}

teardown_run() { # <outdir> -> rc 0 proven, 4 not
  local out="$1" rc=0
  get_token || { echo "teardown: cannot get a token, so the vendor cannot be READ: UNPROVEN" >&2; return 4; }
  kubectl -n "$NS" delete "$CR_RESOURCE" --all --wait=true --timeout=300s >&2 || \
    echo "teardown: CR deletion did not complete; scaling the controller to zero before touching the vendor" >&2
  stop_controller
  python3 "$PY" teardown --nova "$NOVA_URL" --run-dir "$out" --prefix "$PREFIX" --deadline 420 || rc=$?
  return "$rc"
}

do_draw() { # <n> -> 0 (the draw's own verdict is analyse's)
  local n="$1" name d t0 kpid rpid ipid hpid ppid fpid ctl_dep synced=no i
  name="$(server_name "$RUN_ID" "$n")"
  d="$OUT/draw-$n"; mkdir -p "$d"
  echo "== draw $n: $name"
  get_token && put_secret || return 2
  # THE PER-DRAW WINDOW IS STATED HERE, BEFORE THE CREATE IS ISSUED, from the
  # controller's own resyncInterval. `analyse` reads it back from meta.json.
  local resync win="$WINDOW_S"
  resync="$(resync_seconds)" || resync=""
  if [ -n "$resync" ] && [ "$win" -le $(( resync + 60 )) ]; then win=$(( resync + 60 )); fi
  echo "draw $n: controller resyncInterval=${resync:-UNREAD}s; observation window ${win}s (stated before the create)"
  python3 - "$d/meta.json" "$n" "$name" "$win" "$SYNCED_WAIT_S" "$resync" <<'PY'
import json, sys
p, n, name, w, sw, rs = sys.argv[1:]
json.dump({"draw": int(n), "name": name, "window_s": float(w), "synced_wait_s": float(sw),
           "resync_s": int(rs) if rs else None}, open(p, "w"))
PY
  # host state beside the draw
  ( while [ ! -e "$d/stop" ]; do host_sample >> "$d/host.jsonl"; sleep 5; done ) &
  hpid=$!
  # the id, as the OPERATOR learns it, so by-id probing starts at the earliest
  # instant the id exists and not at the first time the list shows it
  ( while [ ! -e "$d/stop" ]; do
      i="$(kubectl -n "$NS" get "$CR_RESOURCE" "$name" -o jsonpath='{.status.server.id}' 2>/dev/null)"
      [ -n "$i" ] && { printf '%s' "$i" > "$d/id.tmp"; mv "$d/id.tmp" "$d/id"; break; }
      sleep 1
    done ) &
  ipid=$!
  NOVA_TOKEN="$NOVA_TOKEN" python3 "$PY" poll --nova "$NOVA_URL" --name "$name" --out "$d/poll.jsonl" \
    --id-file "$d/id" --stop-file "$d/stop" --deadline $(( SYNCED_WAIT_S + WINDOW_S + 400 )) \
    > "$d/poll.stdout" 2>&1 &
  ppid=$!
  # the watch: kubectl -> fifo -> reader. A fifo, not a pipe, so the reader's own
  # exit status and the kubectl pid are both ours to read.
  mkfifo "$d/fifo"
  kubectl -n "$NS" get "$CR_RESOURCE" --field-selector "metadata.name=$name" \
    --watch --output-watch-events -o json > "$d/fifo" 2> "$d/kubectl-watch.err" &
  kpid=$!
  python3 "$READER" < "$d/fifo" > "$d/events.jsonl" 2> "$d/reader.err" &
  rpid=$!
  sleep 2

  # ---- the create. The counter is written BEFORE the apply. ----
  python3 "$PY" run-json "$OUT/run.json" "draws_started=$n" "creates_issued=$n"
  apply_instance "$name" || { echo "apply failed" >&2; }

  # ---- PRECONDITION: the create succeeded. Not part of the result. ----
  t0=$SECONDS
  while [ $(( SECONDS - t0 )) -lt "$SYNCED_WAIT_S" ]; do
    if [ "$(cond_field "$name" Synced status)" = True ] && \
       [ "$(cond_field "$name" Synced reason)" = ReconcileSuccess ]; then synced=yes; break; fi
    sleep 2
  done
  echo "draw $n: Synced precondition: $synced after $(( SECONDS - t0 ))s (Synced status='$(cond_field "$name" Synced status)' reason='$(cond_field "$name" Synced reason)')"
  if [ "$synced" = yes ]; then
    # cap check by READING THE VENDOR: exactly one server under the prefix
    local cnt
    cnt="$(python3 "$PY" count --nova "$NOVA_URL" --prefix "$PREFIX")" || cnt=UNREAD
    if [ "$cnt" != 1 ]; then
      python3 "$PY" run-json "$OUT/run.json" "cap_breach=draw $n: vendor holds '$cnt' server(s) under the prefix, expected exactly 1"
      CAP_BREACH=1
    fi
    # the vendored flap watcher, an INDEPENDENT second instrument on the same CR
    ( fwrc=0
      python3 "$FLAPWATCH" --child "$CR_RESOURCE/$name" -n "$NS" --window "$win" \
        --kubeconfig "${KUBECONFIG:-$HOME/.kube/config}" --context "${KCTX:-$(kubectl config current-context)}" \
        > "$d/flapwatch.out" 2>&1 || fwrc=$?
      echo "$fwrc" > "$d/flapwatch.rc" ) &
    fpid=$!
    sleep "$win"
    wait "$fpid" 2>/dev/null || true
  fi
  # the controller's own log, for the PASS CLASSIFICATION (queuedAt is bound onto
  # every line of an event at v1.34.0+, at any log level, with no debug flag)
  ctl_dep="$(kubectl -n "$NS" get deploy -o name 2>/dev/null | grep -m1 -i instance || true)"
  if [ -n "$ctl_dep" ]; then
    kubectl -n "$NS" logs "$ctl_dep" --tail=-1 > "$d/controller.log" 2> "$d/controller.log.err" || \
      echo "draw $n: controller log read failed (the pass is UNREAD)" >&2
    # the vendored cadence tool, for the denominators and the configured resync.
    # With ONE CR it VOIDs on cadence by its own rule (< 3 full passes); that is
    # expected, its exit code is NOT a gate, and it is printed as evidence only.
    python3 "$HERE/resync-cadence.py" "$d/controller.log" > "$d/resync-cadence.out" 2>&1 || true
    sed 's/^/  resync-cadence: /' "$d/resync-cadence.out" | head -40
  else
    echo "draw $n: no instance controller Deployment found (the pass is UNREAD)" >&2
  fi
  kubectl -n "$NS" get "$CR_RESOURCE" "$name" -o json > "$d/final-cr.json" 2> "$d/final-cr.err" || \
    echo "draw $n: final CR read failed (final-cr.json will be absent: UNREAD)" >&2

  # ---- stop the instruments; the reader must get a CLEAN EOF ----
  kill "$kpid" 2>/dev/null || true
  wait "$rpid" 2>/dev/null || true
  : > "$d/stop"
  wait "$ppid" 2>/dev/null || true
  wait "$hpid" "$ipid" 2>/dev/null || true
  rm -f "$d/fifo"
  python3 "$PY" draw-check "$OUT" "$n" || true   # the live, per-draw read; analyse is the verdict

  # ---- delete this draw's CR, then READ the vendor until it is gone ----
  kubectl -n "$NS" delete "$CR_RESOURCE" "$name" --wait=true --timeout=300s >&2 || \
    echo "draw $n: CR delete did not complete in 300s" >&2
  wait_for "the vendor to hold no server under $PREFIX after draw $n" 60 5 prefix_empty || {
    python3 "$PY" run-json "$OUT/run.json" "cap_breach=draw $n: server still present after delete; refusing to create another"
    CAP_BREACH=1
  }
  return 0
}

run_arm() {
  OUT="$1"
  validate_inputs "${DRAWS:-1}" "${OASGEN_VERSION:-}" || exit 64
  local cs; cs="$(credential_state)" || void "the cloud credential or boot parameters are ${cs}. Nothing was created. This is VOID, not a pass."
  [ "$WINDOW_S" -gt 180 ] || die "WINDOW_S=$WINDOW_S does not exceed the 180s resync, so the window could not contain a re-observe" 64
  RUN_ID="${RUN_ID:-$(date -u +%m%d%H%M%S)}"
  RUN_ID="$(printf '%s' "$RUN_ID" | tr -c 'a-z0-9\n' '-' | tr -d '\n')"
  PREFIX="$(server_prefix "$RUN_ID")"
  mkdir -p "$OUT"
  CAP_BREACH=0

  # THE DISCARD RULES, STATED BEFORE THE DRAWS. They go into run.json now, and
  # `analyse` reads them from there; a run.json without them is VOID.
  local rules; rules="$(python3 "$PY" rules)"
  python3 "$PY" run-json "$OUT/run.json" "oasgen=\"${OASGEN_VERSION}\"" "draws_requested=${DRAWS:-1}" \
    "draws_started=0" "creates_issued=0" "run_id=\"$RUN_ID\"" "rules=$rules" "window_s=$WINDOW_S"
  echo "DISCARD RULES (fixed before draw 1): $rules"
  echo "run $RUN_ID, prefix $PREFIX, oasgen $OASGEN_VERSION, up to ${DRAWS:-1} sequential creates (cap $MAX_DRAWS)"

  get_token || void "could not obtain a Keystone token with the supplied credential"
  # THE INSTRUMENT MUST BE ABLE TO SEE A 404 before it may report that it saw none.
  python3 "$PY" control404 --nova "$NOVA_URL" --out "$OUT/control404.json" || void "the 404 control did not see a 404"
  local pre
  pre="$(python3 "$PY" count --nova "$NOVA_URL" --prefix "$PREFIX")" || void "cannot read the vendor's server list, so no create may be issued"
  [ "$pre" = 0 ] || die "the vendor already holds $pre server(s) under $PREFIX; refusing to create more" 4
  # From here a create can happen: an interrupted run must still tear down.
  TORN=0
  trap _cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  put_secret || die "could not store the token Secret" 4
  apply_configuration || die "could not apply the InstanceConfiguration" 4

  local n
  for (( n = 1; n <= MAX_DRAWS && n <= ${DRAWS:-1}; n++ )); do
    started="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("draws_started",0))' "$OUT/run.json")"
    draw_cap_ok "$started" || { echo "cap reached ($started of $MAX_DRAWS started): no further create" >&2; break; }
    do_draw "$n" || void "draw $n could not start"
    [ "$CAP_BREACH" = 0 ] || { echo "stopping: cap breach recorded, no further creates" >&2; break; }
  done

  local rc=0
  TORN=1
  teardown_run "$OUT" || rc=$?
  [ "$rc" = 0 ] || exit 4
  [ "$CAP_BREACH" = 0 ] || exit 4
  exit 0
}

# ---------------------------------------------------------------------------
self_test() {
  local fails=0 d msg
  d="$(mktemp -d)"
  t() { if [ "$2" = "$3" ]; then echo "  ok   $1"; else echo "  *** WRONG: $1 (want '$2' got '$3')"; fails=$((fails+1)); fi; }
  rcof() { local rc=0; "$@" >/dev/null 2>&1 || rc=$?; echo "$rc"; }
  # after the helpers are defined, before the first arm uses them
  assert_arms_callable

  t "draws=5 oasgen 0.74.0 accepted" 0 "$(rcof validate_inputs 5 0.74.0)"
  t "draws=1 accepted" 0 "$(rcof validate_inputs 1 0.74.0)"
  t "draws=6 REFUSED (64), not clamped" 64 "$(rcof validate_inputs 6 0.74.0)"
  t "draws=50 REFUSED" 64 "$(rcof validate_inputs 50 0.74.0)"
  t "draws=0 REFUSED" 64 "$(rcof validate_inputs 0 0.74.0)"
  t "draws=-1 REFUSED" 64 "$(rcof validate_inputs -1 0.74.0)"
  t "draws=5x REFUSED" 64 "$(rcof validate_inputs 5x 0.74.0)"
  t "draws empty REFUSED" 64 "$(rcof validate_inputs '' 0.74.0)"
  # captured to a variable first: `producer | grep -q` under pipefail can race
  # (the producer takes SIGPIPE) and read as a failed match.
  msg="$(validate_inputs 6 0.74.0 2>&1)"
  t "the refusal says it is a billing cap" yes "$(grep -q 'BILLABLE' <<< "$msg" && echo yes || echo no)"
  t "two oasgen versions in one dispatch REFUSED" 64 "$(rcof validate_inputs 5 '0.74.0 0.75.0')"
  t "a v-prefixed or partial version REFUSED" 64 "$(rcof validate_inputs 5 v0.74.0)"
  t "an empty version REFUSED" 64 "$(rcof validate_inputs 5 '')"
  t "draw_cap_ok: 0 started -> may create" 0 "$(rcof draw_cap_ok 0)"
  t "draw_cap_ok: 4 started -> may create the 5th" 0 "$(rcof draw_cap_ok 4)"
  t "draw_cap_ok: 5 started -> NO sixth" 1 "$(rcof draw_cap_ok 5)"
  t "draw_cap_ok: 9 started -> no" 1 "$(rcof draw_cap_ok 9)"
  t "draw_cap_ok: an unreadable counter -> no" 1 "$(rcof draw_cap_ok '')"
  mkdir -p "$d/bin0"
  t "an env MAX_DRAWS=9 does not raise the cap (DRAWS=7 still refused, 64)" 64 \
    "$(rcof env MAX_DRAWS=9 DRAWS=7 OASGEN_VERSION=0.74.0 bash "$HERE/probe-nova-create-window.sh" run "$d/o0")"
  t "the loop is bounded by MAX_DRAWS" yes \
    "$(grep -q 'for (( n = 1; n <= MAX_[D]RAWS &&' "$HERE/probe-nova-create-window.sh" && echo yes || echo no)"

  # credential
  t "full password credential + boot params -> ok" ok "$(env -i OS_USERNAME=u OS_PASSWORD=p OS_PROJECT_ID=x NOVA_ARM_FLAVOR=f NOVA_ARM_IMAGE=i NOVA_ARM_NETWORK=n bash -c '. '"$HERE"'/probe-nova-create-window.sh --source-only 2>/dev/null; credential_state' 2>/dev/null)"
  t "no credential at all -> absent (rc 2)" 2 \
    "$(env -i NOVA_ARM_FLAVOR=f NOVA_ARM_IMAGE=i NOVA_ARM_NETWORK=n bash -c '. '"$HERE"'/probe-nova-create-window.sh --source-only; credential_state >/dev/null; echo $?' 2>/dev/null)"
  msg="$(env -i NOVA_ARM_FLAVOR=f NOVA_ARM_IMAGE=i NOVA_ARM_NETWORK=n bash -c '. '"$HERE"'/probe-nova-create-window.sh --source-only; credential_state' 2>/dev/null)" || true
  t "absent output NAMES what is missing" yes "$(grep -q 'OS_USERNAME' <<< "$msg" && echo yes || echo no)"
  t "a HALF-set application credential is absent, not a fallback" 2 \
    "$(env -i OS_APPLICATION_CREDENTIAL_ID=a NOVA_ARM_FLAVOR=f NOVA_ARM_IMAGE=i NOVA_ARM_NETWORK=n bash -c '. '"$HERE"'/probe-nova-create-window.sh --source-only; credential_state >/dev/null; echo $?' 2>/dev/null)"
  t "a half-set application credential does NOT fall back to a complete password credential" 2 \
    "$(env -i OS_APPLICATION_CREDENTIAL_ID=a OS_USERNAME=u OS_PASSWORD=p OS_PROJECT_ID=x NOVA_ARM_FLAVOR=f NOVA_ARM_IMAGE=i NOVA_ARM_NETWORK=n bash -c '. '"$HERE"'/probe-nova-create-window.sh --source-only; credential_state >/dev/null; echo $?' 2>/dev/null)"
  t "a full application credential is ok" ok \
    "$(env -i OS_APPLICATION_CREDENTIAL_ID=a OS_APPLICATION_CREDENTIAL_SECRET=b NOVA_ARM_FLAVOR=f NOVA_ARM_IMAGE=i NOVA_ARM_NETWORK=n bash -c '. '"$HERE"'/probe-nova-create-window.sh --source-only; credential_state' 2>/dev/null)"
  t "credential ok but a boot parameter missing -> absent" 2 \
    "$(env -i OS_USERNAME=u OS_PASSWORD=p OS_PROJECT_ID=x NOVA_ARM_FLAVOR=f bash -c '. '"$HERE"'/probe-nova-create-window.sh --source-only; credential_state >/dev/null; echo $?' 2>/dev/null)"

  # names
  t "prefix has a trailing hyphen" "nova-window-12-" "$(server_prefix 12)"
  t "name is prefix + n" "nova-window-12-3" "$(server_name 12 3)"
  t "run 12's prefix does NOT match run 123's server" no \
    "$(case "$(server_name 123 1)" in "$(server_prefix 12)"*) echo yes ;; *) echo no ;; esac)"

  # host sample
  printf '0.52 0.40 0.30 1/200 9999\n' > "$d/la"
  t "host_sample reads load1" 0.52 "$(host_sample "$d/la" | python3 -c 'import json,sys; print(json.load(sys.stdin)["load1"])')"
  t "an unreadable loadavg is UNREAD, never 0" true \
    "$(host_sample "$d/missing" | python3 -c 'import json,sys; j=json.load(sys.stdin); print(str(("load1" not in j) and j.get("load1_unread")).lower())')"

  # cleanup is idempotent and runs teardown once
  t "_cleanup tears down ONCE however many times it fires" 1 \
    "$(bash -c '. '"$HERE"'/probe-nova-create-window.sh --source-only; n=0; teardown_run() { n=$((n+1)); }; OUT=x; TORN=0; _cleanup; _cleanup; _cleanup; echo $n' 2>/dev/null)"
  t "an interrupted run (INT/TERM) is trapped to the cleanup" yes \
    "$(grep -q "^  [t]rap _cleanup EXIT" "$HERE/probe-nova-create-window.sh" && grep -q "^  [t]rap 'exit 130' INT" "$HERE/probe-nova-create-window.sh" && grep -q "^  [t]rap 'exit 143' TERM" "$HERE/probe-nova-create-window.sh" && echo yes || echo no)"
  t "a failed teardown inside the trap exits 4" 4 \
    "$(rcof bash -c '. '"$HERE"'/probe-nova-create-window.sh --source-only; teardown_run() { return 4; }; OUT=x; TORN=0; _cleanup')"
  t "the vendored scripts are the ones named" yes \
    "$([ -f "$HERE/capture-watch-stream.py" ] && [ -f "$HERE/watch-condition-flap.py" ] && grep -q '[R]EADER=.*capture-watch-stream' "$HERE/probe-nova-create-window.sh" && echo yes || echo no)"

  # THE REFUSALS HAPPEN BEFORE ANY CLUSTER OR CLOUD CALL. kubectl/curl/python-poll
  # are replaced by stubs that leave a marker; no marker may exist.
  mkdir -p "$d/bin"
  for c in kubectl curl helm; do
    printf '#!/bin/sh\necho "%s" >> "%s/called"\nexit 0\n' "\$0 \$*" "$d" > "$d/bin/$c"; chmod +x "$d/bin/$c"
  done
  local rc
  rc="$(rcof env PATH="$d/bin:$PATH" DRAWS=6 OASGEN_VERSION=0.74.0 bash "$HERE/probe-nova-create-window.sh" run "$d/o1")"
  t "run with DRAWS=6 exits 64" 64 "$rc"
  t "  and touched NO cluster or cloud tool" no "$([ -e "$d/called" ] && echo yes || echo no)"
  rc="$(rcof env -i PATH="$d/bin:$PATH" DRAWS=5 OASGEN_VERSION=0.74.0 bash "$HERE/probe-nova-create-window.sh" run "$d/o2")"
  t "run with NO credential exits 2 (VOID)" 2 "$rc"
  t "  and touched NO cluster or cloud tool" no "$([ -e "$d/called" ] && echo yes || echo no)"
  msg="$(env -i PATH="$d/bin:$PATH" DRAWS=5 OASGEN_VERSION=0.74.0 bash "$HERE/probe-nova-create-window.sh" run "$d/o3" 2>&1)" || true
  t "  and the message says VOID and absent" yes "$(grep -q 'VOID:.*absent' <<< "$msg" && echo yes || echo no)"
  t "  and created no output directory worth reading as a result" no "$([ -e "$d/o2/run.json" ] && echo yes || echo no)"
  rc="$(rcof env -i PATH="$d/bin:$PATH" bash "$HERE/probe-nova-create-window.sh" check-credential)"
  rc="$(rcof env -i PATH="$d/bin:$PATH" OASGEN_VERSION=0.99.0 bash "$HERE/probe-nova-create-window.sh" run "$d/o4")"
  t "DRAWS unset defaults to ONE (accepted: reaches the credential gate, VOID 2, not refusal 64)" 2 "$rc"
  t "check-credential with nothing set exits 2" 2 "$rc"
  rc="$(rcof env -i PATH="$d/bin:$PATH" OS_USERNAME=u OS_PASSWORD=p OS_PROJECT_ID=x NOVA_ARM_FLAVOR=f NOVA_ARM_IMAGE=i NOVA_ARM_NETWORK=n bash "$HERE/probe-nova-create-window.sh" check-credential)"
  t "check-credential with everything set exits 0" 0 "$rc"
  rc="$(rcof bash "$HERE/probe-nova-create-window.sh" bogus-mode)"
  t "an unknown mode is a usage error 64, never VOID's 2" 64 "$rc"

  rm -rf "$d"
  if [ "$fails" -ne 0 ]; then echo "--- FAIL: $fails arm(s) ---"; return 1; fi
  echo "--- all arms pass ---"
}

# shellcheck disable=SC2317  # `exit` is the fallback when EXECUTED rather than sourced
case "${1:-}" in
  --source-only) return 0 2>/dev/null || exit 0 ;;
  --self-test) self_test; exit $? ;;
  check-credential)
    cs="$(credential_state)" || void "credential/boot parameters ${cs}. Nothing was created. VOID, not a pass."
    echo "credential and boot parameters present"; exit 0 ;;
  check-inputs)
    validate_inputs "${DRAWS:-}" "${OASGEN_VERSION:-}" || exit 64
    [ "${WINDOW_S:-240}" -gt 180 ] 2>/dev/null || die "WINDOW_S=${WINDOW_S:-} must be a number above the 180s resync" 64
    echo "inputs ok: draws=$DRAWS (cap $MAX_DRAWS) oasgen=$OASGEN_VERSION window=${WINDOW_S}s"; exit 0 ;;
  nova-url)
    get_token || exit 2
    [ -z "${GITHUB_ACTIONS:-}" ] || echo "::add-mask::$NOVA_URL"
    printf '%s\n' "$NOVA_URL"; exit 0 ;;
  run)
    [ -n "${2:-}" ] || die "usage: run <outdir>"
    OUT="$2"
    # the workflow supplies these; defaults keep local misuse loud
    : "${OASGEN_VERSION:=}"
    run_arm "$2" ;;
  teardown)
    [ -n "${2:-}" ] || die "usage: teardown <outdir>"
    OUT="$2"
    RUN_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("run_id",""))' "$OUT/run.json" 2>/dev/null)"
    [ -n "$RUN_ID" ] || { echo "teardown: no run_id in $OUT/run.json, so the prefix is unknown: UNPROVEN" >&2; exit 4; }
    PREFIX="$(server_prefix "$RUN_ID")"
    teardown_run "$OUT" || exit 4
    exit 0 ;;
  *) die "usage: $0 --self-test | check-credential | check-inputs | nova-url | run <outdir> | teardown <outdir>" 64 ;;
esac
