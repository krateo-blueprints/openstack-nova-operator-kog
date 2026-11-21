#!/usr/bin/env bash
# shellcheck disable=SC2016  # EVERY arm body below is single-quoted ON PURPOSE:
# its `$(...)` must reach the `bash -c` under test UNEXPANDED. Expanding it here
# would evaluate the arms in THIS shell, where the functions do exist -- which is
# precisely the confusion the subject exists to prevent. This sits directly after
# the shebang because that is the only position shellcheck treats as file-wide.
# Arms for assert_arms_callable. Run directly: bash hack/lib/self-test-guard-self-test.sh
#
# The subject EXITS 3 when it fires, so each arm runs it in a SUBSHELL and reads
# the status. An arm that let it exit would end this script and every later arm
# would silently not run -- the same shape as the bug being guarded against.
set -uo pipefail
export LC_ALL=C
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 3
GUARD="hack/lib/self-test-guard.sh"
pass=0; fail=0
t() { # t <name> <want rc> <want substring|-> <script body>
  local name=$1 wrc=$2 wsub=$3 body=$4 out rc
  out=$(bash -c "source $GUARD
$body" 2>&1); rc=$?
  if [ "$rc" != "$wrc" ]; then
    printf '  %-58s FAIL want rc=%s got %s\n' "$name" "$wrc" "$rc"; fail=$((fail+1)); return
  fi
  if [ "$wsub" != "-" ] && ! printf '%s' "$out" | grep -qF -- "$wsub"; then
    printf '  %-58s FAIL rc ok but missing %q\n' "$name" "$wsub"; fail=$((fail+1)); return
  fi
  printf '  %-58s PASS\n' "$name"; pass=$((pass+1))
}

# 1. the real failure: a function used by an arm, defined BELOW the call site
t "a function defined after the call site is CAUGHT" 3 "not_yet" '
self_test() { assert_arms_callable; printf "%s" "$(not_yet 1)"; }
self_test'

# 2. the same code with the definition moved ABOVE -- the control. Without this,
#    arm 1 would pass even if the guard rejected everything.
t "the SAME arms pass once it is defined above" 0 "-" '
not_yet() { printf ok; }
self_test() { assert_arms_callable; printf "%s" "$(not_yet 1)"; }
self_test'

# 3. builtins and binaries must NOT be false positives
t "printf, grep, sed, jq are not reported missing" 0 "-" '
self_test() { assert_arms_callable; printf "%s%s%s" "$(printf x)" "$(echo y | grep y)" "$(echo z | sed s/z/z/)"; }
self_test'

# 4. the TWO-EMPTY-ARMS case, which is why a guard beats careful reading
t "two undefined functions are BOTH named, not just the first" 3 "beta" '
self_test() { assert_arms_callable; printf "%s%s" "$(alpha)" "$(beta)"; }
self_test'
t "and the first is named too" 3 "alpha" '
self_test() { assert_arms_callable; printf "%s%s" "$(alpha)" "$(beta)"; }
self_test'

# 4b. THE LIMIT, asserted rather than only documented: the guard does nothing
#     unless a self_test actually calls it. A probe that forgets the call gets no
#     protection, which is why the five probes are edited rather than just this
#     lib added.
t "it is INERT when self_test never calls it" 0 "-" '
self_test() { printf "%s" "$(never_defined)"; }
self_test'

# 4c. AN ASSIGNMENT IS NOT A CALL. `$(_tn=$(...) ...)` opens a substitution with
#     a variable assignment; the first version of the guard reported `_tn` as an
#     undefined command. It found this false positive in probe-observe-rejection.sh
#     within a minute of being wired up -- a real one, on real code.
t "an assignment inside \$( ) is NOT reported as a command" 0 "-" '
self_test() { assert_arms_callable; printf "%s" "$(_tn=$(printf 7); printf "%s" "$_tn")"; }
self_test'
#     and the discriminator: the exclusion must not swallow a REAL missing name
#     that happens to sit next to an assignment.
t "an assignment does not mask a genuinely missing function" 3 "alsomissing" '
self_test() { assert_arms_callable; printf "%s%s" "$(_tn=$(printf 7); printf "%s" "$_tn")" "$(alsomissing)"; }
self_test'

# 4d. A FUNCTION THE SELF-TEST DEFINES INSIDE ITSELF is available to its own
#     arms by the time they run. probe-401.sh defines `a()` and `v()` that way
#     and probe-lease-readiness.sh defines `t()`; the first version of this guard
#     flagged all three, which is how the limitation was found -- by running it.
t "a function nested inside self_test is NOT reported" 0 "-" '
self_test() { assert_arms_callable; helper() { printf ok; }; printf "%s" "$(helper)"; }
self_test'
#     and the discriminator: nesting one must not excuse a different missing one
t "a nested definition does not mask a missing function" 3 "stillgone" '
self_test() { assert_arms_callable; helper() { printf ok; }; printf "%s%s" "$(helper)" "$(stillgone)"; }
self_test'

# 5. no self_test at all is not an error -- the guard is opt-in
t "a script with no self_test is left alone" 0 "-" 'assert_arms_callable'

# 6. the message must explain WHY, not just name the symbol
t "the message explains the empty-string mechanism" 3 "EMPTY" '
self_test() { assert_arms_callable; printf "%s" "$(nope)"; }
self_test'

printf '\n  %s passed, %s failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ] || exit 1
