#!/usr/bin/env python3
"""Does the shared release-preflight, AT THE PIN THIS REPO USES, still refuse
the versions this repo's own check used to refuse?

WHY THIS EXISTS. release-tag.yaml used to carry its own rule-13 version check,
run before anything was pushed. PUBLISHING.md rule 12 replaced it with a shared
`release-preflight` job, and keeping both would be two copies of one pattern --
the defect this repo spends its time removing. But deleting a working gate in
favour of a file fetched from another repository at a pin is only safe if the
replacement is MEASURED to cover it, at that pin, on every PR. Otherwise the
pin moves one day to a preflight that does not check the version, and nothing
here notices: the release would publish and the only signal would be
publish-chart refusing to index it AFTER the GHCR push -- the half-state rule 12
exists to prevent.

WHAT IT ASSERTS, in order:

  1. release-tag.yaml pins release-preflight.yaml by 40-hex SHA with a version
     comment. Not found -> VOID, because "no pin" and "a fine pin" must not read
     alike.
  2. That file, fetched at that exact commit, is big enough to be the real
     thing. A short or empty body -> VOID. An empty fetch and an unchanged
     workflow are the same string otherwise.
  3. It defines the version pattern exactly once, and that pattern is
     byte-for-byte the one this repo's check used.
  4. The pattern accepts and refuses the case table below, run against the
     EXTRACTED text rather than a retyped copy.
  5. CONTROL: the same table run against the looser pattern a sibling repo
     actually shipped (`^[0-9]+\\.[0-9]+\\.[0-9]+$`, which accepts 0.015.0) must
     FAIL. A table that passes against both patterns cannot tell them apart and
     proves nothing about either.
  6. CONTROL: the previous charts release, 1.4.2, carried no version check at
     all. Fetched at its own commit, extraction must find ZERO patterns. If it
     finds one, the extraction is matching something other than what it thinks.

THE PATTERN IS NEVER RENDERED THROUGH %r OR repr(). That mistake is on record in
this repo: a harness printed rule 13's pattern with %r, which doubled the
backslashes, so `\\.` became `\\\\.` and it reported that 0.1.9 was REFUSED by a
gate that accepts it -- contradicting a claim already pushed. The extracted text
is compiled and used as-is, and compared with a raw-string literal.

Exit codes: 0 agree, 1 a real disagreement, 2 VOID (nothing was measured).
"""

import base64
import json
import re
import subprocess
import sys

WORKFLOW = ".github/workflows/release-tag.yaml"
SHARED = "krateo-blueprints/charts"
SHARED_PATH = ".github/workflows/release-preflight.yaml"

# The pin for the release whose preflight carried NO version check. Used only as
# the negative control in step 6.
NO_CHECK_COMMIT = "aea92de356085f50ea45477a4e6ae2b026cd6b14"  # charts 1.4.2

PIN = re.compile(
    r"uses:\s*krateo-blueprints/charts/\.github/workflows/release-preflight\.yaml"
    r"@([0-9a-f]{40})\s*#\s*(\S+)")

# Exactly the definition release-preflight.yaml and publish-chart.yaml share.
PATTERN_DEF = re.compile(r"^\s*PLAIN_VERSION='([^']*)'\s*$", re.M)

EXPECTED = r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"

# The table the retired check was verified against, plus the case that caught a
# sibling repo's looser pattern (0.015.0, github-provider-kog#111).
ACCEPT = ["0.1.9", "1.0.0", "10.20.30", "0.0.0"]
REFUSE = ["01.1.0", "0.2.0-rc1", "v0.1.9", "0.1", "0.015.0", "1.0.0+b", "",
          "1.0.0 ", " 1.0.0"]

# What a sibling actually shipped, and what this table must be able to reject.
LOOSE = r"^[0-9]+\.[0-9]+\.[0-9]+$"


def void(msg):
    print("VOID: %s" % msg, file=sys.stderr)
    sys.exit(2)


def fetch(commit):
    """The shared workflow's bytes at one commit, or None."""
    cmd = ["gh", "api",
           "repos/%s/contents/%s?ref=%s" % (SHARED, SHARED_PATH, commit)]
    try:
        raw = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "gh could not run: %s" % exc
    if raw.returncode != 0:
        return None, "gh exited %d: %s" % (raw.returncode,
                                           raw.stderr.strip()[:300])
    try:
        body = base64.b64decode(json.loads(raw.stdout)["content"])
    except (ValueError, KeyError, TypeError) as exc:
        return None, "could not decode the response: %s" % exc
    return body.decode("utf-8", "replace"), None


