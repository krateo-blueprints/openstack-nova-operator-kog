#!/usr/bin/env bash
# Exercise the ACTUAL script text of release.yaml's "Publish the stamped CompositionDefinitions on the
# release" step -- the DRY_RUN guard, the staging, the CHART_VERSION/SOURCE_REF stamping and the
# placeholder assertion -- against a fake repo tree and a stand-in hack/publish-release-assets.sh.
#
# WHY THIS EXISTS. The DRY_RUN guard sits BEFORE the stamping, so every dry run skips it: the stamping
# and its assertion first execute on a real release, after the chart is already pushed to GHCR. A bug
# there is a half-done release. This runs them without a release.
#
# The step body is EXTRACTED from the workflow (yaml.safe_load), never retyped, so it cannot drift.
# No network, no gh, no token. Usage: bash hack/test-release-assets-stamp.sh
set -uo pipefail
export LC_ALL=C
here="$(cd "$(dirname "$0")" && pwd)"
wf="$here/../.github/workflows/release.yaml"
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
STEP_NAME="Publish the stamped CompositionDefinitions on the release"

# Extract the run: body. Exit non-zero (not an empty file) if the step is not found exactly once.
python3 -I - "$wf" "$STEP_NAME" > "$work/step.sh" <<'PY' || { echo "FAIL  could not extract the step from release.yaml"; exit 1; }
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1]))
hits = [s["run"] for j in doc["jobs"].values() for s in j.get("steps", [])
        if s.get("name") == sys.argv[2] and "run" in s]
if len(hits) != 1:
    sys.exit("expected exactly one step named %r, found %d" % (sys.argv[2], len(hits)))
sys.stdout.write(hits[0])
PY
[ -s "$work/step.sh" ] || { echo "FAIL  extracted step is empty"; exit 1; }

# The step uses GNU `sed -i PATTERN FILE` (ubuntu runner). BSD sed (macOS) reads PATTERN as the
# backup suffix, so shim a GNU-compatible sed first on PATH when the system one is not GNU.
mkdir -p "$work/bin"
if ! sed --version >/dev/null 2>&1; then
  real_sed="$(command -v sed)"
  cat > "$work/bin/sed" <<SH
#!/usr/bin/env bash
if [ "\$1" = "-i" ]; then shift; exec "$real_sed" -i '' "\$@"; fi
exec "$real_sed" "\$@"
SH
  chmod +x "$work/bin/sed"
fi

fail=0
# Build a fixture repo: root=yes puts a CD at the root; chart=NAME puts one in NAME/.
mkfixture() {  # dir root(yes|no) chartdir(or "")
  local d="$1"; rm -rf "$d"; mkdir -p "$d/hack"
  cat > "$d/hack/publish-release-assets.sh" <<'PUB'
#!/usr/bin/env bash
# Stand-in publisher: log the tag and each asset's basename + contents.
{ echo "ARGS tag=$1"; shift; for f in "$@"; do echo "ASSET $(basename "$f")"; sed 's/^/  | /' "$f"; done; } >> "$PUB_LOG"
PUB
  local body=$'apiVersion: core.krateo.io/v1alpha1\nspec:\n  chart:\n    version: CHART_VERSION\n  ref: SOURCE_REF'
  [ "$2" = yes ] && printf '%s\n' "$body" > "$d/compositiondefinition.yaml"
  if [ -n "$3" ]; then mkdir -p "$d/$3"; printf '%s\n' "$body" > "$d/$3/compositiondefinition.yaml"; fi
  # Excluded by the step's own -not -path '*/charts/*': must never be attached.
  mkdir -p "$d/charts/dep"; printf 'version: CHART_VERSION\n' > "$d/charts/dep/compositiondefinition.yaml"
}
runstep() {  # script dir DRY_RUN -> sets rc, out in $work/out, log in $PUB_LOG
  export PUB_LOG="$work/pub.log"; : > "$PUB_LOG"
  ( cd "$2" && PATH="$work/bin:$PATH" VERSION=1.2.3 DRY_RUN="$3" bash "$1" ) >"$work/out" 2>&1; rc=$?
}
check() {  # label ok(0/1) -- on failure show output
  if [ "$2" = 0 ]; then echo "ok    $1"; else echo "FAIL  $1"; sed 's/^/      /' "$work/out"; sed 's/^/      log: /' "$PUB_LOG"; fail=1; fi
}
has() { grep -qF -- "$1" "$2"; }   # fixed-string

fx="$work/fx"

# 1. Happy path.
mkfixture "$fx" yes ""
runstep "$work/step.sh" "$fx" false
ok=0
[ "$rc" = 0 ] || ok=1
has "ARGS tag=1.2.3" "$PUB_LOG" || ok=1
has "ASSET compositiondefinition.yaml" "$PUB_LOG" || ok=1
has "version: 1.2.3" "$PUB_LOG" && has "ref: 1.2.3" "$PUB_LOG" || ok=1
{ grep -q CHART_VERSION "$PUB_LOG" || grep -q SOURCE_REF "$PUB_LOG"; } && ok=1   # a surviving placeholder is a failure
has "dep" "$PUB_LOG" && ok=1            # charts/ exclusion held
[ "$(grep -c '^ASSET' "$PUB_LOG")" = 1 ] || ok=1
check "happy path: both placeholders stamped, one asset compositiondefinition.yaml, publisher got tag 1.2.3 (exit $rc)" $ok

