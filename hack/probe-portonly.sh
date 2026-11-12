#!/usr/bin/env bash
# Does a PORT-ONLY redirect carry the credential?
#
# THE AXIS THIS EXISTS FOR. hack/probe-redirect.sh redirects between SIBLING
# hosts -- a.ns.svc -> b.ns.svc -- which Go's net/http strips by itself. The two
# shapes the standard library PERMITS are a subdomain hop and a PORT-ONLY hop,
# because shouldCopyHeaderOnRedirect compares url.Hostname() and that discards
# the port. The subdomain shape is not naturally reachable through Kubernetes
# Service DNS and is covered upstream by authn's own redirect_refusal_test.go.
# The port-only shape IS reachable here: two ports on one Service.
#
# A PREDICTION WITH A NAMED FALSIFIER, from the oasgen-provider maintainers, who
# read their own code for it:
#
#   PREDICTION  the hop is REFUSED by HostPolicy.CheckRedirect, because
#               entry.matches requires an exact port match (e.port == port), and
#               restclient.go installs CheckRedirect per hop rather than
#               refusing redirects wholesale. The credential never travels.
#   FALSIFIER   the token ARRIVES on :9090. Then either the policy is not
#               installed on that client or the port comparison is not reached,
#               and that is a defect in their code rather than a gap in ours.
#
# So this arm is worth running whichever way it lands, and a REFUSED result
# should be reported as "it could have said ARRIVED" rather than as a pass.
#
# TWO MODES:
#   --self-test       drive every outcome of the verdict table. No cluster, no
#                     docker. A table that has only ever produced one of its
#                     verdicts is not known to produce the others.
#   --dry-run         traverse every line of the cluster arm with the cluster
#                     stubbed. Catches the unbound-variable class that otherwise
#                     costs a cluster to find. Exits 0 on purpose.
#   --validate-mock   run the mock's SHIPPED BYTES in Docker and assert the
#                     measuring device works. Needs Docker, not a cluster, and
#                     runs in CI.
#   <kubeconfig> <context> [ns]   the full arm. Needs a cluster, creates none,
#                     deletes none. TWO sub-arms, because one cannot separate
#                     "the policy compares ports" from "the policy refuses every
#                     redirect" from "the policy is not enforcing": arm A hops
#                     :8080 -> :9090 and must be REFUSED, arm B hops :80 -> the
#                     same host with no port and must be PERMITTED.
set -uo pipefail
export LC_ALL=C

# SCRIPT-SCOPED ON PURPOSE. These were 'local' inside validate_mock, and the
# EXIT trap that removes the container fires AFTER that function has returned --
# so under 'set -u' the trap itself died with "cid: unbound variable" and LEFT
# THE CONTAINER RUNNING. A cleanup trap may only reference names that still
# exist when it fires, and on a shared machine a leaked container is not a
# tidiness problem: the only safe way to remove it afterwards is BY ITS OWN ID,
# because filtering by image would match other sessions' nginx mocks.
MOCK_CID=""
MOCK_TMP=""

ok()   { printf '\033[32m  OK: %s\033[0m\n' "$1"; }
bad()  { printf '\033[31m  FAIL: %s\033[0m\n' "$1"; FAILS=$((FAILS+1)); }
say()  { printf '  %s\n' "$1"; }
bold() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
void() { printf '\n\033[33m  VOID: %s\033[0m\n' "$1"; exit 9; }
FAILS=0

# ---- the mock, in ONE place -------------------------------------------------
# Two server blocks on one pod: :8080 redirects to the SAME host on :9090, and
# :9090 is the groundtruth instrument. PRESENT/ABSENT via map, never the value,
# for the same reason probe-redirect.sh does it that way: a verdict must not be
# able to print a credential-shaped string.
#
# QUOTED heredoc: nginx's own $variables must survive verbatim. Expanding them
# would make the map see "" always and log ABSENT forever -- which would read as
# "the credential did not travel", the reassuring direction. __HOST__ is
# substituted by the caller, never by the shell.
mock_conf() {
  cat <<'MOCKEOF'
map $http_x_auth_token $authhdr {
  ""      "ABSENT";
  default "PRESENT";
}
log_format portonly '$remote_addr "$request" host=$host port=$server_port x_auth_token=$authhdr ua="$http_user_agent"';

server {
  listen 8080;
  access_log /dev/stdout portonly;
  default_type application/json;
  # SAME HOST, DIFFERENT PORT. This is the shape url.Hostname() cannot see.
  location = /v2.1/flavors {
    return 302 'http://__HOST__:9090/v2.1/flavors';
  }
  location / { return 404 '{"itemNotFound":{"message":"not found","code":404}}'; }
}

server {
  listen 9090;
  access_log /dev/stdout portonly;
  default_type application/json;
  location = /v2.1/flavors {
    return 200 '{"flavor":{"id":"portonly-fake-id","name":"probe-portonly","ram":2048,"vcpus":1,"disk":20}}';
  }
  location / { return 200 '{"note":"9090 received a request on an unexpected path"}'; }
}

# THE SECOND SUB-ARM, AND WITHOUT IT THE FIRST PROVES LESS THAN IT LOOKS.
# Reached as Service port 80 -> containerPort 8081. The hop here is
# host:80 -> host (no port), which HostPolicy must treat as the SAME endpoint,
# because PermitsURL defaults an empty port to 80 for http before comparing:
#
#     port := u.Port(); if port == "" { case "http": port = "80" }
#     for _, e := range p.entries { if e.matches(host, port, scheme) ... }
#
# So this hop must be PERMITTED. A probe that only measured the 8080->9090 hop
# could not tell "the policy compares ports" from "the policy refuses redirects
# wholesale" from "the policy is not enforcing at all" -- three different worlds
# that all produce one refusal. The PAIR of arms separates them.
#
# The target is a DIFFERENT PATH on the default port, not the same path: the
# same path would redirect to itself and land on "stopped after 10 redirects",
# which is a refusal with an unrelated cause.
server {
  listen 8081;
  access_log /dev/stdout portonly;
  default_type application/json;
  location = /v2.1/flavors {
    return 302 'http://__HOST__/v2.1/flavors-landed';
  }
  location = /v2.1/flavors-landed {
    return 200 '{"flavor":{"id":"portonly-fake-id","name":"probe-portonly","ram":2048,"vcpus":1,"disk":20}}';
  }
  location / { return 404 '{"itemNotFound":{"message":"not found","code":404}}'; }
}
MOCKEOF
}

