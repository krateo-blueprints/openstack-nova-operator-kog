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
# THE FIRST VERSION OF THIS PROBE PRODUCED A FALSE ALARM, and the correction is
# the reason the rest of this file looks the way it does. It asked
# `can-i get secrets` of each controller's ServiceAccount and reported "6 of 6
# CANNOT read Secrets -- do NOT pin this oasgen version". The cluster it ran on
# had NO auth Secret and NO CRs: oasgen had correctly granted nothing, its own
# `status.authSecretRBACNamespaces` was empty and `authSecretDigest` was the FNV
# offset basis -- the digest of no input. "Cannot read a Secret that nobody
# asked it to read" is not the 0.54.0 class, and reporting it would have sent a
# maintainer to look for a break that was not there.
#
# So this probe now PLANTS ITS OWN FIXTURE -- a dummy Secret and a
# <Kind>Configuration referencing it -- and refuses to draw any conclusion
# until oasgen has SAID it granted something. That also splits one question
# into two, which were conflated before:
#
#   oasgen never claims a grant     -> the grant path did not run
#   oasgen claims a grant, can-i no -> the grant is insufficient (the 0.54.0 class)
#
# Those are different defects with different owners, and they must not share a
# message.
#
# THIS PROBE CHURNS THE GENERATED CONTROLLERS, AND THAT CONSTRAINS WHAT MAY RUN
# AFTER IT. There are TWO separate restart triggers upstream, and this probe
# pulls both. The first version of this comment named only one, which is the
# same incompleteness it was written to describe:
#
#   oasgen-provider#507  "A Configuration change restarts the controller
#                        exactly when it is healthy, killing an in-flight
#                        blocking async create"
#   oasgen-provider#588  "A generated controller is restarted on every
#                        deploy.Deploy, so any secretRef Secret-set change
#                        rolls it and loses its logs"
#
# Both titles read from the issues rather than taken from the message that
# named them. #588 is azuredevops-provider-kog's, who measured 8 Deployment
# revisions in 15 seconds against 2 in a full day; they are also the ones who
# pointed out that a suite applying a Configuration AND creating CRs naming
# Secrets has two restart sources, not one.
#
# This probe applies one Configuration per kind (the #507 path) AND those
# Configurations name a Secret through tokenRef (the #588 path), then deletes
# both on exit -- so every generated controller is rolled repeatedly per run,
# from two independent causes.
#
#   => RUN LOG-BASED PROBES FIRST. hack/probe-549-reapply.sh counts lines in a
#      controller's log across a window. If this probe runs first, those logs
#      are gone and the count starts from zero mid-measurement. The #549 probe
#      has a log-head check that would VOID rather than lie, but a VOID is a
#      wasted window and the ordering is free.
#
# IT ALSO MEANS AN ATTRIBUTION OF MINE WAS INCOMPLETE. The comment below on the
# end-to-end check says the probe's own annotation caused the False/Creating
# state it was reading. That is one sufficient cause; #588's controller restart
# is a second, and the run I drew the conclusion from did both at once. The fix
# -- wait for reconvergence rather than snapshotting -- is right under either
# cause, which is why the result stands; but "my annotation caused it" was a
# choice between two explanations that I did not measure apart.
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
# WHAT IS RUNNING IS THE POD'S DIGEST, NOT THE DEPLOYMENT'S TAG. Measured the
# hard way on 2026-11-11: after `helm upgrade` to 0.58.0 the Deployment's image
# field read `:0.58.0` while the pod still carried the 0.57.0 digest, because
# the rollout had not finished. A probe that reports the Deployment's tag as
# "the version under test" would have labelled a 0.57.0 measurement 0.58.0, and
# nothing in its output would have shown it. The only reason I caught it was
# comparing against a digest the maintainer had published.
#
# So: wait for the rollout, then read the POD's imageID, and VOID if the pods
# disagree with each other -- mid-rollout is not a version.
# shellcheck disable=SC2329  # invoked immediately below
_running_digest() {
  local pods digests
  K -n "$NS" rollout status deploy/oasgen-provider --timeout=180s >/dev/null 2>&1 || return 1
  pods=$(K -n "$NS" get pod -l app.kubernetes.io/name=oasgen-provider \
    -o jsonpath='{range .items[*]}{.status.containerStatuses[0].imageID}{"\n"}{end}' 2>/dev/null) || return 1
  [ -n "$pods" ] || return 1
  digests=$(printf '%s\n' "$pods" | sed 's/.*sha256://' | sort -u | grep -c .)
  [ "$digests" = "1" ] || return 2
  printf '%s\n' "$pods" | head -1
}
RUNDIG=$(_running_digest); rc=$?
case "$rc" in
  0) say "running digest: $RUNDIG" ;;
  2) void "the oasgen-provider pods are running MORE THAN ONE image digest, so there is no single version under test. Mid-rollout is not a version." ;;
  *) void "could not determine the running image digest (rollout never completed, or no pod reported one). The Deployment's tag is what was REQUESTED, not what is running." ;;
