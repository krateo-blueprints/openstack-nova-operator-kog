#!/usr/bin/env bash
#
# When this chart's observe cannot look a resource up, does the controller
# CREATE IT AGAIN?
#
# WHY THIS IS A REAL QUESTION AND NOT A HYPOTHETICAL. rd-flavor.yaml's get verb
# takes its path parameter from the CUSTOM RESOURCE, not from the vendor:
#
#     - action: get
#       path: /flavors/{id}
#       requestFieldMapping:
#         - inPath: id
#           inCustomResource: status.flavor.id
#
# So observe can only issue a GET once `status.flavor.id` is populated, and that
# field is populated from the create response by `populateStatusFields`, which
# `continue`s silently when a declared field is absent from the vendor's payload
# -- no error, no event, no log. Two ways to end up with an empty id therefore
# exist, and one of them needs no misconfiguration at all: a vendor whose create
# response omits `id`.
#
# THIS ALSO EXPLAINS AN OBSERVATION THAT LOOKED LIKE A DEFECT. In the #108 401
# arms the mock refused every request, so no create ever succeeded,
# `status.flavor.id` was never populated, the get verb had no id to substitute,
# and the access log contained POSTs only. That was read as "the controller
# reaches create even though observe was refused". It is simpler than that:
# observe had nothing to look up. The arms below are the ones that can tell the
# two apart, because they let the create SUCCEED first.
#
#   ARM X   create returns 200 with NO `id` in the body.
#           Does the controller POST again, and again?
#           This is the cheap arm and the likely one: it needs only a vendor
#           whose response shape differs from the OAS, which real Nova already
#           does elsewhere (flavor LIST omits ram/vcpus/disk; quota_set returns
#           `id`, not `tenant_id`).
#
#   ARM Y   create returns 200 WITH an id, then every request starts failing.
#           Does the controller POST again?
#           This is "is a rejection on observe read as absence".
#
# WHAT A FINDING WOULD MEAN: a duplicate resource at the vendor for every
# reconcile, invisible from the CR, which is a billing problem rather than an
# aesthetic one.
#
#   hack/probe-observe-rejection.sh <kubeconfig> <context> [namespace]
#   hack/probe-observe-rejection.sh --self-test     # classifier only, no cluster
#
# RUNS ON AN EXISTING CLUSTER. Creates no cluster and deletes none.
set -uo pipefail
export LC_ALL=C

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m  OK: %s\033[0m\n' "$*"; }
bad()  { printf '\033[31m  BAD: %s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mFAIL: %s\033[0m\n' "$*" >&2; exit 1; }
void() { printf '\n\033[33mVOID: %s\033[0m\n' "$*" >&2; exit 3; }

PROBE_UA=probe-observe-control

# ---------------------------------------------------------------------------
# shellcheck source=hack/lib/wait.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/wait.sh"
# SOURCED HERE, above every user, for two reasons. Correctness: this script
# runs under `set -uo pipefail` and NOT `set -e`, so a source that
# fails -- a moved lib, a copy of this file taken out of the tree -- does not
# stop it. Every wait_for would then be "command not found", return non-zero,
# and the cluster arms would VOID blaming the cluster for a missing file. Found
# while mutation-testing the arms below: the mutants were run from a scratch
# directory, wait_for was undefined, and three arms failed for that reason
# while appearing to indict the mutation. An instrument that is absent must not
# read as a fact about the thing it measures.
command -v wait_for >/dev/null \
  || fail "hack/lib/wait.sh was sourced but did not define wait_for. Nothing
    below can wait for anything; every retry would read as a failure of the
    thing being waited for."
# shellcheck source=hack/lib/attribute.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/attribute.sh"
# Same guard, and it earned itself immediately: the self-test arms for these
# two parsers were written before this source was, and TWO of them PASSED with
# the functions undefined -- "0 timed records" and "both parsers agree" are both
# satisfied by two parsers that do not exist. The arms below were rewritten to
# be non-vacuous as well, but the guard is what makes the failure say so.
# Written as an `if` rather than `A && B || fail`: that spelling is correct
# here, but it is the shape SC2015 flags because it stops being correct the
# moment anyone adds a third condition. A guard against a silent instrument is
# the wrong place to be clever.
if ! command -v attribute_log >/dev/null || ! command -v timed_log >/dev/null; then
  fail "hack/lib/attribute.sh was sourced but did not define attribute_log and
    timed_log. Every access-log reading below would be empty, and an empty log
    reads as 'the controller made no request' -- the exact false answer this
    probe exists to avoid."
fi

# THE CLASSIFIER, kept away from any cluster so it can be self-tested.
#
# Reads attribution records (METHOD PATH STATUS UA) on stdin and prints
#
#     VERDICT=recreated   at least one POST from the controller in this window
#     VERDICT=observed    GETs but no POST -- observe ran and did not re-create
#     VERDICT=silent      no controller request at all
#     VERDICT=other       controller requests, but neither GET nor POST
#
# `silent` IS NOT `observed`. "Zero POSTs because the controller did not
# re-create" and "zero POSTs because the controller never looked" are the same
# integer, and the second is an instrument failure. They must not share a word.
# ---------------------------------------------------------------------------
classify_records() {
  local ua="$1" recs posts gets ctl
  recs=$(cat)
  ctl=$(printf '%s\n' "$recs" | awk -v ua="$ua" 'NF >= 4 && $4 != ua')
  if [ -z "$ctl" ]; then echo "VERDICT=silent"; echo "CTL=0 POST=0 GET=0"; return 0; fi
  posts=$(printf '%s\n' "$ctl" | awk '$1 == "POST"' | grep -c . || true)
  gets=$(printf '%s\n' "$ctl" | awk '$1 == "GET"' | grep -c . || true)
  printf 'CTL=%s POST=%s GET=%s\n' "$(printf '%s\n' "$ctl" | grep -c . || true)" "$posts" "$gets"
  if [ "$posts" -ge 1 ];  then echo "VERDICT=recreated"
  elif [ "$gets" -ge 1 ]; then echo "VERDICT=observed"
  else                         echo "VERDICT=other"; fi
}

verdict_of() { classify_records "$1" | sed -n 's/^VERDICT=//p'; }

self_test() {
  local pass=0 fail_n=0
  t() {
    if [ "$2" = "$3" ]; then printf '  %-56s PASS\n' "$1"; pass=$((pass+1))
    else printf '  %-56s FAIL want=%s got=%s\n' "$1" "$2" "$3"; fail_n=$((fail_n+1)); fi
  }
  # shellcheck disable=SC2016  # single quotes keep awk's $n unexpanded below
  :

  t "a POST from the controller is a re-create" recreated \
    "$(printf 'POST /v2.1/p/flavors 200 Go-http-client/1.1\n' | verdict_of "$PROBE_UA")"
  t "GETs with no POST is observe without re-create" observed \
    "$(printf 'GET /v2.1/p/flavors/x 401 Go-http-client/1.1\nGET /v2.1/p/flavors/x 401 Go-http-client/1.1\n' | verdict_of "$PROBE_UA")"
  t "no controller record is SILENT, never observed" silent \
    "$(printf '' | verdict_of "$PROBE_UA")"

  # THE ARM THAT MAKES THE WINDOW MEAN ANYTHING. The probe's own control
  # request hits the same paths the controller uses, so if it were counted the
  # verdict would be a measurement of this script.
  t "the probe's own POST does not count as a re-create" silent \
    "$(printf 'POST /v2.1/p/flavors 200 %s\n' "$PROBE_UA" | verdict_of "$PROBE_UA")"
  t "probe GET plus controller GET leaves observe" observed \
    "$(printf 'GET /v2.1/p/flavors/x 401 %s\nGET /v2.1/p/flavors/x 401 Go-http-client/1.1\n' "$PROBE_UA" | verdict_of "$PROBE_UA")"

  # A record the parser could not fill must not be read as a controller request.
  t "a short record is ignored rather than counted" silent \
    "$(printf 'GET /v2.1/p/flavors\n' | verdict_of "$PROBE_UA")"
  t "neither GET nor POST is OTHER, not observed" other \
    "$(printf 'DELETE /v2.1/p/flavors/x 401 Go-http-client/1.1\n' | verdict_of "$PROBE_UA")"

  # Counts are reported, so they are asserted.
  t "counts are reported alongside the verdict" "CTL=3 POST=1 GET=2" \
    "$(printf 'POST /a 200 c\nGET /b 401 c\nGET /b 401 c\n' | classify_records "$PROBE_UA" | sed -n 's/^CTL=/CTL=/p')"

  # MUTATION ARM: if the classifier stopped distinguishing POST from GET, the
  # two verdicts above would collapse. Asserted, not described.
  t "recreated and observed are different answers" different \
    "$([ "$(printf 'POST /a 200 c\n' | verdict_of "$PROBE_UA")" != \
         "$(printf 'GET /a 200 c\n'  | verdict_of "$PROBE_UA")" ] && echo different || echo same)"

  # ----------------------------------------------------------------------
  # THE READINESS LOOP, asserted off-cluster. Run 44334008550 VOIDed because
  # the three mock behaviour controls were single-shot against services that
  # had just started -- the THIRD time this probe made that mistake. A comment
  # would not have caught the fourth; these arms will. They drive the real
  # wait_for from hack/lib/wait.sh against a stub that honours curl_through's
  # contract, with attempts/interval small enough to stay off the clock.
  local n rc
  # (a) a transient 000 is waited out, AND $CODE reaches the caller. If CODE
  #     did not survive wait_for's invocation, every assertion after each
  #     control would read a stale value -- passing or voiding on the previous
  #     mock's status.
  # shellcheck disable=SC2329  # invoked indirectly, as wait_for's "$@"
  _stub_late() { n=$((n+1)); if [ "$n" -lt 3 ]; then CODE=""; else CODE=200; fi
                 [ -n "${CODE:-}" ] && [ "$CODE" != "000" ]; }
  n=0; CODE=sentinel
  rc=0; wait_for "stub" 5 1 _stub_late noid /p >/dev/null 2>&1 || rc=1
  t "a transient 000 is waited out and CODE reaches the caller" "rc=0 CODE=200 n=3" \
    "rc=$rc CODE=$CODE n=$n"

  # (b) a REAL but wrong status is judged on the FIRST attempt. This is the
  #     arm that keeps the retry honest: without it, wrapping the controls in
  #     wait_for could have turned a genuinely misconfigured mock into a 90s
  #     timeout, which reads as "slow cluster" instead of "wrong mock".
  # shellcheck disable=SC2329  # invoked indirectly, as wait_for's "$@"
  _stub_wrong() { n=$((n+1)); CODE=503; [ -n "$CODE" ] && [ "$CODE" != "000" ]; }
  n=0; CODE=sentinel
  rc=0; wait_for "stub" 15 6 _stub_wrong ok /p >/dev/null 2>&1 || rc=1
  t "a real but wrong status is judged on the first attempt" "rc=0 CODE=503 n=1" \
    "rc=$rc CODE=$CODE n=$n"

  # (c) a mock that never answers must TIME OUT, not pass.
  # shellcheck disable=SC2329  # invoked indirectly, as wait_for's "$@"
  _stub_never() { n=$((n+1)); CODE=""; false; }
  n=0; CODE=sentinel
  rc=0; wait_for "stub" 3 1 _stub_never reject /p >/dev/null 2>&1 || rc=1
  t "a mock that never answers times out rather than passing" "rc=1 n=3" \
    "rc=$rc n=$n"

  # (d) MUTATION GUARD on the contract itself. Arms (a)-(c) exercise a stub;
  #     this one asserts the real curl_through still discriminates 000 from a
  #     status. Without that line the loop would stop on attempt 1 with
  #     CODE=000 and the next assertion would void -- the original bug.
  t "curl_through still refuses 000 as an answer" present \
    "$(declare -f curl_through | grep -q '!= "000"' && echo present || echo GONE)"

  # ----------------------------------------------------------------------
  # THE TIMED PARSER, asserted off-cluster on both log formats. The whole
  # reason a SECOND parser exists is that prepending $msec must not disturb
  # ATTRIBUTE_AWK, which probe-401.sh and validate-401-mock.sh both depend on.
  # That is a claim about two formats, so both are fed in here.
  local tsline plainline
  tsline='1763481683.123 10.244.0.5 "POST /v2.1/army/flavors HTTP/1.1" 200 "Go-http-client/1.1"'
  plainline='10.244.0.5 "GET /v2.1/flavors HTTP/1.1" 401 "Go-http-client/1.1"'

  t "a timestamped record yields its msec first" \
    "1763481683.123 POST /v2.1/army/flavors 200 Go-http-client/1.1" \
    "$(printf '%s\n' "$tsline" | timed_log)"

  # THE COMPATIBILITY ARM. probe-401.sh does not log a timestamp and must keep
  # reading identically through the shared parser.
  t "the old parser is unchanged by the new field" \
    "POST /v2.1/army/flavors 200 Go-http-client/1.1" \
    "$(printf '%s\n' "$tsline" | attribute_log)"
  t "the old parser still reads a timestamp-less log" \
    "GET /v2.1/flavors 401 Go-http-client/1.1" \
    "$(printf '%s\n' "$plainline" | attribute_log)"

  # AN IP IS NOT A TIME. Run the timed parser on probe-401's format and $1 holds
  # a remote address; it must yield NOTHING rather than a column of plausible
  # nonsense. Zero records is an empty answer, which is readable; "172.17.0.1"
  # in a seconds column is a wrong one.
  # Stated as a PAIR, because "0 records" alone is also what a missing parser
  # produces -- which is exactly how an earlier draft of this arm passed with
  # timed_log undefined. One timed record on the timestamped line and zero on
  # the plain one cannot both be satisfied by an absent instrument.
  t "timed: 1 record with a timestamp, 0 without" "ts=1 plain=0" \
    "ts=$(printf '%s\n' "$tsline" | timed_log | grep -c . || true) plain=$(printf '%s\n' "$plainline" | timed_log | grep -c . || true)"

  # MUTATION ARM: the two parsers must agree about WHICH records exist, or a
  # timestamp could be attached to a different request than the one counted.
  # Agreement AND non-emptiness, for the same reason: two parsers that both
  # produce nothing agree perfectly.
  t "both parsers see the same records, and it is not zero" "agree=yes n=1" \
    "$(_tn=$(printf '%s\n' "$tsline" | timed_log | grep -c . || true)
       _an=$(printf '%s\n' "$tsline" | attribute_log | grep -c . || true)
       printf 'agree=%s n=%s' "$([ "$_tn" = "$_an" ] && echo yes || echo NO)" "$_tn")"

  # (g) THE TWO CURL FAILURES ARE TOLD APART. Run 44347239443 timed out with
  #     "mock-ok never returned an HTTP status" and an EMPTY predicate stderr,
  #     because curl_through sent kubectl's stderr to /dev/null -- so "curl
  #     reached nothing" and "kubectl could not run the pod" were one reading.
  #     Driven here against a stubbed K, so it needs no cluster.
  # EVERY STUB LIVES IN A SUBSHELL, and that is not tidiness. The real K() is
  # defined at the line noted above and the cluster path calls self_test AFTER
  # it, so a stub defined here would shadow it -- and the `unset -f K` an
  # earlier draft used to clean up would have DELETED the real K for the rest of
  # the run. A subshell cannot leak either way.
  local got NS=selftest
  # ARGUMENT-AWARE, and not for tidiness: curl_through now issues `run`, then
  # polls `get pod -o jsonpath={.status.phase}`, then `logs`. A stub that
  # answers every call identically never reaches Succeeded, so the bounded
  # phase loop spins its full 20 seconds per arm -- measured, this self-test
  # went from about 1s to 44s before the stubs were split.
  got="$( K() { case "$*" in *logs*) printf '200\n' ;; *jsonpath*phase*) printf 'Succeeded\n' ;; *) return 0 ;; esac; }
          CODE=""
          curl_through ok /p 2>&1 >/dev/null
          printf '|rc=%s|CODE=%s' "$?" "$CODE" )"
  t "a status reaches CODE with no stderr noise" "|rc=0|CODE=200" "$got"

  # kubectl fails: no status, and the reason MUST reach stderr for wait_for.
  got="$( K() { echo "Error from server (NotFound): pods not found" >&2; return 7; }
          CODE=""
          curl_through ok /p 2>&1 >/dev/null )"
  t "kubectl's failure reaches stderr, naming the exit status" "status=yes reason=yes" \
    "status=$(case "$got" in *"exited 7"*) echo yes ;; *) echo NO ;; esac) reason=$(case "$got" in *NotFound*) echo yes ;; *) echo NO ;; esac)"

  # A LITERAL 000 is curl's own answer, not a missing instrument: it must NOT
  # emit the stderr block, because an empty stderr is how a timeout says
  # "curl ran and reached nothing".
  got="$( K() { case "$*" in *logs*) printf '000\n' ;; *jsonpath*phase*) printf 'Succeeded\n' ;; *) return 0 ;; esac; }
          CODE=""
          curl_through ok /p 2>&1 >/dev/null
          printf '|rc=%s|CODE=%s' "$?" "$CODE" )"
  t "a literal 000 fails the predicate but prints no stderr" "|rc=1|CODE=000" "$got"

  # (i) curl_through MUST NOT GO BACK TO AN ATTACH. Run 44361232022 VOIDed at
  #     the first behaviour control with endpoints present, and the stderr
  #     capture named the cause: `kubectl run --rm -i` streams the container's
  #     stdout over an attach, and a curl that finishes first writes into a
  #     stream nobody is reading -- exit 0, no output, no error, fifteen retries
  #     losing the same race because retrying a race does not change the odds.
  #     The pod log survives the exit; the attach does not. Asserted statically,
  #     because the attach form is shorter and will look like a simplification
  #     to the next reader.
  local ct kcalls
  ct=$(declare -f curl_through)
  kcalls=$(printf '%s\n' "$ct" | grep -E 'K -n ' || true)
  # shellcheck disable=SC2016  # the single quotes are the point: the patterns
  # below match the LITERAL text `logs "$pod"` in curl_through's body, so
  # expanding $pod here would make the arm search for the empty string and pass
  # on anything. A directive must sit immediately before the command it governs
  # -- an earlier placement five lines up had no effect at all.
  t "no kubectl call in curl_through streams over an attach" \
    "kcalls=4 rm=0 dash_i=0 attach_false=1 logs=1" \
    "kcalls=$(printf '%s\n' "$kcalls" | grep -c . || true) \
rm=$(printf '%s\n' "$kcalls" | grep -c -- ' --rm' || true) \
dash_i=$(printf '%s\n' "$kcalls" | grep -cE ' -i( |$)' || true) \
attach_false=$(printf '%s\n' "$kcalls" | grep -c -- '--attach=false' || true) \
logs=$(printf '%s\n' "$kcalls" | grep -cE 'logs "\$pod"' || true)"

  # (h) #648's CONTROLS MAY NOT VOID THE RUN, AND ITS RETURNS MAY NOT BE
  #     UNCONDITIONAL. Two static facts about measure_648, both of which I got
  #     wrong by hand within five minutes of each other:
  #
  #       * a `void` inside it exits 3 and discards arms X, Y and Z -- which is
  #         exactly what run 44351381915 did to arm Y;
  #       * a `return 1` that is NOT guarded by a y648_bad immediately above it
  #         returns on every run, leaving #648 permanently unmeasured. My first
  #         attempt put one after the closing `fi` instead of inside it, and
  #         `bash -n` accepts that happily.
  local m648
  m648=$(awk '/^measure_648\(\) \{/{f=1} f{print} f&&/^\}$/{exit}' "${BASH_SOURCE[0]}")
  t "measure_648 contains no void (it would discard measured arms)" 0 \
    "$(printf '%s\n' "$m648" | grep -cE '^[[:space:]]*void ' || true)"
  t "every return in measure_648 sits inside its guard, not after the fi" "ret=3 stray=0" \
    "$(printf '%s\n' "$m648" | awk '
       function ind(x) { match(x, /^[[:space:]]*/); return RLENGTH }
       /[^[:space:]]/ { if (prev != "" && $0 ~ /^[[:space:]]*return 1$/) {
                          if (prev ~ /^[[:space:]]*fi$/ && ind(prev) < ind($0)) stray++
                        }
                        if ($0 ~ /^[[:space:]]*return 1$/) ret++
                        prev = $0 }
       END { printf "ret=%d stray=%d", ret+0, stray+0 }')"

  # (f) EVERY CONTROL IS DEFINED BEFORE IT IS CALLED. This script runs under
  #     `set -uo pipefail` and NOT `set -e`, so calling a function that is not
  #     defined yet prints "command not found" and CARRIES ON -- the control is
  #     skipped and the arms run anyway. That is not hypothetical: the
  #     single-owner check was first written with its definition 370 lines
  #     below its first call site, which would have skipped it silently on
  #     every run while the probe reported arm verdicts as usual. Static, so it
  #     needs no cluster, and it generalises to any control added later.
  local defs_bad
  defs_bad=$(awk '
    /^[a-z_]+\(\) \{/ { name = $1; sub(/\(\)$/, "", name); def[name] = NR }
    /^[[:space:]]*(assert|verify)_[a-z_]+ / {
      c = $1
      if (!(c in def) || def[c] > NR) print c " called at line " NR
    }
  ' "${BASH_SOURCE[0]}")
  t "every assert_/verify_ control is defined before it is called" "" "$defs_bad"

  # (e) THE ARM THAT WOULD HAVE CAUGHT INSTANCE #3, and the only one that can.
  #     Arms (a)-(d) prove the loop works; not one of them notices if the
  #     behaviour controls stop going through it -- they would all still pass
  #     against the single-shot code that VOIDed run 44334008550. A witness
  #     placed where the code is easy to call cannot see what the code is for.
  #     So this arm reads THIS SCRIPT'S OWN TEXT: every invocation of
  #     curl_through must be the predicate of a wait_for, and there must be
  #     exactly three (one per mock). Continuations are joined first, because
  #     the real call sites are split across lines with a trailing backslash.
  local joined wrapped bare
  # SCOPED OUTSIDE self_test, because arm (g) below legitimately calls
  # curl_through against a stubbed K and those calls are not behaviour controls.
  # Without this the arm reported bare=3 for its own siblings -- the arm working
  # correctly over the wrong extent, which is this repo's commonest error shape
  # and worth catching in the arm that exists to catch it.
  joined="$(awk 'BEGIN{skip=0} /^self_test\(\) \{/{skip=1} skip==0{print} /^\}$/{if(skip==1)skip=0}' "${BASH_SOURCE[0]}" \
            | sed -e ':a' -e '/\\$/{N;s/\\\n/ /;ba' -e '}')"
  # EVERY INVOCATION, not only the ones that begin a line. The first version of
  # this arm counted lines whose FIRST word is curl_through, and reported
  # bare=0 while a fourth, unwrapped call site read `if ! curl_through ok ...`
  # twenty lines from the three it was watching. The guard's extent was
  # narrower than the thing it guards -- this repo's commonest error shape,
  # inside the arm written to catch it.
  #
  # So: every line that invokes curl_through must ALSO name wait_for (the
  # wrapper may be on the same joined line, or the invocation may be a
  # predicate function wait_for is given). A call site that mentions neither is
  # single-shot.
  local invokes
  invokes=$(printf '%s\n' "$joined" \
    | grep -E 'curl_through [a-z"$]' | grep -v '^[[:space:]]*#' || true)
  wrapped=$(printf '%s\n' "$invokes" | grep -c 'wait_for' || true)
  bare=$(printf '%s\n' "$invokes" | grep -vc 'wait_for' || true)
  t "every live curl_through invocation goes through wait_for" "wrapped=3 bare=1" \
    "wrapped=$wrapped bare=$bare"
  # The one `bare` is the predicate DEFINITION -- `_y648_probe_get() {
  # curl_through ok ...; }` -- which is a function body, not a call site; the
  # wait_for that drives it is on its own line. Asserted by name so that a
  # genuinely single-shot call cannot hide in this allowance.
  t "the one unwrapped invocation is the named predicate body" 1 \
    "$(printf '%s\n' "$invokes" | grep -v 'wait_for' | grep -c '_y648_probe_get() {' || true)"

  printf '\n  %d passed, %d failed\n' "$pass" "$fail_n"
  [ "$fail_n" -eq 0 ] || fail "the classifier is wrong; no cluster result would be readable"
  ok "classifier distinguishes recreated / observed / silent / other"
}

PROJ_X=armx
PROJ_Y=army

# ---------------------------------------------------------------------------
# THE MOCK CONFIGS, AS FUNCTIONS, so the validator below and the cluster deploy
# use the SAME BYTES. A validator that checks a retyped copy of a config is a
# validator of the copy.
#
# log_format SITS OUTSIDE `server {}`, AND THAT IS NOT A STYLE CHOICE. Files in
# conf.d are included inside nginx's `http` context, where log_format is legal;
# inside `server` it is not, and nginx refuses to start:
#
#     [emerg] "log_format" directive is not allowed here in
#             /etc/nginx/conf.d/default.conf:3
#
# This probe's first cluster run died exactly there -- CrashLoopBackOff, a
# readiness probe that could never pass, and a VOID forty seconds in. The config
# had rendered perfectly: every nginx $variable survived the heredoc. It was
# simply not a legal config, and nothing in this repo could say so without a
# cluster. --validate-mock is that something.
# ---------------------------------------------------------------------------
mock_conf() {
  printf '%s\n' \
    "log_format probe '\$msec \$remote_addr \"\$request\" \$status \"\$http_user_agent\"';" \
    'server {' \
    '  listen 8080;' \
    '  access_log /dev/stdout probe;' \
    '  default_type application/json;'
  case "$1" in
    noid)
      # A create that SUCCEEDS and returns no id. populateStatusFields continues
      # silently on an absent declared field, so status.flavor.id stays empty and
      # the get verb has no path parameter to substitute.
      printf '%s\n' \
        "  location = /v2.1/${PROJ_X}/flavors {" \
        "    return 200 '{\"flavor\":{\"name\":\"probe-x\",\"ram\":512,\"vcpus\":1,\"disk\":1}}';" \
        '  }' ;;
    ok)
      printf '%s\n' \
        "  location = /v2.1/${PROJ_Y}/flavors {" \
        "    return 200 '{\"flavor\":{\"id\":\"probe-flavor-1\",\"name\":\"probe-y\",\"ram\":512,\"vcpus\":1,\"disk\":1}}';" \
        '  }' \
        "  location = /v2.1/${PROJ_Y}/flavors/probe-flavor-1 {" \
        "    return 200 '{\"flavor\":{\"id\":\"probe-flavor-1\",\"name\":\"probe-y\",\"ram\":512,\"vcpus\":1,\"disk\":1}}';" \
        '  }' ;;
    reject)
      printf '%s\n' \
        '  location / {' \
        "    return 401 '{\"error\":{\"code\":401,\"title\":\"Unauthorized\",\"message\":\"The request you have made requires authentication.\"}}';" \
        '  }' ;;
    *) fail "mock_conf: unknown mock '$1'" ;;
  esac
  [ "$1" = reject ] || printf '%s\n' \
    "  location / { return 404 '{\"itemNotFound\":{\"code\":404,\"message\":\"nope\"}}'; }"
  printf '%s\n' '}'
}