# ---- the verdict, as a FUNCTION so every outcome is testable ----------------
# THE PAIR IS THE CLASSIFIER, and one arm cannot separate three worlds: a
# port-aware policy, one that refuses every redirect, and one that enforces
# nothing all produce a refusal on arm A alone. Kept as a function with no
# cluster in it so --self-test can drive all of its outcomes; a verdict table
# that has only ever produced one of its verdicts is not known to produce the
# others.
classify_portonly() { # classify_portonly <arm_a> <arm_b>
  case "${1:-}/${2:-}" in
    refused/permitted|refused/permitted-stripped) echo port-aware ;;
    arrived/*)                                    echo falsified ;;
    refused/refused)                              echo refuses-everything ;;
    stripped/*)                                   echo stripped-not-refused ;;
    *)                                            echo unclassified ;;
  esac
}

portonly_self_test() {
  local pass=0 fail=0
  t() { # t <label> <want> <got>
    if [ "$2" = "$3" ]; then printf '  %-56s PASS\n' "$1"; pass=$((pass+1))
    else printf '  %-56s FAIL want=%s got=%s\n' "$1" "$2" "$3"; fail=$((fail+1)); fi
  }
  bold "verdict self-test (no cluster, no docker)"
  t "refused + permitted -> the prediction holds" \
      port-aware           "$(classify_portonly refused permitted)"
  t "refused + permitted-stripped -> still port-aware" \
      port-aware           "$(classify_portonly refused permitted-stripped)"
  # THE FALSIFIER MUST BE REACHABLE. A classifier that could not print
  # 'falsified' would make every port-aware verdict unfalsifiable, which is the
  # reassuring direction and the whole reason this probe was worth writing.
  t "arrived on :9090 -> falsified, whatever arm B said" \
      falsified            "$(classify_portonly arrived permitted)"
  t "arrived + refused -> still falsified" \
      falsified            "$(classify_portonly arrived refused)"
  # The arm that stops arm A's refusal from being over-read.
  t "both refused -> refuses-everything, NOT port-aware" \
      refuses-everything   "$(classify_portonly refused refused)"
  t "followed-but-stripped is its own reading" \
      stripped-not-refused "$(classify_portonly stripped permitted)"
  # A combination with no reading must SAY so rather than fall into the nearest
  # verdict. Reported, never rounded.
  t "an unknown pair -> unclassified, not the nearest match" \
      unclassified         "$(classify_portonly weird permitted)"
  t "empty arms -> unclassified" \
      unclassified         "$(classify_portonly '' '')"
  # And the control: the five verdicts must be DISTINCT, or the table above
  # could pass with a function that returned one string.
  local n
  n=$(printf '%s\n' \
        "$(classify_portonly refused permitted)" \
        "$(classify_portonly arrived permitted)" \
        "$(classify_portonly refused refused)" \
        "$(classify_portonly stripped permitted)" \
        "$(classify_portonly weird weird)" | sort -u | grep -c .)
  t "the five verdicts are distinct (control)" 5 "$n"
  echo
  echo "  $pass passed, $fail failed"
  [ "$fail" -eq 0 ] || return 1
  return 0
}

validate_mock() {
  command -v docker >/dev/null 2>&1 || void "docker is not available; --validate-mock runs the mock."
  docker info >/dev/null 2>&1 || void "docker is installed but not responding, so the mock was never started. Not a pass."
  bold "--validate-mock: the measuring device, from the bytes this probe ships"
  local conf p8 p9 p81 logs
  conf=$(mock_conf | sed 's/__HOST__/127.0.0.1/g')
  # shellcheck disable=SC2016  # the UNEXPANDED $http_x_auth_token is the
  # needle: if it had expanded, that is the always-ABSENT defect being tested
  # for, and the map would read empty forever.
  printf '%s\n' "$conf" | grep -qF 'map $http_x_auth_token $authhdr' \
    || void "the mock config lost its literal map variable, so it would log ABSENT forever."
  printf '%s\n' "$conf" | grep -qF ':9090/v2.1/flavors' \
    || void "the mock's 302 does not target port 9090, so the arm would not be a port-only hop."
  printf '%s\n' "$conf" | grep -q '__HOST__' \
    && void "__HOST__ survived substitution, so the redirect target is not a real host."
  ok "config extracted with the map intact and a :9090 target"

  MOCK_TMP=$(mktemp -d); printf '%s\n' "$conf" > "$MOCK_TMP/default.conf"
  MOCK_CID=$(docker run -d -p 0:8080 -p 0:9090 -p 0:8081 \
        -v "$MOCK_TMP/default.conf:/etc/nginx/conf.d/default.conf:ro" \
        nginxinc/nginx-unprivileged:1.27-alpine 2>/dev/null)
  [ -n "$MOCK_CID" ] || void "could not start the mock; nginx may have rejected the two-server config."
  p8=$(docker port "$MOCK_CID" 8080/tcp 2>/dev/null | head -1 | sed 's/.*://')
  p9=$(docker port "$MOCK_CID" 9090/tcp 2>/dev/null | head -1 | sed 's/.*://')
  p81=$(docker port "$MOCK_CID" 8081/tcp 2>/dev/null | head -1 | sed 's/.*://')
  [ -n "$p81" ] || void "port 8081 is not published, so the default-port sub-arm's block is unreachable and arms 5-6 would be blank."
  [ -n "$p8" ] && [ -n "$p9" ] || void "both ports must be published for a port-only hop to be observable (8080=$p8 9090=$p9)."
  for _ in $(seq 1 30); do curl -s -o /dev/null -m 2 "http://127.0.0.1:$p8/" && break; sleep 1; done
  [ "$(docker inspect -f '{{.State.Running}}' "$MOCK_CID" 2>/dev/null)" = "true" ] || {
    say "logs: $(docker logs "$MOCK_CID" 2>&1 | tail -3 | tr '\n' '|')"
    void "the container is not running, so nginx rejected the shipped config."
  }

  bold "ARM 1 -- :8080 must answer 302 to the same host on :9090"
  local code loc
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 5 "http://127.0.0.1:$p8/v2.1/flavors")
  loc=$(curl -s -D - -o /dev/null -m 5 "http://127.0.0.1:$p8/v2.1/flavors" | tr -d '\r' | awk '/^[Ll]ocation:/{print $2}')
  if [ "$code" = "302" ] && [ "${loc##*:}" = "9090/v2.1/flavors" ]; then ok "302 -> $loc"
  else bad "expected a 302 to :9090, got code=$code location=${loc:-<none>}"; fi

  bold "ARM 2 -- :9090 must log PRESENT with a token and ABSENT without"
  curl -s -o /dev/null -m 5 -H 'X-Auth-Token: dummy-not-a-credential' "http://127.0.0.1:$p9/v2.1/flavors" || true
  curl -s -o /dev/null -m 5 "http://127.0.0.1:$p9/v2.1/flavors" || true
  sleep 1
  logs=$(docker logs "$MOCK_CID" 2>&1)
  local nP nA
  nP=$(printf '%s\n' "$logs" | grep -c 'port=9090 x_auth_token=PRESENT')
  nA=$(printf '%s\n' "$logs" | grep -c 'port=9090 x_auth_token=ABSENT')
  # NOT 'A && ok || bad': in that form 'bad' also runs if 'ok' itself fails,
  # which would report a finding because the printf returned non-zero.
  if [ "$nP" -ge 1 ]; then ok "PRESENT logged on :9090"
  else bad "a token sent to :9090 was not logged PRESENT; the instrument cannot see a credential and would report no travel whatever happened."; fi
  if [ "$nA" -ge 1 ]; then ok "ABSENT logged on :9090"
  else bad "a request without a token was not logged ABSENT; the field cannot express absence."; fi

  bold "ARM 3 -- the two must DISAGREE"
  if [ "$nP" -ge 1 ] && [ "$nA" -ge 1 ]; then
    ok "both values observed on :9090, so the field discriminates"
  else
    bad "only one value observed (PRESENT=$nP ABSENT=$nA). A constant field cannot distinguish a credential that travelled from one that did not, and always-ABSENT reads as 'it did not'."
  fi

  bold "ARM 4 -- the token VALUE must never appear"
  if printf '%s\n' "$logs" | grep -qF 'dummy-not-a-credential'; then
    bad "the access log contains the token value."
  else
    ok "only PRESENT/ABSENT reached the log"
  fi


  bold "ARM 5 -- :8081 must 302 to the SAME HOST WITH NO PORT"
  # This is what makes sub-arm B a default-port hop rather than another
  # differing-port hop. If the target carried ":8081" or any port, arm B would
  # measure the same thing as arm A and the pair would stop separating the three
  # worlds it exists to separate.
  local code81 loc81 hostpart
  code81=$(curl -s -o /dev/null -w '%{http_code}' -m 5 "http://127.0.0.1:$p81/v2.1/flavors")
  loc81=$(curl -s -D - -o /dev/null -m 5 "http://127.0.0.1:$p81/v2.1/flavors" | tr -d '\r' | awk '/^[Ll]ocation:/{print $2}')
  # Strip scheme, then take everything before the first '/': that is host[:port].
  hostpart=${loc81#http://}; hostpart=${hostpart%%/*}
  if [ "$code81" = "302" ] && [ -n "$loc81" ] && [ "$hostpart" = "${hostpart%%:*}" ]; then
    ok "302 -> $loc81 (host '$hostpart' carries no port)"
  else
    bad "expected a 302 whose Location host has NO port, got code=$code81 location=${loc81:-<none>} host='$hostpart'.
    With a port there, sub-arm B is a second differing-port hop and the verdict table collapses."
  fi

  bold "ARM 6 -- the landing path must answer 200 and be logged separately"
  local codeland
  codeland=$(curl -s -o /dev/null -w '%{http_code}' -m 5 -H 'X-Auth-Token: dummy-not-a-credential' \
             "http://127.0.0.1:$p81/v2.1/flavors-landed")
  if [ "$codeland" = "200" ]; then ok "landing path answered 200"
  else bad "the landing path answered '$codeland'. Sub-arm B reads PERMITTED from a logged request on that path, so it must be reachable."; fi

  bold "ARM 7 -- the SHIPPED parser must separate callers on real nginx output"
  # Same discipline as hack/validate-401-mock.sh assertion 5: --validate-mock
  # would otherwise test the parser against log lines this file typed, and a
  # real format differing by a field or a quote would still pass while the
  # cluster arm reported zero controller requests about a healthy cluster.
  local psrc recs own oth
  psrc=$(awk '/^# ---8<--- PORTONLY_AWK$/{f=1;next} /^# ---8<--- END PORTONLY_AWK$/{f=0} f' "$0")
  if ! printf '%s' "$psrc" | grep -q 'x_auth_token='; then
    bad "could not extract PORTONLY_AWK from this script between its markers. Nothing was
    validated: a parser that cannot be found is not a parser that passed."
  else
    eval "$psrc"
    curl -s -o /dev/null -m 5 -A 'probe-portonly-control' -H 'X-Auth-Token: dummy-not-a-credential' \
         "http://127.0.0.1:$p9/v2.1/flavors" >/dev/null 2>&1
    curl -s -o /dev/null -m 5 -A 'Go-http-client/1.1' \
         "http://127.0.0.1:$p9/v2.1/flavors" >/dev/null 2>&1
    sleep 1
    recs=$(docker logs "$MOCK_CID" 2>&1 | awk "$PORTONLY_AWK")
    own=$(printf '%s\n' "$recs" | awk '$4 == "probe-portonly-control"' | grep -c .)
    oth=$(printf '%s\n' "$recs" | awk '$4 == "Go-http-client/1.1"' | grep -c .)
    if [ "${own:-0}" -ge 1 ] && [ "${oth:-0}" -ge 1 ]; then
      ok "separation works on real bytes: $own marked as the probe's, $oth as another client's"
    else
      bad "the parser did not separate the two User-Agents on real nginx output (own=$own other=$oth,
    want >=1 each). A filter matching nothing and one matching everything both leave one
    of these at zero, and the cluster arm's 'no controller request' verdict depends on neither."
      say "last 4 records: $(printf '%s\n' "$recs" | tail -4 | tr '\n' '|')"
    fi
    # NO NOISE LINE MAY BECOME A RECORD. nginx writes startup and error lines to
    # the same stream; a parser without its empty-field guard turns each into a
    # record with an empty port and ua="-". Measured on a 7-line corpus: 2
    # requests produced 2 records with the guard and 7 without. In the cluster
    # arm that is not merely noise -- "-" is not the probe's marker, so it counts
    # as a SECOND distinct non-probe caller and the caller-column check reports a
    # FALSE FINDING about traffic that was never a request.
    #
    # A BEFORE/AFTER DELTA CANNOT SEE THIS, and that is why there isn't one here.
    # The first version of this arm sent two requests and asserted the record
    # count moved by two. Phantom records come from startup lines that are
    # already in the log before the window opens, so the delta is two either
    # way: the mutant survived the arm written to catch it. Mutation-proven, then
    # replaced by two arms that look at the records themselves.
    local badport noise
    badport=$(printf '%s\n' "$recs" | awk '$1 != "8080" && $1 != "9090" && $1 != "8081"' | grep -c .)
    if [ "${badport:-0}" -eq 0 ]; then
      ok "every parsed record carries one of the three real ports"
    else
      bad "$badport record(s) carry a port that is none of 8080/9090/8081. Those are not
    requests -- most likely nginx startup lines parsed as records -- and in the
    cluster arm each would appear as a caller named \"-\"."
      say "offending: $(printf '%s\n' "$recs" | awk '$1 != "8080" && $1 != "9090" && $1 != "8081"' | head -3 | tr '\n' '|')"
    fi
    # And the direct arm: a real nginx noise line, through the SHIPPED parser.
    noise=$(printf '%s\n' \
      '2026/11/12 00:30:01 [notice] 1#1: start worker processes' \
      '/docker-entrypoint.sh: Configuration complete; ready for start up' \
      'nginx: [emerg] bind() to 0.0.0.0:80 failed (13: Permission denied)' \
      | awk "$PORTONLY_AWK" | grep -c .)
    if [ "${noise:-1}" -eq 0 ]; then
      ok "three real nginx noise lines yield zero records"
    else
      bad "nginx noise lines produced $noise record(s). Each becomes a phantom caller in
    the cluster arm's attribution table."
    fi

    # The PORT field is what both arms select on, so it must survive parsing.
    if printf '%s\n' "$recs" | awk '$1 == "9090"' | grep -q .; then
      ok "the server_port survives parsing (9090 found as field 1)"
    else
      bad "no record carries 9090 in the port field, so both arms would select nothing."
    fi
  fi
  bold "ARM 8 -- the readiness target, and why it must not be an httpGet on /"
  # THE ARM THAT WOULD HAVE SAVED A CLUSTER WINDOW. The cluster arm VOIDed with
  # "the mock never became ready" while nginx was serving perfectly: the
  # readiness probe was an httpGet on /, which this config answers with 404 BY
  # DESIGN, and kubelet requires 200-399. Nothing here could see it, because the
  # warm-up above uses curl exit status -- which is 0 for a 404.
  #
  # So this arm measures the status explicitly and asserts the shipped manifest
  # uses tcpSocket, which is the only readiness form that is correct for a mock
  # whose whole job is to answer 404 to everything it does not recognise.
  local root_code
  root_code=$(curl -s -o /dev/null -w '%{http_code}' -m 5 "http://127.0.0.1:$p8/")
  if [ "$root_code" = "404" ]; then
    ok "GET / on :8080 is $root_code by design, so it can never satisfy an httpGet probe"
  else
    bad "GET / on :8080 answered $root_code, not 404. The config changed; re-check whether
    an httpGet readiness probe would now be viable before relying on this arm."
  fi
  # Checked directly, not via $? -- this repo has already shipped one defect from
  # reading a status indirectly.
  if ! curl -sf -o /dev/null -m 5 "http://127.0.0.1:$p8/" 2>/dev/null; then
    ok "and curl -f DOES reject it, which is the check the warm-up lacked"
  else
    bad "curl -f accepted a $root_code response, so the status-aware check is not working
    and this arm cannot distinguish an error status from a success."
  fi
  if grep -qE '^\s*tcpSocket: \{ port: 8080 \}' "$0"; then
    ok "the shipped manifest uses a tcpSocket readiness probe"
  else
    bad "the shipped readiness probe is NOT tcpSocket. An httpGet on a path this mock
    404s can never become ready, and an httpGet on any path puts kubelet in the
    access log as a second non-probe caller."
  fi

  bold "RESULT"
  if [ "$FAILS" -ne 0 ]; then
    printf '\033[31m  %s assertion(s) failed: the port-only arm would measure with a broken instrument.\033[0m\n' "$FAILS"
    return 1
  fi
  ok "the mock performs BOTH hops, its :9090 end says PRESENT and ABSENT, and the shipped parser separates callers."
  say "It could have reported a broken instrument and did not."
  return 0
}

# Installed at SCRIPT scope so it is still valid when it fires, and it removes
# only the container THIS run created, by id. Never by image filter: other
# sessions on this machine run nginx mocks from the same image.
# shellcheck disable=SC2329  # invoked by the EXIT trap on the next line
mock_cleanup() {
  [ -n "$MOCK_CID" ] && docker rm -f "$MOCK_CID" >/dev/null 2>&1
  [ -n "$MOCK_TMP" ] && rm -rf "$MOCK_TMP"
  return 0
}
trap mock_cleanup EXIT

if [ "${1:-}" = "--validate-mock" ]; then validate_mock; exit $?; fi
if [ "${1:-}" = "--self-test" ]; then
  portonly_self_test || { printf '\n\033[31mFAIL: the verdict table does not behave as specified.\033[0m\n' >&2; exit 1; }
  ok "the verdict table can produce every one of its five readings"
  exit 0
fi

# =============================================================================
# THE CLUSTER ARM
# =============================================================================
# RUNS ON AN EXISTING CLUSTER. It creates no cluster and deletes none: cluster
# lifecycle belongs to hack/gke-run.sh, which deletes by exact name with
# verification. A probe that deletes a cluster it did not create is how another
# session's control plane disappears on a shared project.
# ---- --dry-run: traverse every line of the cluster arm with no cluster -------
# WHY THIS EXISTS, AND IT IS NOT HYPOTHETICAL. hack/probe-590-restart.sh died on
# a BILLING GKE cluster with "N_CTRL: unbound variable" -- a name used one line
# above its assignment. Its --self-test could not reach that line, so the defect
# was only ever reachable by spending a cluster. A dry run that traverses the
# WHOLE main path under 'set -u' is the only thing that finds that class before
# the window is spent.
#
# It stubs the cluster, not the logic: every condition, count, classification
# and printf below runs for real. It exits 0 ON PURPOSE -- it asserts
# traversability, not a verdict, and the records it feeds are chosen to drive the
# port-aware path, which is one path of four.
DRY=0
if [ "${1:-}" = "--dry-run" ]; then DRY=1; shift; fi
if [ "$DRY" = "1" ]; then
  set -- "${1:-/dev/null}" "${2:-dry-context}" "${3:-krateo-system}"
  DRY_RVFILE=$(mktemp)
  printf '1\n' > "$DRY_RVFILE"
  # The synthetic access log. Chosen so the positive control passes and the
  # verdict lands on port-aware: the probe's own two requests on :9090, a
  # controller request on :8080 that is NOT followed to :9090, and a controller
  # request on :8081 that IS followed to the landing path.
  dry_log() {
    cat <<'DRYLOG'
10.0.0.9 "GET /v2.1/flavors HTTP/1.1" host=h port=9090 x_auth_token=PRESENT ua="probe-portonly-control"
10.0.0.9 "GET /v2.1/flavors HTTP/1.1" host=h port=9090 x_auth_token=ABSENT ua="probe-portonly-control"
10.0.0.5 "POST /v2.1/flavors HTTP/1.1" host=h port=8080 x_auth_token=PRESENT ua="Go-http-client/1.1"
10.0.0.5 "POST /v2.1/flavors HTTP/1.1" host=h port=8081 x_auth_token=PRESENT ua="Go-http-client/1.1"
10.0.0.5 "GET /v2.1/flavors-landed HTTP/1.1" host=h port=8081 x_auth_token=PRESENT ua="Go-http-client/1.1"
2026/11/12 00:30:01 [notice] 1#1: start worker processes
DRYLOG
  }
  # Stubs are matched on the ARGUMENT SHAPE, most specific first. A stub that
  # answered everything with success would make the traversal prove nothing
  # about the parsing below it.
  K() {
    case "$*" in
      version*)                 return 0 ;;
      *"get crd"*)              echo "crd-exists"; return 0 ;;
      *"create configmap"*)     echo "apiVersion: v1"; echo "kind: ConfigMap"; return 0 ;;
      *"rollout status"*)       return 0 ;;
      *"run probe-po-"*)        echo 200; return 0 ;;
      *"logs deploy/mock-portonly"*) dry_log; return 0 ;;
      *resourceVersion*)        local n; n=$(cat "$DRY_RVFILE"); echo $((n+1)) > "$DRY_RVFILE"; echo "rv$n"; return 0 ;;
      *status.conditions*)      echo "type=Ready status=False reason=Creating message=dry run"; return 0 ;;
      *delete*)                 return 0 ;;
      # ONLY THE '-f -' FORM READS STDIN. The first version drained stdin for
      # every apply, and 'apply -f <file>' has nothing piped into it -- so 'cat'
      # blocked on the terminal and the dry run hung with no output, because
      # printf to a redirected file is block-buffered and nothing had flushed.
      # A stub that consumes more than the real command does is its own defect.
      *"apply -f -"*)           cat >/dev/null 2>&1; return 0 ;;
      *apply*)                  return 0 ;;
      *)                        return 0 ;;
    esac
  }
  H() { echo "[dry] helm $*" >/dev/null; return 0; }
  # NO _curl_in_cluster STUB HERE, deliberately. An earlier version had one and
  # it was DEAD CODE: the real definition appears later in the file and
  # overrides it, so the traversal was never using it. It happened to work
  # because the K stub answers 'run probe-po-...' -- i.e. the real helper was
  # exercised all along, through K. Found while writing probe-548-delete.sh,
  # where the same dead stub made an arm misclassify. A stub that cannot run is
  # worse than no stub: it says the real path is not covered when it is.
else
  :
fi

KUBECONFIG_PATH="${1:?usage: probe-portonly.sh <kubeconfig> <context> [namespace] | --validate-mock}"
CONTEXT="${2:?a context is required: never rely on the current kubectl context, because the shared kubeconfig reaches real remote clusters}"
NS="${3:-krateo-system}"

# NEVER AMBIENT. Every cluster call carries the kubeconfig and the context.
if [ "$DRY" = "0" ]; then
  K() { kubectl --kubeconfig "$KUBECONFIG_PATH" --context "$CONTEXT" "$@"; }
  H() { helm --kubeconfig "$KUBECONFIG_PATH" --kube-context "$CONTEXT" "$@"; }
fi

GROUP=nova.openstack.krateo.io
HOST="mock-portonly.${NS}.svc.cluster.local"
CFG=probe-portonly-cfg
CR=probe-portonly
SEC=probe-portonly-token
# The marker that separates the probe's own traffic from the controller's. The
# probe curls the SAME paths the controller uses, so without it a request in the
# log cannot be attributed. One string, used by the curl that sends it and by
# the awk that excludes it.
PROBE_UA=probe-portonly-control

[ -f chart/Chart.yaml ] || void "run me from the repo root: chart/Chart.yaml is not here, so no chart could be staged."

# shellcheck source=hack/lib/wait.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/wait.sh"

bold "preconditions -- each failure says what it means, not just that it failed"
K version --request-timeout=10s >/dev/null 2>&1 \
  || void "the API server at context $CONTEXT is not reachable. Nothing was measured."
_served() { K get crd "computeflavorconfigurations.${GROUP}" >/dev/null 2>&1 && K get crd "computeflavors.${GROUP}" >/dev/null 2>&1; }
_served || void "both halves of the ComputeFlavor pair must be served before an arm can
    apply a CR. Install the chart first; this probe measures redirect behaviour,
    not installation."
ok "API reachable and both halves of the pair are served"

# ---- the mock, as a Deployment plus a Service with THREE ports ---------------
# A Service, not a bare pod IP: the hop under test is expressed in DNS, and a
# redirect to a pod IP would be a different shape (no name, so no host
# comparison to speak of).
bold "mock-portonly: :8080 hops to :9090, and :80 hops to the same host with no port"
MOCK_CONF=$(mock_conf | sed "s/__HOST__/${HOST}/g")
printf '%s\n' "$MOCK_CONF" | grep -q '__HOST__' \
  && void "__HOST__ survived substitution, so a redirect target is not a real host and
    the arm would measure a hop to a literal placeholder."
K -n "$NS" create configmap mock-portonly-conf --from-literal=default.conf="$MOCK_CONF" \
  --dry-run=client -o yaml 2>/dev/null | K -n "$NS" apply -f - >/dev/null 2>&1 \
  || void "could not apply the mock ConfigMap; nothing downstream ran."
K -n "$NS" apply -f - >/dev/null 2>&1 <<YAML || void "could not apply the mock Deployment/Service."
apiVersion: apps/v1
kind: Deployment
metadata: { name: mock-portonly, namespace: ${NS} }
spec:
  replicas: 1
  selector: { matchLabels: { app: mock-portonly } }
  template:
    metadata: { labels: { app: mock-portonly } }
    spec:
      containers:
        - name: nginx
          image: nginxinc/nginx-unprivileged:1.27-alpine
          ports:
            - { containerPort: 8080 }
            - { containerPort: 9090 }
            - { containerPort: 8081 }
          volumeMounts: [ { name: conf, mountPath: /etc/nginx/conf.d } ]
          # A readiness probe, because a successful rollout means "the container
          # started", not "nginx is listening". Measured on probe-401: the
          # positive control got curl code 000 immediately after a successful
          # rollout on a loaded machine.
          # tcpSocket, NOT httpGet, and both halves of that matter.
          #
          # MEASURED: 'GET /' on :8080 returns 404 by design -- the 8080 block
          # answers /v2.1/flavors with a 302 and everything else with 404 -- and
          # kubelet's httpGet requires 200-399. So the pod could NEVER become
          # ready, 'rollout status' timed out, and the probe VOIDed with "nginx
          # may have rejected the config" when nginx was serving perfectly.
          # Cost a cluster window to discover.
          #
          # The Docker validator could not catch it: its warm-up used
          # 'curl -s -o /dev/null', and CURL EXITS 0 ON A 404. Measured both
          # ways -- exit 0 without -f, exit 22 with it. A check that cannot
          # express an HTTP error status cannot verify a readiness path.
          #
          # tcpSocket also keeps kubelet OUT of the access log. An httpGet
          # readiness probe appears as a caller with UA 'kube-probe/...', which
          # is a second non-probe caller and would trip the attribution check
          # that exists to notice exactly that.
          readinessProbe:
            tcpSocket: { port: 8080 }
            initialDelaySeconds: 1
            periodSeconds: 2
      volumes:
        - name: conf
          configMap: { name: mock-portonly-conf }
---
apiVersion: v1
kind: Service
metadata: { name: mock-portonly, namespace: ${NS} }
spec:
  selector: { app: mock-portonly }
  ports:
    - { name: hop-source,   port: 8080, targetPort: 8080 }
    - { name: hop-target,   port: 9090, targetPort: 9090 }
    # Service port 80 so the chart can be pointed at the host with NO port,
    # which is the whole point of sub-arm B: an empty port must default to 80.
    - { name: default-port, port: 80,   targetPort: 8081 }
YAML
K -n "$NS" rollout status deploy/mock-portonly --timeout=180s >/dev/null 2>&1 \
  || void "the mock never became ready, so nginx may have rejected the three-server
    config. Nothing was measured; check: kubectl -n $NS logs deploy/mock-portonly"
ok "mock rolled out with three server blocks and a three-port Service"

# ---- the log reader ---------------------------------------------------------
# ---8<--- PORTONLY_AWK
# One record per request: PORT PATH TOKENSTATE UA. UA spaces squeezed to '+' so
# callers can select on $4 with awk's default splitting instead of matching a
# prefix of it.
# shellcheck disable=SC2016  # single quotes are required: $0, $1 and RSTART
# are AWK's, and double quotes would have the shell substitute its own
# positional parameters -- empty here -- leaving a program that parses nothing.
PORTONLY_AWK='
{
  port = ""; tok = ""; ua = ""; path = ""
  if (match($0, /port=[0-9]+/))                 { port = substr($0, RSTART+5, RLENGTH-5) }
  if (match($0, /x_auth_token=[A-Z]+/))         { tok  = substr($0, RSTART+13, RLENGTH-13) }
  if (match($0, /ua="[^"]*"/))                  { ua   = substr($0, RSTART+4, RLENGTH-5) }
  if (match($0, /"[A-Z]+ [^ "]+ HTTP\/[0-9.]+"/)) {
    req = substr($0, RSTART+1, RLENGTH-2); split(req, r, " "); path = r[2]
  }
  if (port == "" || tok == "" || path == "") next
  if (ua == "") ua = "-"
  gsub(/ /, "+", ua)
  print port, path, tok, ua
}
'
# ---8<--- END PORTONLY_AWK
MOCK_TAIL=800
read_records() { K -n "$NS" logs deploy/mock-portonly --tail="$MOCK_TAIL" 2>/dev/null | awk "$PORTONLY_AWK"; }
n_records()    { printf '%s\n' "$1" | grep -c . ; }

bold "POSITIVE CONTROL -- :9090 must be able to log BOTH states before any absence is read"
# THE ORDER IS THE POINT. The verdict below turns on whether a request reached
# :9090. If the instrument cannot log a request on :9090 at all -- wrong port in
# the Service, nginx block not loaded, log format changed -- then "nothing
# reached :9090" is printed by an instrument that cannot see anything, and that
# is the REASSURING direction. So the probe makes :9090 log both states using
# traffic it sends itself, first.
_curl_in_cluster() { # _curl_in_cluster <name> <url> [extra-args...]
  local name="$1" url="$2"; shift 2
  # -m 10: without a max-time a hung connection waits indefinitely, and the
  # difference between "refused" and "hung" is itself diagnostic.
  K -n "$NS" run "$name" --rm -i --quiet --restart=Never \
    --image=curlimages/curl:8.11.0 -- \
    -s -m 10 -o /dev/null -w '%{http_code}' -A "$PROBE_UA" "$@" "$url" 2>/dev/null | grep -oE '[0-9]{3}' | head -1
}
# RETRIED, NOT SINGLE-SHOT, and this lesson was already written down in this
# repo before I failed to reuse it. hack/probe-401.sh carries a comment dated
# 2026-11-11: "a rollout-status wait means the container started, not nginx is
# listening -- the positive control got curl code 000 immediately after a
# successful rollout". It wraps its own control in wait_for for exactly that.
# This probe did a single curl and VOIDed on 000, twice, costing two cluster
# windows for a cause a sibling probe had already diagnosed and fixed.
#
# A pod being Ready is not the Service being routable: kube-proxy programs the
# rules after the endpoint appears. The third run's own diagnostic proved it --
# a LATER request to :8080 was logged by the mock while the earlier :9090 ones
# got 000, so the mock, the Service and DNS were all fine by then.
# shellcheck disable=SC2329  # invoked by NAME through wait_for
_po_control_answers() {
  C_PRESENT=$(_curl_in_cluster probe-po-present "http://${HOST}:9090/v2.1/flavors" -H 'X-Auth-Token: dummy-not-a-credential')
  C_ABSENT=$(_curl_in_cluster  probe-po-absent  "http://${HOST}:9090/v2.1/flavors")
  [ "${C_PRESENT:-000}" = "200" ] && [ "${C_ABSENT:-000}" = "200" ]
}
wait_for "the mock's :9090 to answer the probe's own requests" 15 6 _po_control_answers >/dev/null 2>&1 || true
if [ "${C_PRESENT:-000}" != "200" ] || [ "${C_ABSENT:-000}" != "200" ]; then
  # A VOID THAT CANNOT BE DIAGNOSED COSTS ANOTHER CLUSTER. The first version said
  # "a connectivity or Service failure" and stopped, which is true and useless:
  # it left me guessing between a broken Service, a DNS failure, a port that is
  # not routing, and a curl pod that never ran. Every one of those is cheap to
  # distinguish from inside the cluster and impossible to distinguish afterwards,
  # because the cluster is deleted before anyone reads the output.
  #
  # THE COMPARISON IS THE POINT: :8080 is known to be bound (nginx binds all
  # three ports, verified in Docker), so if :8080 answers and :9090 does not the
  # fault is per-PORT -- the Service's hop-target entry -- while if neither
  # answers it is the Service, its selector, or DNS.
  say "collecting evidence before giving up, because the cluster is about to be deleted:"
  # MY FIRST DIAGNOSTIC CHANGED TWO VARIABLES AT ONCE and I am not repeating
  # that: it hit a different PORT at a later TIME, so a success there could mean
  # "8080 routes and 9090 does not" OR "both route now and did not before". The
  # third run hit exactly that ambiguity. So :9090 is retried here too, and the
  # pair separates the two readings:
  #   :9090 now OK  -> it was TIMING; the control above gave up too early
  #   :9090 still bad, :8080 OK -> genuinely PER-PORT; read the endpoints below
  say "  --- :9090 retried, and :8080 for comparison ---"
  say "  :9090 retry -> $(_curl_in_cluster probe-po-diag9 "http://${HOST}:9090/v2.1/flavors" || echo 'no status')"
  say "  :8080       -> $(_curl_in_cluster probe-po-diag8 "http://${HOST}:8080/" || echo 'no status')"
  say "  --- Service, its ports, and its endpoints ---"
  K -n "$NS" get svc mock-portonly -o wide 2>&1 | sed 's/^/      /'
  K -n "$NS" get endpoints mock-portonly -o yaml 2>&1 | grep -E 'name:|port:|ip:' | sed 's/^/      /'
  say "  --- the pod: phase, readiness and restarts ---"
  K -n "$NS" get pods -l app=mock-portonly -o wide 2>&1 | sed 's/^/      /'
  say "  --- nginx's own last words ---"
  K -n "$NS" logs deploy/mock-portonly --tail=15 2>&1 | sed 's/^/      /'
  void "the probe's own requests to :9090 did not both get 200 (with-token=${C_PRESENT:-none}
    without-token=${C_ABSENT:-none}). That is a connectivity or Service failure, not a
    finding about credentials, and every verdict below would have been about this.
    The evidence above distinguishes the four causes: compare the :8080 row against
    the :9090 result, and read the endpoints list for a missing port."
fi
sleep 2
RECS=$(read_records)
CTL_P=$(printf '%s\n' "$RECS" | awk -v ua="$PROBE_UA" '$1=="9090" && $3=="PRESENT" && $4==ua' | grep -c .)
CTL_A=$(printf '%s\n' "$RECS" | awk -v ua="$PROBE_UA" '$1=="9090" && $3=="ABSENT"  && $4==ua' | grep -c .)
if [ "${CTL_P:-0}" -lt 1 ] || [ "${CTL_A:-0}" -lt 1 ]; then
  say "last 6 records: $(printf '%s\n' "$RECS" | tail -6 | tr '\n' '|')"
  void "the instrument cannot report both states on :9090 (PRESENT=$CTL_P ABSENT=$CTL_A from
    the probe's OWN requests). A field that cannot say PRESENT cannot distinguish a
    credential that travelled from one that did not, and always-ABSENT reads as
    'it did not travel'."
fi
ok "instrument proven on the probe's own traffic: :9090 logged PRESENT and ABSENT"
BASE=$(n_records "$RECS")
say "records before the arms: $BASE (the probe's own; every count below is a delta)"

# ---- the chart, staged once and re-pointed per arm --------------------------
STAGE=$(mktemp -d)
cp -R chart "$STAGE/chart"
# CHART_VERSION must be stamped or helm refuses the chart outright.
find "$STAGE/chart" -name Chart.yaml -exec sed -i.bak "s/CHART_VERSION/0.0.0-probe/g" {} + 2>/dev/null
find "$STAGE/chart" -name '*.bak' -delete 2>/dev/null
ok "staged a stamped chart at $STAGE/chart"

point_chart_at() { # point_chart_at <url>
  H upgrade --install nova "$STAGE/chart" -n "$NS" --set nova.url="$1" --wait --timeout 8m \
    > /tmp/probe-portonly-helm.log 2>&1 \
    || { sed 's/^/    /' /tmp/probe-portonly-helm.log >&2
         void "helm upgrade to $1 failed, so nothing was measured at that endpoint."; }
}

apply_cr() {
  local f; f=$(mktemp)
  cat > "$f" <<YAML
apiVersion: v1
kind: Secret
metadata: { name: ${SEC}, namespace: ${NS} }
stringData: { token: dummy-not-a-credential }
---
apiVersion: ${GROUP}/v1alpha1
kind: ComputeFlavorConfiguration
metadata: { name: ${CFG}, namespace: ${NS} }
spec:
  authentication:
    apiKey:
      tokenRef: { name: ${SEC}, namespace: ${NS}, key: token }
      header: X-Auth-Token
---
apiVersion: ${GROUP}/v2-1
kind: ComputeFlavor
metadata: { name: ${CR}, namespace: ${NS} }
spec:
  configurationRef: { name: ${CFG}, namespace: ${NS} }
  flavor: { name: ${CR}, ram: 2048, vcpus: 1, disk: 20 }
YAML
  # EXIT STATUS READ. An unchecked apply is how a previous probe in this repo
  # reported success over two BadRequests.
  if ! K -n "$NS" apply -f "$f" > /tmp/probe-portonly-apply.log 2>&1; then
    sed 's/^/    /' /tmp/probe-portonly-apply.log >&2
    echo "    --- the manifest it refused ---" >&2
    sed 's/^/    /' "$f" >&2
    void "the probe's own CRs did not apply, so no arm was measured."
  fi
  rm -f "$f"
}

read_rv()   { K -n "$NS" get "computeflavors.${GROUP}" "$CR" -o jsonpath='{.metadata.resourceVersion}' 2>/dev/null; }
read_conds(){ K -n "$NS" get "computeflavors.${GROUP}" "$CR" \
                -o jsonpath='{range .status.conditions[*]}type={.type} status={.status} reason={.reason} message={.message}{"\n"}{end}' 2>/dev/null; }

# classify <port> <path> -- how many CONTROLLER requests landed there, by state
ctl_count() { # ctl_count <records> <port> <path> <tokenstate>
  printf '%s\n' "$1" | awk -v ua="$PROBE_UA" -v p="$2" -v pa="$3" -v t="$4" \
    '$4!=ua && $1==p && $2==pa && $3==t' | grep -c .
}

# =============================================================================
bold "ARM A -- the chart points at :8080, whose /v2.1/flavors hops to :9090"
# =============================================================================
point_chart_at "http://${HOST}:8080/v2.1"
apply_cr
# shellcheck disable=SC2329  # invoked by NAME through wait_for
_has_conds() { [ -n "$(read_conds | tr -d '[:space:]')" ]; }
wait_for "the CR to carry a status condition at the :8080 endpoint" 40 6 _has_conds \
  || void "the CR never produced a condition within 240s. That is a TIMEOUT, not a
    finding that the credential did not travel."
read_conds | sed 's/^/    /'
RV_A=$(read_rv)
sleep 3
RECS_A=$(read_records)
A_SRC=$(ctl_count "$RECS_A" 8080 /v2.1/flavors PRESENT)
A_9090_P=$(ctl_count "$RECS_A" 9090 /v2.1/flavors PRESENT)
A_9090_A=$(ctl_count "$RECS_A" 9090 /v2.1/flavors ABSENT)

# THE SUBJECT MUST HAVE ACTED. If the controller never reached :8080 there was
# no hop to refuse, and "nothing arrived on :9090" is true for a reason that has
# nothing to do with the redirect policy.
if [ "${A_SRC:-0}" -lt 1 ]; then
  say "controller records seen: $(printf '%s\n' "$RECS_A" | awk -v ua="$PROBE_UA" '$4!=ua' | tail -5 | tr '\n' '|')"
  void "no CONTROLLER request with a credential reached :8080, so no redirect was ever
    offered to it. The arm measured nothing about the hop; a condition on the CR
    was produced by something upstream of the request."
fi
if [ "${A_9090_P:-0}" -ge 1 ]; then
  ARM_A=arrived
  bad "FALSIFIER FIRED: the credential ARRIVED on :9090 ($A_9090_P request(s) PRESENT)."
elif [ "${A_9090_A:-0}" -ge 1 ]; then
  ARM_A=stripped
  ok "the hop was FOLLOWED but the credential was stripped ($A_9090_A ABSENT on :9090)"
else
  ARM_A=refused
  ok "no controller request reached :9090 at all: the hop was REFUSED"
fi
say "ARM A = $ARM_A   (:8080 PRESENT=$A_SRC  :9090 PRESENT=$A_9090_P ABSENT=$A_9090_A)"

# =============================================================================
bold "ARM B -- the chart points at the host with NO PORT, which must default to 80"
# =============================================================================
# Same CR object, re-pointed chart, exactly as probe-401 does it: two CRs would
# differ by more than the endpoint and the comparison would prove nothing.
point_chart_at "http://${HOST}/v2.1"
# shellcheck disable=SC2329  # invoked by NAME through wait_for
_rv_advanced() { local rv; rv=$(read_rv); [ -n "$rv" ] && [ "$rv" != "$RV_A" ]; }
wait_for "the CR's resourceVersion to advance past arm A's" 40 6 _rv_advanced \
  || void "the CR's resourceVersion never moved after the chart was re-pointed, so arm B
    may be a re-read of arm A's state rather than a measurement at the new endpoint."
read_conds | sed 's/^/    /'
sleep 3
RECS_B=$(read_records)
B_SRC=$(ctl_count "$RECS_B" 8081 /v2.1/flavors PRESENT)
B_LAND_P=$(ctl_count "$RECS_B" 8081 /v2.1/flavors-landed PRESENT)
B_LAND_A=$(ctl_count "$RECS_B" 8081 /v2.1/flavors-landed ABSENT)
if [ "${B_SRC:-0}" -lt 1 ]; then
  say "controller records seen: $(printf '%s\n' "$RECS_B" | awk -v ua="$PROBE_UA" '$4!=ua' | tail -5 | tr '\n' '|')"
  void "no CONTROLLER request with a credential reached the default-port block, so sub-arm
    B offered no hop either. Without B, arm A cannot be told apart from a policy
    that refuses every redirect or enforces nothing."
fi
if [ "${B_LAND_P:-0}" -ge 1 ]; then
  ARM_B=permitted
  ok "the default-port hop was PERMITTED and the credential travelled ($B_LAND_P PRESENT)"
elif [ "${B_LAND_A:-0}" -ge 1 ]; then
  ARM_B=permitted-stripped
  ok "the default-port hop was FOLLOWED but the credential was stripped"
else
  ARM_B=refused
  bad "the default-port hop was REFUSED. host:80 and host must compare equal, and
    that needs BOTH defaulting sites to agree: PermitsURL defaults the REQUEST's
    empty port to 80 before entry.matches, and parseEntry defaults the ENTRY's
    empty port the same way. A refusal here means one of those two is missing --
    and if it is the entry side, then a port-less allowlist entry matches nothing
    at all, which is the common way of writing one. Independent of arm A either
    way, which is why this sub-arm exists."
fi
say "ARM B = $ARM_B   (:8081 source PRESENT=$B_SRC  landed PRESENT=$B_LAND_P ABSENT=$B_LAND_A)"

# =============================================================================
bold "CALLER COLUMN -- an unmarked caller of the probe's own would be read as the controller"
# =============================================================================
# ironic's addition, and it closes the hole marking alone cannot: the thing you
# forget to mark is by definition the thing you will not think to mark. So the
# distinct non-probe callers are PRINTED and counted, rather than trusted to be
# one.
ALL=$(printf '%s\n%s\n' "$RECS_A" "$RECS_B")
printf '    %-6s %-26s %-9s %s\n' PORT PATH TOKEN CALLER
printf '%s\n' "$ALL" | awk -v ua="$PROBE_UA" '$4!=ua {print $1, $2, $3, $4}' | sort -u |
  while read -r p pa t u; do printf '    %-6s %-26s %-9s %s\n' "$p" "$pa" "$t" "$u"; done
N_CALLERS=$(printf '%s\n' "$ALL" | awk -v ua="$PROBE_UA" '$4!=ua {print $4}' | sort -u | grep -c .)
if [ "${N_CALLERS:-0}" -eq 1 ]; then
  ok "exactly one non-probe caller, so every count above is attributable to it"
else
  bad "$N_CALLERS distinct non-probe callers reached the mock. The counts above mix
    them, and an unmarked request of the probe's own would appear here as the
    controller's. Read the table before the verdict."
fi

# =============================================================================
bold "VERDICT -- the PAIR, because one arm cannot separate three worlds"
# =============================================================================
VERDICT=$(classify_portonly "$ARM_A" "$ARM_B")
echo "  VERDICT=$VERDICT"
case "$VERDICT" in
  port-aware)
    ok "REFUSED the differing-port hop and PERMITTED the default-port hop."
    say "This is the oasgen-provider prediction, and it could have said otherwise:"
    say "the falsifier (the token arriving on :9090) was observable and did not fire,"
    say "and arm B shows the policy was enforcing rather than refusing everything." ;;
  falsified)
    bad "The credential travelled across a port-only hop. Either HostPolicy is not
    installed on this client or entry.matches is not reached. This is the named
    falsifier and it belongs upstream, not in this chart's docs." ;;
  refuses-everything)
    bad "BOTH hops were refused. A policy that refuses the default-port hop is not
    port-aware, it is redirect-blind, and arm A's refusal says nothing about port
    comparison. Two defects are consistent with it: the request-side empty-port
    defaulting in PermitsURL, or the entry-side one in parseEntry." ;;
  stripped-not-refused)
    say "The hop was FOLLOWED and the credential removed. That is Go's own header
    handling rather than the policy refusing, so the endpoint allowlist was never
    the thing protecting it here -- worth telling oasgen, because their prediction
    names CheckRedirect specifically." ;;
  *)
    bad "ARM_A=$ARM_A ARM_B=$ARM_B is a combination this probe has no reading for.
    Reporting it rather than forcing it into the nearest verdict." ;;
esac
say "ARM_A=$ARM_A ARM_B=$ARM_B callers=$N_CALLERS"

bold "cleanup -- the probe's objects only; the CLUSTER IS LEFT RUNNING"
K -n "$NS" delete "computeflavors.${GROUP}" "$CR" --ignore-not-found --timeout=60s >/dev/null 2>&1
K -n "$NS" delete "computeflavorconfigurations.${GROUP}" "$CFG" --ignore-not-found >/dev/null 2>&1
K -n "$NS" delete secret "$SEC" --ignore-not-found >/dev/null 2>&1
K -n "$NS" delete deploy/mock-portonly svc/mock-portonly cm/mock-portonly-conf --ignore-not-found >/dev/null 2>&1
rm -rf "$STAGE"
if [ "$DRY" = "1" ]; then
  rm -f "$DRY_RVFILE"
  bold "DRY RUN COMPLETE"
  say "Every line of the cluster arm executed under set -u, including both arms,"
  say "the caller column and the verdict table. That is ALL this proves."
  say "It did NOT contact a cluster, install a chart, or measure a redirect, and"
  say "the records were chosen to drive the port-aware path -- one path of four."
  exit 0
fi
[ "$FAILS" -eq 0 ] || exit 1
exit 0