# 2. MUTATION: the assertion must be able to fail. Break the stamp so it matches nothing.
sed_pat='s/CHART_VERSION/${VERSION}/g; s/SOURCE_REF/${VERSION}/g'
mut_sed="$work/mutant-sed.sh"
python3 -I - "$work/step.sh" "$mut_sed" "$sed_pat" <<'PY'
import sys
src = open(sys.argv[1]).read(); pat = sys.argv[3]
if src.count(pat) != 1:
    sys.exit(3)               # the mutation does not apply -> VOID, not pass
open(sys.argv[2], "w").write(src.replace(pat, 's/NOMATCH_A/${VERSION}/g; s/NOMATCH_B/${VERSION}/g'))
PY
if [ $? -ne 0 ] || cmp -s "$work/step.sh" "$mut_sed"; then
  echo "VOID  mutation 1 (stamp matches nothing) did not apply to the extracted step; the assertion is UNTESTED"; fail=1
else
  mkfixture "$fx" yes ""
  runstep "$mut_sed" "$fx" false
  ok=0
  [ "$rc" != 0 ] || ok=1
  has "::error::compositiondefinition.yaml still carries a version placeholder" "$work/out" || ok=1
  [ -s "$PUB_LOG" ] && ok=1                 # publisher must NOT be reached
  check "mutation (stamp matches nothing): assertion fails the step, names the placeholder, publisher not reached (exit $rc)" $ok
fi
# 2b. Partial mutation: only SOURCE_REF left unstamped -> the second grep alone must catch it.
mut_ref="$work/mutant-ref.sh"
python3 -I - "$work/step.sh" "$mut_ref" "$sed_pat" <<'PY'
import sys
src = open(sys.argv[1]).read(); pat = sys.argv[3]
if src.count(pat) != 1:
    sys.exit(3)
open(sys.argv[2], "w").write(src.replace(pat, 's/CHART_VERSION/${VERSION}/g; s/NOMATCH_B/${VERSION}/g'))
PY
if [ $? -ne 0 ] || cmp -s "$work/step.sh" "$mut_ref"; then
  echo "VOID  mutation 2 (SOURCE_REF not stamped) did not apply; the SOURCE_REF half of the assertion is UNTESTED"; fail=1
else
  mkfixture "$fx" yes ""
  runstep "$mut_ref" "$fx" false
  ok=0; [ "$rc" != 0 ] || ok=1
  has "::error::" "$work/out" || ok=1
  check "mutation (only SOURCE_REF unstamped): the SOURCE_REF half of the assertion also fails the step (exit $rc)" $ok
fi
# Control: the unmutated step passes on the identical fixture, so cases 2/2b fail because of the mutation.
mkfixture "$fx" yes ""; runstep "$work/step.sh" "$fx" false
check "control: unmutated step passes on the same fixture the mutants fail on (exit $rc)" $([ "$rc" = 0 ] && echo 0 || echo 1)

# 3. DRY_RUN=true: exit 0, nothing stamped, publisher not invoked.
mkfixture "$fx" yes ""
runstep "$work/step.sh" "$fx" true
ok=0
[ "$rc" = 0 ] || ok=1
has "DRY RUN" "$work/out" || ok=1
[ -s "$PUB_LOG" ] && ok=1
has "version: CHART_VERSION" "$fx/compositiondefinition.yaml" || ok=1   # source untouched
check "DRY_RUN=true: exits 0, publisher not invoked, nothing stamped (exit $rc)" $ok

# 4. Chart-directory CD (no root CD).
mkfixture "$fx" no mychart
runstep "$work/step.sh" "$fx" false
ok=0
[ "$rc" = 0 ] || ok=1
has "ASSET compositiondefinition-mychart.yaml" "$PUB_LOG" || ok=1
has "ASSET compositiondefinition.yaml" "$PUB_LOG" && ok=1
has "version: 1.2.3" "$PUB_LOG" || ok=1
check "chart-dir CD: asset is compositiondefinition-mychart.yaml, stamped (exit $rc)" $ok

# 5. No CompositionDefinition anywhere (only the excluded charts/ one).
mkfixture "$fx" no ""
runstep "$work/step.sh" "$fx" false
ok=0
[ "$rc" = 0 ] || ok=1
has "no CompositionDefinition to publish" "$work/out" || ok=1
[ -s "$PUB_LOG" ] && ok=1
check "no CD: exits 0 with 'no CompositionDefinition to publish', publisher not invoked (exit $rc)" $ok

exit $fail