# ---------------------------------------------------------------------------
# --validate-mock: do these three configs actually LOAD, and does each answer
# the way its arm needs? Runs real nginx in docker. No cluster.
#
# WHY IT EXISTS: see mock_conf. The first cluster run VOIDed on
# CrashLoopBackOff because log_format sat inside `server {}` -- a config that
# rendered perfectly and was illegal. Ten minutes of cluster build to learn
# something nginx says in one line.
# ---------------------------------------------------------------------------
validate_mock() {
  command -v docker > /dev/null 2>&1 || void "docker is not on PATH, so no config was validated."
  docker info > /dev/null 2>&1 || void "docker is installed but not running, so no config was validated."
  command -v curl > /dev/null 2>&1 || void "curl is not on PATH, so no behaviour could be checked."
  local bad_n=0
  # mock : path to probe : expected code on that path : expected code on /
  for spec in "noid:/v2.1/${PROJ_X}/flavors:200:404" \
              "ok:/v2.1/${PROJ_Y}/flavors/probe-flavor-1:200:404" \
              "reject:/anything:401:401"; do
    local m p want_p want_root d cid port code_p code_root n
    m=${spec%%:*}; spec=${spec#*:}
    p=${spec%%:*}; spec=${spec#*:}
    want_p=${spec%%:*}; want_root=${spec#*:}

    d=$(mktemp -d)
    mock_conf "$m" > "$d/default.conf"

    # THE CONTROL THAT THIS VALIDATOR'S FIRST VERSION DID NOT HAVE, and the
    # reason it is first. `mock_conf` was defined BELOW this function, so the
    # generated file was EMPTY, nginx started happily with no server block, and
    # all three mocks reported "config loads, nginx stays up". A deliberately
    # broken variant passed too -- which is how it was caught. An empty subject
    # agreeing with a correct one is not agreement.
    n=$(wc -c < "$d/default.conf" | tr -d ' ')
    if [ "${n:-0}" -lt 40 ] || ! grep -q 'listen 8080' "$d/default.conf"; then
      bad "mock-$m: the generated config is $n bytes and has no 'listen 8080'.
        Nothing was validated -- nginx would start with no server block and stay
        up, which reads exactly like a pass."
      bad_n=$((bad_n+1)); rm -rf "$d"; continue
    fi

    # NO --rm, AND THE CONNECTION IS WAITED FOR RATHER THAN ASSUMED. The first
    # version slept 2 seconds and curled once; on a GitHub runner all three
    # mocks answered 000 -- no connection -- while the container was up and the
    # config was fine. hack/validate-401-mock.sh has reached a container in this
    # same CI for weeks, and the difference was its readiness loop, so this now
    # uses that proven shape rather than a different guess. `--rm` also goes:
    # with it, a container that exits takes its logs with it, and the logs are
    # how an illegal config explains itself.
    # A SINGLE FILE, NOT THE DIRECTORY, and the argument order and attempt
    # count copied from hack/validate-401-mock.sh rather than re-invented.
    # That script reaches its container in this same CI job -- it SUCCEEDED in
    # the very run where all three of these answered 000, with their ports
    # published and nginx logging "start worker processes" 16 seconds before
    # the window even opened. So the fault was not startup timing, and the only
    # remaining differences between the two invocations were the
    # directory-vs-file mount and 15 attempts against 30. Both are now the
    # proven shape, and everything knowable is dumped on failure so a third
    # occurrence explains itself instead of being guessed at again.
    #
    # THE LIMIT THIS CREATES, stated rather than discovered: the cluster mounts
    # these bytes as a ConfigMap over the conf.d DIRECTORY, while this mounts
    # the single file. The CONFIG is identical and that is what is under test;
    # the mount mechanism is not. A defect that only appears under a directory
    # mount -- the entrypoint failing to rewrite a read-only default.conf, say --
    # would pass here and fail on the cluster, which is how the log_format bug
    # reached a cluster in the first place.
    cid=$(docker run -d -p 0:8080 -v "$d/default.conf:/etc/nginx/conf.d/default.conf:ro" \
            nginxinc/nginx-unprivileged:1.27-alpine 2>/dev/null)
    if [ -z "$cid" ]; then
      bad "mock-$m: the container would not start at all."; bad_n=$((bad_n+1)); rm -rf "$d"; continue
    fi
    sleep 1
    # nginx EXITS on an illegal config, so "still running" is the load test.
    # Captured, not piped: a race would print "nginx REFUSED this config" about
    # a container that is running perfectly well.
    running=$(docker ps -q --no-trunc)
    if ! grep -q "$cid" <<< "$running" ; then
      bad "mock-$m: nginx REFUSED this config --"
      docker logs "$cid" 2>&1 | grep -E 'emerg|error' | sed 's/^/        /'
      docker rm -f "$cid" > /dev/null 2>&1
      bad_n=$((bad_n+1)); rm -rf "$d"; continue
    fi
    port=$(docker port "$cid" 8080/tcp 2>/dev/null | head -1 | sed 's/.*://')
    if [ -z "$port" ]; then
      bad "mock-$m: docker published no host port, so nothing could be reached.
        Every status below would be 000 for a reason that is not the config."
      docker rm -f "$cid" > /dev/null 2>&1
      bad_n=$((bad_n+1)); rm -rf "$d"; continue
    fi
    # Wait for the socket, bounded. A 000 after this is the mock, not the clock.
    ready=no
    for _try in $(seq 1 30); do
      if curl -s -o /dev/null -m 2 "http://127.0.0.1:${port}/" 2>/dev/null; then ready=yes; break; fi
      sleep 1
    done
    if [ "$ready" != yes ]; then
      bad "mock-$m: nothing answered on 127.0.0.1:${port} within 30s, although
        the container is running. That is a connectivity failure, not a wrong
        status. Everything known about it follows."
      echo "        docker port:"
      docker port "$cid" 2>&1 | sed "s/^/          /"
      echo "        curl:"
      curl -sv -m 3 "http://127.0.0.1:${port}/" 2>&1 \
        | grep -iE "connect|refused|trying|timed" | head -4 | sed "s/^/          /"
      echo "        container log:"
      docker logs "$cid" 2>&1 | tail -5 | sed "s/^/          /"
      docker rm -f "$cid" > /dev/null 2>&1
      bad_n=$((bad_n+1)); rm -rf "$d"; continue
    fi
    code_p=$(curl -s -o /dev/null -m 5 -w '%{http_code}' "http://127.0.0.1:${port}${p}" 2>/dev/null)
    code_root=$(curl -s -o /dev/null -m 5 -w '%{http_code}' "http://127.0.0.1:${port}/" 2>/dev/null)

    # THE TIMESTAMP IS VALIDATED AGAINST BYTES A REAL NGINX WROTE, not against a
    # retyped sample. `$msec` was added to log_format for oasgen-provider#648,
    # and a log_format mistake is precisely what CrashLoopBackOff'd this probe's
    # first cluster run -- nginx refuses an illegal directive and exits, so the
    # arm VOIDs forty seconds in with no idea why. nginx accepting the config is
    # already checked above; what is checked here is that the field is actually
    # EMITTED and parses, which a loadable-but-wrong format would not give.
    local logs tn an now skew
    logs=$(docker logs "$cid" 2>&1)
    tn=$(printf '%s\n' "$logs" | timed_log | grep -c . || true)
    an=$(printf '%s\n' "$logs" | attribute_log | grep -c . || true)
    now=$(date -u +%s)
    # The first timestamp's whole-second part, compared against this machine's
    # clock. A format that emitted the wrong variable would still parse as ten
    # digits and a dot -- $request_time, say, is "0.001" and fails the shape,
    # but a wrong EPOCH-shaped field would not. So the value is range-checked.
    skew=$(printf '%s\n' "$logs" | timed_log | awk -v now="$now" 'NR==1 { d = now - $1; if (d < 0) d = -d; printf "%.0f", d }')
    if [ "${tn:-0}" -lt 1 ]; then
      bad "mock-$m: nginx loaded the config and answered, but NOT ONE access-log
        line carried a parseable \$msec. The timestamped format is not reaching
        the log, so #648's three timestamps would all be empty -- and an empty
        timestamp reads as 'no request' rather than as a broken format."
      printf '%s\n' "$logs" | tail -3 | sed 's/^/          /'
      bad_n=$((bad_n+1))
    elif [ "${tn:-0}" != "${an:-0}" ]; then
      bad "mock-$m: the two parsers disagree on this real log -- timed=$tn,
        attributed=$an. A timestamp would then be attached to a different
        request than the one counted."
      bad_n=$((bad_n+1))
    elif [ -z "${skew:-}" ] || [ "${skew:-99999}" -gt 600 ]; then
      bad "mock-$m: the first \$msec parses but is ${skew:-<unreadable>}s from
        this machine's clock. The field has the right SHAPE and the wrong value,
        which is the one way a loadable format still ruins a timing."
      bad_n=$((bad_n+1))
    else
      ok "mock-$m: \$msec reaches the log ($tn records, both parsers agree, ${skew}s from this clock)"
    fi
    docker rm -f "$cid" > /dev/null 2>&1; rm -rf "$d"

    # BEHAVIOUR, not just loadability. A config that loads and answers the wrong
    # status turns every arm into a measurement of the wrong thing, and 000
    # (nothing listening) must never read as a pass.
    if [ "$code_p" = "$want_p" ] && [ "$code_root" = "$want_root" ]; then
      ok "mock-$m: loads, and answers $code_p on $p and $code_root on /"
    else
      bad "mock-$m: answered $code_p on $p (want $want_p) and $code_root on / (want $want_root)."
      bad_n=$((bad_n+1))
    fi
  done
  if [ "$bad_n" -gt 0 ]; then
    fail "$bad_n of 3 mock configs are wrong. The cluster arms would VOID on
    CrashLoopBackOff ten minutes in, or worse, measure the wrong behaviour."
  fi
  ok "all three mock configs load in real nginx AND answer as their arm needs"
}

# SOURCED HERE, above the dispatch, and curl_through DEFINED here for the same
# reason: --self-test now asserts how the two behave together, and a predicate
# that only exists on the cluster path cannot be tested off-cluster. Three
# instances of the single-shot bug went unnoticed because the code that had it
# was unreachable from any check that did not need a kind cluster.
# curl_through <mock> <path> -> sets CODE, returns 0 only once a REAL status
# arrived. That last line is the whole contract: it is what lets wait_for retry
# a 000 (no HTTP response at all) while judging any real status immediately.
# Delete it and wait_for would accept 000 as an answer, which is instance #1
# of this probe's recurring defect. self_test asserts the line is still there.
# shellcheck disable=SC2329  # invoked indirectly, as wait_for's "$@"
#
# That directive is load-bearing, measured and not assumed: excised, lint.yaml's
# exact invocation (-x over scripts/*.sh hack/*.sh hack/lib/*.sh) exits 1 at
# linter version 0.11.0. SC2329's own text licenses it -- "or ignored if invoked
# indirectly" -- and that is the literal situation.
#
# Honest limit on the above: probe-401.sh hands _mock_answers to wait_for the
# same way and is NOT flagged, and I could not reduce the difference to a
# minimal repro. It is not the nesting, the extra arguments, the redirect, or
# the order of the source -- each tested separately and each ruled out. So the
# directive states the true thing the check asks for and claims nothing about
# why its sibling escapes.
# KUBECTL'S STDERR IS KEPT, and run 44347239443 is why. That run timed out
# after 90s with "mock-ok never returned an HTTP status at all (curl 000
# throughout)" and `wait_for` printed "Predicate's last stderr (empty means it
# failed silently, not that it ran clean)" -- empty, because this function sent
# kubectl's stderr to /dev/null. So there was no way to tell apart:
#
#   * curl ran in the pod and got no HTTP response      (a real finding)
#   * kubectl could not create, attach to or collect    (my instrument failed)
#     the pod at all, so no output ever existed
#
# Both leave CODE empty, and the VOID message asserted the first. That is the
# same defect this probe exists to avoid, in the probe's own plumbing: a
# suppressed error becoming a claim about the world. Stderr now reaches
# wait_for, which already keeps the predicate's last stderr and prints it on
# timeout.
#
# HOW TO READ A TIMEOUT FROM THIS PREDICATE, since the two cases are told apart
# by whether wait_for's "last stderr" is empty:
#
#   stderr EMPTY      curl ran and reported a literal 000 -- it reached nothing.
#                     Look at the Service's endpoints (endpoints_of, below).
#   stderr NON-EMPTY  kubectl could not run the pod or collect its output, and
#                     the lines name the exit status and what it said.
#
# No flag variable carries this: the presence of the stderr IS the signal, so
# there is nothing to keep in sync. An earlier draft set a CODE_SRC that no
# caller read, which shellcheck flagged (SC2034) and which was the right call --
# a variable declared to document an intent is one more thing that can disagree
# with the code.
# THE OUTPUT IS READ FROM THE POD'S LOG, NOT FROM AN ATTACH, and run
# 44361232022 is why. It VOIDed at the FIRST behaviour control with endpoints
# confirmed present (noid=1 ok=1 reject=1), and the stderr capture added in
# #200 named a case neither branch had anticipated:
#
#   kubectl run exited 0 and produced no 3-digit status.
#   stdout was:
#   kubectl stderr:
#
# kubectl SUCCEEDED and collected nothing. That is the `--rm -i` attach race:
# `kubectl run -i` streams the container's stdout over an attach, and a curl
# that finishes before the attach is established writes into a stream nobody
# is reading. Exit 0, no output, no error -- and fifteen retries in a row lost
# the same race, because retrying a race does not change the odds.
#
# So the pod is created WITHOUT --rm and WITHOUT -i, waited for, and its output
# read from the log, which survives the container's exit. That removes the race
# rather than retrying it. The pod is deleted on the way out; `--wait=false`
# because its log has already been read and nothing downstream needs it gone.
curl_through() {
  local err out rc pod phase i
  err=$(mktemp)
  pod="probe-curl-$RANDOM"
  K -n "$NS" run "$pod" --restart=Never --attach=false --quiet \
      --image=curlimages/curl:8.11.0 -- \
      -s -o /dev/null -w '%{http_code}' -A "$PROBE_UA" \
      "http://mock-$1.${NS}.svc.cluster.local:8080$2" > /dev/null 2> "$err"
  rc=$?
  if [ "$rc" -eq 0 ]; then
    # Bounded, and short: this is already inside a wait_for, so a slow pull is
    # retried at the outer level rather than waited out twice.
    for i in $(seq 1 20); do
      phase=$(K -n "$NS" get pod "$pod" -o jsonpath='{.status.phase}' 2>/dev/null)
      case "$phase" in Succeeded|Failed) break ;; esac
      sleep 1
    done
    out=$(K -n "$NS" logs "$pod" 2>> "$err")
    K -n "$NS" delete pod "$pod" --wait=false > /dev/null 2>&1
  else
    out=""
  fi
  # NO PIPE. The obvious `printf '%s' "$out" | grep -oE ... | head -1` is an
  # early-exit consumer fed by an unbounded producer, which is the pipefail race
  # this repo removed from 59 sites today -- and scripts/validate.py's own screen
  # flagged it here, which is the screen earning its place on code written after
  # it. $out is three digits in practice, but "safe by size" is a threshold and
  # not a property, so the pipe goes instead of being justified.
  CODE=""
  [[ $out =~ ([0-9][0-9][0-9]) ]] && CODE="${BASH_REMATCH[1]}"
  if [ -z "${CODE:-}" ]; then
    # To stderr, so wait_for keeps it and a timeout can say WHICH failure it was.
    # Emitted ONLY here, which is what makes "stderr empty" mean "curl said 000".
    { printf 'kubectl run exited %s and the pod log held no 3-digit status.\n' "$rc"
      printf 'pod phase reached: %s\n' "${phase:-<never read>}"
      printf 'pod log was: %s\n' "$(printf '%s' "$out" | tr -d '\n' | cut -c1-200)"
      printf 'kubectl stderr: %s\n' "$(tr -d '\n' < "$err" | cut -c1-400)"
      printf 'rc=0 with an empty log and empty stderr used to mean the --rm -i\n'
      printf 'attach lost the race; the log is read directly now, so this\n'
      printf 'combination means the container produced nothing.\n'
    } >&2
  fi
  rm -f "$err"
  [ -n "${CODE:-}" ] && [ "$CODE" != "000" ]
}

# endpoints_of <mock> -> the Service's ready endpoint count.
#
# A SECOND INSTRUMENT THAT FAILS DIFFERENTLY from curl. "Service has no
# endpoints" and "nginx refused the connection" both show up as curl 000, and
# they call for opposite actions. `kubectl rollout status` returning does not
# imply the endpoints have propagated -- that is what made run 44334008550 VOID.
endpoints_of() {
  K -n "$NS" get endpoints "mock-$1" \
    -o jsonpath='{range .subsets[*]}{range .addresses[*]}{.ip}{"\n"}{end}{end}' 2>/dev/null \
    | grep -c . || true
}

if [ "${1:-}" = "--validate-mock" ]; then
  say "mock config validation against real nginx (no cluster)"
  validate_mock
  exit 0
fi

[ "${1:-}" = "--self-test" ] && { say "classifier self-test (no cluster)"; self_test; exit 0; }

KUBECONFIG_PATH="${1:?usage: probe-observe-rejection.sh <kubeconfig> <context> [namespace] | --self-test}"
CONTEXT="${2:?usage: probe-observe-rejection.sh <kubeconfig> <context> [namespace] | --self-test}"
NS="${3:-krateo-system}"
K() { kubectl --kubeconfig "$KUBECONFIG_PATH" --context "$CONTEXT" "$@"; }

[ -f chart/Chart.yaml ] || fail "run me from the repo root"
# shellcheck source=hack/lib/attribute.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/attribute.sh"

GROUP=$(awk '/resourceGroup:/ {print $2; exit}' chart/values.yaml 2>/dev/null)
case "${GROUP:-}" in
  *.*) ;;
  *) fail "could not read resourceGroup from chart/values.yaml (got '${GROUP:-<empty>}')." ;;
