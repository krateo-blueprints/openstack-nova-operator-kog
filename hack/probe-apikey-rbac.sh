#!/usr/bin/env bash
# Can the generated controllers actually READ their auth Secret?
#
# WHY THIS EXISTS, and it is not hypothetical. oasgen 0.54.0 removed a blanket
# secrets grant on the reasoning that a narrow Role "already covered the same
# ground". That Role's paths were three HAND-WRITTEN ones -- basic's
# usernameRef and passwordRef, and bearer's tokenRef -- while the generator
# also emits a third shape, apiKey in a header, whose tokenRef was never added.
# A consumer running 25 apiKey kinds and 11 bearer kinds against the same vendor
# in the same cluster measured the result: the 11 bearer kinds kept reconciling
# and the 25 apiKey kinds did not, with `can-i get secrets` returning no in
# every namespace while the CR's own access was fine.
#
# ALL SIX of this chart's documents are apiKey in a header. So this chart is the
# instrument for that class, and the measurement has to be re-run on every tag
# rather than inferred from the previous one. No static check caught it and none
# would: a test suite with one scheme per document passes while a collector
# handles a subset, however many documents it has.
#
# CREDENTIAL-FREE BY DESIGN. The question is whether the controller can READ the
# Secret, not whether the vendor accepts what is in it, so this plants a dummy
# token. Nothing here needs or should have a real credential.
#
# RUNS ON AN EXISTING CLUSTER. Creates no cluster and deletes none.
set -uo pipefail
export LC_ALL=C

# shellcheck source=hack/lib/wait.sh
. "$(dirname "$0")/lib/wait.sh"

KUBECONFIG_PATH="${1:-}"
CONTEXT="${2:-}"
NS="${3:-krateo-system}"

bold() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
ok()   { printf '\033[32m  OK: %s\033[0m\n' "$1"; }
bad()  { printf '\033[31m  FOUND: %s\033[0m\n' "$1"; }
say()  { printf '  %s\n' "$1"; }

# VOID is the exit code for "could not measure". It must never share a code
# with "measured, and it is fine" -- that is the defect this whole file is
# shaped around.
void() { printf '\n\033[33m  VOID: %s\033[0m\n' "$1"; exit 9; }

K() { kubectl --kubeconfig "$KUBECONFIG_PATH" --context "$CONTEXT" "$@"; }

# --- the classifier, kept pure so --self-test can exercise it ---------------
# can-i output is "yes" or "no" on stdout; anything else is an instrument
# failure and must not be read as either answer.
classify_cani() {
  case "$1" in
    yes) echo allowed ;;
    no)  echo denied ;;
    *)   echo unreadable ;;
  esac
}

# Given the controller's verdict and the two controls, say what was measured.
# The controls are what make a "denied" meaningful: without them, a denial is
# equally explained by can-i being broken, or by every SA being denied.
verdict() {
  local subject="$1" pos="$2" neg="$3"
  if [ "$pos" != allowed ] || [ "$neg" != denied ]; then
    echo void
  elif [ "$subject" = allowed ]; then
    echo clean
  elif [ "$subject" = denied ]; then
    echo found
  else
    echo void
  fi
}