esac


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
cleanup() {
  K -n "$NS" delete serviceaccount probe-rbac-nobody --ignore-not-found >/dev/null 2>&1 || true
  K -n "$NS" delete secret probe-rbac-token --ignore-not-found >/dev/null 2>&1 || true
  for row in "${CFG_KINDS[@]:-}"; do
    [ -n "$row" ] || continue
    K -n "$NS" delete "${row%%$'\t'*}" probe-rbac-cfg --ignore-not-found >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT

# A cluster-admin-ish principal must read yes, or can-i is not answering.
POS=$(classify_cani "$(K auth can-i get secrets -n "$NS" 2>/dev/null)")
NEG=$(classify_cani "$(K auth can-i get secrets -n "$NS" --as "$NEG_SA" 2>/dev/null)")
say "positive control (me):            $POS"
say "negative control (bound to none): $NEG"
[ "$POS" = allowed ] || void "can-i does not report 'yes' even for my own identity, so a 'no' below would be uninterpretable."
[ "$NEG" = denied ]  || void "can-i does not report 'no' for a ServiceAccount with no bindings, so it cannot distinguish denied from allowed."
ok "can-i reports yes for an allowed principal and no for an unbound one"

bold "FIXTURE -- plant a dummy Secret and a Configuration that references it"
# Credential-free: a literal placeholder. The question is whether the
# controller can READ the Secret, never whether the vendor accepts its contents.
SEC=probe-rbac-token
CFG=probe-rbac-cfg
# ONE CONFIGURATION PER KIND, and the reason is a false positive this probe
# produced twice. Planting a single ComputeAggregateConfiguration got exactly
# one grant -- nova-aggregate=["krateo-system"], every other RestDefinition
# empty -- and the probe then reported "5 of 6 controllers CANNOT read Secrets"
# as the 0.54.0 class. They could not read it because NOTHING HAD ASKED THEM
# TO. A controller with no Configuration referencing a Secret is correctly
# denied, and checking every controller against a Secret only one of them was
# granted is checking a subject against a condition that does not apply to it.
#
# All six of this chart's documents are apiKey, so all six are the corpus and
# all six get a fixture.
# A LITERAL TAB, NOT `\t`. In an ERE, BSD grep (macOS) interprets `\t` as a
# tab and GNU grep (Linux, and CI) interprets it as a literal `t`. So
# `Configuration\t` matched here and required "Configurationt" on CI --
# meaning this discovery found NOTHING on Linux and the probe VOIDed with
# "no <Kind>Configuration CRD is served". Every result this probe has ever
# produced came from a Mac, so the defect was invisible until CI ran it.
TAB=$(printf '\t')
mapfile -t CFG_KINDS < <(K get crd -o jsonpath='{range .items[*]}{.spec.names.kind}{"\t"}{.spec.group}{"\t"}{.spec.versions[0].name}{"\n"}{end}' 2>/dev/null | grep -E "^[A-Za-z]+Configuration$TAB")
[ "${#CFG_KINDS[@]}" -gt 0 ] || void "no <Kind>Configuration CRD is served, so there is no way to reference a Secret and nothing to grant RBAC for."
say "Configuration kinds served: ${#CFG_KINDS[@]}"

K -n "$NS" create secret generic "$SEC" --from-literal=token=placeholder-not-a-credential >/dev/null 2>&1 || true
K -n "$NS" get secret "$SEC" >/dev/null 2>&1 || void "could not create the fixture Secret in $NS, so nothing below was measured."

PLANTED=0
for row in "${CFG_KINDS[@]}"; do
  ck="${row%%$'\t'*}"; rest="${row#*$'\t'}"
  cg="${rest%%$'\t'*}"; cv="${rest##*$'\t'}"
  MANIFEST=$(mktemp)
  cat > "$MANIFEST" <<EOF
apiVersion: $cg/$cv
kind: $ck
metadata:
  name: $CFG
  namespace: $NS
spec:
  authentication:
    apiKey:
      tokenRef:
        name: $SEC
        namespace: $NS
        key: token
      header: X-Auth-Token
EOF
  if K apply -f "$MANIFEST" >/dev/null 2>&1; then
    PLANTED=$((PLANTED+1))
  else
    say "could not apply $ck -- it will be reported as ungranted below rather than silently skipped"
  fi
  rm -f "$MANIFEST"
done
[ "$PLANTED" -eq "${#CFG_KINDS[@]}" ] || \
  void "planted only $PLANTED of ${#CFG_KINDS[@]} Configuration fixtures. A partial fixture produces partial grants, which is exactly the false positive this probe already made once."
ok "fixture planted: Secret/$SEC referenced by all ${#CFG_KINDS[@]} Configuration kinds"

bold "WAIT FOR OASGEN TO CLAIM A GRANT -- its own status, not my inference"
# status.authSecretRBACNamespaces is oasgen's statement about where it granted
# access. Until it is non-empty there is nothing to test, and a `no` from can-i
# would mean "not granted yet" rather than "granted and insufficient".
# shellcheck disable=SC2329  # invoked by name through wait_for
_claimed() {
  # EVERY RestDefinition must claim a grant, not merely one of them. `any` was
  # the first version and it let the probe proceed with five of six ungranted,
  # then blame the controllers for being denied.
  local n_total n_granted
  n_total=$(K -n "$NS" get restdefinitions.ogen.krateo.io --no-headers 2>/dev/null | wc -l | tr -d ' ') || return 1
  [ "${n_total:-0}" -gt 0 ] || return 1
  n_granted=$(K -n "$NS" get restdefinitions.ogen.krateo.io \
    -o jsonpath='{range .items[*]}{.status.authSecretRBACNamespaces}{"\n"}{end}' 2>/dev/null \
    | grep -c 'krateo' ) || n_granted=0
  [ "$n_granted" -eq "$n_total" ]
}
# TOUCH THE RESTDEFINITIONS, then wait LONGER THAN THE REQUEUE PERIOD.
# oasgen watches RestDefinitions, not Configurations, so planting a
# Configuration does not wake it: the grant lands on the next periodic
# reconcile. Its own log says `requeue-after` three minutes, and the first
# version of this waited 120s -- SHORTER THAN THE SYSTEM'S OWN PERIOD -- and
# reported "oasgen never recorded a grant", which was a statement about my
# patience rather than about the grant path.
#
# The touch is an annotation, which is a main-resource write and does force a
# reconcile (a /status patch does not). It is safe as an accelerator because it
# cannot fake the outcome: only oasgen can write
# status.authSecretRBACNamespaces, so nothing I do here can satisfy the gate
# the way a resourceVersion gate can be satisfied by its own trigger.
for rd in "${RDS[@]}"; do
  K -n "$NS" annotate restdefinitions.ogen.krateo.io "$rd" \
    "probe-rbac//touch=$(date +%s)" --overwrite >/dev/null 2>&1 || true
done
say "touched ${#RDS[@]} RestDefinition(s) to force a reconcile; waiting up to 300s"
if ! wait_for "oasgen to record an authSecretRBAC grant" 100 3 _claimed; then
  bad "oasgen never recorded an authSecretRBAC grant (status.authSecretRBACNamespaces stayed empty)"
  say "after a Configuration referencing Secret/$SEC was applied and accepted, the"
  say "RestDefinitions were touched to force a reconcile, and 300s elapsed -- which"
  say "is longer than the provider's own ~180s requeue period."
  say "This is the GRANT PATH not running -- a different defect from a grant"
  say "that is insufficient, and it must not be reported as the 0.54.0 class."
  exit 1
fi
CLAIMED=$(K -n "$NS" get restdefinitions.ogen.krateo.io -o jsonpath='{range .items[*]}{.metadata.name}={.status.authSecretRBACNamespaces} {end}' 2>/dev/null)
ok "oasgen claims a grant: $CLAIMED"

bold "SUBJECT -- can each controller read the Secret it was granted?"
mapfile -t DEPLOYS < <(K -n "$NS" get deploy -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.template.spec.serviceAccountName}{"\n"}{end}' 2>/dev/null | grep -vE '^[[:space:]]*$' )
[ "${#DEPLOYS[@]}" -gt 0 ] || void "could not list Deployments in $NS, so no controller ServiceAccount was examined."

FINDINGS=0; EXAMINED=0
for row in "${DEPLOYS[@]}"; do
  dep="${row%%$'\t'*}"; sa="${row##*$'\t'}"
  case "$dep" in *oasgen*|*core-provider*|*controller-render*) continue ;; esac
  [ -n "$sa" ] || sa=default
  EXAMINED=$((EXAMINED+1))
  # The NAMED form, because an auth-secret grant is legitimately
  # resourceNames-scoped and the collection form would then read `no` for a
  # perfectly correct Role. Asking the unnamed question was the other half of
  # the original false alarm.
  subj=$(classify_cani "$(K auth can-i get "secret/$SEC" -n "$NS" --as "system:serviceaccount:$NS:$sa" 2>/dev/null)")
  v=$(verdict "$subj" "$POS" "$NEG")
  case "$v" in
    clean) ok  "$dep (sa=$sa) can read Secret/$SEC" ;;
    found) bad "$dep (sa=$sa) CANNOT read Secret/$SEC though oasgen granted it -- this is the 0.54.0 class"; FINDINGS=$((FINDINGS+1)) ;;
    void)  void "can-i returned something uninterpretable for $dep (sa=$sa). Not a pass." ;;
  esac