esac

# The classifier is self-tested BEFORE any cluster work: a cluster result read
# through a broken classifier is worse than no result.
say "classifier self-test, before touching the cluster"
self_test

KIND_UNDER_TEST=ComputeFlavor

# EXACTLY ONE RestDefinition MAY SERVE THE KIND UNDER TEST, asserted before any
# arm and again after every install.
#
# Run 44342938736 is why this exists. The workflow installed the chart as
# release `nova` to create the CRDs; the probe installs it as `nova-probe`. Two
# releases of one chart means two RestDefinitions per Kind, two controllers, and
# the CR is reconciled by whichever one claims it. It claimed the wrong one, so
# the POST went to https://nova.invalid/v2.1/p/flavors -- the OTHER release's
# URL -- and the observe died on an RBAC denial for a Secret the sibling's
# ServiceAccount held no grant for. The probe DID refuse to report a result, but
# it VOIDed saying "the controller never created anything", which is a true
# statement about the wrong subject. This check names the cause in one line.
#
# TWO INDEPENDENT READINGS, because they fail differently: the helm release
# count is the CAUSE, the per-Kind RestDefinition count is the EFFECT. Someone
# installing this chart twice under two names trips the first; some other chart
# serving the same Kind in this group trips only the second.
assert_single_owner() {
  local when="$1" rds total n releases
  rds=$(K get restdefinitions.ogen.krateo.io -n "$NS" \
          -o jsonpath='{range .items[*]}{.metadata.name} {.spec.resourceGroup} {.spec.resource.kind}{"\n"}{end}' 2>/dev/null)
  total=$(printf '%s\n' "$rds" | grep -c . || true)
  # CONTROL: zero RestDefinitions means the read failed or the chart is absent.
  # With nothing to count, "exactly one owner" cannot be concluded -- that is a
  # missing instrument, not a clean cluster.
  if [ "${total:-0}" -eq 0 ]; then
    void "$when: no RestDefinition at all is visible in $NS. Either the read
    failed or the chart is not installed, and with nothing to count 'one owner'
    is not a conclusion. Missing instrument, not a clean cluster."
  fi
  n=$(printf '%s\n' "$rds" \
      | awk -v g="$GROUP" -v k="$KIND_UNDER_TEST" '$2 == g && $3 == k { c++ } END { print c+0 }')
  if [ "${n:-0}" -ne 1 ]; then
    printf '    RestDefinitions visible (%s total):\n' "$total" >&2
    printf '%s\n' "$rds" | sed 's/^/      /' >&2
    void "$when: $n RestDefinition(s) serve $KIND_UNDER_TEST in $GROUP, and
    exactly one may. With two, the CR is reconciled by whichever controller
    claims it -- possibly one pointed at a different vendor URL, whose
    ServiceAccount holds no grant for this probe's auth Secret. Every arm below
    would measure the wrong installation. Install the chart ONCE, under the
    release name this probe upgrades (nova-probe)."
  fi
  releases=$(helm --kubeconfig "$KUBECONFIG_PATH" --kube-context "$CONTEXT" \
               list -n "$NS" -o json 2>/dev/null \
             | grep -o '"chart":"openstack-nova-operator-kog[^"]*"' | grep -c . || true)
  if [ "${releases:-0}" -gt 1 ]; then
    void "$when: $releases helm releases of this chart are installed in $NS.
    Each generates its own RestDefinition per Kind, so the arms would measure
    whichever controller happened to claim the CR. Install it once."
  fi
  ok "$when: exactly one RestDefinition serves $KIND_UNDER_TEST, from ${releases:-?} release of this chart"
}

