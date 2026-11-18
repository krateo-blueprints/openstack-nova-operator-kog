#!/usr/bin/env bash
# Render this chart's six OAS documents against ONE oasgen-provider version on a
# throwaway kind cluster, then diff the generated CRD shape against the pinned
# baseline CI publishes. Credential-free: nova.url points at RFC 2606
# `nova.invalid`, so "no credentials" is structural rather than promised.
#
# WHY THIS EXISTS. "Do our six kinds still reconcile on version X" was answered
# three times by reading release notes and once, on 2026-11-17, by measuring --
# and only the measurement found the 202 create-response defect that four
# readings had missed. The reading is not a cheap version of the measurement; it
# is a different question with a different answer.
#
# WHAT IT REFUSES TO DO. Report a clean result it cannot support. Every refusal
# below exits 3 (VOID) rather than 0, because an empty fingerprint diffs equal
# against another empty fingerprint and a missing baseline makes "no change"
# unfalsifiable:
#
#   * no version argument                 -> 2  usage
#   * 1-min loadavg >= LOAD_MAX           -> 1  shared machine, refuse to pile on
#   * a cluster with our name ALREADY     -> 3  it is not ours; NEVER touch it
#   * baseline fingerprint unobtainable   -> 3  nothing to diff against
#   * zero generated CRDs                 -> 3  (crd-shape.py's own exit 3)
#   * resource-CRD count != RestDefs      -> 3  a loop over that set checks the
#                                               wrong thing
#
# THE CLUSTER SAFETY PROPERTY, which is the one worth stating twice: this script
# deletes ONLY a cluster it created in this run. `aruba-110` holds the only copy
# of a client credential and other sessions' clusters hold live CRs, so a name
# collision must stop the run, not win it.
#
# SEAMS. Every external command is overridable so --selftest can drive every
# verdict above with no cluster and no network: KIND_BIN, KUBECTL_BIN, HELM_BIN,
# GH_BIN, LOADAVG_CMD, LOAD_MAX, SCRATCH.
set -uo pipefail
export LC_ALL=C          # a comma decimal separator turns the load compare into
                         # a syntax error; this script does arithmetic on it

KIND_BIN="${KIND_BIN:-kind}"
KUBECTL_BIN="${KUBECTL_BIN:-kubectl}"
HELM_BIN="${HELM_BIN:-helm}"
GH_BIN="${GH_BIN:-gh}"
LOADAVG_CMD="${LOADAVG_CMD:-sysctl -n vm.loadavg}"
LOAD_MAX="${LOAD_MAX:-8}"
NODE_IMAGE="${NODE_IMAGE:-kindest/node:v1.36.1}"
NS="${NS:-krateo-system}"
GROUP="${GROUP:-nova.openstack.krateo.io}"
REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
# The file that DEFINES the pin. A seam, so the "pin is not a literal" guard
# can be exercised against a fixture instead of only in production, where it
# would be reached exactly once -- the time it was reached by accident.
PIN_FILE="${PIN_FILE:-$REPO_ROOT/.github/workflows/validate.yaml}"
SCRATCH="${SCRATCH:-${TMPDIR:-/tmp}}"

VOID=3; FAIL=1; USAGE=2
created_cluster=""       # set only after a successful create

say(){ printf '\n=== %s ===\n' "$*"; }
note(){ printf '  %s\n' "$*"; }
void(){ printf '\nVOID: %s\n' "$*" >&2; exit "$VOID"; }
fail(){ printf '\nFAIL: %s\n' "$*" >&2; exit "$FAIL"; }

# ---------------------------------------------------------------------------
# load gate -- Diego's rule for this shared 8-CPU machine, 2026-11-12
# ---------------------------------------------------------------------------
load_1min(){
  # macOS prints `{ 2,04 3,23 3,53 }`; Linux /proc-style prints `2.04 3.23 3.53`.
  # Take the FIRST numeric field either way and normalise the separator.
  $LOADAVG_CMD 2>/dev/null | tr -d '{}' | tr ',' '.' | awk '{for(i=1;i<=NF;i++) if($i+0==$i){print $i; exit}}'
}
check_load(){
  local l; l=$(load_1min)
  [ -n "$l" ] || void "could not read the 1-minute load average; refusing to guess"
  note "1-min loadavg = $l (max $LOAD_MAX)"
  awk -v l="$l" -v m="$LOAD_MAX" 'BEGIN{exit !(l<m)}' \
    || fail "loadavg $l is at or above $LOAD_MAX -- another session is working; retry later"
}

# ---------------------------------------------------------------------------
# cluster safety
# ---------------------------------------------------------------------------
cluster_exists(){ $KIND_BIN get clusters 2>/dev/null | grep -qx "$1"; }