selftest() {
  local fails=0 got
  bold "--self-test: the classifier and the control gating"
  for pair in "yes:allowed" "no:denied" ":unreadable" "error:unreadable" "Yes:unreadable"; do
    got=$(classify_cani "${pair%%:*}")
    if [ "$got" = "${pair##*:}" ]; then
      say "classify_cani ${pair%%:*} -> $got  ok"
    else
      say "classify_cani ${pair%%:*} -> $got, wanted ${pair##*:}  *** WRONG ***"; fails=$((fails+1))
    fi
  done
  # subject:pos:neg:want
  for row in \
    "allowed:allowed:denied:clean" \
    "denied:allowed:denied:found" \
    "unreadable:allowed:denied:void" \
    "denied:denied:denied:void" \
    "denied:allowed:allowed:void" \
    "allowed:unreadable:denied:void" \
    "allowed:allowed:allowed:void"
  do
    IFS=: read -r s p n want <<<"$row"
    got=$(verdict "$s" "$p" "$n")
    if [ "$got" = "$want" ]; then
      say "verdict($s, pos=$p, neg=$n) -> $got  ok"
    else
      say "verdict($s, pos=$p, neg=$n) -> $got, wanted $want  *** WRONG ***"; fails=$((fails+1))
    fi
  done
  # The suite must be able to produce every verdict, or it proves nothing about
  # a function returning a constant.
  local seen_clean seen_found seen_void
  seen_clean=$(verdict allowed allowed denied)
  seen_found=$(verdict denied allowed denied)
  seen_void=$(verdict allowed allowed allowed)
  if [ "$seen_clean" = clean ] && [ "$seen_found" = found ] && [ "$seen_void" = void ]; then
    say "all three verdicts reachable  ok"
  else
    say "*** SUITE DEFECT: not every verdict is reachable ***"; fails=$((fails+1))
  fi
  # The two control failures must VOID rather than being absorbed into the
  # subject's answer: a denied SUBJECT with a broken control is NOT a finding.
  if [ "$(verdict denied denied denied)" = void ] && [ "$(verdict denied allowed denied)" = found ]; then
    say "a denial with broken controls VOIDs; with good controls it is a finding  ok"
  else
    say "*** SUITE DEFECT: control gating does not change the verdict ***"; fails=$((fails+1))
  fi
  if [ "$fails" -ne 0 ]; then printf '\n  --- FAIL: %s arm(s) ---\n' "$fails"; return 1; fi
  printf '\n  --- all arms pass ---\n'
  return 0
}

if [ "${1:-}" = "--self-test" ]; then selftest; exit $?; fi

[ -n "$KUBECONFIG_PATH" ] && [ -n "$CONTEXT" ] || {
  echo "usage: probe-apikey-rbac.sh <kubeconfig> <context> [namespace]" >&2
  echo "       probe-apikey-rbac.sh --self-test" >&2
  echo "The context is REQUIRED and never defaulted: the ambient kubeconfig on" >&2
  echo "a shared machine reaches real remote clusters." >&2
  exit 2
}

bold "preconditions -- each failure says what it means"
K version --request-timeout=10s >/dev/null 2>&1 || \
  void "the cluster is not reachable with that kubeconfig/context. Nothing below was measured."
OASGEN_IMG=$(K -n "$NS" get deploy -l app.kubernetes.io/name=oasgen-provider \
  -o jsonpath='{.items[0].spec.template.spec.containers[0].image}' 2>/dev/null)
[ -n "$OASGEN_IMG" ] || void "could not find the oasgen-provider Deployment in $NS, so the version under test is unknown."
say "oasgen image under test: $OASGEN_IMG"

mapfile -t RDS < <(K -n "$NS" get restdefinitions.ogen.krateo.io -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null)
[ "${#RDS[@]}" -gt 0 ] || void "zero RestDefinitions in $NS. A probe over an empty set reports clean on any cluster."
say "RestDefinitions: ${#RDS[@]} (${RDS[*]})"

# Every document here must BE apiKey, or this probe is measuring the wrong
# scheme and a clean result says nothing about the class it exists for.
APIKEY_N=0
for rd in "${RDS[@]}"; do
  if [ "$(K -n "$NS" get restdefinitions.ogen.krateo.io "$rd" -o jsonpath='{.status.hasSecuritySchemes}' 2>/dev/null)" = "true" ]; then
    APIKEY_N=$((APIKEY_N+1))
  fi
done
[ "$APIKEY_N" -eq "${#RDS[@]}" ] || \
  void "only $APIKEY_N of ${#RDS[@]} RestDefinitions report hasSecuritySchemes=true. This probe is for the apiKey path; a mixed corpus needs the scheme read per document first."
ok "all ${#RDS[@]} documents report a security scheme"