def table_holds(pattern):
    """(ok, complaints) for the case table against one pattern string."""
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return False, ["the pattern does not compile: %s" % exc]
    bad = []
    for v in ACCEPT:
        if not rx.match(v):
            bad.append("refuses %r, which this repo released" % v)
    for v in REFUSE:
        if rx.match(v):
            bad.append("ACCEPTS %r, which must be refused" % v)
    return not bad, bad


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == "--self-test":
        # The harness, with no network: the table must pass the expected pattern
        # and FAIL the loose one. If it passes both it cannot discriminate.
        ok_strict, _ = table_holds(EXPECTED)
        ok_loose, loose_why = table_holds(LOOSE)
        print("  strict pattern passes the table : %s" % ok_strict)
        print("  loose pattern FAILS the table   : %s" % (not ok_loose))
        if loose_why:
            print("      because it %s" % loose_why[0])
        if not ok_strict or ok_loose:
            print("the case table cannot tell the two patterns apart, so it "
                  "proves nothing about either", file=sys.stderr)
            return 1
        print("harness self-test: the table discriminates")
        return 0

    try:
        wf = open(WORKFLOW, encoding="utf-8").read()
    except OSError as exc:
        void("%s could not be read (%s), so there is no pin to check." % (WORKFLOW, exc))

    pins = PIN.findall(wf)
    if len(pins) != 1:
        void("%s carries %d release-preflight pin(s) and must carry exactly one. "
             "Zero means this gate would check a workflow nobody calls; more than "
             "one means it cannot know which is authoritative." % (WORKFLOW, len(pins)))
    commit, version = pins[0]
    print("pin: release-preflight.yaml @ %s (# %s)" % (commit, version))

    body, err = fetch(commit)
    if body is None:
        void("could not fetch %s at %s -- %s. Nothing was measured; this is an "
             "unreachable instrument, not a passing check." % (SHARED_PATH, commit, err))
    if len(body) < 1000:
        void("%s at %s is only %d bytes. That is not the shared workflow, and an "
             "empty body would otherwise read as 'no version check found'."
             % (SHARED_PATH, commit, len(body)))
    print("fetched %d bytes at that commit" % len(body))

    found = PATTERN_DEF.findall(body)
    if len(found) != 1:
        void("the fetched preflight defines the version pattern %d time(s); "
             "expected exactly one PLAIN_VERSION='...'. The pin may point at a "
             "release that does not check the version, which is precisely what "
             "this gate exists to catch -- but %d is also what a changed spelling "
             "would give, so it is reported as VOID rather than as a defect."
             % (len(found), len(found)))
    pattern = found[0]

    problems = []
    if pattern != EXPECTED:
        problems.append(
            "the shared preflight's pattern is not the one this repo's retired "
            "check used.\n      shared:   %s\n      expected: %s" % (pattern, EXPECTED))

    ok, why = table_holds(pattern)
    if not ok:
        for w in why:
            problems.append("the shared pattern %s" % w)

    # CONTROL 5: the table must reject the loose pattern, or it proves nothing.
    loose_ok, loose_why = table_holds(LOOSE)
    if loose_ok:
        void("the case table ACCEPTS the loose pattern %s as well, so it cannot "
             "tell a correct preflight from a sloppy one. Every result above is "
             "unreadable." % LOOSE)
    print("control: the table rejects the loose pattern (%s)" % loose_why[0])

    # CONTROL 6: charts 1.4.2's preflight had no version check at all.
    old_body, old_err = fetch(NO_CHECK_COMMIT)
    if old_body is None:
        print("control SKIPPED: could not fetch the 1.4.2 preflight (%s). The "
              "extraction above is therefore unproven against a file that lacks "
              "the pattern." % old_err, file=sys.stderr)
    else:
        n_old = len(PATTERN_DEF.findall(old_body))
        if n_old != 0:
            void("the negative control failed: charts 1.4.2's preflight (%d bytes) "
                 "yields %d pattern(s), and it carries no version check. The "
                 "extraction is matching something other than what it thinks."
                 % (len(old_body), n_old))
        print("control: the 1.4.2 preflight (%d bytes) yields 0 patterns, as it must"
              % len(old_body))

    if problems:
        print("\n%d problem(s):\n" % len(problems), file=sys.stderr)
        for p in problems:
            print("  %s" % p, file=sys.stderr)
        return 1

    print("the shared release-preflight at this pin refuses everything the "
          "retired check refused, and accepts everything it accepted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