cleanup(){
  local rc=$?
  if [ -n "$created_cluster" ]; then
    say "deleting the cluster THIS RUN created ($created_cluster)"
    if $KIND_BIN delete cluster --name "$created_cluster" --kubeconfig "$KCFG" >/dev/null 2>&1; then
      note "deleted"
    else
      note "delete reported an error -- check by hand: kind get clusters"
    fi
  fi
  exit $rc
}

# ---------------------------------------------------------------------------
# baseline: the fingerprint CI publishes for the PINNED version
# ---------------------------------------------------------------------------
pinned_version(){
  # validate.yaml, NOT smoke.yaml. smoke.yaml used to carry a duplicate of this
  # key and its own comment said to move the value somewhere shared; smoke is
  # now a matrix whose OASGEN_VERSION is `${{ matrix.oasgen }}`, so reading it
  # there yields a TEMPLATE EXPRESSION, not a version. That is how this broke:
  # the literal `${{` travelled into a path and the script reported "baseline
  # artifact is not crd-shape-${{", which sends a reader to the baseline when
  # the fault is in the pin. validate.yaml is the single source of truth that
  # verify_documented_pin already screens the docs against.
  grep -E '^[[:space:]]*OASGEN_VERSION:' "$PIN_FILE" \
    | head -1 \
    | sed -E 's/.*OASGEN_VERSION:[[:space:]]*"?([^"[:space:]]*)"?.*/\1/'
  # `*` not `+`: with `+` an EMPTY value (`OASGEN_VERSION: ""`) fails to match,
  # sed passes the line through unchanged, and the caller receives the whole
  # line `  OASGEN_VERSION: ""` AS THE VERSION. The -n check below then never
  # fires, because a non-empty wrong answer is not an empty one. Found by
  # asserting an arm's MESSAGE rather than only its exit code.
}
fetch_baseline(){
  local ver="$1" dest="$2" rid
  rid=$($GH_BIN run list --workflow=smoke.yaml --branch main --status success -L 1 \
        --json databaseId --jq '.[0].databaseId' 2>/dev/null)
  [ -n "$rid" ] || return 1
  # THE CHECK THAT CAN DISAGREE. `-n` makes the request deterministic, but a
  # name CI never published would then fail as an empty download rather than as
  # "this run carries no baseline for that version" -- the same unreadable
  # result this script exists to avoid. Ask the run what it has first.
  if ! $GH_BIN api "repos/{owner}/{repo}/actions/runs/$rid/artifacts" \
        --jq '.artifacts[].name' 2>/dev/null | grep -qx "crd-shape-$ver"; then
    printf 'run %s publishes no artifact named crd-shape-%s\n' "$rid" "$ver" >&2
    return 1
  fi
  # BY NAME, INTO A DIRECTORY THIS SCRIPT NAMES. smoke is a matrix and publishes
  # one fingerprint per arm (crd-shape-0.52.0 AND crd-shape-0.74.0, both present
  # in the same run -- verified, not assumed), so an unqualified download pulls
  # both and the `find -print -quit` below picks whichever the filesystem hands
  # back first.
  #
  # AND `-n` CHANGES THE LAYOUT, which is how the previous version of this fix
  # broke: with `-n` gh extracts the artifact's CONTENTS straight into `-D`, with
  # no `<name>/` directory, so the file landed at `$dest/crd-shape.txt` and the
  # version guard below correctly refused it -- a deterministic VOID in place of
  # a flaky wrong answer. Measured both ways:
  #     with    -n  ->  <dest>/crd-shape.txt
  #     without -n  ->  <dest>/crd-shape-0.52.0/crd-shape.txt  (+ the other arm)
  # So the directory is created here, named for the version asked for, and the
  # guard still has a path to check.
  rm -rf "$dest"; mkdir -p "$dest/crd-shape-$ver"
  $GH_BIN run download "$rid" -D "$dest/crd-shape-$ver" -n "crd-shape-$ver" \
    >/dev/null 2>&1 || return 1
  local f; f=$(find "$dest" -name 'crd-shape.txt' -print -quit 2>/dev/null)
  [ -n "$f" ] || return 1
  # STRUCTURAL ONLY, AND SAYING SO RATHER THAN LETTING IT READ AS EVIDENCE.
  # This directory is created a few lines above by this script, named for the
  # version it asked for, so the match is guaranteed and this case can no longer
  # fail. It is kept as a tripwire for a refactor that stops creating it, not as
  # a measurement.
  #
  # THE CHECK THAT CAN ACTUALLY DISAGREE is the artifact listing above: it asks
  # the run what it published and refuses when `crd-shape-<version>` is not in
  # the answer. The fingerprint file itself carries no version string -- its
  # header is a CRD and field count -- so content cannot settle this either, and
  # pretending the path did would be an instrument that cannot report a problem.
  case "$f" in
    *"/crd-shape-$ver/"*) : ;;
    *) printf 'baseline artifact is not crd-shape-%s: %s\n' "$ver" "$f" >&2; return 1 ;;
  esac
  printf '%s\n' "$f"
}