say "preconditions -- each failure says what it means"
K get --raw /readyz > /dev/null 2>&1 \
  || void "cannot reach the API server (kubeconfig=$KUBECONFIG_PATH context=$CONTEXT). Nothing measured."
for crd in "computeflavors.$GROUP" "computeflavorconfigurations.$GROUP"; do
  K get crd "$crd" > /dev/null 2>&1 \
    || void "CRD $crd is absent, so the chart is not installed in this cluster. Install it first; this is NOT evidence about observe."
done
command -v helm > /dev/null 2>&1 || void "helm is not on PATH, so the arms cannot be installed."
K get ns "$NS" > /dev/null 2>&1 || K create ns "$NS" > /dev/null 2>&1 \
  || void "namespace $NS does not exist and could not be created."
ok "API reachable, both CRDs present, helm available"
assert_single_owner "before any arm"

# ---------------------------------------------------------------------------
# THREE MOCKS, THREE SERVICES, AND THE REASON IT IS NOT ONE MOCK WITH A SWITCH.
#
# Arm Y needs the vendor to answer a GET successfully and then stop. The obvious
# implementation -- patch one mock's config and restart it -- DESTROYS THE
# ACCESS LOG, which is the only witness this probe has. Patching a Service
# selector keeps the log but puts both arms' traffic in one pod's log, where a
# POST cannot be attributed to a window.
#
# So each behaviour gets its own pod, its own Service and its own log, and the
# arm is selected by which URL the chart is installed with. Separation is then a
# property of the setup rather than of a timestamp comparison.
# ---------------------------------------------------------------------------
say "deploying the three mocks"
for m in noid ok reject; do
  d=$(mktemp -d)
  mock_conf "$m" > "$d/default.conf"
  K -n "$NS" create configmap "mock-${m}-conf" --from-file="$d/default.conf" \
    --dry-run=client -o yaml | K -n "$NS" apply -f - >/dev/null \
    || void "could not create mock-${m}-conf"
  rm -rf "$d"
done

for m in noid ok reject; do
  K -n "$NS" apply -f - >/dev/null <<EOF
