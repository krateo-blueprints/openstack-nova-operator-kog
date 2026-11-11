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
#   --validate-mock   run the mock's SHIPPED BYTES in Docker and assert the
#                     measuring device works. Needs Docker, not a cluster, and
#                     runs in CI.
#   <kubeconfig> <context> [ns]   the full arm. Needs a cluster.
set -uo pipefail
export LC_ALL=C

# SCRIPT-SCOPED ON PURPOSE. These were `local` inside validate_mock, and the
# EXIT trap that removes the container fires AFTER that function has returned --
# so under `set -u` the trap itself died with "cid: unbound variable" and LEFT
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
log_format portonly '$remote_addr "$request" host=$host port=$server_port x_auth_token=$authhdr';

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
MOCKEOF
}

validate_mock() {
  command -v docker >/dev/null 2>&1 || void "docker is not available; --validate-mock runs the mock."
  docker info >/dev/null 2>&1 || void "docker is installed but not responding, so the mock was never started. Not a pass."
  bold "--validate-mock: the measuring device, from the bytes this probe ships"
  local conf p8 p9 logs
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
  MOCK_CID=$(docker run -d -p 0:8080 -p 0:9090 \
        -v "$MOCK_TMP/default.conf:/etc/nginx/conf.d/default.conf:ro" \
        nginxinc/nginx-unprivileged:1.27-alpine 2>/dev/null)
  [ -n "$MOCK_CID" ] || void "could not start the mock; nginx may have rejected the two-server config."
  p8=$(docker port "$MOCK_CID" 8080/tcp 2>/dev/null | head -1 | sed 's/.*://')
  p9=$(docker port "$MOCK_CID" 9090/tcp 2>/dev/null | head -1 | sed 's/.*://')
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
  # NOT `A && ok || bad`: in that form `bad` also runs if `ok` itself fails,
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

  bold "RESULT"
  if [ "$FAILS" -ne 0 ]; then
    printf '\033[31m  %s assertion(s) failed: the port-only arm would measure with a broken instrument.\033[0m\n' "$FAILS"
    return 1
  fi
  ok "the mock performs a port-only hop and its :9090 end can say both PRESENT and ABSENT."
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

echo "usage: probe-portonly.sh --validate-mock" >&2
echo "       (the cluster arm is not written yet: it needs a kind cluster, and" >&2
echo "        the mock above is validated first so the window is not spent" >&2
echo "        debugging the instrument. See PR for the prediction and falsifier.)" >&2
exit 2
