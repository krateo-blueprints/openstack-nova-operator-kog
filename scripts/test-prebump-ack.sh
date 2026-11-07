#!/usr/bin/env bash
# Calibrate hack/prebump-gate.py's ACK mechanism against the SUBJECT.
#
# WHY THIS EXISTS. The ack is a BYPASS. It is the only way a changed protected
# file can produce exit 0, so if it ever stops being able to refuse, the gate
# silently becomes decoration and a bump that moves the derived apiVersion
# sails through. The gate's own PASS arm runs on every pin change; its REFUSAL
# arms run nowhere, which is the shape this repo keeps finding.
#
# THE DEFECT IS INJECTED INTO THE REAL SCRIPT, not a reimplementation of it.
# A test that proves "a checker can return non-zero" proves nothing about this
# checker. Arm A copies hack/prebump-gate.py and changes ONE THING -- the
# recorded hash -- then runs that copy.
#
# Needs `gh` with network: the gate reads the upstream compare API. In CI that
# is GH_TOKEN. If gh cannot run, this VOIDs (exit 3) rather than passing.
set -uo pipefail
# `|| exit` not `|| true`: every path below is relative to the repo root, so a
# failed cd would run the arms against whatever directory we happened to be in
# and could report a clean pass having tested nothing.
cd "$(dirname "$0")/.." || exit 3
GATE=hack/prebump-gate.py
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
rc=0

[ -f "$GATE" ] || { echo "$GATE not found. VOID, not a pass."; exit 3; }
command -v gh >/dev/null 2>&1 || { echo "gh not on PATH; the gate cannot read"; \
  echo "upstream, so its refusals cannot be calibrated. VOID, not a pass."; exit 3; }

# gh ON PATH IS NOT gh ABLE TO READ, and conflating the two cost a red CI run.
# Added to validate.yaml without GH_TOKEN, every `gh api .../contents` call
# failed, so the gate saw every protected path as ABSENT and exited 3 (VOID)
# where the arms expected 1 (REFUSE) -- three mismatches that looked like a
# broken calibration and were actually a missing secret. A missing token is an
# ENVIRONMENT fault, so it must say so here rather than be diagnosed as the
# subject misbehaving.
if ! gh api repos/krateo-platformops/oasgen-provider --jq .name >/dev/null 2>&1; then
  echo "gh is on PATH but cannot read the upstream repo -- almost certainly no"
  echo "GH_TOKEN. The gate would see every protected path as absent and VOID,"
  echo "so its refusal arms cannot be distinguished from that. VOID, not a pass."
  exit 3
fi

# The acked pair and its recorded hash come FROM the subject, so this test
# cannot drift from it.
read -r ACK_OLD ACK_NEW ACK_SHA <<EOF2
$(python3 -c '
import importlib.util, sys
spec = importlib.util.spec_from_file_location("g", "hack/prebump-gate.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
acks = getattr(m, "ACKS", {})
if len(acks) != 1:
    sys.exit("expected exactly one ACK to calibrate, found %d" % len(acks))
(old, new, _path), v = next(iter(acks.items()))
print(old, new, v["patch_sha256"])
')
EOF2
[ -n "${ACK_SHA:-}" ] || { echo "could not read the ACK out of $GATE. VOID."; exit 3; }
echo "calibrating against the subject's own ack: $ACK_OLD -> $ACK_NEW"
echo

check() { # name expected_rc expected_substring actual_rc file [expected-to-mismatch]
  local name=$1 erc=$2 sub=$3 arc=$4 f=$5 deliberate=${6:-}
  if [ "$arc" = "$erc" ] && grep -q -- "$sub" "$f"; then
    echo "  ok       $name (exit $arc, said \"$sub\")"
  elif [ -n "$deliberate" ]; then
    # Labelled so a HEALTHY run contains zero occurrences of the bare failure
    # word -- otherwise the meta-control's own output trips anyone grepping CI
    # logs for trouble, and a test that cries wolf gets muted.
    echo "  MISMATCH (expected)  $name: wanted exit $erc, got $arc -- reported, which is the point"
  else
    echo "  MISMATCH $name: expected exit $erc containing \"$sub\", got exit $arc"
    sed 's/^/             /' "$f" | tail -6
    rc=1
  fi
}

# ---- ARM A: a STALE ack must refuse, not fall back to honouring itself ----
sed "s/$ACK_SHA/$(printf '0%.0s' $(seq 1 64))/" "$GATE" > "$TMP/stale.py"
if cmp -s "$GATE" "$TMP/stale.py"; then
  echo "  MISMATCH arm A: the hash substitution changed nothing, so this arm"
  echo "             would test the unmodified subject. VOID."
  exit 3
fi
python3 "$TMP/stale.py" "$ACK_OLD" "$ACK_NEW" > "$TMP/a.out" 2>&1
check "stale ack refuses" 1 "STALE" "$?" "$TMP/a.out"

# ---- ARM B: a pair with no ack must refuse ----
python3 "$GATE" 0.52.0 0.53.0 > "$TMP/b.out" 2>&1
check "unacked pair refuses" 1 "record an ACK" "$?" "$TMP/b.out"

# ---- ARM C: the acked pair must pass, and say so as an ACK not a silent OK ----
python3 "$GATE" "$ACK_OLD" "$ACK_NEW" > "$TMP/c.out" 2>&1
check "acked pair passes" 0 "ACK " "$?" "$TMP/c.out"

# ---- META-CONTROL: a wrong expectation must be REPORTED ----
# Without this, every "ok" above could come from a check() that cannot fail.
echo
echo "meta-control (a deliberately wrong expectation must be reported):"
check "meta: acked pair 'should' exit 99" 99 "ACK " 0 "$TMP/c.out" deliberate > "$TMP/meta.out" 2>&1
cat "$TMP/meta.out"
if ! grep -q 'MISMATCH (expected)' "$TMP/meta.out"; then
  echo "  the checker did NOT report a wrong expectation, so every ok above is"
  echo "  worthless. VOID, not a pass."
  exit 3
fi
echo "  ok       the checker reports a wrong expectation"

echo
[ "$rc" = "0" ] && echo "prebump ACK calibration: all arms behaved" \
                || echo "prebump ACK calibration: SOMETHING DID NOT"
exit "$rc"
