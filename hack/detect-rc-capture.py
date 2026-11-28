#!/usr/bin/env python3
"""Find a command whose rc is captured on the NEXT line while errexit is ON.

GitHub runs `run:` steps as `bash -e {0}`; a script's own `set -uo pipefail` does
NOT clear that -e. errexit is exempt only for a command whose status is TESTED --
if/while/until, &&, ||, or a leading !.  So this dies at line 1:

    cmd > out 2>&1
    RC=$?

while all of these are fine:

    cmd || RC=$?        if ! cmd; then ...        set +e; cmd; RC=$?; set -e

ERREXIT STATE IS TRACKED, because the scoped `set +e` ... `set -e` form is
CORRECT and the first version of this script reported both real-world instances
of it as findings -- a 100% false-positive rate on the only two hits it had. A
detector that cries wolf on the correct idiom gets switched off.

Two verdicts, because they are different facts:
  UNGUARDED  errexit is on and the command's status is not tested -> the bug
  UNSCOPED   `set +e` was never restored -> not a bug, but the remainder of the
             block is unguarded, so a line added later is unprotected

EXIT CODES. 1 only for UNGUARDED. UNSCOPED prints and does NOT fail: it is a
fragility, not a defect (the capture works today), and a screen that fails on a
working idiom gets switched off -- which is the same outcome as the first
version's false positives, reached by a different road.

PROVENANCE. This is the defect that made the classifier in
.github/workflows/apikey-secret-read-arm.yaml unreachable for every non-zero
probe exit. It was found by a dispatch (run 46164705298) while 42 passing
self-test arms over the classifier said nothing was wrong -- the arms drove the
classifier's verdict function, never the line that was supposed to feed it.
Fixed in PR #277. This script exists so the next one is found by a screen.

SCOPE. scripts/validate.py runs it over .github/workflows/*.y*ml, hack/*.sh and
scripts/*.sh, and CI runs `--self-test`. Each comment below that says WHY a
branch exists records an iteration this detector got wrong; do not simplify
them away.
"""
import re, sys, io

CAPTURE  = re.compile(r'^\s*[A-Za-z_][A-Za-z0-9_]*=\$\?\s*(#.*)?$')
TESTED   = re.compile(r'(\|\||&&|^\s*(if|while|until)\b|^\s*!\s)')
SET_OFF  = re.compile(r'(^|;|\s)set\s+\+e')
SET_ON   = re.compile(r'(^|;|\s)set\s+-[a-z]*e')
CONT     = re.compile(r'\\\s*$')

RUN_KEY  = re.compile(r'^\s*(-\s+)?run:')

def scan(text, name="<text>", errexit=True, blocks=False):
    """errexit: the state at the START of the file.

    THIS ARGUMENT EXISTS BECAUSE ASSUMING True EVERYWHERE PRODUCED 13 FALSE
    POSITIVES IN ONE TREE. A workflow `run:` block really does start with
    errexit ON, because GitHub invokes it as `bash -e {0}`. A standalone .sh
    starts with it OFF unless the script says otherwise -- and the probes here
    deliberately use `set -uo pipefail` with NO -e precisely so they CAN capture
    $? on the next line. Flagging that idiom is flagging the correct code."""
    hits, lines, off_at = [], text.split("\n"), None
    for i, ln in enumerate(lines):
        # blocks=True (YAML): every `run:` is a SEPARATE `bash -e {0}`, so errexit
        # RESETS to armed at each boundary and a `set +e` cannot leak into the
        # next block. "No hits today" is not a reason to leave the leak: it is
        # dormant, not absent, and goes live the first time a block ends unrestored.
        if blocks and RUN_KEY.match(ln):
            errexit, off_at = True, None
        if SET_OFF.search(ln):
            errexit, off_at = False, i + 1
        elif SET_ON.search(ln):
            errexit = True
        if not CAPTURE.match(ln):
            continue
        j = i - 1
        while j >= 0 and (lines[j].strip() == "" or lines[j].lstrip().startswith("#")):
            j -= 1
        if j < 0:
            continue
        # walk back over backslash continuations to the command's FIRST line
        start = j
        while start > 0 and CONT.search(lines[start - 1]):
            start -= 1
        cmd_lines = lines[start:j + 1]
        if any(TESTED.search(c) for c in cmd_lines):
            continue
        # (1/4) DECIDE THE VERDICT HERE ONLY WHEN errexit IS ON. When it is off,
        # the verdict needs the lookahead below.
        if errexit:
            hits.append(("UNGUARDED", start + 1, cmd_lines[0].strip(), i + 1, name))
        else:
            # errexit is OFF here. The SCOPED idiom restores it AFTER the capture,
            # so the verdict cannot be decided at this line -- look forward for the
            # re-arm. Deciding here is what made the first version report both
            # real-world instances of the correct idiom as findings.
            # In blocks mode the lookahead stops at the next `run:`: a `set -e`
            # in a LATER block does not restore THIS one.
            tail = lines[i + 1:]
            if blocks:
                for k, l in enumerate(tail):
                    if RUN_KEY.match(l):
                        tail = tail[:k]
                        break
            restored = any(SET_ON.search(l) and not SET_OFF.search(l)
                           for l in tail)
            # off_at is None when errexit was NEVER armed -- a script that simply
            # does not use it. There is nothing "unscoped" about that; it is the
            # deliberate idiom. UNSCOPED is only for a `set +e` that turned it OFF
            # and never put it back.
            if off_at is not None and not restored:
                hits.append(("UNSCOPED", off_at,
                             f"set +e at line {off_at} is never restored",
                             i + 1, name))
    return hits

