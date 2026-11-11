#!/usr/bin/env bash
# Does probe-redirect.sh's GROUNDTRUTH mock actually distinguish a credentialed
# request from an uncredentialed one?
#
# WHY THIS ONE ABOVE ALL THE OTHERS. That mock is the instrument behind this
# repo's only security-relevant published claim: docs/configuration.md states
# that below oasgen 0.50.0 a 302 from the configured Nova endpoint sent the
# Keystone token to the redirect target, "measured in a kind cluster on 0.49.0,
# with the receiving host reporting what it got: x_auth_token=PRESENT".
#
# That sentence is true only if the receiving host can say PRESENT *and* can say
# ABSENT. Its nginx config derives the field from
#
#     map $http_x_auth_token $authhdr { "" "ABSENT"; default "PRESENT"; }
#
# so a misspelled variable, a map in the wrong context, or an escaping mistake
# yields ALWAYS-ABSENT -- and always-ABSENT reads as "the credential did not
# leak". The failure direction is reassurance on a security finding, which is
# the worst direction available. Nothing had ever run these bytes outside the
# probe itself.
#
# BOTH ARMS ARE REQUIRED, and the pair is the point. An always-ABSENT mock
# passes the uncredentialed arm and fails only the credentialed one; an
# always-PRESENT mock does the reverse. So the two arms must disagree with each
# other, and that is asserted separately from either arm's own result.
#
# EXTRACTION DISCIPLINE, AND IT IS THE OPPOSITE OF probe-401's. That probe's
# ConfigMap heredoc is UNQUOTED, so its bytes must be re-expanded through the
# shell to reproduce what it sends. THIS probe's heredoc is QUOTED (`<<'EOF'`),
# so its bytes are literal and running them through the shell would EAT
# $http_x_auth_token -- manufacturing exactly the always-ABSENT defect being
# tested for. Same mistake, opposite direction. So this extracts verbatim and
# asserts the nginx variables survived.
#
# NEEDS DOCKER, NOT A CLUSTER.
set -uo pipefail
export LC_ALL=C

HERE="$(cd "$(dirname "$0")" && pwd)"
PROBE="$HERE/probe-redirect.sh"

ok()   { printf '\033[32m  OK: %s\033[0m\n' "$1"; }
bad()  { printf '\033[31m  FAIL: %s\033[0m\n' "$1"; FAILS=$((FAILS+1)); }
say()  { printf '  %s\n' "$1"; }
void() { printf '\033[33m  VOID: %s\033[0m\n' "$1"; exit 9; }
FAILS=0

[ -r "$PROBE" ] || void "cannot read $PROBE, so there are no shipped bytes to validate."
command -v docker >/dev/null 2>&1 || void "docker is not available; this validates the mock by running it."
docker info >/dev/null 2>&1 || void "docker is installed but not responding, so the mock was never started. Not a pass."