bold "CONTROLS -- can-i must be able to say both yes and no here"
# The POSITIVE control is my own identity rather than a borrowed
# ServiceAccount: whoever runs this can already list Deployments and create a
# ServiceAccount, so if can-i says "no" for me it is not answering at all. The
# NEGATIVE control has to be manufactured, because every pre-existing
# ServiceAccount might legitimately be allowed and a control that is allowed by
# accident proves nothing.
NEG_SA="system:serviceaccount:$NS:probe-rbac-nobody"
K -n "$NS" create serviceaccount probe-rbac-nobody >/dev/null 2>&1 || true
# shellcheck disable=SC2329  # invoked by the EXIT trap on the next line
cleanup() { K -n "$NS" delete serviceaccount probe-rbac-nobody --ignore-not-found >/dev/null 2>&1 || true; }
trap cleanup EXIT

# A cluster-admin-ish principal must read yes, or can-i is not answering.
POS=$(classify_cani "$(K auth can-i get secrets -n "$NS" 2>/dev/null)")
NEG=$(classify_cani "$(K auth can-i get secrets -n "$NS" --as "$NEG_SA" 2>/dev/null)")
say "positive control (me):            $POS"
say "negative control (bound to none): $NEG"
[ "$POS" = allowed ] || void "can-i does not report 'yes' even for my own identity, so a 'no' below would be uninterpretable."
[ "$NEG" = denied ]  || void "can-i does not report 'no' for a ServiceAccount with no bindings, so it cannot distinguish denied from allowed."
ok "can-i reports yes for an allowed principal and no for an unbound one"

bold "SUBJECT -- each generated controller's own ServiceAccount"
mapfile -t DEPLOYS < <(K -n "$NS" get deploy -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.template.spec.serviceAccountName}{"\n"}{end}' 2>/dev/null | grep -vE '^\s*$' )
[ "${#DEPLOYS[@]}" -gt 0 ] || void "could not list Deployments in $NS, so no controller ServiceAccount was examined."

FINDINGS=0; EXAMINED=0
for row in "${DEPLOYS[@]}"; do
  dep="${row%%$'\t'*}"; sa="${row##*$'\t'}"
  case "$dep" in *oasgen*|*core-provider*|*controller-render*) continue ;; esac
  [ -n "$sa" ] || sa=default
  EXAMINED=$((EXAMINED+1))
  subj=$(classify_cani "$(K auth can-i get secrets -n "$NS" --as "system:serviceaccount:$NS:$sa" 2>/dev/null)")
  v=$(verdict "$subj" "$POS" "$NEG")
  case "$v" in
    clean) ok  "$dep (sa=$sa) can read Secrets in $NS" ;;
    found) bad "$dep (sa=$sa) CANNOT read Secrets in $NS -- this is the 0.54.0 class"; FINDINGS=$((FINDINGS+1)) ;;
    void)  void "can-i returned something uninterpretable for $dep (sa=$sa). Not a pass." ;;
  esac
done
[ "$EXAMINED" -gt 0 ] || void "found ${#DEPLOYS[@]} Deployments but none looked like a generated controller, so zero controllers were examined."
say "controllers examined: $EXAMINED"

bold "END TO END -- permission is not reconciliation"
NOTREADY=0
for rd in "${RDS[@]}"; do
  cond=$(K -n "$NS" get restdefinitions.ogen.krateo.io "$rd" -o jsonpath='{range .status.conditions[?(@.type=="Ready")]}{.status}{"/"}{.reason}{end}' 2>/dev/null)
  [ -n "$cond" ] || cond="<no Ready condition>"
  case "$cond" in
    True/*) say "$rd: $cond" ;;
    *)      say "$rd: $cond   <-- not Ready"; NOTREADY=$((NOTREADY+1)) ;;
  esac
done

bold "VERDICT"
if [ "$FINDINGS" -gt 0 ]; then
  bad "$FINDINGS of $EXAMINED controller ServiceAccount(s) cannot read Secrets."
  say "This is the shape of the 0.54.0 break. Do NOT pin this oasgen version."
  exit 1
fi
if [ "$NOTREADY" -gt 0 ]; then
  bad "RBAC is fine but $NOTREADY of ${#RDS[@]} RestDefinitions are not Ready."
  say "Permission is necessary and not sufficient; read the controller log for the reason."
  exit 1
fi
ok "apiKey RBAC: $EXAMINED controller(s) can read Secrets, ${#RDS[@]} definitions Ready, controls both directions."
say "This is a measurement of $OASGEN_IMG and of nothing else. Re-run it on the next tag."
exit 0