# ---------------------------------------------------------------------------
# the real run
# ---------------------------------------------------------------------------
render(){
  local VER="$1"
  CLUSTER="nova-render-${VER//./}"
  KCFG="$SCRATCH/kube-$CLUSTER.yaml"
  CTX="kind-$CLUSTER"
  local K="$KUBECTL_BIN --kubeconfig $KCFG --context $CTX"
  local H="$HELM_BIN --kubeconfig $KCFG --kube-context $CTX"
  local STAGE="$SCRATCH/render-stage-$VER"

  say "load gate"; check_load

  say "baseline fingerprint for the pinned version"
  local PIN; PIN=$(pinned_version)
  [ -n "$PIN" ] || void "could not read OASGEN_VERSION from $PIN_FILE"
  # It must be a LITERAL version. A workflow expression, a shell variable or an
  # empty string each produce a plausible-looking string that then travels into
  # an artifact path and fails there, blaming the baseline for a fault in the
  # pin. Setup and subject must fail differently.
  #
  # MOST SPECIFIC CASE FIRST, and that ordering is load-bearing: `${{ matrix.x }}`
  # reduces to `${{`, which also fails the semver shape, so with the shape test
  # first the expression branch was DEAD CODE and the useful message never
  # printed. An unreachable branch whose text names the real cause is worse than
  # no branch, because the text is there to be found by grep and believed.
  case "$PIN" in
    *'$'*|*'{'*|*'}'*)
      void "OASGEN_VERSION in $PIN_FILE is an unexpanded expression ($PIN), not a version. smoke.yaml is a matrix and its copy of this key is \${{ matrix.oasgen }}; the pin must be read from a file that holds a literal" ;;
  esac
  case "$PIN" in
    *[0-9].[0-9]*) : ;;
    *) void "OASGEN_VERSION in $PIN_FILE is $PIN, which is not a literal version. Nothing downstream can use it, and carrying it into an artifact path would report this as a baseline problem" ;;
  esac
  note "pin is $PIN"
  local BASE
  BASE=$(fetch_baseline "$PIN" "$SCRATCH/render-baseline") \
    || void "could not obtain the $PIN baseline fingerprint from the latest green smoke run. A diff needs two sides; one side is not a result"
  note "baseline: $BASE ($(wc -l < "$BASE" | tr -d ' ') lines)"

  say "cluster safety"
  if cluster_exists "$CLUSTER"; then
    void "a kind cluster named $CLUSTER ALREADY EXISTS. It is not this run's, so this script will not use it and will not delete it. Rename or remove it deliberately"
  fi
  note "no cluster named $CLUSTER; safe to create"

  say "create $CLUSTER"
  trap cleanup EXIT INT TERM
  $KIND_BIN create cluster --name "$CLUSTER" --image "$NODE_IMAGE" --kubeconfig "$KCFG" \
    || fail "kind create failed"
  created_cluster="$CLUSTER"

  say "stage a stamped chart (never edit the working tree)"
  rm -rf "$STAGE"; mkdir -p "$STAGE"
  cp -R "$REPO_ROOT/chart" "$STAGE/chart"
  sed "s/CHART_VERSION/0.0.0-ci/g" "$REPO_ROOT/chart/Chart.yaml" > "$STAGE/chart/Chart.yaml"
  if grep -q CHART_VERSION "$STAGE/chart/Chart.yaml"; then fail "CHART_VERSION stamp failed"; fi

  say "install oasgen-provider $VER from OCI"
  $H upgrade --install oasgen-provider-crds \
    oci://ghcr.io/krateo-platformops/charts/oasgen-provider-crds \
    --version "$VER" -n "$NS" --create-namespace --wait || fail "crds chart $VER"
  $H upgrade --install oasgen-provider \
    oci://ghcr.io/krateo-platformops/charts/oasgen-provider \
    --version "$VER" -n "$NS" --wait --timeout 10m || fail "provider chart $VER"
  say "provider image actually running"
  $K -n "$NS" get deploy -o jsonpath='{range .items[*]}{.metadata.name}{"  "}{.spec.template.spec.containers[0].image}{"\n"}{end}'

  say "install this chart against an unroutable endpoint"
  $H upgrade --install nova "$STAGE/chart" -n "$NS" \
    --set nova.url=https://nova.invalid/v2.1 --wait --timeout 5m || fail "chart install"

  say "wait for RestDefinitions and generated CRDs"
  local RD=restdefinitions.ogen.krateo.io
  for _ in $(seq 1 20); do $K get crd "$RD" >/dev/null 2>&1 && break; sleep 3; done
  $K get crd "$RD" >/dev/null 2>&1 || fail "$RD never created"
  $K wait --for=condition=Established "crd/$RD" --timeout=180s || fail "$RD not Established"
  $K -n "$NS" wait "$RD" --all --for=condition=Ready --timeout=420s
  note "rd-wait rc=$?"

  local RD_N; RD_N=$($K -n "$NS" get "$RD" --no-headers | wc -l | tr -d ' ')
  $K get crd -o json > "$SCRATCH/crds-$VER.json" || fail "crd dump"
  local PLURALS; PLURALS=$(python3 -c '
import json,sys
g=sys.argv[1]
d=json.load(open(sys.argv[2]))
print("\n".join(sorted(c["spec"]["names"]["plural"] for c in d["items"]
      if c["spec"]["group"]==g and not c["spec"]["names"]["plural"].endswith("configurations"))))' \
      "$GROUP" "$SCRATCH/crds-$VER.json")
  local P_N; P_N=$(printf '%s\n' "$PLURALS" | grep -c . )
  note "$RD_N RestDefinitions, $P_N resource CRDs"
  [ "$RD_N" -eq 0 ] && void "no RestDefinitions installed"
  [ "$P_N" -ne "$RD_N" ] && void "$P_N resource CRDs for $RD_N RestDefinitions -- a loop over this set would check the wrong thing"
  local p
  for p in $PLURALS; do
    $K wait --for=condition=Established "crd/$p.$GROUP" --timeout=180s || fail "crd $p not Established"
  done

  # READ THE CAVEAT BELOW BEFORE TREATING ANY OF THIS AS HEALTH. A
  # RestDefinition reports Ready=True/Synced=True while its generated controller
  # sits blocked and reconciles nothing -- measured upstream on 0.83.0-rc1. The
  # RD condition is the provider's statement about RENDERING the definition, not
  # about the controller working, and it is the field a platform surfaces, which
  # makes it the most convincing of the three signals that agree with a broken
  # install. The controller-health step further down is the one that can
  # disagree.
  say "RestDefinition conditions and schemaWarnings"
  note "Ready/Synced here attest the RENDER, not that any controller reconciles"
  $K -n "$NS" get "$RD" -o json > "$SCRATCH/rds-$VER.json"
  python3 "$REPO_ROOT/hack/render-report.py" "$SCRATCH/rds-$VER.json" "$SCRATCH/crds-$VER.json" "$GROUP"

  say "every served resource version must have a controller"
  $K -n "$NS" get deploy -o json > "$SCRATCH/deploys-$VER.json"
  "$REPO_ROOT/hack/assert-served-controlled.py" \
    "$SCRATCH/crds-$VER.json" "$SCRATCH/deploys-$VER.json" "$SCRATCH/rds-$VER.json" \
    || fail "a served resource version has no controller"

  # WHY THIS STEP EXISTS. oasgen#771/#806 moved Lease ownership: the Lease name
  # used to be DERIVED by the controller from the version it observed, while the
  # Role granting access to it is named with a fixed `v1alpha1`. Every kind this
  # chart serves is at `v2-1`, so a mismatch takes out 6 of 6 rather than some
  # fraction -- which makes this repo the sharp case for that fix and not a
  # formality.
  #
  # AND THE ANSWER IS A NAME, so it is DUMPED VERBATIM rather than summarised.
  # The upstream session's prediction of that name has been wrong twice, and a
  # summary of a name is not a name. The previous run of this script answered
  # "do all six reach Ready" and then deleted its cluster with the Roles
  # unrecorded, so the question had to be asked again -- which is the whole
  # reason this is a step and not something typed by hand afterwards.
  say "RBAC and ownership plumbing, verbatim"
  note "Roles in $NS (name, then the rules that matter)"
  $K -n "$NS" get role -o json > "$SCRATCH/roles-$VER.json"
  python3 - "$SCRATCH/roles-$VER.json" <<'PYEOF'
import json, sys
doc = json.load(open(sys.argv[1]))
items = doc.get("items") or []
if not items:
    print("    NO Roles at all -- that is a reading about RBAC, not about this chart")
for r in sorted(items, key=lambda x: x["metadata"]["name"]):
    name = r["metadata"]["name"]
    rules = r.get("rules")
    print("    {}".format(name))
    if rules is None:
        print("        rules: null")
        continue
    for rule in rules:
        res = ",".join(rule.get("resources") or []) or "-"
        grp = ",".join(rule.get("apiGroups") or []) or "(core)"
        names = ",".join(rule.get("resourceNames") or []) or "(any)"
        verbs = ",".join(rule.get("verbs") or []) or "-"
        print("        {}/{}  resourceNames={}  verbs={}".format(grp, res, names, verbs))
PYEOF
  note "downward-API env on each generated controller Deployment"
  python3 - "$SCRATCH/deploys-$VER.json" <<'PYEOF'
import json, sys
doc = json.load(open(sys.argv[1]))
for d in sorted(doc.get("items") or [], key=lambda x: x["metadata"]["name"]):
    name = d["metadata"]["name"]
    if not name.endswith("-controller"):
        continue
    envs = []
    for c in d["spec"]["template"]["spec"].get("containers") or []:
        for e in c.get("env") or []:
            src = e.get("valueFrom", {}).get("fieldRef", {}).get("fieldPath")
            envs.append("{}={}".format(e["name"], src or e.get("value", "")))
    print("    {}  env=[{}]".format(name, ", ".join(envs) if envs else ""))
PYEOF

  # CAME UP and STAYED UP are two questions, and until now this script only
  # asked the first -- badly. assert-served-controlled.py checks a Deployment
  # NAMED <rd>-controller EXISTS, which a fully crashlooping controller
  # satisfies. The failure shape that matters here is a pod that passes its
  # readiness probe and then exits: `availableReplicas: 1` read once is true in
  # that world too, so a single read cannot tell the two apart.
  say "controllers must have COME UP and STAYED UP"
  SETTLE="${SETTLE_SECONDS:-45}"
  note "settling ${SETTLE}s before reading restart counts"
  sleep "$SETTLE"
  $K -n "$NS" get deploy -o json > "$SCRATCH/deploys-settled-$VER.json"
  $K -n "$NS" get pod    -o json > "$SCRATCH/pods-settled-$VER.json"
  # THE LOGS ARE THE ONLY THING THAT SEPARATES ALIVE FROM WORKING. Availability
  # and restart counts are both green on a controller blocked forever on a
  # denied Lease create (measured upstream against 0.83.0-rc1: Running, 0
  # restarts, no workers, an authorizer error repeating every few seconds). A
  # failed `logs` read lands its own error text in the file, which contains
  # neither ownership line and therefore reads as VOID -- unknown, not healthy.
  LOGDIR="$SCRATCH/logs-$VER"; rm -rf "$LOGDIR"; mkdir -p "$LOGDIR"
  $K -n "$NS" get deploy -o name > "$SCRATCH/depnames-$VER.txt"
  sed 's|^deployment.apps/||' "$SCRATCH/depnames-$VER.txt" \
    | grep -- '-controller$' > "$SCRATCH/ctlnames-$VER.txt" || true
  while read -r d; do
    [ -n "$d" ] || continue
    $K -n "$NS" logs "deploy/$d" --tail=400 > "$LOGDIR/$d.log" 2>&1 || true
  done < "$SCRATCH/ctlnames-$VER.txt"
  note "captured $(wc -l < "$SCRATCH/ctlnames-$VER.txt" | tr -d ' ') controller log(s)"
  "$REPO_ROOT/hack/assert-controllers-healthy.py" \
    "$SCRATCH/deploys-settled-$VER.json" "$SCRATCH/pods-settled-$VER.json" \
    "$SETTLE" "$LOGDIR" \
    || fail "a generated controller is down, has restarted, or is not reconciling"

  say "fingerprint, and diff against the $PIN baseline"
  python3 "$REPO_ROOT/hack/crd-shape.py" < "$SCRATCH/crds-$VER.json" > "$SCRATCH/shape-$VER.txt"
  local rc=$?
  [ "$rc" -eq 0 ] || void "crd-shape.py refused (exit $rc) -- it will not fingerprint what it cannot see"
  note "$(wc -l < "$SCRATCH/shape-$VER.txt" | tr -d ' ') lines"
  echo
  if diff "$BASE" "$SCRATCH/shape-$VER.txt"; then
    echo
    note "SHAPE IDENTICAL to $PIN"
  else
    echo
    note "shape differs from $PIN -- read every line above before calling it benign."
    note "A new constraint can reject a CR that already applies; a new default= is"
    note "unrecoverable, because the apiserver materialises it onto existing objects."
  fi
  say "DONE for $VER"
}

