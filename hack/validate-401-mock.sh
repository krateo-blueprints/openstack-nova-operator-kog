#!/usr/bin/env bash
# Does probe-401.sh's mock, AS SHIPPED, do what the probe assumes of it?
#
# WHY. probe-401.sh embeds an nginx config in a heredoc and then draws
# conclusions from two of its properties: that it answers every request with the
# configured status, and that its ACCESS LOG records that status -- the probe's
# positive control cross-checks curl against that log, so a mock that served
# correctly but logged nothing would turn the control into a permanent VOID,
# and one that logged a status it had not served would turn it into a permanent
# pass. Neither failure is visible from the probe's own output.
#
# Nothing in this repo had ever asserted that the shipped bytes behave that way.
# Credit to openstack-ironic-operator-kog, who validate their own mock against
# the shipped bytes and said it is the only reason they trust the measuring
# device. This is that, for ours.
#
# THE BYTES ARE EXTRACTED, NEVER RETYPED. A copy of the config in this file
# would test the copy: it would pass while the probe shipped something else,
# which is the whole class of defect this repo keeps finding. So the config is
# pulled out of probe-401.sh by the same heredoc delimiter the probe uses, and
# the extraction itself is asserted to have found something.
#
# NEEDS DOCKER, NOT A CLUSTER.
set -uo pipefail
export LC_ALL=C

HERE="$(cd "$(dirname "$0")" && pwd)"
PROBE="$HERE/probe-401.sh"
STATUS="${1:-401}"

ok()   { printf '\033[32m  OK: %s\033[0m\n' "$1"; }
bad()  { printf '\033[31m  FAIL: %s\033[0m\n' "$1"; FAILS=$((FAILS+1)); }
say()  { printf '  %s\n' "$1"; }
void() { printf '\033[33m  VOID: %s\033[0m\n' "$1"; exit 9; }
FAILS=0

[ -r "$PROBE" ] || void "cannot read $PROBE, so there are no shipped bytes to validate."
command -v docker >/dev/null 2>&1 || void "docker is not available; this validates the mock by running it."
docker info >/dev/null 2>&1 || void "docker is installed but not responding, so the mock was never started. Not a pass."