printf '\n\033[1m== EXTRACT the groundtruth config, verbatim\033[0m\n'
# The groundtruth ConfigMap is the one whose data contains the `groundtruth`
# log_format. Take the block under its `default.conf: |` and strip the YAML
# indentation. No shell expansion: see the note above.
CONF=$(awk '
  /name: redirect-target-conf/ { want=1 }
  want && /default\.conf: \|/  { grab=1; next }
  grab && /^EOF$/              { exit }
  grab                         { sub(/^    /, ""); print }
' "$PROBE")
LINES=$(printf '%s\n' "$CONF" | grep -c .)
[ "${LINES:-0}" -ge 8 ] || void "extracted only ${LINES:-0} non-empty lines for the groundtruth config. The extraction failed, so a pass below would describe an empty config."
# shellcheck disable=SC2016  # the literal $http_x_auth_token IS the needle:
# if the shell had expanded it, that is precisely the defect being tested for.
printf '%s\n' "$CONF" | grep -qF 'map $http_x_auth_token $authhdr' || void "the extracted config has no literal 'map \$http_x_auth_token \$authhdr'. Either the probe changed shape or something expanded the nginx variables -- and an expanded config would start nginx and log ABSENT forever."
# shellcheck disable=SC2016  # same: the unexpanded form is the needle
printf '%s\n' "$CONF" | grep -qF 'x_auth_token=$authhdr' || void "the extracted log_format does not reference \$authhdr literally."
ok "extracted $LINES lines, with the map and log_format variables intact"

TMP=$(mktemp -d)
CID=""
# shellcheck disable=SC2329  # invoked by the EXIT trap below
cleanup() { [ -n "$CID" ] && docker rm -f "$CID" >/dev/null 2>&1; rm -rf "$TMP"; }
trap cleanup EXIT
# The probe substitutes __FAKE_ID__ at apply time; any value serves here.
printf '%s\n' "$CONF" | sed 's/__FAKE_ID__/validate-fake-id/g' > "$TMP/default.conf"

printf '\n\033[1m== RUN the shipped bytes in plain Docker\033[0m\n'
CID=$(docker run -d -p 0:8080 -v "$TMP/default.conf:/etc/nginx/conf.d/default.conf:ro" \
  nginxinc/nginx-unprivileged:1.27-alpine 2>/dev/null)
[ -n "$CID" ] || void "could not start the mock container. If nginx rejected the config, the map may be in a context conf.d is not included at -- which the probe's own comment claims it is."
PORT=$(docker port "$CID" 8080/tcp 2>/dev/null | head -1 | sed 's/.*://')
[ -n "$PORT" ] || void "the container started but published no port, so the mock was never reachable."
for _ in $(seq 1 30); do curl -s -o /dev/null -m 2 "http://127.0.0.1:$PORT/" && break; sleep 1; done
# nginx exiting on a bad config looks like a slow start; check it is still up.
[ "$(docker inspect -f '{{.State.Running}}' "$CID" 2>/dev/null)" = "true" ] || {
  say "container logs: $(docker logs "$CID" 2>&1 | tail -3 | tr '\n' '|')"
  void "the mock container is not running, so nginx rejected the shipped config. That is a finding about the config, but it is not a measurement of the map."
}
say "mock listening on 127.0.0.1:$PORT"

printf '\n\033[1m== ARM 1 -- a request WITH X-Auth-Token must log PRESENT\033[0m\n'
curl -s -o /dev/null -m 5 -H 'X-Auth-Token: dummy-not-a-credential' "http://127.0.0.1:$PORT/v2.1/flavors" || true
sleep 1
WITH=$(docker logs "$CID" 2>&1 | grep -c 'x_auth_token=PRESENT')
if [ "$WITH" -ge 1 ]; then ok "logged x_auth_token=PRESENT for a credentialed request"
else bad "a request carrying X-Auth-Token did NOT log PRESENT. The instrument behind the 0.50.0 claim cannot see a credential, so it would report no leak whatever happened."; fi

printf '\n\033[1m== ARM 2 -- a request WITHOUT it must log ABSENT\033[0m\n'
curl -s -o /dev/null -m 5 "http://127.0.0.1:$PORT/v2.1/flavors" || true
sleep 1
ABSENT=$(docker logs "$CID" 2>&1 | grep -c 'x_auth_token=ABSENT')
if [ "$ABSENT" -ge 1 ]; then ok "logged x_auth_token=ABSENT for an uncredentialed request"
else bad "a request without X-Auth-Token did NOT log ABSENT, so the field cannot express absence."; fi

printf '\n\033[1m== ARM 3 -- the two arms must DISAGREE\033[0m\n'
# An always-PRESENT mock passes arm 1; an always-ABSENT mock passes arm 2.
# Only a mock that produces BOTH values can distinguish a leak from no leak.
if [ "$WITH" -ge 1 ] && [ "$ABSENT" -ge 1 ]; then
  ok "the same mock produced both PRESENT and ABSENT, so the field discriminates"
else
  bad "the mock produced only one of the two values (PRESENT=$WITH ABSENT=$ABSENT). A constant field cannot distinguish a leaked credential from an absent one, and always-ABSENT reads as 'no leak'."
fi

printf '\n\033[1m== ARM 4 -- the log must never contain the token VALUE\033[0m\n'
# The map exists so a verdict can never print a credential-shaped string. If the
# raw value ever appears, that design has silently stopped holding.
if docker logs "$CID" 2>&1 | grep -qF 'dummy-not-a-credential'; then
  bad "the access log contains the token VALUE. The map is meant to keep the raw credential out of any output."
else
  ok "the token value never appears in the log, only PRESENT/ABSENT"
fi

printf '\n\033[1m== RESULT\033[0m\n'
if [ "$FAILS" -ne 0 ]; then
  printf '\033[31m  %s assertion(s) failed: the instrument behind the 0.50.0 claim does not behave as that claim requires.\033[0m\n' "$FAILS"
  exit 1
fi
ok "the groundtruth mock distinguishes a credentialed request from an uncredentialed one, and never logs the value."
exit 0