apiVersion: apps/v1
kind: Deployment
metadata: { name: mock-${m}, namespace: ${NS} }
spec:
  replicas: 1
  selector: { matchLabels: { app: mock-${m} } }
  template:
    metadata: { labels: { app: mock-${m} } }
    spec:
      containers:
        - name: nginx
          image: nginxinc/nginx-unprivileged:1.27-alpine
          ports: [ { containerPort: 8080 } ]
          volumeMounts: [ { name: conf, mountPath: /etc/nginx/conf.d } ]
          # tcpSocket, not httpGet: a 401 and a 404 are SUCCESSFUL responses for
          # these mocks, so an httpGet probe would have to be told not to treat
          # them as unready. No backticks in this comment -- it sits inside an
          # unquoted heredoc, where they would be command substitution.
          readinessProbe:
            tcpSocket: { port: 8080 }
            initialDelaySeconds: 1
            periodSeconds: 2
            failureThreshold: 30
      volumes: [ { name: conf, configMap: { name: mock-${m}-conf } } ]
---
apiVersion: v1
kind: Service
metadata: { name: mock-${m}, namespace: ${NS} }
spec:
  selector: { app: mock-${m} }
  ports: [ { port: 8080, targetPort: 8080 } ]
EOF
  K -n "$NS" rollout status "deploy/mock-${m}" --timeout=180s >/dev/null \
    || void "mock-${m} never became ready, so its arm was never offered anything."
done
ok "mock-noid, mock-ok and mock-reject are ready, each with its own Service and log"


url_of() { printf 'http://mock-%s.%s.svc.cluster.local:8080/v2.1/%s' "$1" "$NS" "$2"; }

# EVERY MOCK IS CONFIRMED TO BEHAVE AS DESIGNED BEFORE ANY ARM RUNS. A mock
# that answers differently from its config turns every verdict below into a
# measurement of the wrong thing, and the access-log table would look just as
# authoritative either way.
# ENDPOINTS BEFORE CURL, because every control below costs up to 90s and they
# share one most-likely cause. `kubectl rollout status` returning means the pod
# passed its readiness probe; it does NOT mean the Service's endpoints have
# propagated. Checking once here turns a 4.5-minute three-way timeout into one
# line that names the cause.
# shellcheck disable=SC2329  # invoked indirectly, as wait_for's "$@"
_all_endpoints_ready() {
  [ "$(endpoints_of noid)" -ge 1 ] && [ "$(endpoints_of ok)" -ge 1 ] \
    && [ "$(endpoints_of reject)" -ge 1 ]
}
wait_for "all three mock Services to have at least one ready endpoint" 20 3 _all_endpoints_ready \
  || void "after 60s the mock Services still do not all have an endpoint:
    mock-noid=$(endpoints_of noid) mock-ok=$(endpoints_of ok) mock-reject=$(endpoints_of reject).
    The pods passed their readiness probes (rollout status returned above), so
    this is endpoint propagation, not nginx. Nothing was measured."
ok "all three mock Services have endpoints: noid=$(endpoints_of noid) ok=$(endpoints_of ok) reject=$(endpoints_of reject)"

say "mock behaviour controls -- asserted, not assumed"
# RETRIED, not single-shot, and that is the whole point of this block's shape.
#
# `kubectl rollout status` returning means each pod passed its readiness probe.
# It does NOT mean the Service's endpoints have reached kube-proxy on the node
# this curl pod lands on, and it says nothing about whether THIS pod's own image
# is pulled yet. Run 44334008550 VOIDed in exactly that gap: "mock-noid, mock-ok
# and mock-reject are ready" at 15:21:19.72, curl 000 at 15:21:23.08 -- 3.4
# seconds later -- with `probe-curl-19314 Pulling` in the event stream 3 seconds
# before the verdict. Nothing was wrong with the mock.
#
# The distinction the retry encodes is the one probe-401.sh:347-352 already
# makes: curl reports 000 when it got NO HTTP RESPONSE AT ALL (DNS, no endpoint,
# nginx not yet bound), which is transient and worth waiting out; any real
# 3-digit status is an answer and is judged IMMEDIATELY, on the first attempt,
# because a wrong status is not a slow one. Collapsing those two failures makes
# a cold cluster read as "the mock answered wrongly" -- or, worse, makes a real
# misconfiguration read as a timeout.
#
# `curl_through` is itself the predicate: it returns 0 only once a real status
# arrived, and leaves that status in $CODE for the assertion on the next line.
#
# THIRD INSTANCE of the same mistake in this probe -- a single-shot check
# against a service that had just started. The first was the docker mock
# validator, the second the in-cluster mock rollout, this is the third. All
# three times probe-401.sh already contained the readiness loop; all three times
# this file copied its structure and left the retry behind. The pattern is not
# "clusters are slow", it is that a check written against an already-warm
# service cannot tell "not yet" from "not ever".
wait_for "mock-noid to return any HTTP status to an in-cluster request" 15 6 \
  curl_through noid "/v2.1/${PROJ_X}/flavors" \
  || { ep=$(endpoints_of noid)
       void "mock-noid never returned an HTTP status at all within 90s. That is a
    connectivity failure, NOT the mock answering wrongly, and nothing downstream
    was measured. Service mock-noid has $ep ready endpoint(s) -- 0 means the pod
    is not behind the Service (so curl had nothing to reach), non-zero means it
    is and nginx did not answer. The predicate's last stderr is above: it names
    whether kubectl could run the pod at all."; }
[ "$CODE" = "200" ] || void "mock-noid answered $CODE on the create path, expected 200."
wait_for "mock-ok to return any HTTP status to an in-cluster request" 15 6 \
  curl_through ok "/v2.1/${PROJ_Y}/flavors/probe-flavor-1" \
  || { ep=$(endpoints_of ok)
       void "mock-ok never returned an HTTP status at all within 90s. Connectivity
    failure, not a measurement. Service mock-ok has $ep ready endpoint(s); the
    predicate's last stderr above says whether kubectl could run the pod."; }
[ "$CODE" = "200" ] || void "mock-ok answered $CODE on the get path, expected 200."
wait_for "mock-reject to return any HTTP status to an in-cluster request" 15 6 \
  curl_through reject "/v2.1/${PROJ_Y}/flavors/probe-flavor-1" \
  || { ep=$(endpoints_of reject)
       void "mock-reject never returned an HTTP status at all within 90s.
    Connectivity failure, not a measurement. Service mock-reject has $ep ready
    endpoint(s); the predicate's last stderr above says whether kubectl could run
    the pod."; }
[ "$CODE" = "401" ] || void "mock-reject answered $CODE, expected 401."
ok "noid=200-without-id, ok=200-with-id, reject=401, each confirmed through its Service"

# records_from <mock> -> attribution records for that pod's whole log
# EVERY LOG READ IS PINNED TO ONE NAMED POD, not to `deploy/mock-X`.
#
# `kubectl logs deploy/...` resolves the deployment to SOME pod, and immediately
# after a `rollout restart` completes the terminating old pod can still be the
# one it picks. That is what happened on the second run: the empty-log control
# fired with "4 request record(s) after a restart" while the NEW pod's log was
# in fact empty. The control was right to refuse -- an unexplained instrument is
# a reason to stop -- and the cause was this line, not stale traffic.
#
# So the pod names are captured once, after the restart settles, and every
# subsequent read names a pod. A log attributed to "whichever pod kubectl felt
# like" is not an attribution.
POD_NOID=""; POD_OK=""; POD_REJECT=""

mock_pod_var() {
  case "$1" in
    noid)   printf '%s' "$POD_NOID" ;;
    ok)     printf '%s' "$POD_OK" ;;
    reject) printf '%s' "$POD_REJECT" ;;
    *) fail "mock_pod_var: unknown mock '$1'" ;;
  esac
}

# Exactly ONE pod, or the caller cannot know which log it read.
single_pod() {
  local pods n
  pods=$(K -n "$NS" get pods -l "app=mock-$1" -o name 2>/dev/null)
  n=$(printf '%s\n' "$pods" | grep -c . || true)
  [ "${n:-0}" -eq 1 ] || return 1
  printf '%s' "$pods"
}

records_from() {
  local pod; pod=$(mock_pod_var "$1")
  [ -n "$pod" ] || void "records_from $1 was called before the mock pods were
    pinned. Every count below it would be a count of nothing."
  K -n "$NS" logs "$pod" --tail=2000 2>/dev/null | attribute_log
}

# The same log, carrying nginx's $msec: MSEC METHOD PATH STATUS UA.
timed_from() {
  local pod; pod=$(mock_pod_var "$1")
  [ -n "$pod" ] || void "timed_from $1 was called before the mock pods were
    pinned. Every timestamp below it would be a timestamp of nothing."
  K -n "$NS" logs "$pod" --tail=2000 2>/dev/null | timed_log
}

# first_msec <mock> <METHOD> -> the earliest CONTROLLER request of that method.
#
# NO EARLY-EXIT CONSUMER, and that is deliberate rather than stylistic. The
# obvious spelling is `timed_from ok | awk '... {print $1; exit}'`, and under
# this script's `set -o pipefail` it is a race: `kubectl logs --tail=2000` of an
# access log is ~240KB, well past the 64KB pipe buffer, so the producer blocks,
# takes EPIPE when awk exits early, and pipefail promotes it. The direction
# would be safe here -- a truncated read yields an empty timestamp, which this
# arm reports as UNDETERMINED rather than as "fast" -- but safe-by-direction is
# not a reason to write a race. So awk reads to EOF and keeps the minimum.
#
# The comparison is forced numeric. Both operands are ten digits, a dot and
# three digits, so a string compare would agree today and stop agreeing in 2286.
first_msec() {
  timed_from "$1" | awk -v ua="$PROBE_UA" -v want="$2" '
    $5 != ua && $2 == want { if (m == "" || $1+0 < m+0) m = $1 }
    END { print m }'
}

# How many requests of that method the PROBE ITSELF made, which is the control
# that proves the attribution above is doing work rather than being decorative.
probe_msec_count() {
  timed_from "$1" | awk -v ua="$PROBE_UA" -v want="$2" '$5 == ua && $2 == want { n++ } END { print n+0 }'
}

# The Ready condition's own transition time, recorded by the controller rather
# than by this script's polling cadence.
# THE REASON COMES WITH THE TIME, because Ready=True alone is not the
# discriminator. #648 is about the gap between a successful create and the
# observe that reports it, so the transition of interest is
# `Creating` -> `Available`; a resource that was never in Creating reaching
# Ready=True would look identical and mean something else.
ready_at() {
  K -n "$NS" get "$RES" "$1" -o \
    jsonpath='{range .status.conditions[?(@.type=="Ready")]}{.status} {.reason} {.lastTransitionTime}{end}' 2>/dev/null
}

# The create's own success timestamp, written on the create path. t1 minus this
# isolates the OBSERVE SCHEDULING from anything inside the create itself --
# second-granular, which is ample for a 180s question.
# THE WHOLE ANNOTATION MAP IS READ, NOT THE ONE DOTTED KEY, and that is about
# telling two failures apart. A jsonpath of
# `{.metadata.annotations.krateo\.io/external-create-succeeded}` needs the dot
# in the domain escaped; get that escaping wrong and kubectl writes to stderr
# and prints nothing, so `2>/dev/null` would turn a malformed query into "the
# annotation is absent" -- an instrument failure reported as a fact about the
# cluster. Reading the map has no escaping to get wrong, and a non-zero kubectl
# exit is kept and surfaced separately from an empty result.
#
# Prints the timestamp, or "ERR:<kubectl stderr>" if the read itself failed.
create_succeeded_at() {
  local out err rc
  err=$(mktemp)
  out=$(K -n "$NS" get "$RES" "$1" -o jsonpath='{.metadata.annotations}' 2> "$err")
  rc=$?
  if [ "$rc" -ne 0 ]; then
    printf 'ERR:%s' "$(tr -d '\n' < "$err" | cut -c1-200)"
    rm -f "$err"; return 0
  fi
  rm -f "$err"
  printf '%s' "$out" \
    | sed -n 's/.*"krateo\.io\/external-create-succeeded":"\([^"]*\)".*/\1/p'
}

# EVERY ARM'S VERDICT IS A DELTA OVER A LOG
#
# PLACED HERE, BELOW records_from, DELIBERATELY. Written twenty lines higher it
# called records_from before that function existed: `command not found`, an
# empty count, and `[ 0 -eq 0 ]` passing -- a control that cannot fail, which is
# the same mistake that made this file's first mock validator validate an empty
# config. Third instance in one session; the shape is 'a check placed above the
# thing it checks'., so the log's starting state is part
# of the instrument. A second run against pods left over from a first one starts
# with POSTs already in the log: the "wait for the controller to POST at least
# once" gate is satisfied by a STALE record before the new CR is even created,
# the window then opens before the real create, and that create lands inside it
# as a "new" POST -- a false `recreated`. Restarting the mocks is cheaper than
# reasoning about timestamps, and the emptiness is asserted rather than assumed.
say "restarting the mocks so every arm starts from an empty log"
for m in noid ok reject; do
  K -n "$NS" rollout restart "deploy/mock-${m}" >/dev/null 2>&1
