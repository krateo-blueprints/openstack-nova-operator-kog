# shellcheck shell=bash
# shellcheck disable=SC2016  # the `$(` and `()` patterns below are LITERAL TEXT
# matched in grep output, not expansions. Expanding them is precisely the bug
# this file exists to catch. File-wide because several lines carry them.
# assert_arms_callable -- every function a self-test INVOKES must already be
# defined when the arms run.
#
# Source this file; it defines one function and sets nothing else.
#
#   source hack/lib/self-test-guard.sh
#   self_test() { assert_arms_callable; t "..." want "$(some_fn ...)"; ... }
#
# WHY THIS EXISTS, and it is measured rather than imagined. On 2026-11-21 a new
# classifier was added to probe-lease-readiness.sh with seven arms, and the
# function was defined BELOW the line that calls self_test. All seven arms ran
# against a function that did not exist yet:
#
#   t "a failed write is MINE" "write-failed" "$(restore_classify 1 yes ready)"
#                                              ^ command not found
#
# A BASH COMMAND SUBSTITUTION TURNS "command not found" INTO AN EMPTY STRING.
# There is no error, no non-zero status reaching the arm, nothing on stderr that
# the harness reads. Every arm compared "" against its expectation and failed --
# which looked like seven broken assertions rather than one ordering mistake.
#
# WORSE, AND THIS IS THE REASON FOR A GUARD RATHER THAN CAREFUL READING: two
# arms whose job is to prove two outputs DIFFER both received "", so they
# compared EQUAL. A discriminator arm reported SAME. Had the two residue arms
# been the only new ones, seven empty strings would have passed as agreement.
#
# HOW IT WORKS. It reads self_test's own body with `declare -f`, extracts every
# identifier appearing at the start of a command substitution, and requires each
# to resolve via `command -v` -- which accepts functions, builtins and PATH
# binaries alike, so `printf`, `grep` and `jq` are not false positives while a
# not-yet-defined function is caught. THERE IS NO LIST TO MAINTAIN: the set is
# derived from the arms themselves, so it cannot drift out of date the way a
# hand-written list of function names would.
#
# AN ASSIGNMENT IS NOT A CALL. `$(_tn=$(...) ...)` opens a substitution with a
# VARIABLE ASSIGNMENT, and the first version of this guard reported `_tn` as an
# undefined command -- a false positive it found in probe-observe-rejection.sh
# within a minute of being wired up. Candidates ending in `=` are dropped.
#
# LIMIT, stated rather than left to be discovered: it inspects `$( name ...`
# only. A function invoked at a command position (`if some_fn; then`) is not
# checked, because distinguishing that from shell keywords and assignments needs
# a parser, and the silent-empty-string failure this exists for is specific to
# command substitution. A call in the OTHER form fails with a visible 127.
assert_arms_callable() {
  local body f missing=""
  body=$(declare -f self_test 2>/dev/null) || return 0
  [ -n "$body" ] || return 0
  
  # Functions the self-test defines INSIDE itself are available to its own arms
  # by the time they run, even though they do not exist at this instant. They
  # must not be reported. (probe-401.sh defines `a()` and `v()` this way and
  # probe-lease-readiness.sh defines `t()`; the first version of this guard
  # flagged all three.)
  #
  # NOTE THE `function` KEYWORD. `declare -f` does not reprint a nested
  # definition the way it was written: `helper() { ...; }` comes back as
  # `    function helper () `. A pattern written against the SOURCE form matches
  # nothing here, silently, which is how the first version of this still flagged
  # probe-401's `a` and `v` after they had supposedly been excluded.
  local nested
  nested=$(printf '%s\n' "$body" \
           | grep -oE '^[[:space:]]+(function[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*[[:space:]]*\(\)' \
           | sed -e 's/^[[:space:]]*//' -e 's/^function[[:space:]]*//' -e 's/[[:space:]]*()$//' \
           | sort -u)
  for f in $(printf '%s\n' "$body" \
             | grep -oE '\$\([[:space:]]*[A-Za-z_][A-Za-z0-9_]*=?' \
             | grep -v '=$' \
             | sed -e 's/^\$(//' -e 's/^[[:space:]]*//' | sort -u); do
    printf '%s\n' "$nested" | grep -qxF "$f" && continue
    command -v "$f" > /dev/null 2>&1 || missing="$missing $f"
  done
  [ -z "$missing" ] && return 0
  printf '\033[31mself-test: invoked by the arms but does NOT resolve:%s\033[0m\n' "$missing" >&2
  printf 'A bash command substitution turns "command not found" into an EMPTY\n' >&2
  printf 'STRING, so every arm using one would compare "" against its expectation\n' >&2
  printf -- '-- and two such arms compare EQUAL to each other, which reads as\n' >&2
  printf 'agreement. Define the function ABOVE the line that calls self_test.\n' >&2
  exit 3
}