# ---------------------------------------------------------------------------
# --selftest: drive every verdict with stubs. No cluster, no network.
# ---------------------------------------------------------------------------
selftest(){
  local pass=0 failn=0
  # NOT `local tmp`. The EXIT trap below runs after this function has returned,
  # where a local is out of scope: under `set -u` the trap itself then died with
  # "tmp: unbound variable" AFTER printing a clean 9/9, and left its temp dir
  # behind. A self-test that reports success and then crashes is the exact shape
  # this script exists to catch, so it is recorded rather than quietly fixed.
  tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
  mkdir -p "$tmp/bin" "$tmp/scratch"
  # stub kind: reports whichever clusters STUB_CLUSTERS names
  cat > "$tmp/bin/kind" <<'EOF'
#!/usr/bin/env bash
if [ "${1:-}" = "get" ] && [ "${2:-}" = "clusters" ]; then
  printf '%s\n' ${STUB_CLUSTERS:-}
  exit 0
fi
if [ "${1:-}" = "create" ]; then echo "STUB-CREATE $*"; exit "${STUB_CREATE_RC:-0}"; fi
if [ "${1:-}" = "delete" ]; then echo "STUB-DELETE $*" >> "${STUB_DELETE_LOG:-/dev/null}"; exit 0; fi
exit 0
EOF
  cat > "$tmp/bin/gh" <<'EOF'
#!/usr/bin/env bash
exit "${STUB_GH_RC:-1}"
EOF
  chmod +x "$tmp/bin/kind" "$tmp/bin/gh"

  # EXPECT_TEXT (set in the CALLER's scope, deliberately not exported into the
  # child) pins an arm to its CAUSE and not just its exit code. Almost every
  # refusal in this script is VOID=3, so an arm asserting only the code can pass
  # having refused for a completely different reason -- which is how a suite
  # reports coverage it does not have.
  arm(){ # arm <name> <expected-rc> <env-assignments...>
    local name="$1" want="$2"; shift 2
    local out rc why=""
    out=$(env PATH="$tmp/bin:$PATH" KIND_BIN="$tmp/bin/kind" GH_BIN="$tmp/bin/gh" \
              SCRATCH="$tmp/scratch" "$@" bash "$0" "${ARG:-0.0.0-stub}" 2>&1)
    rc=$?
    if [ "$rc" != "$want" ]; then
      why="want rc=$want, got $rc"
    elif [ -n "${EXPECT_TEXT:-}" ]; then
      if ! printf '%s' "$out" | grep -qF -- "$EXPECT_TEXT"; then
        why="rc=$rc as wanted, but refused for another reason: no match for ${EXPECT_TEXT}"
      fi
    fi
    if [ -z "$why" ]; then
      printf '    %-58s PASS (rc=%s)\n' "$name" "$rc"; pass=$((pass+1))
    else
      printf '    %-58s FAIL (%s)\n' "$name" "$why"; failn=$((failn+1))
      printf '%s\n' "$out" | sed 's/^/        /' | tail -4
    fi
  }

  echo "hack/render-check.sh --selftest"
  echo
  echo "  Each arm must reach a DIFFERENT verdict. An arm that cannot fail is not"
  echo "  evidence, so a positive control sits beside every refusal."
  echo

  echo "  usage"
  local out rc
  out=$(bash "$0" 2>&1); rc=$?
  if [ "$rc" = "$USAGE" ]; then printf '    %-58s PASS (rc=%s)\n' "no version argument -> usage" "$rc"; pass=$((pass+1))
  else printf '    %-58s FAIL (want %s, got %s)\n' "no version argument -> usage" "$USAGE" "$rc"; failn=$((failn+1)); fi

  echo "  load gate"
  arm "loadavg over the max -> FAIL"        "$FAIL" LOADAVG_CMD="echo { 19,00 5,00 5,00 }" LOAD_MAX=8
  arm "loadavg unreadable -> VOID"          "$VOID" LOADAVG_CMD="true"                     LOAD_MAX=8
  # POSITIVE CONTROL for the gate: a LOW load must get PAST it and fail later,
  # at the baseline step. Without this arm the two above would also pass if the
  # gate refused everything unconditionally.
  arm "low loadavg passes the gate (-> baseline VOID)" "$VOID" \
      LOADAVG_CMD="echo { 0,10 0,10 0,10 }" LOAD_MAX=8 STUB_GH_RC=1

  echo "  comma vs dot decimal separator (LC_ALL=C arithmetic)"
  arm "dot-separated loadavg over the max -> FAIL" "$FAIL" LOADAVG_CMD="echo 19.00 5.00 5.00" LOAD_MAX=8

  echo "  baseline"
  arm "baseline unobtainable -> VOID"       "$VOID" LOADAVG_CMD="echo { 0,10 0,10 0,10 }" STUB_GH_RC=1

  echo "  the pin must be a literal, not a workflow expression"
  # Regression arm. smoke.yaml became a matrix on 2026-11-18 and this script
  # was still reading OASGEN_VERSION from it, so the pin came back as
  # `${{ matrix.oasgen }}`; the literal travelled into an artifact path and the
  # script blamed the baseline. The fixture asserts it really does hold an
  # expression before the arm relies on it.
  # shellcheck disable=SC2016  # the UNEXPANDED ${{ ... }} is the fixture: a
  # version this arm expanded would be a literal and would not reproduce it.
  printf 'env:\n  OASGEN_VERSION: ${{ matrix.oasgen }}\n' > "$tmp/pin-expr.yaml"
  grep -q 'matrix.oasgen' "$tmp/pin-expr.yaml" \
    || { echo "    fixture does not contain an expression -- vacuous arm"; failn=$((failn+1)); }
  EXPECT_TEXT="an unexpanded expression"
  arm "pin is a workflow expression -> VOID" "$VOID" \
      LOADAVG_CMD="echo { 0,10 0,10 0,10 }" PIN_FILE="$tmp/pin-expr.yaml"
  printf 'env:\n  OASGEN_VERSION: ""\n' > "$tmp/pin-empty.yaml"
  EXPECT_TEXT="could not read OASGEN_VERSION"
  arm "pin is empty -> VOID"                 "$VOID" \
      LOADAVG_CMD="echo { 0,10 0,10 0,10 }" PIN_FILE="$tmp/pin-empty.yaml"
  EXPECT_TEXT=""
  # Control for EXPECT_TEXT itself: the matcher must be able to FAIL an arm
  # whose code is right and whose cause is wrong, or the two arms above prove
  # nothing beyond what rc already said.
  EXPECT_TEXT="this string appears in no refusal this script can emit"
  before=$failn
  arm "EXPECT_TEXT control: wrong cause must FAIL" "$VOID" \
      LOADAVG_CMD="echo { 0,10 0,10 0,10 }" PIN_FILE="$tmp/pin-empty.yaml"
  EXPECT_TEXT=""
  if [ "$failn" -gt "$before" ]; then
    failn=$((failn-1)); pass=$((pass+1))
    printf '    %-58s PASS (it failed, as the control requires)\n' "...and that control failed on purpose"
  else
    printf '    %-58s FAIL (EXPECT_TEXT cannot fail an arm -- it proves nothing)\n' "...and that control failed on purpose"
    failn=$((failn+1))
  fi

  echo "  cluster safety -- THE ARM THAT PROTECTS OTHER SESSIONS"
  # A pre-existing cluster with our name must VOID *and must not be deleted*.
  # The baseline is made obtainable first, or this would VOID for the wrong
  # reason and the arm would prove nothing.
  cat > "$tmp/bin/gh" <<'EOF'
#!/usr/bin/env bash
# `run list` -> an id. `run download -D <dest>` must WRITE a baseline there:
# fetch_baseline rm -rf's the destination first, so a stub that only exits 0
# leaves nothing to find and every arm below it would VOID at the baseline
# step -- passing for the wrong reason.
if [ "${1:-}" = "run" ] && [ "${2:-}" = "list" ]; then echo 123; exit 0; fi
if [ "${1:-}" = "api" ]; then
  # the artifact listing fetch_baseline consults before downloading
  echo "crd-shape-${STUB_PIN:-0.52.0}"
  exit 0
fi
if [ "${1:-}" = "run" ] && [ "${2:-}" = "download" ]; then
  dest=""; want=""
  for a in "$@"; do
    case "$prev" in -D) dest="$a" ;; -n) want="$a" ;; esac
    prev="$a"
  done
  [ -n "$dest" ] || exit 1
  # A matrix publishes one fingerprint per arm, so the caller MUST name the one
  # it wants. Refusing here means deleting `-n` from the real call breaks this
  # suite instead of quietly reintroducing a non-deterministic pick.
  [ -n "$want" ] || { echo "stub: run download called with no -n; a matrix publishes several artifacts" >&2; exit 1; }
  # MIRRORS REAL gh: with -n the artifact's CONTENTS go straight into -D, with no
  # <name>/ directory of its own. A stub that invented that directory is exactly
  # what let the layout bug reach a real run.
  mkdir -p "$dest"
  printf '# stub baseline\nFoo v1 spec.a type=string\n' > "$dest/crd-shape.txt"
  exit 0
