#!/usr/bin/env bash
#
# Assert that smoke.yaml's sample-apply guard refuses what it must refuse.
# No cluster, no network, no credentials.
#
# WHY THIS EXISTS. `THE GUARD — every shipped sample must apply` applies every
# `chart/samples/*.yaml` against a live API server. The smoke job's own run is
# that guard's POSITIVE arm and it runs on every PR. Its REFUSAL arms never run
# there, because this repo always has samples and they always apply. So the only
# paths that matter -- an empty sample set, and a sample that does not apply --
# were never exercised by anything. A guard whose refusal has never run is a
# guard nobody knows the shape of.
#
# THE SUBJECT IS THE WORKFLOW TEXT, NOT A COPY OF IT. The step is pulled out of
# smoke.yaml by a structural YAML parse keyed on its `name`, asserting that
# exactly one step matched. This matters because the first calibration of this
# guard ran a RECONSTRUCTION of its logic in a scratch file, and proved only
# that the reconstruction worked. That is the same defect three screens in this
# repo have already had: the check was correct, and its subject was not the
# thing intended. A peer named the rule -- the arm has to inject the defect into
# the SUBJECT, not merely prove the checker can return non-zero.
#
# Extraction is structural rather than a grep, because a grep for the step would
# drift the moment someone reworded the `name:` -- and silently, reporting that
# the guard passes because nothing was found to test.
#
# `kubectl` is STUBBED on PATH, so the arms that reach it run with no cluster.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 2
WF=.github/workflows/smoke.yaml
[ -f "$WF" ] || { echo "missing $WF" >&2; exit 2; }

TMP=$(mktemp -d) || exit 2
trap 'rm -rf "$TMP"' EXIT

pass=0; fail=0
EXPECT_MISMATCH=0

# ---------------------------------------------------------------- extraction
# Keyed on the step's `name`, with the count asserted. If the step is renamed
# or duplicated this exits 3 (VOID) rather than reporting a pass for a step it
# never found.
python3 - "$WF" "$TMP/guard.sh" <<'PY' || exit 3
import io, sys, yaml
wf, out = sys.argv[1], sys.argv[2]
doc = yaml.safe_load(io.open(wf, encoding="utf-8"))
steps = doc["jobs"]["smoke"]["steps"]
hits = [s for s in steps if "THE GUARD" in (s.get("name") or "")]
if len(hits) != 1:
    print(f"VOID: expected exactly 1 step whose name contains 'THE GUARD', "
          f"found {len(hits)}. The step was renamed, removed or duplicated, so "
          f"there is nothing to calibrate and a pass would be meaningless.",
          file=sys.stderr)
    sys.exit(3)
run = hits[0]["run"]
if "chart/samples" not in run:
    print("VOID: the extracted step does not mention chart/samples, so it is "
          "not the guard this test is about.", file=sys.stderr)
    sys.exit(3)
io.open(out, "w", encoding="utf-8").write(run)
print(f"  extracted {len(run.splitlines())} lines verbatim from {wf}")
PY

# ------------------------------------------------------------------ kubectl stub
# Exit status comes from KUBECTL_RC so an arm can make the apply fail.
mkdir -p "$TMP/bin"
cat > "$TMP/bin/kubectl" <<'STUB'
#!/usr/bin/env bash
echo "stub kubectl: $*"
exit "${KUBECTL_RC:-0}"
STUB
chmod +x "$TMP/bin/kubectl"

# Run the extracted guard in a sandbox holding only the named sample files.
# `env -i` is deliberate: inheriting a KUBECONFIG would let a real cluster
# answer, and then these results would depend on who ran them.
run_guard() {
  local dir=$1 rc_stub=$2
  mkdir -p "$TMP/$dir/chart/samples"
  ( cd "$TMP/$dir" && env -i PATH="$TMP/bin:/usr/bin:/bin" \
      KUBECTL_RC="$rc_stub" bash "$TMP/guard.sh" 2>&1 )
}

