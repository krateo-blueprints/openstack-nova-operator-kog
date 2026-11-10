#!/bin/bash
# Self-test for hack/lib/wait.sh. Run it: hack/lib/wait-self-test.sh
# Every arm asserts BOTH the exit code and, where it matters, the message --
# because a helper whose timeout path is untested is the defect it exists to
# prevent. Exits 1 if any arm misbehaves.
set -uo pipefail
export LC_ALL=C
cd "$(dirname "$0")" || exit 2
# shellcheck source=hack/lib/wait.sh
source ./wait.sh

PASS=0; FAIL=0
chk() { # chk <label> <expected-rc> <actual-rc> [grep-pattern] [file]
  local lbl="$1" exp="$2" got="$3" pat="${4:-}" f="${5:-}"
  if [ "$got" != "$exp" ]; then printf '  %-46s FAIL rc=%s want=%s\n' "$lbl" "$got" "$exp"; FAIL=$((FAIL+1)); return; fi
  if [ -n "$pat" ] && ! grep -q -- "$pat" "$f"; then
    printf '  %-46s FAIL rc ok but message lacks %s\n' "$lbl" "$pat"; FAIL=$((FAIL+1)); return
  fi
  printf '  %-46s PASS\n' "$lbl"; PASS=$((PASS+1))
}

O=$(mktemp)

# --- success paths
wait_for "a truth" 3 1 true > "$O" 2>&1; chk "predicate true immediately" 0 "$?"

COUNT_FILE=$(mktemp); echo 0 > "$COUNT_FILE"
_third() { local n; n=$(cat "$COUNT_FILE"); n=$((n+1)); echo "$n" > "$COUNT_FILE"; [ "$n" -ge 3 ]; }
wait_for "the third attempt" 5 1 _third > "$O" 2>&1; chk "predicate true on 3rd attempt" 0 "$?"
chk "  and it really took 3 attempts" 3 "$(cat "$COUNT_FILE")"

_pipe() { printf 'alpha\nbeta\n' | grep -q beta; }
wait_for "a pipeline predicate" 2 1 _pipe > "$O" 2>&1; chk "pipeline via function name" 0 "$?"

# a predicate that assigns a caller variable must be visible afterwards
CAPTURED=""
_assign() { CAPTURED=hello; [ -n "$CAPTURED" ]; }
wait_for "an assigning predicate" 2 1 _assign > "$O" 2>&1
chk "predicate can set a caller variable" "hello" "$CAPTURED"

# --- the timeout path, which is the whole point
wait_for "something that never happens" 2 1 false > "$O" 2>&1
chk "never true -> rc 1" 1 "$?" "TIMEOUT after 2 attempts" "$O"
chk "  message names WHAT was waited for" 0 0 "something that never happens" "$O"
chk "  message says it is not a measurement" 0 0 "not a measurement" "$O"
chk "  message prints the predicate" 0 0 "predicate:  false" "$O"

_noisy() { echo 'the tool rejected your flag' >&2; return 1; }
wait_for "a noisy failure" 2 1 _noisy > "$O" 2>&1
chk "predicate stderr is surfaced" 1 "$?" "the tool rejected your flag" "$O"

wait_for "a missing binary" 2 1 definitely-no-such-command-xyz > "$O" 2>&1
chk "missing command -> rc 1, not a pass" 1 "$?" "TIMEOUT" "$O"

# --- refusals: a misuse must not look like a pass
wait_for "zero attempts" 0 1 true > "$O" 2>&1
chk "attempts=0 refused (would be a false pass)" 2 "$?" "zero attempts would" "$O"
wait_for "bad attempts" abc 1 true > "$O" 2>&1
chk "non-integer attempts refused" 2 "$?" "must be integers" "$O"
wait_for "too few args" 3 1 > "$O" 2>&1
chk "missing predicate refused" 2 "$?" "usage:" "$O"

# --- it must not sleep after the final failed attempt
S=$SECONDS
wait_for "no trailing sleep" 2 3 false > "$O" 2>&1
E=$((SECONDS-S))
if [ "$E" -le 4 ]; then printf '  %-46s PASS (%ss, one interval not two)\n' "no sleep after the last attempt" "$E"; PASS=$((PASS+1));
else printf '  %-46s FAIL (%ss, slept after the final attempt)\n' "no sleep after the last attempt" "$E"; FAIL=$((FAIL+1)); fi

rm -f "$O" "$COUNT_FILE"
echo
echo "  $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