done
for m in noid ok reject; do
  K -n "$NS" rollout status "deploy/mock-${m}" --timeout=180s >/dev/null \
    || void "mock-${m} did not come back after a restart."
  # Wait for the OLD pod to be gone, not merely for the new one to be ready --
  # that gap is what made the previous run read a terminating pod's log.
  # shellcheck disable=SC2329  # invoked indirectly, by wait_for
  _one_pod() { single_pod "$m" > /dev/null; }
  wait_for "exactly one mock-${m} pod to remain after the restart" 30 4 _one_pod \
    || void "mock-${m} still has more than one pod after 120s, so a log read
    could not be attributed to either."
  case "$m" in
    noid)   POD_NOID=$(single_pod noid) ;;
    ok)     POD_OK=$(single_pod ok) ;;
    reject) POD_REJECT=$(single_pod reject) ;;
  esac
  n=$(records_from "$m" | grep -c . || true)
  [ "${n:-0}" -eq 0 ] \
    || void "mock-${m}'s log already holds $n request record(s) after a restart,
    so no delta measured against it is attributable to this run."
done
printf '    pinned pods: %s %s %s\n' "$POD_NOID" "$POD_OK" "$POD_REJECT"
ok "all three mock logs are empty and pinned to one pod each"

# THE CHART CARRIES `version: CHART_VERSION` UNTIL RELEASE, so helm refuses it
# unstamped -- `chart.metadata.version "CHART_VERSION" is invalid`. Staged here
# rather than required of the caller: a probe that depends on an undocumented
# prior step reports "helm could not install" for a reason the reader cannot
# see, and every arm below would VOID with the cause one layer away. Caught
# before the first run by checking `helm template` against the working tree.
CHART_PATH=$(mktemp -d)/chart
mkdir -p "$CHART_PATH"
cp -R chart/. "$CHART_PATH"/
sed "s/CHART_VERSION/0.0.0-probeobserve/g" chart/Chart.yaml > "$CHART_PATH/Chart.yaml"
if grep -q CHART_VERSION "$CHART_PATH/Chart.yaml"; then
  fail "failed to stamp CHART_VERSION; helm would refuse the chart and the error
    would point at helm rather than at this line."
fi
ok "staged a stamped chart at $CHART_PATH"

install_at() {
  helm --kubeconfig "$KUBECONFIG_PATH" --kube-context "$CONTEXT" \
    upgrade --install nova-probe "$CHART_PATH" -n "$NS" \
    --set nova.url="$1" --wait --timeout 8m > /tmp/probe-observe-helm.log 2>&1 \
    || { sed 's/^/    /' /tmp/probe-observe-helm.log >&2
         void "helm could not install the chart pointing at $1. No arm ran."; }
  # Re-checked after EVERY install, not only once at the start: this call is the
  # one operation in the probe that can create a second owner, and a rival that
  # appeared here would otherwise surface only as a baffling arm result.
  assert_single_owner "after pointing the chart at $1"
}

WINDOW="${PROBE_WINDOW:-120}"
# Initialised so the verdict block cannot be the first place a missing value
# appears: under `set -u` that would abort the summary AFTER every arm had run.
ARM_648=unmeasured
CFG=probe-observe-cfg
K -n "$NS" create secret generic probe-observe-token \
  --from-literal=token=probe-token-not-a-credential >/dev/null 2>&1 || true

# apply_cr <name> -> applies the Configuration and one ComputeFlavor
apply_cr() {
  local man; man=$(mktemp)
  cat > "$man" <<EOF
apiVersion: ${GROUP}/v1alpha1
kind: ComputeFlavorConfiguration
metadata: { name: ${CFG}, namespace: ${NS} }
spec:
  authentication:
    apiKey:
      tokenRef: { name: probe-observe-token, namespace: ${NS}, key: token }
      header: X-Auth-Token
---
apiVersion: ${GROUP}/v2-1
kind: ComputeFlavor
metadata: { name: $1, namespace: ${NS} }
spec:
  configurationRef: { name: ${CFG}, namespace: ${NS} }
  flavor: { name: $1, ram: 512, vcpus: 1, disk: 1 }
EOF
  # The apply's exit status is read. An unchecked apply is how an earlier probe
  # in this repo reported success over two BadRequests.
  if ! K -n "$NS" apply -f "$man" > /tmp/probe-observe-apply.log 2>&1; then
    sed 's/^/    /' /tmp/probe-observe-apply.log >&2
    echo "    --- the manifest it refused ---" >&2
    sed 's/^/    /' "$man" >&2
    rm -f "$man"
    void "the probe's own CRs did not apply, so no arm ran."
  fi
  rm -f "$man"
}

# THE RESOURCE NAME IS FULLY QUALIFIED, AND THAT IS NOT PEDANTRY. `computeflavor`
# is AMBIGUOUS in this cluster -- it prefix-matches both `computeflavors` and
# `computeflavorconfigurations`, so kubectl answers
#
#     error: you must specify only one resource
#
# on stderr and nothing on stdout. The first version of this probe read
# status.flavor.id through that ambiguous name with 2>/dev/null, got an empty
# string, and announced "status.flavor.id is empty, which is the condition under
# test". The field WAS empty, so the arm reached the right answer for the wrong
# reason -- and would have said the same thing about a CR that did not exist at
# all. The broken-instrument answer and the reassuring answer were the same
# empty string, again.
RES="computeflavors.${GROUP}"

cr_exists() { K -n "$NS" get "$RES" "$1" -o name > /dev/null 2>&1; }
status_id()   { K -n "$NS" get "$RES" "$1" -o jsonpath='{.status.flavor.id}' 2>/dev/null; }
conditions()  { K -n "$NS" get "$RES" "$1" -o \
                  jsonpath='{range .status.conditions[*]}type={.type} status={.status} reason={.reason}{"\n"}{end}' 2>/dev/null; }
status_keys() { K -n "$NS" get "$RES" "$1" -o jsonpath='{.status.flavor}' 2>/dev/null; }

# THE WINDOW MUST OUTLAST THE CONTROLLER'S RESYNC, read from the controller's
# own startup log rather than guessed. rest-dynamic-controller 0.52.0 logs
# `"resyncInterval":180000000000` -- 180 SECONDS -- and a 120s window saw
# exactly one reconcile, so the first run VOIDed on its own liveness control.
# Confirmed twice: the log field, and two observe events 179.9s apart.
# THE LIVENESS WITNESS FOR ARM X IS THE CONTROLLER'S OWN LOG, NOT HTTP TRAFFIC.
#
# WHY, AND THIS WAS A STRUCTURAL ERROR IN THE FIRST THREE RUNS. Arm X's liveness
# control demanded at least one request at the mock during the window, on the
# reasoning that "0 new POSTs" and "nothing was reconciling" are the same
# integer. They are -- but the behaviour UNDER TEST is precisely that the
# controller reconciles and issues NO HTTP REQUEST AT ALL, because with
# status.flavor.id empty there is nothing to GET. So the control could never
# pass on the state the arm exists to characterise: it VOIDed three runs in a
# row while the controller was reconciling perfectly well, twice, 179.9s apart,
# saying "External resource is up to date" each time.
#
# The witness has to be one layer out: the controller's own reconcile events for
# this CR. Then "reconciled and made no request" is a RESULT -- the finding, in
# fact -- and "did not reconcile" is still VOID.
controller_dep() { K -n "$NS" get deploy -o name 2>/dev/null | grep -m1 'flavor-controller'; }

# reconcile_events <cr-name> -> how many log lines name that CR
reconcile_events() {
  local dep; dep=$(controller_dep)
  [ -n "$dep" ] || return 1
  K -n "$NS" logs "$dep" --tail=4000 2>/dev/null | grep -c "\"name\":\"$1\"" || true
}

resync_seconds() {
  local dep ns
  dep=$(K -n "$NS" get deploy -o name 2>/dev/null | grep -m1 'flavor-controller')
  [ -n "$dep" ] || return 1
  ns=$(K -n "$NS" logs "$dep" --tail=300 2>/dev/null \
       | sed -n 's/.*"resyncInterval":\([0-9][0-9]*\).*/\1/p' | head -1)
  [ -n "$ns" ] || return 1
  echo $(( ns / 1000000000 ))
}
count_method() { records_from "$1" | awk -v ua="$PROBE_UA" -v m="$2" '$4 != ua && $1 == m' | grep -c . || true; }
count_ctl()    { records_from "$1" | awk -v ua="$PROBE_UA" '$4 != ua' | grep -c . || true; }

# ===========================================================================
say "ARM X -- the create succeeds and returns no id"
# ===========================================================================
install_at "$(url_of noid "$PROJ_X")"
apply_cr probe-noid

# shellcheck disable=SC2329  # invoked indirectly, by wait_for
_x_posted() { [ "$(count_method noid POST)" -ge 1 ]; }
wait_for "the controller to POST to mock-noid at least once" 40 6 _x_posted \
  || void "no controller POST ever reached mock-noid within 240s. The controller
    never created anything, so nothing can be said about whether it re-creates.
    That is a timeout or an install problem, NOT a clean result."

# THE CONTROL THAT DECIDES WHETHER THIS ARM MEANS ANYTHING. The arm is about an
# EMPTY status.flavor.id. If the field is populated, nginx returned an id after
# all and the window below measures the healthy path.
cr_exists probe-noid \
  || void "the CR probe-noid does not exist, so every field read below would be
    empty for a reason that has nothing to do with the vendor's response."
X_ID=$(status_id probe-noid)
if [ -n "${X_ID:-}" ]; then
  void "status.flavor.id on probe-noid is '$X_ID', but mock-noid's create response
    carries no id. The field is populated from somewhere this probe does not
    model, so the arm cannot express the defect it was written for."
fi
ok "status.flavor.id is empty, which is the condition under test"

RESYNC=$(resync_seconds)
if [ -z "${RESYNC:-}" ]; then
  void "could not read resyncInterval from the flavor controller's log, so there
    is no way to know whether a ${WINDOW}s window is long enough to contain a
    second reconcile. Guessing it is would make any 'no re-create' answer
    unreadable."
fi
if [ "$WINDOW" -le "$RESYNC" ]; then
  void "the window is ${WINDOW}s and the controller's resyncInterval is
    ${RESYNC}s, so at most one reconcile can land inside it. 'No new POSTs'
    would mean 'no second reconcile happened', which is a statement about this
    probe. Set PROBE_WINDOW above ${RESYNC}."
fi
ok "window ${WINDOW}s > resyncInterval ${RESYNC}s, so at least one further reconcile is due"

# ONE CONTROL PER READ. The reconcile witness is a grep over the controller's
# log, so it is proven able to see THIS CR and proven able to report absence for
# a name that cannot be there -- before any count from it is believed.
X_E0=$(reconcile_events probe-noid)
if [ -z "${X_E0:-}" ] || [ "$X_E0" -lt 1 ]; then
  void "the controller's log holds no line naming probe-noid, although the CR
    exists and a POST reached the mock. The grep, the log retention or the
    deployment name is at fault, so no reconcile count from it is readable."
fi
X_ABSENT=$(reconcile_events probe-definitely-not-a-cr)
if [ "${X_ABSENT:-1}" -ne 0 ]; then
  void "the reconcile witness reports $X_ABSENT line(s) for a CR name that does
    not exist, so it cannot distinguish presence from absence."
fi
ok "reconcile witness sees probe-noid ($X_E0 line(s)) and correctly sees nothing for a name that does not exist"

X_N0=$(count_method noid POST); X_C0=$(count_ctl noid)
say "ARM X window: ${WINDOW}s, starting at POST=$X_N0 CTL=$X_C0 RECONCILES=$X_E0"
sleep "$WINDOW"
X_N1=$(count_method noid POST); X_C1=$(count_ctl noid)
X_E1=$(reconcile_events probe-noid)
X_DPOST=$((X_N1 - X_N0)); X_DCTL=$((X_C1 - X_C0)); X_DREC=$((X_E1 - X_E0))

printf '    %-7s %-34s %-6s %s\n' METHOD PATH STATUS COUNT
records_from noid | awk -v ua="$PROBE_UA" '$4 != ua { print $1, $2, $3 }' | sort | uniq -c | sort -rn |
  while read -r n m pa st; do printf '    %-7s %-34s %-6s x%s\n' "$m" "$pa" "$st" "$n"; done