check() {
  local desc=$1 want_rc=$2 want_msg=$3 out=$4 got_rc=$5
  local ok=1
  [ "$got_rc" = "$want_rc" ] || ok=0
  if [ -n "$want_msg" ] && ! printf '%s\n' "$out" | grep -qF "$want_msg"; then ok=0; fi
  if [ "$ok" = "1" ]; then
    pass=$((pass + 1)); printf '  PASS  %s (exit %s)\n' "$desc" "$got_rc"
  elif [ "$EXPECT_MISMATCH" = "1" ]; then
    fail=$((fail + 1))
    printf '  MISMATCH (expected)  %s -- wanted exit %s, got %s\n' \
      "$desc" "$want_rc" "$got_rc"
  else
    fail=$((fail + 1))
    printf '  FAIL  %s\n        wanted exit %s containing %q\n        got exit %s:\n%s\n' \
      "$desc" "$want_rc" "$want_msg" "$got_rc" \
      "$(printf '%s\n' "$out" | sed 's/^/          /')"
  fi
}

echo "== smoke.yaml's sample guard, refusal arms (no cluster)"

# 1. THE ARM THIS TEST EXISTS FOR. An empty sample set must be VOID, not a
#    pass. Before the count assertion the guard survived this only because
#    bash leaves an unmatched glob literal and kubectl then errored on the
#    pattern -- a real protection, but an incidental one.
out=$(run_guard empty 0); rc=$?
check "an empty sample set is VOID, not a pass" 3 "VOID, not a pass" "$out" "$rc"

# 2. The guard must still DISCRIMINATE after the count assertion was added.
#    This is the arm that would catch the count check having broken the rc
#    logic -- the actual point of the guard is detecting a sample that fails.
mkdir -p "$TMP/bad/chart/samples"
printf 'kind: Fake\n' > "$TMP/bad/chart/samples/a.yaml"
out=$(run_guard bad 1); rc=$?
check "a sample that does not apply still fails the guard" 1 \
  "does not apply against the CRDs" "$out" "$rc"

# 3. The positive arm, with the count surfaced. smoke's own run covers this
#    against real samples; here it confirms the count is reported and the loop
#    iterates rather than short-circuiting.
mkdir -p "$TMP/good/chart/samples"
printf 'kind: Fake\n' > "$TMP/good/chart/samples/a.yaml"
printf 'kind: Fake\n' > "$TMP/good/chart/samples/b.yaml"
out=$(run_guard good 0); rc=$?
check "two applying samples pass, and the count is reported" 0 \
  "2 shipped samples to apply" "$out" "$rc"

# META-CONTROL. Every arm above asserts a refusal or a count, and a harness
# that reported PASS unconditionally would look identical. So assert a
# deliberately WRONG expectation and require it to be reported as a mismatch.
# Labelled "MISMATCH (expected)" rather than FAIL, because a healthy run whose
# output contains the word FAIL trains readers to skim past a real one.
echo "== meta-control: the harness must be able to fail"
before=$fail
EXPECT_MISMATCH=1
out=$(run_guard good 0); rc=$?
check "a deliberately wrong expectation" 99 "this message does not exist" "$out" "$rc"
EXPECT_MISMATCH=0
if [ "$fail" -gt "$before" ]; then
  fail=$((fail - 1)); pass=$((pass + 1))
  echo "  PASS  the harness reported a failure when it should -- the results"
  echo "        above are evidence rather than an unconditional PASS"
else
  fail=$((fail + 1))
  echo "  FAIL  the harness did NOT fail a wrong expectation. Every PASS above"
  echo "        is meaningless, because it cannot distinguish outcomes."
fi

printf '\n  %d passed, %d failed\n' "$pass" "$fail"
[ "$fail" = "0" ] || exit 1
echo "  smoke.yaml's sample guard refuses an empty set and a failing sample"