done
[ "$EXAMINED" -gt 0 ] || void "found ${#DEPLOYS[@]} Deployments but none looked like a generated controller, so zero controllers were examined."
say "controllers examined: $EXAMINED"

bold "END TO END -- permission is not reconciliation"
# THIS CHECK WAS TRIPPED BY THE PROBE'S OWN TRIGGER. The annotation above
# forces a reconcile, so reading the conditions immediately afterwards catches
# every definition at `False/Creating` -- and the first version of this
# reported "6 of 6 RestDefinitions are not Ready" as a finding. The probe
# caused the state it then complained about, which is the same defect as a gate
# satisfied by its own trigger, pointing the other way.
#
# `Creating` is a CONVERGING state, not a failure -- core-provider's own
# readiness classifier counts it as converging. So the question is not "is it
# Creating right now" but "does it come back", and that needs a bounded wait
# whose timeout is a finding and whose success is not.
# shellcheck disable=SC2329  # invoked by name through wait_for
_all_ready() {
  local n
  n=$(K -n "$NS" get restdefinitions.ogen.krateo.io \
    -o jsonpath='{range .items[*]}{range .status.conditions[?(@.type=="Ready")]}{.status}{"\n"}{end}{end}' 2>/dev/null \
    | grep -c '^True$') || return 1
  [ "$n" -eq "${#RDS[@]}" ]
}
NOTREADY=0
if wait_for "all ${#RDS[@]} RestDefinitions to return to Ready after the touch" 60 5 _all_ready; then
  ok "all ${#RDS[@]} definitions reconciled back to Ready=True"
else
  NOTREADY=1
  for rd in "${RDS[@]}"; do
    cond=$(K -n "$NS" get restdefinitions.ogen.krateo.io "$rd" -o jsonpath='{range .status.conditions[?(@.type=="Ready")]}{.status}{"/"}{.reason}{end}' 2>/dev/null)
    say "$rd: ${cond:-<no Ready condition>}"
  done
fi

bold "VERDICT"
if [ "$FINDINGS" -gt 0 ]; then
  bad "$FINDINGS of $EXAMINED controller ServiceAccount(s) cannot read Secrets."
  say "This is the shape of the 0.54.0 break. Do NOT pin this oasgen version."
  exit 1
fi
if [ "$NOTREADY" -gt 0 ]; then
  bad "RBAC is fine but the definitions did not return to Ready within 300s."
  say "Permission is necessary and not sufficient. This is a TIMEOUT on"
  say "reconvergence after a forced reconcile, not a snapshot of a transient:"
  say "read the controller log for the reason."
  exit 1
fi
ok "apiKey RBAC: $EXAMINED controller(s) can read Secrets, ${#RDS[@]} definitions Ready, controls both directions."
say "This is a measurement of $OASGEN_IMG and of nothing else. Re-run it on the next tag."
exit 0