# LIVENESS BEFORE INTERPRETATION -- measured against RECONCILES, not requests.
printf '    reconcile log lines for probe-noid: %s -> %s (delta %s)\n' "$X_E0" "$X_E1" "$X_DREC"
printf '    HTTP requests at the vendor:        %s -> %s (delta %s)\n' "$X_C0" "$X_C1" "$X_DCTL"
if [ "$X_DREC" -lt 1 ]; then
  void "the controller logged NO further line about probe-noid during the
    ${WINDOW}s window. A delta of 0 new POSTs is then not evidence that it does
    not re-create -- it is evidence that nothing reconciled. The window is
    ${WINDOW}s and resyncInterval is ${RESYNC}s, so either the controller is not
    running or its log has rotated past the window."
fi
if [ "$X_DPOST" -ge 1 ]; then
  bad "ARM X = RECREATED. $X_DPOST new POST(s) in ${WINDOW}s after the first
    create, with status.flavor.id empty throughout. Each one is another resource
    at the vendor, and the CR shows nothing."
  ARM_X=recreated
else
  if [ "$X_DCTL" -eq 0 ]; then
    ok "ARM X = no re-create, AND no HTTP request at all. The controller
    reconciled $X_DREC time(s) in ${WINDOW}s and called the vendor ZERO times:
    with status.flavor.id empty there is nothing to GET, so observe decides
    'up to date' without looking. That is the finding, not a quiet pass."
  else
    ok "ARM X = no re-create. $X_DCTL request(s) in the window, 0 of them POST,
    over $X_DREC reconcile(s)."
  fi
  ARM_X=stable
fi
conditions probe-noid | sed 's/^/    /'

# ===========================================================================
say "ARM Z -- can a CR whose id never populated be DELETED, and is the vendor told?"
# ===========================================================================
# THIS ARM EXISTS BECAUSE ARM X'S ANSWER TURNED OUT TO BE THE LESS INTERESTING
# HALF. Measured 2026-11-18 on rest-dynamic-controller 0.52.0: an id-less CR is
# NOT re-created -- and it is also never observed and never deleted at the
# vendor, while every surface reports success. The controller says so itself:
#
#     "External resource is up to date"                        (observe, no GET)
#     "External resource is not addressable, releasing
#      finalizer without calling the API"                      (delete)
#
# and the CR carries Ready=True/Available, Synced=True/ReconcileSuccess plus
# `krateo.io/external-create-succeeded`. So the operator KNOWS a create
# succeeded and still treats the resource as unaddressable, with nothing in
# Kubernetes recording that a resource was abandoned.
#
# WHICH FIELDS DID POPULATE is printed, because it is the discriminator: if
# status.flavor carries name/ram/vcpus/disk and not id, the response WAS parsed
# and only the declared identifier was missing. A wholly empty status would mean
# something else entirely.
say "what DID populate from the create response:"
printf '    status.flavor = %s\n' "$(status_keys probe-noid)"
conditions probe-noid | sed 's/^/    /'

# THE CONTROL FIRST, AND IT IS THE WHOLE ARM. "Zero DELETEs from the controller"
# and "this mock does not log DELETEs" are the same integer. So the probe sends
# one DELETE of its own, under its own User-Agent, and asserts it appears --
# before any absence is read as a finding.
Z_OWN_BEFORE=$(records_from noid | awk -v ua="$PROBE_UA" '$4 == ua && $1 == "DELETE"' | grep -c . || true)
K -n "$NS" run "probe-del-$RANDOM" --rm -i --restart=Never --quiet \
  --image=curlimages/curl:8.11.0 -- \
  -s -o /dev/null -X DELETE -A "$PROBE_UA" \
  "http://mock-noid.${NS}.svc.cluster.local:8080/v2.1/${PROJ_X}/flavors/control" >/dev/null 2>&1
# shellcheck disable=SC2329  # invoked indirectly, by wait_for
_z_own_logged() { [ "$(records_from noid | awk -v ua="$PROBE_UA" '$4 == ua && $1 == "DELETE"' | grep -c . || true)" -gt "$Z_OWN_BEFORE" ]; }
wait_for "the probe's own DELETE to appear in mock-noid's log" 10 3 _z_own_logged \
  || void "the probe sent a DELETE to mock-noid and its own log does not show one.
    A count of the controller's DELETEs would measure this instrument, so no
    absence below is readable."
ok "mock-noid logs DELETEs when it receives them, so a zero from the controller is an absence"

# EVENTS, NOT JUST THE LOG -- AND THIS IS THE DIFFERENCE BETWEEN TWO VERSIONS
# THAT THE REST OF THIS ARM CANNOT SEE.
#
# The first version of arm Z had three outcomes: deleted / hung / leaked. The
# oasgen-provider session read their own code and pointed out that it would
# print `leaked` for 0.52.0 and 0.74.0 IDENTICALLY -- while 0.62.0 added a
# Warning EVENT on exactly this path:
#
#     released <Kind>/<name> in namespace "<ns>" while its external resource
#     may still exist: the create succeeded but the resource could not be
#     addressed ... Check the external system for an object matching these
#     identifiers
#
# An orphan that is REPORTED is recoverable; a silent one is not. An instrument
# that gives both the same word is the shape this repo keeps finding in itself,
# so `leaked` splits into `leaked-reported` and `leaked-silent`.
#
# AN EVENT, NOT A CONDITION, AND A POST-DELETE READ IS THEREFORE FINE: the CR is
# removed in the same pass, so a condition set there would live for
# milliseconds, while events outlive the object and carry involvedObject.
events_for() {
  K -n "$NS" get events --field-selector "involvedObject.name=$1" \
    -o jsonpath='{range .items[*]}{.type}|{.reason}|{.message}{"\n"}{end}' 2>/dev/null
}

# CONTROL ON THE EVENT READ, because "no Warning" and "events are not readable
# here" are the same empty string. The namespace must have SOME event at all.
Z_EV_ANY=$(K -n "$NS" get events -o name 2>/dev/null | grep -c . || true)
if [ "${Z_EV_ANY:-0}" -lt 1 ]; then
  void "no event of any kind is readable in namespace $NS, so the absence of a
    Warning on the deleted CR would say nothing about the controller. Events may
    be disabled or expired here."
fi
ok "$Z_EV_ANY event(s) readable in $NS, so an absence of Warnings is an absence"
Z_EV0=$(events_for probe-noid | grep -c . || true)

Z_D0=$(count_method noid DELETE)
K -n "$NS" delete "$RES" probe-noid --timeout=90s > /tmp/probe-observe-del.log 2>&1
Z_DEL_RC=$?
sed 's/^/    /' /tmp/probe-observe-del.log
Z_D1=$(count_method noid DELETE)
Z_DELTA=$((Z_D1 - Z_D0))
printf '    controller DELETEs at the vendor: %s -> %s (delta %s)\n' "$Z_D0" "$Z_D1" "$Z_DELTA"
K -n "$NS" logs "$(K -n "$NS" get deploy -o name | grep -m1 flavor-controller)" --tail=80 2>/dev/null \
  | grep -o '"msg":"[^"]*"' | tail -4 | sed 's/^/    controller said /'

Z_EV=$(events_for probe-noid)
Z_WARN=$(printf '%s\n' "$Z_EV" | awk -F'|' '$1 == "Warning"' | grep -c . || true)
printf '    events for probe-noid: %s before the delete, %s now (%s Warning)\n' \
  "$Z_EV0" "$(printf '%s\n' "$Z_EV" | grep -c . || true)" "$Z_WARN"
printf '%s\n' "$Z_EV" | grep -v '^$' | sed 's/^/    event /' | head -6

if cr_exists probe-noid; then
  ARM_Z=hung
  bad "ARM Z = the CR did not finish deleting (rc=$Z_DEL_RC). It is waiting on a
    finalizer, which is a VISIBLE failure -- the operator is not pretending."
elif [ "$Z_DELTA" -ge 1 ]; then
  ARM_Z=deleted
  ok "ARM Z = the CR is gone AND $Z_DELTA DELETE(s) reached the vendor."
elif [ "${Z_WARN:-0}" -ge 1 ]; then
  ARM_Z=leaked-reported
  bad "ARM Z = LEAK, REPORTED. The CR is gone, rc=$Z_DEL_RC, ZERO DELETEs
    reached the vendor -- but $Z_WARN Warning event(s) say so, verbatim above.
    The resource is abandoned AND recoverable, because an operator can find it
    from the identifiers in that event. Still a leak; no longer a silent one."
else
  ARM_Z=leaked-silent
  bad "ARM Z = SILENT LEAK. The CR is gone, rc=$Z_DEL_RC, ZERO DELETEs reached
    the vendor -- with the control above proving this mock logs them -- and NO
    Warning event was emitted. A resource that was really created is abandoned
    and nothing in Kubernetes records it."
fi

# ===========================================================================
say "ARM Y -- the create succeeds WITH an id, then every request is rejected"
# ===========================================================================
install_at "$(url_of ok "$PROJ_Y")"
apply_cr probe-okid

# shellcheck disable=SC2329  # invoked indirectly, by wait_for
_y_has_id() { [ -n "$(status_id probe-okid)" ]; }
wait_for "status.flavor.id on probe-okid to be populated from the create response" 40 6 _y_has_id \
  || void "status.flavor.id never populated at mock-ok, although its create
    response carries one. Without it the get verb has no path parameter, so arm
    Y would be measuring arm X again -- the very confusion this probe exists to
    end."
Y_ID=$(status_id probe-okid)
ok "status.flavor.id = '$Y_ID', so observe now has something to look up"

# ===========================================================================
# oasgen-provider#648 -- WRAPPED IN A FUNCTION so a control failure here cannot
# discard arms that already produced answers.
#
# Run 44351381915 is why. A #648 control called `void`, which exits 3, and that
# killed ARM Y -- which had already populated status.flavor.id and was about to
# produce a verdict. I had written in this file's own history that `no-observe`
# must not count toward the exit code BECAUSE voiding would discard arms
# measured in the same run, and then let a different #648 control void anyway.
# One principle, applied in one place and not the other.
#
# So every #648 control calls y648_bad -- which records the reason and marks the
# sub-arm `unmeasured` -- and then RETURNS at the call site, because a function
# cannot return on behalf of its caller and writing as though it could is how a
# guard stops guarding. The verdict block still
# prints it; the exit code still ignores it (only `waits-for-resync` counts);
# and arms X, Y and Z keep their answers. An unmeasured sub-question and a
# failed run are different outcomes.
Y648_SKIP=""
y648_bad() {
  Y648_SKIP="$*"
  ARM_648=unmeasured
  printf '\033[33m  #648 UNMEASURED: %s\033[0m\n' "$*" >&2
}

