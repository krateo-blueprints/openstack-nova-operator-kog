# shellcheck shell=bash
# wait_for -- poll a predicate until it succeeds, and FAIL LOUDLY if it never does.
#
# Source this file; it defines one function and sets nothing else.
#
#   source hack/lib/wait.sh
#   wait_for <what> <attempts> <interval-seconds> <command> [args...]
#
# WHY THIS EXISTS. The repo had nine bounded retry loops of the shape
#
#     for _ in $(seq 1 60); do <predicate> && break; sleep 3; done
#     <next command>
#
# and not one of them could tell its own timeout from its own success: the loop
# falls through either way. In the worst case found, the next line was
# `ok "ComputeFlavor CRD Established"`, so 120 seconds of discovery never
# serving the kind printed as a PASS (fixed separately, in probe-361.sh). In the
# gentler cases the following `kubectl wait --timeout` fails loudly, so the gate
# is real and the loop is only a fast path -- but the error then describes the
# symptom and never says which wait expired.
#
# So: `wait_for` returns 0 only when the predicate actually succeeded, and on
# timeout prints what it waited for, for how long, the predicate itself, and the
# predicate's own last stderr. Under `set -e` (GitHub's `bash -e {0}` included)
# a timeout aborts the step, which is the intent.
#
# THE PREDICATE IS A COMMAND AND ITS ARGUMENTS, NOT A STRING, and deliberately:
# a string would need `eval`, and these call sites interpolate $GROUP, $NS and
# $RD into predicates that already contain quotes -- inside YAML. For a pipeline
# or a multi-step predicate, define a function and pass its NAME; bash invokes a
# function through "$@" exactly like a command, so the predicate can pipe, grep
# and assign caller variables with no quoting puzzle:
#
#   _served() { kubectl api-resources --api-group="$GROUP" | grep -q -w ComputeFlavor; }
#   wait_for "discovery to serve ComputeFlavor in $GROUP" 40 3 _served
#
# STDERR IS KEPT, not discarded. A predicate that fails because the tool
# rejected a flag, or because RBAC refused the verb, must not read the same as a
# predicate that is merely not true yet -- that is the defect this file's whole
# family comes from, and `2>/dev/null` is its shell spelling.
wait_for() {
  if [ "$#" -lt 4 ]; then
    echo "wait_for: usage: wait_for <what> <attempts> <interval> <command> [args...]" >&2
    return 2
  fi
  local what="$1" attempts="$2" interval="$3"
  shift 3

  case "$attempts$interval" in
    *[!0-9]*) echo "wait_for: attempts and interval must be integers (got '$attempts' '$interval')" >&2; return 2 ;;
  esac
  if [ "$attempts" -lt 1 ]; then
    echo "wait_for: attempts must be >= 1 (got $attempts); zero attempts would" >&2
    echo "  return without ever evaluating the predicate, which is the exact" >&2
    echo "  false pass this function exists to prevent." >&2
    return 2
  fi

  local err i
  err="$(mktemp)"
  for (( i = 1; i <= attempts; i++ )); do
    if "$@" > /dev/null 2> "$err"; then
      rm -f "$err"
      return 0
    fi
    [ "$i" -lt "$attempts" ] && sleep "$interval"
  done

  {
    echo "TIMEOUT after ${attempts} attempts x ${interval}s ($(( attempts * interval ))s):"
    echo "  waited for: $what"
    echo "  predicate:  $*"
    echo "  This is a TIMEOUT, not a measurement -- the predicate never succeeded."
    echo "  Predicate's last stderr (empty means it failed silently, not that it ran clean):"
    sed 's/^/    /' "$err"
  } >&2
  rm -f "$err"
  return 1
}
