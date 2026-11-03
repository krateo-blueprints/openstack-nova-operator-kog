#!/usr/bin/env bash
#
# Assert get-token.sh's credential-free paths. No network, no credentials.
#
# WHY THIS EXISTS. `scripts/get-token.sh` was the only user-facing script in
# this repo with ZERO coverage -- CI referenced every other script in hack/ and
# scripts/ at least once. It is also the script a reader of the quickstart
# actually runs, and the one that handles credentials.
#
# Its happy path needs a real Keystone and cannot be tested here. Its REFUSALS
# can: every case below exits before the first network call, so the whole suite
# runs in CI with no cluster, no credential and no egress.
#
# THE CASE THAT MATTERS MOST is the half-set application credential. The script
# chooses its auth method by inspecting the environment:
#
#   OS_APPLICATION_CREDENTIAL_ID + ..._SECRET  ->  application_credential
#   otherwise                                  ->  password
#
# If exactly one of the pair were set and the script FELL BACK to password auth,
# it would send a password the operator had deliberately chosen not to use --
# silently, with a successful exit. That guard was written and never exercised.
# A guard nobody has run is a guard nobody knows the shape of.
#
# EVERY VALUE HERE IS AN INERT SENTINEL, assembled from variables rather than
# written as a literal beside its name. gitleaks' `curl-auth-header` rule was
# right to flag that shape in hack/probe-redirect.sh: a credential-shaped
# literal next to a credential-shaped name is the pattern that IS a leak in
# another file, whatever the value happens to be.
#
# OS_AUTH_URL points at an RFC 2606 reserved name throughout. If a case that is
# supposed to refuse before the network ever reached `curl`, it would fail on
# DNS rather than quietly succeeding -- so a passing assertion cannot be hiding
# an escape to the network.
set -uo pipefail

SCRIPT="$(dirname "$0")/get-token.sh"
[ -x "$SCRIPT" ] || { echo "not executable: $SCRIPT" >&2; exit 1; }

SENTINEL_ID="sentinel-app-cred-id-not-a-credential"
SENTINEL_SECRET="sentinel-app-cred-secret-not-a-credential"
SENTINEL_PW="sentinel-not-a-password"
UNROUTABLE="https://keystone.invalid/v3"

pass=0; fail=0

# Run get-token.sh in a scrubbed environment with only the named vars set.
# `env -i` matters: inheriting a real openrc from the operator's shell would
# make these results depend on who ran them.
# EXPECT_MISMATCH=1 means the caller WANTS this case not to match, for the
# meta-control. It is labelled "MISMATCH (expected)" rather than FAIL, because a
# literal FAIL here would trip anyone grepping CI logs for failures -- and a
# test suite whose healthy output contains the word FAIL trains readers to
# ignore it, which is how a real failure gets skimmed past.
EXPECT_MISMATCH=0

run_case() {
  local desc=$1 want_exit=$2 want_msg=$3; shift 3
  local out rc
  out=$(env -i PATH="$PATH" HOME="$HOME" "$@" bash "$SCRIPT" 2>&1)
  rc=$?
  local ok=1
  [ "$rc" = "$want_exit" ] || ok=0
  if [ -n "$want_msg" ] && ! printf '%s\n' "$out" | grep -qF "$want_msg"; then ok=0; fi
  if [ "$ok" = "1" ]; then
    pass=$((pass + 1)); printf '  PASS  %s (exit %s)\n' "$desc" "$rc"
  elif [ "$EXPECT_MISMATCH" = "1" ]; then
    fail=$((fail + 1))   # counted, then reversed by the meta-control
    printf '  MISMATCH (expected)  %s -- wanted exit %s, got %s\n' "$desc" "$want_exit" "$rc"
  else
    fail=$((fail + 1))
    printf '  FAIL  %s\n        wanted exit %s containing %q\n        got exit %s:\n%s\n' \
      "$desc" "$want_exit" "$want_msg" "$rc" "$(printf '%s\n' "$out" | sed 's/^/          /')"
  fi
}

echo "== get-token.sh refusals (no network, no credentials)"

# 1. The guard that has never been exercised, both directions. A fallback to
#    password auth here would send a credential the operator chose not to use.
run_case "ID set without SECRET refuses" 2 "Set BOTH" \
  OS_APPLICATION_CREDENTIAL_ID="$SENTINEL_ID" \
  OS_AUTH_URL="$UNROUTABLE"

run_case "SECRET set without ID refuses" 2 "Set BOTH" \
  OS_APPLICATION_CREDENTIAL_SECRET="$SENTINEL_SECRET" \
  OS_AUTH_URL="$UNROUTABLE"

# 2. The password path must name the missing variable rather than failing late.
run_case "empty environment names a missing var" 2 "missing required env var" \
  OS_AUTH_URL="$UNROUTABLE"

# 3. --help must succeed and print usage. A zero exit with no output would mean
#    the sed range that renders the header had silently drifted.
help_out=$(env -i PATH="$PATH" HOME="$HOME" bash "$SCRIPT" --help 2>&1); help_rc=$?
if [ "$help_rc" = "0" ] && printf '%s\n' "$help_out" | grep -qF "OS_APPLICATION_CREDENTIAL_ID"; then
  pass=$((pass + 1)); echo "  PASS  --help exits 0 and documents the app-credential vars"
else
  fail=$((fail + 1))
  echo "  FAIL  --help: exit $help_rc, and it did not mention OS_APPLICATION_CREDENTIAL_ID"
  echo "        (a zero exit with drifted output is worse than a failure: the"
  echo "         documented vars are how an operator learns the app-cred path)"
fi

# META-CONTROL. Every assertion above is a refusal, and a harness that reported
# PASS unconditionally would look identical. So assert a deliberately WRONG
# expectation and require it to FAIL. Without this, "4 passed" is not evidence.
echo "== meta-control: the harness must be able to fail"
before_fail=$fail
EXPECT_MISMATCH=1
run_case "a deliberately wrong expectation" 99 "this message does not exist" \
  OS_AUTH_URL="$UNROUTABLE"
EXPECT_MISMATCH=0
if [ "$fail" -gt "$before_fail" ]; then
  fail=$((fail - 1))   # expected; do not count against the suite
  pass=$((pass + 1))
  echo "  PASS  the harness reported a failure when it should -- the results above"
  echo "        are evidence rather than an unconditional PASS"
else
  echo "  FAIL  the harness did NOT fail a wrong expectation. Every PASS above is"
  echo "        meaningless, because the harness cannot distinguish outcomes."
  fail=$((fail + 1))
fi

printf '\n  %d passed, %d failed\n' "$pass" "$fail"
[ "$fail" = "0" ] || exit 1
echo "  get-token.sh: all credential-free refusals behave as documented"