fi
exit 0
EOF
  chmod +x "$tmp/bin/gh"
  : > "$tmp/delete.log"
  arm "our cluster name already exists -> VOID" "$VOID" \
      LOADAVG_CMD="echo { 0,10 0,10 0,10 }" \
      STUB_CLUSTERS="nova-render-000-stub" STUB_DELETE_LOG="$tmp/delete.log"
  if [ -s "$tmp/delete.log" ]; then
    printf '    %-58s FAIL (it deleted a cluster it did not create!)\n' "...and did NOT delete it"; failn=$((failn+1))
    cat "$tmp/delete.log" | sed 's/^/        /'
  else
    printf '    %-58s PASS (delete log empty)\n' "...and did NOT delete it"; pass=$((pass+1))
  fi
  # POSITIVE CONTROL for the collision arm: the SAME setup with a DIFFERENT
  # existing cluster name must get past the safety check and reach the create.
  arm "a different cluster name does NOT block -> create reached" "$FAIL" \
      LOADAVG_CMD="echo { 0,10 0,10 0,10 }" \
      STUB_CLUSTERS="aruba-110 helm-test" STUB_CREATE_RC=1

  echo
  echo "  hack/render-report.py -- driven here because validate.py requires every"
  echo "  hack script to be exercised by CI, and the only other caller is a real run"
  rep(){ # rep <name> <expected-rc> <rds-json> <crds-json>
    local name="$1" want="$2" rj="$3" cj="$4" rc
    printf '%s' "$rj" > "$tmp/rds.json"; printf '%s' "$cj" > "$tmp/crds.json"
    python3 "$REPO_ROOT/hack/render-report.py" "$tmp/rds.json" "$tmp/crds.json" "$GROUP" \
      > "$tmp/rep.out" 2>&1
    rc=$?
    if [ "$rc" = "$want" ]; then
      printf '    %-58s PASS (rc=%s)\n' "$name" "$rc"; pass=$((pass+1))
    else
      printf '    %-58s FAIL (want %s, got %s)\n' "$name" "$want" "$rc"; failn=$((failn+1))
      sed 's/^/        /' "$tmp/rep.out" | tail -4
    fi
  }
  local CFG_CRD='{"items":[{"spec":{"group":"'"$GROUP"'","names":{"kind":"XConfiguration","plural":"xconfigurations"},"versions":[{"name":"v1alpha1","schema":{"openAPIV3Schema":{"properties":{"spec":{"properties":{"authentication":{"properties":{"apiKey":{"properties":{"header":{},"tokenRef":{}}}}}}}}}}}]}}]}'
  rep "no RestDefinitions -> VOID"            "$VOID" '{"items":[]}' "$CFG_CRD"
  rep "no Configuration CRDs in group -> VOID" "$VOID" \
      '{"items":[{"metadata":{"name":"a"},"status":{"conditions":[],"schemaWarnings":[]}}]}' \
      '{"items":[]}'
  # POSITIVE CONTROL: the same report with both halves present must PASS, or the
  # two VOIDs above would also fire on a healthy render and prove nothing.
  rep "healthy fixture -> 0"                  "0" \
      '{"items":[{"metadata":{"name":"a"},"status":{"conditions":[{"type":"Ready","status":"True","reason":"Available"}],"schemaWarnings":[]}}]}' \
      "$CFG_CRD"
  # The absent-vs-empty distinction: no schemaWarnings KEY at all must say so,
  # because "0 warnings" and "the field does not exist here" are different claims.
  rep "schemaWarnings absent -> 0, and says UNKNOWN"  "0" \
      '{"items":[{"metadata":{"name":"a"},"status":{"conditions":[]}}]}' "$CFG_CRD"
  if grep -q "ABSENT on every definition" "$tmp/rep.out"; then
    printf '    %-58s PASS\n' "...and names it as absent, not clean"; pass=$((pass+1))
  else
    printf '    %-58s FAIL (no ABSENT notice)\n' "...and names it as absent, not clean"; failn=$((failn+1))
  fi

  echo
  echo "  $pass passed, $failn failed"
  [ "$failn" -eq 0 ] || return 1
  return 0
}

# ---------------------------------------------------------------------------
main(){
  case "${1:-}" in
    --selftest) selftest; exit $? ;;
    "" ) printf 'usage: %s <oasgen-provider-version>\n       %s --selftest\n' "$0" "$0" >&2; exit "$USAGE" ;;
    -* ) printf 'unknown flag: %s\n' "$1" >&2; exit "$USAGE" ;;
    *  ) render "$1" ;;
  esac
}
main "$@"
