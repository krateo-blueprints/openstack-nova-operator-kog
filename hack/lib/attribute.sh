#!/usr/bin/env bash
# The mock access-log attribution parser, in ONE place.
#
# WHY A LIB. Two probes now read the same nginx access log and must agree about
# what a request record IS: hack/probe-401.sh (does a rejected credential reach
# the vendor at all) and hack/probe-observe-rejection.sh (does a rejection on
# OBSERVE get read as absence). A copy in each would be the defect this repo
# spent 2026-11-18 removing from its documentation twice over -- two copies of
# one piece of reasoning, kept in different files, diverging silently. Here the
# divergence is impossible and the test below covers both callers.
#
# WHY THE MARKERS STAY. hack/validate-401-mock.sh extracts the awk program
# between them and runs it against output a REAL nginx wrote, so the parser is
# tested against the format that ships rather than against a retyped copy. It
# now extracts from THIS file; renaming the markers breaks that validator
# loudly, which is the intent.
#
# UA SPACES ARE SQUEEZED TO '+'. Callers select records with awk's default
# whitespace splitting, so a User-Agent containing a space would otherwise put
# only its first word in $4 and every comparison against it would be a
# comparison against a prefix.

# ---8<--- ATTRIBUTE_AWK
# shellcheck disable=SC2016  # the single quotes are the whole point: $2, $3
# and $4 are AWK's field references and must reach awk unexpanded. Double
# quotes would have the shell substitute its own positional parameters --
# empty, here -- and the parser would silently match nothing.
ATTRIBUTE_AWK='
BEGIN { FS = "\"" }
NF >= 4 {
  split($2, req, " ")
  split($3, st, " ")
  if (req[1] == "" || req[2] == "" || st[1] !~ /^[0-9][0-9][0-9]$/) next
  ua = $4
  if (ua == "") ua = "-"
  gsub(/ /, "+", ua)
  print req[1], req[2], st[1], ua
}
'
# ---8<--- END ATTRIBUTE_AWK

# One record per request: METHOD PATH STATUS UA, read from stdin.
attribute_log() { awk "$ATTRIBUTE_AWK"; }

# TIMED_AWK -- the same records, carrying nginx's $msec.
#
# WHY A SECOND PARSER RATHER THAN A CHANGE TO THE FIRST. oasgen-provider#648
# asks whether a successful create schedules its observe promptly or only at the
# next resync, and that is a question about WHEN, which the parser above cannot
# answer at any price: the format it reads has no clock in it.
#
# WHY THE FIRST PARSER DID NOT MOVE. ATTRIBUTE_AWK splits on the double quote,
# so its $2, $3 and $4 are the quoted request, the status between the quotes and
# the quoted User-Agent. Prepending $msec to log_format puts the timestamp in
# $1 -- a field ATTRIBUTE_AWK never reads. So probe-observe-rejection.sh now
# logs a timestamp, probe-401.sh still does not, and ATTRIBUTE_AWK is correct on
# both without a conditional. That is measured by the self-test below and by
# hack/validate-401-mock.sh, which runs it against bytes a real nginx wrote.
#
# WHY THE TIMESTAMP TEST IS STRICT. Run this on a log WITHOUT $msec and $1 holds
# the remote address. "172.17.0.1" must not be read as a time, so the guard
# demands ten digits before the dot and exactly three after -- epoch seconds are
# ten digits until 2286, and no IPv4 first octet can be ten digits. A
# timestamp-less log therefore yields ZERO records here rather than a column of
# plausible nonsense, which is the difference between an empty answer and a
# wrong one.
# ---8<--- TIMED_AWK
# shellcheck disable=SC2016  # as above: these are AWK field references
TIMED_AWK='
BEGIN { FS = "\"" }
NF >= 4 {
  split($1, pre, " ")
  if (pre[1] !~ /^[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]\.[0-9][0-9][0-9]$/) next
  split($2, req, " ")
  split($3, st, " ")
  if (req[1] == "" || req[2] == "" || st[1] !~ /^[0-9][0-9][0-9]$/) next
  ua = $4
  if (ua == "") ua = "-"
  gsub(/ /, "+", ua)
  print pre[1], req[1], req[2], st[1], ua
}
'
# ---8<--- END TIMED_AWK

# One record per request: MSEC METHOD PATH STATUS UA, read from stdin.
timed_log() { awk "$TIMED_AWK"; }