measure_648() {
  # oasgen-provider#648 -- does a successful create schedule its observe, or does
  # the resource sit Creating until the next resync?
  #
  # Asked for by the oasgen-provider session, which has read half the deciding
  # branch: unstructured-runtime v1.27.1 calls rc.CreateWithResult (measured) and
  # rest-dynamic-controller implements it (measured, restResources.go:1512), but
  # the `actionErr == nil` tail of handleCreate -- the part that decides whether
  # an observe is scheduled -- is unread. A live timing settles it independently
  # of the reading, which is worth more than either alone.
  #
  # IT RIDES ON THIS ARM AND ADDS NO TRAFFIC. mock-ok is already standing, the
  # create has already happened, and these are three timestamps read out of a log
  # that was being written anyway. Nothing here changes what arms X, Y or Z
  # measure.
  #
  # THE THREE TIMES, and why each comes from where it does:
  #   t0  the controller's first POST at mock-ok   -- nginx's $msec
  #   t1  the controller's first GET  at mock-ok   -- nginx's $msec; the observe
  #   t2  the CR's first Ready=True                -- the condition's own
  #                                                   lastTransitionTime, so the
  #                                                   number is the controller's
  #                                                   and not this script's
  #                                                   polling granularity
  say "#648 -- is the observe after a successful create prompt, or one resync late?"

  # CONTROL ONE: THE PROBE MAKES ITS OWN TRAFFIC HERE, rather than looking for
  # traffic it made earlier.
  #
  # Run 44351381915 VOIDed on the earlier version of this control, which asserted
  # that the behaviour controls' probe-UA GET was still in mock-ok's log. It is
  # not: three steps above, "restarting the mocks so every arm starts from an
  # empty log" rolls all three Deployments precisely to empty them. **The control
  # asserted a precondition the probe's own design removes.** It was right to
  # refuse -- with no probe traffic present the UA filter is untested -- but it
  # refused for a reason about the rig.
  #
  # So it issues the request itself, now, after the restart. That is strictly
  # better than the original intent: it TESTS the filter on traffic whose
  # timestamp is known to fall inside the window being measured, instead of
  # assuming older traffic survived.
  # WAITED FOR, and the omission here was the fourth instance of one mistake.
  # This request lands right after "restarting the mocks so every arm starts
  # from an empty log", which REPLACES all three pods -- so the Service's
  # endpoints have to propagate again, exactly as at first deploy. A single-shot
  # curl against a just-restarted Service is the failure from run 44334008550,
  # fixed in the three behaviour controls and then reintroduced twenty lines
  # away in a call site those controls' own guard did not count.
  # shellcheck disable=SC2329  # invoked indirectly, as wait_for's "$@"
  _y648_probe_get() { curl_through ok "/v2.1/${PROJ_Y}/flavors/probe-flavor-1"; }
  if ! wait_for "mock-ok to answer the probe's own control request after the restart" \
       15 6 _y648_probe_get; then
    y648_bad "the probe could not reach mock-ok to lay down its own control
      request. Without it the controller/probe distinction is untested and the
      numbers below would be unattributable."
    return 1
  else
    Y648_PROBE_GETS=$(probe_msec_count ok GET)
    if [ "${Y648_PROBE_GETS:-0}" -lt 1 ]; then
      y648_bad "the probe's own GET at mock-ok returned $CODE but does not appear
      in the log under this probe's User-Agent ($Y648_PROBE_GETS found). Either the
      timestamped log_format is not reaching the log or the parser is not matching
      it -- so excluding probe traffic is untested and the numbers are
      unattributable."
      return 1
    else
      ok "mock-ok's log holds $Y648_PROBE_GETS GET(s) from this probe, laid down
      here, so excluding them is a filter that is exercised rather than assumed"
    fi
  fi

  T0=$(first_msec ok POST)
  if [ -z "${T0:-}" ]; then
    y648_bad "no CONTROLLER POST found in mock-ok's log, although status.flavor.id was
      populated from a create response. The create being timed is not in the log
      this is reading, so t0 does not exist and nothing can be measured from it."
    return 1
  fi
  ok "t0 (controller POST at mock-ok) = $T0"

  # WAIT FOR THE OBSERVE, BOUNDED PAST THE RESYNC. The whole question is whether
  # the first GET lands in seconds or at the next resync, so the bound has to
  # outlast a resync or "waits for resync" and "never observes" would be the same
  # reading. RESYNC is read from the controller's own startup log further up.
  Y648_BOUND=$(( (RESYNC + 90 + 5) / 6 ))

  # THE PRIOR CONDITION STATE IS NOT IN THE CR, so it has to be sampled. A
  # condition holds only its current value: once Ready flips to
  # `True/Available` there is nothing left to say it was ever `False/Creating`,
  # and "#648 is live" versus "this resource was never in Creating at all" are
  # different findings. So the reason is sampled before the wait and on every
  # attempt during it. The sampling is a deliberate side effect of the predicate,
  # noted here because a predicate that writes files is otherwise a surprise.
  Y648_REASONS=$(mktemp)
  ready_at probe-okid >> "$Y648_REASONS"
  printf '\n' >> "$Y648_REASONS"
  # shellcheck disable=SC2329  # invoked indirectly, by wait_for
  _y648_observed() {
    ready_at probe-okid >> "$Y648_REASONS"
    printf '\n' >> "$Y648_REASONS"
    [ -n "$(first_msec ok GET)" ]
  }
  if wait_for "the controller's first GET at mock-ok (the observe)" "$Y648_BOUND" 6 _y648_observed; then
    T1=$(first_msec ok GET)
    T2_RAW=$(ready_at probe-okid)
    Y648_DT=$(awk -v a="$T0" -v b="$T1" 'BEGIN { printf "%.1f", b - a }')
    # HALF THE RESYNC IS THE DISCRIMINATOR, not a fixed 180. A prompt observe
    # lands in seconds; a resync-driven one lands at ~RESYNC. Half the interval
    # separates them with room on both sides, and it moves if the interval does.
    Y648_HALF=$(( RESYNC / 2 ))
    if awk -v d="$Y648_DT" -v h="$Y648_HALF" 'BEGIN { exit !(d < h) }'; then
      ARM_648="prompt-observe"
      ok "#648 = PROMPT. t1-t0 = ${Y648_DT}s, under half the ${RESYNC}s resync
      interval, so the create scheduled its own observe at this version."
    else
      ARM_648="waits-for-resync"
      bad "#648 = LIVE. t1-t0 = ${Y648_DT}s against a ${RESYNC}s resync interval,
      so a successful create is NOT followed by a prompt observe: the resource
      reads Creating until the resync brings the next reconcile around."
    fi
    printf '    t0 POST  %s\n    t1 GET   %s   (t1-t0 = %ss)\n    Ready    %s\n' \
      "$T0" "$T1" "$Y648_DT" "${T2_RAW:-<no Ready condition>}"
    # WAS IT EVER Creating? Sampled, because the CR cannot be asked after the
    # fact. `grep -c` reads to EOF and cannot race; the file is a few hundred
    # bytes either way.
    Y648_SAW_CREATING=$(grep -c 'Creating' "$Y648_REASONS" || true)
    Y648_SAW_AVAIL=$(grep -c 'Available' "$Y648_REASONS" || true)
    printf '    Ready reason samples: %s with Creating, %s with Available, over %s sample(s)\n' \
      "$Y648_SAW_CREATING" "$Y648_SAW_AVAIL" "$(grep -c . "$Y648_REASONS" || true)"
    if [ "${Y648_SAW_CREATING:-0}" -eq 0 ]; then
      bad "#648 SIDE FINDING: the Ready condition was NEVER sampled as Creating,
      across every sample from immediately after the create onward. #648 describes
      a resource SITTING in Creating until the observe; a resource that is never
      in Creating is a different thing and is reported as different, not folded
      into the timing above."
    fi

    # t1 MINUS THE CREATE'S OWN SUCCESS STAMP, which isolates observe scheduling
    # from anything inside the create. Absent annotation is reported as absent --
    # an arithmetic on an empty string would print a number that means nothing.
    Y648_CREATED=$(create_succeeded_at probe-okid)
    case "${Y648_CREATED:-}" in
      ERR:*) printf '    t1-succeeded  NOT MEASURED -- the annotation read itself failed: %s\n' \
               "${Y648_CREATED#ERR:}"
             printf '                  (that is this probe failing, not the CR lacking the annotation)\n'
             Y648_CREATED="" ;;
    esac
    if [ -z "${Y648_CREATED:-}" ]; then
      printf '    t1-succeeded  not computed (see the line above, or: no krateo.io/external-create-succeeded annotation)\n'
    elif Y648_CS=$(date -u -d "$Y648_CREATED" +%s 2>/dev/null) && [ -n "${Y648_CS:-}" ]; then
      printf '    t1-succeeded  %ss (annotation %s)\n' \
        "$(awk -v a="$Y648_CS" -v b="$T1" 'BEGIN { printf "%.1f", b - a }')" "$Y648_CREATED"
    else
      printf '    t1-succeeded  not computed: %s is not parseable by this date(1)\n' "$Y648_CREATED"
    fi

    # t2 is reported, not classified. Converting an RFC3339 stamp needs GNU date,
    # and a conversion that fails must not be dressed up as a measurement.
    if [ -n "${T2_RAW:-}" ]; then
      Y648_READY_ISO=${T2_RAW##* }
      if T2=$(date -u -d "$Y648_READY_ISO" +%s 2>/dev/null) && [ -n "${T2:-}" ]; then
        printf '    t2-t0    %ss (Ready=True, from the condition lastTransitionTime)\n' \
          "$(awk -v a="$T0" -v b="$T2" 'BEGIN { printf "%.0f", b - a }')"
      else
        printf '    t2-t0    not computed: %s is not parseable by this date(1)\n' "$Y648_READY_ISO"
      fi
    fi
  else
    # NOT "FAST". An absent observe and a prompt one must never read alike.
    ARM_648="no-observe"
    bad "#648 = UNDETERMINED. No controller GET reached mock-ok within
      $(( Y648_BOUND * 6 ))s of its create, which is past the ${RESYNC}s resync
      interval. That is neither a prompt observe nor a resync-late one, and it is
      reported as neither."
  fi
}

# Called, not inlined, and its failure is not the run's.
measure_648 || true
if [ -n "${Y648_SKIP:-}" ]; then
  say "#648 was not measured; arms Y and the verdict continue"
fi


# ATTRIBUTION BY CONSTRUCTION. mock-reject has its own pod and its own log, and
# nothing has addressed it yet. Asserted rather than assumed: if traffic is
# already there, no POST found later can be attributed to the post-rejection
# window.
Y_PRE=$(count_ctl reject)
if [ "$Y_PRE" -ne 0 ]; then
  void "mock-reject's log already holds $Y_PRE controller request(s) before the
    chart was pointed at it. The window below would not be attributable."
fi
ok "mock-reject's log holds 0 controller requests, so its whole log is the window"

install_at "$(url_of reject "$PROJ_Y")"
# shellcheck disable=SC2329  # invoked indirectly, by wait_for
_y_reached() { [ "$(count_ctl reject)" -ge 1 ]; }
wait_for "the controller to reach mock-reject after the re-point" 40 6 _y_reached \
  || void "the controller never reached mock-reject within 240s of the chart
    being pointed at it. Either the new URL has not taken effect or nothing is
    reconciling; either way the silence below is about this probe."
say "ARM Y window: ${WINDOW}s"
sleep "$WINDOW"

printf '    %-7s %-34s %-6s %s\n' METHOD PATH STATUS COUNT
records_from reject | awk -v ua="$PROBE_UA" '$4 != ua { print $1, $2, $3 }' | sort | uniq -c | sort -rn |
  while read -r n m pa st; do printf '    %-7s %-34s %-6s x%s\n' "$m" "$pa" "$st" "$n"; done

Y_CLASS=$(records_from reject | classify_records "$PROBE_UA")
printf '    %s\n' "$Y_CLASS"
ARM_Y=$(printf '%s\n' "$Y_CLASS" | sed -n 's/^VERDICT=//p')
case "$ARM_Y" in
  recreated) bad "ARM Y = RECREATED. The controller POSTed again after its GET was
    rejected, so a rejection on observe is read as absence." ;;
  observed)  ok "ARM Y = observe propagated. GETs were rejected and no POST followed." ;;
  silent)    void "ARM Y is SILENT: no controller request in the window although one
    arrived before it. Nothing measured." ;;
  *)         bad "ARM Y = $ARM_Y -- neither GET nor POST. Read the table above." ;;
esac
conditions probe-okid | sed 's/^/    /'

say "VERDICT"
printf '    ARM X (create returns no id)        %s\n' "$ARM_X"
printf '    ARM Z (delete with no id)           %s\n' "$ARM_Z"
printf '    ARM Y (observe rejected after id)   %s\n' "$ARM_Y"
printf '    #648  (observe after a good create)  %s\n' "${ARM_648:-unmeasured}"
printf '    window per arm                      %ss\n' "$WINDOW"
cat <<'NOTE'

    WHAT A "stable" OR "observed" ANSWER IS WORTH. It is an absence over one
    window on one cluster, not a proof. Lengthen it with PROBE_WINDOW and the
    claim strengthens; it never becomes a guarantee. A "recreated" answer, by
    contrast, is positive evidence and needs no window argument.

    CLEANUP IS NOT AUTOMATIC. Both CRs are left in place deliberately: deleting
    probe-okid while the chart points at mock-reject sends the delete verb into
    a 401 and the CR will hang on its finalizer, which is this chart's known and
    deliberate behaviour. To clean up, point the chart back at mock-ok first:

      helm upgrade --install nova-probe ./chart -n <ns> --set nova.url=<mock-ok url>
      kubectl -n <ns> delete computeflavor probe-okid probe-noid
      helm uninstall nova-probe -n <ns>
NOTE
# Written as an `if`, not as `A || B && exit`: that form parses as
# `(A || B) && exit` and happens to be right here, which is exactly why it is
# the wrong thing to leave in a script -- SC2015 has already been earned twice
# in this repo's probes.
case "$ARM_Z" in leaked-*) Z_FAIL=yes ;; *) Z_FAIL=no ;; esac

# #648 IS IN THE EXIT DECISION, because a BAD that leaves the exit code at 0 is
# a finding nobody reads: the job goes green and the red line sits in a log
# nobody opens. `waits-for-resync` is positive evidence and counts.
#
# `no-observe` DELIBERATELY DOES NOT, and the asymmetry is the point. It means
# #648 was not measured -- not that it is fine. VOID would be the honest code
# for that sub-arm, but VOID exits 3 and would discard arms X, Y and Z, which
# WERE measured in the same run. So it stays visible in the verdict block above
# and leaves the code alone, rather than being promoted to a defect it is not,
# or voiding three good answers it has nothing to do with.
case "$ARM_648" in waits-for-resync) F648=yes ;; *) F648=no ;; esac
if [ "$ARM_X" = recreated ] || [ "$ARM_Y" = recreated ] || [ "$Z_FAIL" = yes ] \
   || [ "$F648" = yes ]; then
  exit 1
fi
exit 0