def initial_errexit(path):
    """(2/4) The initial state comes from the FILE TYPE. A YAML workflow's run:
    blocks are invoked as `bash -e {0}`; a standalone script's state is whatever
    its own `set` line says. Shared by the CLI, scripts/validate.py and the
    self-test, so a mutation here cannot pass any of them silently."""
    return path.endswith((".yaml", ".yml"))

def selftest():
    arms = [
        ("BROKEN bare",             "probe.sh > out 2>&1\nRC=$?\n",                       ["UNGUARDED"]),
        ("BROKEN continuation",     "tool --a \\\n  --b --c\nRC=$?\n",                    ["UNGUARDED"]),
        ("BROKEN comment between",  "probe.sh\n# note\nRC=$?\n",                          ["UNGUARDED"]),
        ("BROKEN cmd-substitution", 'out="$(prog x)"\nRC=$?\n',                            ["UNGUARDED"]),
        ("guarded ||",              "probe.sh || RC=$?\nX=$?\n",                           []),
        ("guarded if !",            "if ! probe.sh; then\n  RC=$?\nfi\n",                   []),
        ("guarded &&",              "probe.sh && echo ok\nRC=$?\n",                         []),
        ("SCOPED set +e/-e",        "set +e\nprobe.sh\nRC=$?\nset -e\n",                    []),
        ("SCOPED with euo above",   "set -euo pipefail\nset +e\np.sh\nRC=$?\nset -e\n",     []),
        ("UNSCOPED set +e",         "set +e\nprobe.sh\nRC=$?\n",                            ["UNSCOPED"]),
        # The FIRST capture here is correctly scoped (set -e follows it), so only the
        # SECOND is a finding. This arm expected both until the lookahead landed and
        # proved the expectation wrong rather than the code.
        ("re-armed then broken",    "set +e\na.sh\nR1=$?\nset -e\nb.sh\nR2=$?\n",  ["UNGUARDED"]),
        ("no capture",              "probe.sh > out\necho done\n",                          []),
        # The initial-state parameter, both directions. A .sh with `set -uo pipefail`
        # and no -e is the deliberate idiom these probes use; calling it a finding is
        # what 13 false positives looked like.
        ("sh: set -uo, no -e",      "set -uo pipefail\nprobe.sh\nRC=$?\n",                 [], False),
        ("sh: set -euo IS armed",   "set -euo pipefail\nprobe.sh\nRC=$?\n",               ["UNGUARDED"], False),
        ("yaml default is armed",   "probe.sh\nRC=$?\n",                                   ["UNGUARDED"], True),
        # Each run: block is its own shell. Block 1 ends with an unrestored set +e
        # (UNSCOPED, line 4's capture); block 2 starts armed again, so its bare
        # capture is UNGUARDED. Without the reset it read as unarmed and reported
        # nothing for block 2.
        ("yaml: set +e not leaked", "- run: |\n    set +e\n    a.sh\n    R=$?\n- run: |\n    b.sh\n    R=$?\n", ["UNSCOPED", "UNGUARDED"], True, True),
        ("yaml: later set -e != restore", "- run: |\n    set +e\n    a.sh\n    R=$?\n- run: |\n    set -e\n", ["UNSCOPED"], True, True),
    ]
    bad = 0
    for path, want_on in (("w.yaml", True), ("w.yml", True), ("p.sh", False)):
        if initial_errexit(path) is not want_on:
            print(f"  initial_errexit({path!r}) != {want_on}  FAIL"); bad += 1
    for arm in arms:
        name, src, want = arm[0], arm[1], arm[2]
        start_on = arm[3] if len(arm) > 3 else True
        got = [h[0] for h in scan(src, "<t>", start_on, len(arm) > 4 and arm[4])]
        ok = got == want
        if not ok: bad += 1
        print(f"  {name:26s} want {str(want):26s} got {str(got):26s} {'PASS' if ok else 'FAIL'}")
    outcomes = {tuple(a[2]) for a in arms}
    if len(outcomes) < 3:
        print("  SUITE FAIL: the arms do not cover all three outcomes"); bad += 1
    print(f"  {len(arms)} arms + 3 file-type arms, {bad} failed")
    return 1 if bad else 0

if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(selftest())
    rc = 0
    for p in sys.argv[1:]:
        start_on = initial_errexit(p)
        for (kind, cl, cmd, rl, _n) in scan(io.open(p, encoding="utf-8", errors="replace").read(), p, start_on, start_on):
            print(f"{p}:{cl}: {kind}  {cmd}")
            print(f"{p}:{rl}:   capture at this line")
            if kind == "UNGUARDED":
                rc = 1
    sys.exit(rc)