printf '\n\033[1m== EXTRACT the nginx config from the shipped probe\033[0m\n'
# The ConfigMap heredoc is the one containing `default.conf:`. Take the lines
# between that key and the heredoc terminator, then strip the YAML block
# indentation. ${REJECT_STATUS} is expanded the same way the probe expands it,
# because the probe's heredoc is unquoted.
# THE EXTRACTION MUST GO THROUGH THE SHELL, and the first version of this did
# not. It un-escaped `\$` with sed and then expanded only ${REJECT_STATUS},
# which made an escaped `\$remote_addr` and a bare `$remote_addr` extract
# IDENTICALLY -- so it could not detect the one failure the probe's own comment
# warns about. Mutation-tested and it passed the mutant, which is how I found
# it: I had validated a TRANSFORMATION of the shipped bytes rather than the
# bytes.
#
# The probe sends its heredoc through an unquoted shell heredoc, so `\$x` is a
# literal `$x` and a bare `$x` is expanded (to empty, for nginx's own
# variables, which are not shell variables). Reproducing that needs the shell,
# so the raw body is re-fed to a heredoc with only REJECT_STATUS set -- exactly
# the probe's environment.
RAW=$(awk '
  /default\.conf: \|/ { grab=1; next }
  grab && /^EOF$/      { exit }
  grab                 { sub(/^    /, ""); print }
' "$PROBE")
[ -n "$RAW" ] || void "extracted nothing between 'default.conf: |' and the heredoc terminator in $PROBE."
CONF=$(REJECT_STATUS="$STATUS" bash -c 'cat <<SHIPPEDEOF
'"$RAW"'
SHIPPEDEOF
')
LINES=$(printf '%s\n' "$CONF" | grep -c . )
[ "${LINES:-0}" -ge 6 ] || void "extracted only ${LINES:-0} non-empty config lines from $PROBE. The extraction failed, so a pass below would be about an empty config."
printf '%s\n' "$CONF" | grep -qF "return $STATUS" || void "the extracted config contains no 'return $STATUS'. Either the probe changed shape or REJECT_STATUS did not expand -- and an unexpanded config would still start nginx, just serving the wrong thing."
# shellcheck disable=SC2016  # the literal $remote_addr is exactly the needle:
# if the shell had expanded it, that is the defect being tested for.
printf '%s\n' "$CONF" | grep -qF '$remote_addr' || void "the extracted config has no literal \$remote_addr. The shell ate it, which is the escaping trap -- nginx would start and log blank fields. This VOIDs rather than failing an assertion because it means the config under test is not the config the probe intends."
ok "extracted $LINES config lines, with 'return $STATUS' and nginx's own \$variables surviving as literals"

TMP=$(mktemp -d)
# shellcheck disable=SC2329  # invoked by the EXIT trap set below
cleanup() {
  docker rm -f "$CID" >/dev/null 2>&1 || true
  rm -rf "$TMP"
}
CID=""
trap cleanup EXIT
printf '%s\n' "$CONF" > "$TMP/default.conf"

printf '\n\033[1m== RUN the shipped bytes in plain Docker\033[0m\n'
CID=$(docker run -d -p 0:8080 -v "$TMP/default.conf:/etc/nginx/conf.d/default.conf:ro" \
  nginxinc/nginx-unprivileged:1.27-alpine 2>/dev/null)
[ -n "$CID" ] || void "could not start the mock container, so nothing below was measured."
PORT=$(docker port "$CID" 8080/tcp 2>/dev/null | head -1 | sed 's/.*://')
[ -n "$PORT" ] || void "the container started but no port was published, so the mock was never reachable."
say "mock listening on 127.0.0.1:$PORT"

for _ in $(seq 1 30); do
  curl -s -o /dev/null -m 2 "http://127.0.0.1:$PORT/" && break
  sleep 1
done

printf '\n\033[1m== ASSERTION 1 -- it answers with the configured status\033[0m\n'
GOT=$(curl -s -o /dev/null -w '%{http_code}' -m 5 "http://127.0.0.1:$PORT/v2.1/flavors" 2>/dev/null)
if [ "$GOT" = "$STATUS" ]; then ok "served $GOT for /v2.1/flavors"
else bad "served '$GOT', expected '$STATUS'. The probe's whole premise is that this mock refuses everything with that code."; fi

printf '\n\033[1m== ASSERTION 2 -- the ACCESS LOG records that status\033[0m\n'
# This is the property probe-401.sh cross-checks curl against. A mock that
# serves correctly but logs nothing makes that control VOID forever.
LOG=$(docker logs "$CID" 2>&1)
# RE-ANCHORED when log_format gained $http_user_agent. The old pattern put the
# status at end-of-line; a quoted UA field now follows it, so that pattern could
# never match again and this assertion would have failed for a reason that has
# nothing to do with the mock.
if printf '%s\n' "$LOG" | grep -qE "\" $STATUS \""; then
  ok "access log carries a line with the served status followed by the UA field"
  say "$(printf '%s\n' "$LOG" | grep -E "\" $STATUS \"" | tail -1)"
else
  bad "no access-log line ends with the served status. probe-401.sh's positive control greps for exactly that, so it would VOID on every run."
  say "last 3 log lines: $(printf '%s\n' "$LOG" | tail -3 | tr '\n' '|')"
fi

printf '\n\033[1m== ASSERTION 3 -- the log fields are populated, not empty\033[0m\n'
# The heredoc-quoting trap the probe's own comment warns about: get the escaping
# wrong and nginx starts fine but logs empty fields. That is invisible to
# assertion 1 and would quietly break the cross-check.
if printf '%s\n' "$LOG" | grep -qE '^[0-9].*"(GET|HEAD|POST) [^"]+" [0-9]{3} "[^"]*"$'; then
  ok "a log line has a populated address, request, status AND a UA field"
else
  bad "no log line has all three fields populated -- this is the \$-escaping trap the probe's comment warns about: nginx accepts the config and logs blanks."
  say "last 3 log lines: $(printf '%s\n' "$LOG" | tail -3 | tr '\n' '|')"
fi

printf '\n\033[1m== ASSERTION 4 -- absence is recorded as absence\033[0m\n'
# The mock must not invent traffic. A request count that grows without requests
# would make the probe's per-arm attribution meaningless.
BEFORE=$(docker logs "$CID" 2>&1 | grep -cE '" [0-9]{3} "')
sleep 3
AFTER=$(docker logs "$CID" 2>&1 | grep -cE '" [0-9]{3} "')
if [ "$BEFORE" = "$AFTER" ]; then ok "no requests logged during 3s of silence (count held at $BEFORE)"
else bad "the log grew from $BEFORE to $AFTER with no requests sent. The mock invents traffic, so any per-arm count is unusable."; fi

printf '\n\033[1m== ASSERTION 5 -- the shipped ATTRIBUTION PARSER, on real nginx output\033[0m\n'
# WHY HERE AND NOT ONLY IN --self-test. probe-401.sh --self-test feeds the parser
# log lines THIS REPO TYPED. That tests the parser against our idea of nginx's
# format. If the real format differs by a field, a space or a quote, the
# self-test still passes and the probe reports "0 controller requests" about a
# healthy cluster. So the same bytes are extracted and run against output a real
# nginx actually wrote.
PARSER_SRC=$(awk '/^# ---8<--- ATTRIBUTE_AWK$/{f=1;next} /^# ---8<--- END ATTRIBUTE_AWK$/{f=0} f' "$PROBE")
if ! printf '%s' "$PARSER_SRC" | grep -q 'BEGIN { FS'; then
  bad "could not extract ATTRIBUTE_AWK from $PROBE between its markers. Nothing was
    validated: a parser that cannot be found is not a parser that passed."
else
  eval "$PARSER_SRC"
  _recs()  { docker logs "$CID" 2>&1 | awk "$ATTRIBUTE_AWK"; }
  # ONE counting method, used for both ends of the delta. The first version used
  # `wc -l` before and `grep -c .` after; they agree on this input, but a delta
  # computed with two different instruments is a delta that can be off by one
  # for reasons unrelated to what it claims to measure.
  _nrecs() { printf '%s\n' "$1" | grep -c . ; }
  N_BEFORE=$(_nrecs "$(_recs)")
  # Two requests, two DIFFERENT User-Agents, so the separation the probe relies
  # on is exercised on real bytes and in both directions.
  curl -s -o /dev/null -m 5 -A 'probe-401-control'   "http://127.0.0.1:$PORT/v2.1/flavors"   >/dev/null 2>&1
  curl -s -o /dev/null -m 5 -A 'Go-http-client/1.1'  "http://127.0.0.1:$PORT/v2.1/servers"   >/dev/null 2>&1
  RECS=$(_recs); N_AFTER=$(_nrecs "$RECS")
  D=$((N_AFTER - N_BEFORE))
  if [ "$D" -ne 2 ]; then
    bad "sent exactly 2 requests but the parser's record count moved by $D ($N_BEFORE -> $N_AFTER).
      Either nginx log lines are not being parsed, or non-request lines are being
      counted as requests. Both make the probe's controller-request count wrong."
    say "last 4 records: $(printf '%s\n' "$RECS" | tail -4 | tr '\n' '|')"
  else
    ok "2 requests sent, exactly 2 records parsed (no noise line counted as a request)"
  fi
  OWN=$(printf '%s\n' "$RECS" | awk '$4 == "probe-401-control"' | wc -l | tr -d ' ')
  OTH=$(printf '%s\n' "$RECS" | awk '$4 == "Go-http-client/1.1"' | wc -l | tr -d ' ')
  if [ "${OWN:-0}" -ge 1 ] && [ "${OTH:-0}" -ge 1 ]; then
    ok "separation works on real bytes: $OWN marked as the probe's, $OTH as another client's"
  else
    bad "the parser did not separate the two User-Agents on real nginx output
      (own=$OWN other=$OTH, want >=1 each). A filter that matches nothing and one
      that matches everything both leave one of these at zero, and probe-401's
      'the controller never reached the mock' verdict depends on neither."
    say "last 4 records: $(printf '%s\n' "$RECS" | tail -4 | tr '\n' '|')"
  fi
  # The path must survive parsing too: attribution that cannot tell /flavors
  # from /servers cannot attribute anything.
  if printf '%s\n' "$RECS" | awk '$2 == "/v2.1/servers"' | grep -q .; then
    ok "the request PATH survives parsing (/v2.1/servers found as field 2)"
  else
    bad "no record carries /v2.1/servers in the path field, so per-path attribution
      would be blank for every request."
  fi
fi

printf '\n\033[1m== RESULT\033[0m\n'
if [ "$FAILS" -ne 0 ]; then
  printf '\033[31m  %s assertion(s) failed: the measuring device does not behave as probe-401.sh assumes.\033[0m\n' "$FAILS"
  exit 1
fi
ok "the shipped mock serves $STATUS, logs it with populated fields, and invents nothing."
exit 0
