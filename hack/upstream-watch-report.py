#!/usr/bin/env python3
"""Turn an upstream-watch outcome into a GitHub issue, de-duplicated by version.

WHY THIS EXISTS, in the reviewer's words: **prose is not a mechanism.**
`upstream-watch` prints a careful explanation of whose problem a red run is, and
almost nobody will read it -- a red SCHEDULED run notifies roughly one person
(whoever last edited the workflow) and nobody opens the Actions tab daily. That
is the same pattern as this repo's gates that printed SKIP for weeks: the text
was correct and the mechanism was absent.

An issue reaches every watcher of the repo as a notification, and the issue list
becomes the history of which releases render this chart and which do not.

THE DECISION TABLE IS THE WHOLE PROGRAM, and it is pure so that it can be
tested without touching GitHub:

  outcome  existing issue for this version?  action
  fail     no                                open
  fail     yes                               comment     (NOT a second issue)
  pass     yes                               close       (with a run link)
  pass     no                                nothing
  void     --                                open/comment the STANDING void issue
  void     --                                never touches a version issue

A WRONG CLOSE IS THE DANGEROUS DIRECTION. Closing drops a finding silently,
while a duplicate issue is merely untidy. So `pass` closes only on an
unambiguous green for that exact version, and never closes the void issue --
a dead instrument stays visible until someone looks at it.

AND A FAILED SEARCH IS NOT "NO ISSUE". If the lookup errors, this reports
`unknown` and opens nothing: duplicating an issue every day because a search
flaked is how an alerting channel becomes noise, and noise is how a real break
goes unread.

Exit codes: 0 reported, 2 usage, 3 the search or the write failed (said loudly).
"""

import json
import re
import subprocess
import sys

REPO = "krateo-blueprints/openstack-nova-operator-kog"
VOID_TITLE = "upstream-watch could not ask the question"
LABEL = "upstream-watch"

PLAIN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def version_title(version):
    return f"upstream oasgen-provider {version} does not render this chart"


def decide(*, outcome, state):
    """(outcome, state) -> action for the VERSION issue.

    `state` is "none", "open", "closed" or None (the search failed).

    SEARCHING CLOSED ISSUES TOO IS NOT A REFINEMENT, IT IS A FIX. The first
    version of this searched `--state open` only. If a version's issue was
    closed -- by a later green run, or by a human -- and that version then fails
    again (a re-tag, or a render that was flaky and turns out to be real), an
    open-only search finds nothing and opens a DUPLICATE, splitting the history
    of that version across two issues. The reviewer caught it.

    Pure, total, and the only place the version policy lives.
    """
    if outcome == "fail":
        if state is None:
            return "abstain"
        if state == "open":
            return "comment"
        if state == "closed":
            return "reopen"          # NOT a second issue
        return "open"
    if outcome == "pass":
        if state is None:
            return "abstain"
        return "close" if state == "open" else "noop"
    # A void says nothing about any version: the question was never asked.
    return "noop"


def decide_void(*, outcome, state):
    """(outcome, state) -> action for the STANDING void issue.

    Separate table because the void issue answers a different question -- "can
    this job still ask?" -- and it is NEVER closed automatically. A dead
    instrument stays visible until a human reads it.

    But an always-open issue becomes the thing everyone learns to ignore, so a
    recovery is MARKED rather than tidied away: on the first pass after a void
    it gets a comment saying the instrument works again, and a human closes it
    having read that.
    """
    if state is None:
        return "abstain"
    if outcome == "void":
        if state == "open":
            return "void-comment"
        if state == "closed":
            return "void-reopen"     # the next void reopens rather than duplicates
        return "void-open"
    if outcome == "pass":
        # Recovery is noted, never closed. `fail` says nothing about the
        # instrument: it asked the question and got an answer.
        return "void-recovered" if state == "open" else "noop"
    return "noop"


def valid_version(v):
    return bool(PLAIN.match(v or ""))


def run(cmd, timeout=120):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def create_cmd(*, title, body, label=None):
    """The issue-create command, as a pure value.

    FACTORED OUT BECAUSE THE DEGRADED PATH WAS THE UNTESTED ONE. The label
    fallback had two arms pinning its DISCRIMINATOR -- that the retry fires on an
    error mentioning "label" and not on an unrelated failure -- and nothing
    asserting that the retry actually builds a create WITHOUT the label. The
    provider session pointed out that the untested fallback is exactly where
    this class lives, which is the same argument as the 0 -> 1 mutant arm two
    commits earlier. They were right, and I had stopped one step short.
    """
    cmd = ["gh", "issue", "create", "--repo", REPO, "--title", title, "--body", body]
    if label:
        cmd += ["--label", label]
    return cmd


def create_attempts(*, title, body):
    """The create commands to try, IN ORDER: with the label, then without.

    THE SEQUENCE IS THE VALUE, not just each command. Factoring `create_cmd`
    alone was not enough: a mutation making the RETRY pass the label too --
    which would fail for the identical reason and lose the alert again -- passed
    every arm, because the arms tested the function while the defect was at the
    call site. The boundary moved one level out.

    So `act()` iterates over this list and the arms assert on the list: two
    attempts, the first labelled, the second not. A call site that retries with
    the label now fails an arm rather than silently repeating the bug.
    """
    return [create_cmd(title=title, body=body, label=LABEL),
            create_cmd(title=title, body=body)]


def search_cmd(title):
    """The search command, as a pure value so the self-test can inspect it.

    FACTORED OUT FOR ONE REASON: with the command inline, reverting `--state all`
    to `--state open` -- the exact bug the reviewer caught -- passed the whole
    self-test. 25 arms, all green, against a mutation that reintroduces the
    duplicate-issue defect. The arms tested `decide()`, which takes the state as
    an ARGUMENT and so cannot tell where the state came from.

    A test that cannot see the defect it was written for is the vacuous-gate
    failure this repo keeps finding in its own instruments and in its peers'.
    """
    return ["gh", "issue", "list", "--repo", REPO, "--state", "all",
            "--search", title, "--json", "number,title,state", "--limit", "50"]


def find_issue(title):
    """Returns (state, number, error).

    state is "none", "open", "closed", or None when the search FAILED -- which
    is NOT the same as finding nothing, and the caller abstains on it.

    BOTH STATES ARE SEARCHED. An open-only search turns a previously-closed
    version issue into a duplicate the next time that version fails.
    """
    rc, out, err = run(search_cmd(title))
    if rc != 0:
        return None, None, err.strip()[:200]
    try:
        items = json.loads(out or "[]")
    except Exception as exc:                                   # noqa: BLE001
        return None, None, f"unparseable: {exc}"
    exact = [it for it in items if it.get("title", "").strip() == title.strip()]
    if not exact:
        return "none", None, None
    # Prefer an OPEN match: if both an open and a closed issue somehow share the
    # title, commenting on the open one is right and reopening the closed one
    # would create a second live issue for one version.
    for it in exact:
        if (it.get("state") or "").upper() == "OPEN":
            return "open", it.get("number"), None
    return "closed", exact[0].get("number"), None


def self_test():
    checks = []

    def t(name, want, got):
        checks.append((name, want, got, want == got))

    # THE VERSION TABLE, every cell.
    t("fail with no issue opens one", "open", decide(outcome="fail", state="none"))
    t("fail with an OPEN issue comments, never duplicates", "comment", decide(outcome="fail", state="open"))
    t("fail with a CLOSED issue REOPENS, never duplicates", "reopen", decide(outcome="fail", state="closed"))
    t("pass with an open issue closes it", "close", decide(outcome="pass", state="open"))
    t("pass with a closed issue does nothing", "noop", decide(outcome="pass", state="closed"))
    t("pass with no issue does nothing", "noop", decide(outcome="pass", state="none"))
    t("a void touches no version issue", "noop", decide(outcome="void", state="open"))
    # THE UNKNOWN COLUMN: a failed search must not read as "no issue exists",
    # or a flaky lookup opens a duplicate every day it persists.
    t("fail with an UNKNOWN search abstains", "abstain", decide(outcome="fail", state=None))
    t("pass with an UNKNOWN search abstains", "abstain", decide(outcome="pass", state=None))
    t("the version table NEVER opens a duplicate when one exists", True,
      all(decide(outcome="fail", state=st) != "open" for st in ("open", "closed")))

    # THE LABEL FALLBACK is a string test rather than a behaviour test, because
    # the behaviour needs a GitHub write. It pins the DISCRIMINATOR: the retry
    # fires on an error mentioning "label" and not on every failure, so a real
    # permissions error still exits 3 rather than silently retrying forever.
    t("a label error triggers the no-label retry", True, "label" in "could not add label: 'x' not found".lower())
    t("an unrelated error does NOT", False, "label" in "HTTP 403: Resource not accessible".lower())

    # THE DEGRADED PATH'S COMMAND, not just its trigger. The untested fallback
    # is where this class lives -- the provider session caught that the two arms
    # above pin WHEN the retry fires and nothing pinned WHAT it then runs.
    with_label = create_cmd(title="T", body="B", label=LABEL)
    without = create_cmd(title="T", body="B")
    t("the normal create carries the label", True, "--label" in with_label)
    t("the DEGRADED create carries NO label", False, "--label" in without)
    t("the degraded create still opens an issue", True,
      without[:3] == ["gh", "issue", "create"])
    t("the degraded create keeps the title, which IS the key", True,
      "T" in without and without[without.index("--title") + 1] == "T")
    t("the degraded create keeps the body", True,
      without[without.index("--body") + 1] == "B")
    t("both forms target this repo", True,
      all(c[c.index("--repo") + 1] == REPO for c in (with_label, without)))

    # THE SEQUENCE, because the arms above test the FUNCTION and the defect that
    # escaped them was at the CALL SITE: a retry that passes the label too would
    # fail for the identical reason and lose the alert again. Measured -- that
    # mutation passed all 35 arms before these three.
    # KEYWORD-ONLY IS PREVENTION, NOT DETECTION, and this arm locks it in. The
    # provider session found the same shape in their own code -- three strings in
    # a row, where swapping two compiled, inverted the message, and failed none
    # of fifteen arms. Measured here before the fix: swapping decide()'s two
    # arguments at the call site failed 0 of 38 arms, because every arm calls
    # decide with literals and no arm sees the call.
    #
    # A positional swap leaves every token present and changes only POSITIONS,
    # so a `contains`-style assertion cannot see it -- the same equivalence-class
    # fault as a renamed-symbol mutant against a substring matcher. Rather than
    # add positional assertions, make the swap IMPOSSIBLE: `*` forces keywords,
    # so a positional call is a TypeError at the call rather than a wrong answer
    # three layers later.
    def _positional_rejected(fn, *args):
        try:
            fn(*args)
            return False
        except TypeError:
            return True
    t("decide REFUSES positional arguments", True,
      _positional_rejected(decide, "fail", "none"))
    t("decide_void REFUSES positional arguments", True,
      _positional_rejected(decide_void, "void", "none"))
    t("create_cmd REFUSES positional arguments", True,
      _positional_rejected(create_cmd, "T", "B"))
    t("create_attempts REFUSES positional arguments", True,
      _positional_rejected(create_attempts, "T", "B"))

    att = create_attempts(title="T", body="B")
    t("there are exactly two create attempts", 2, len(att))
    # INDEXED DEFENSIVELY so a wrong LENGTH reports as an arm failure rather than
    # an IndexError. Measured: with a bare att[1], the "only one attempt" mutant
    # exited 1 by CRASHING while the checks list was being built, so the length
    # arm never printed. Caught is not the same as explained, and a crash tells
    # the next reader less than "want=2 got=1".
    t("the FIRST attempt carries the label", True,
      "--label" in (att[0] if len(att) > 0 else []))
    t("the SECOND attempt does NOT", False,
      "--label" in (att[1] if len(att) > 1 else ["--label"]))

    # THE SEARCH ITSELF, because the arms above take `state` as an argument and
    # therefore cannot tell where it came from. Without these two, reverting
    # `--state all` to `--state open` passes every other arm -- measured.
    cmd = search_cmd("anything")
    t("the search asks for BOTH states", "all",
      cmd[cmd.index("--state") + 1] if "--state" in cmd else "<no --state>")
    t("the search requests the state FIELD, or state is unreadable", True,
      any("state" in c for c in cmd if c.startswith("number") or "," in c))

    # THE VOID TABLE, which answers a different question and never closes.
    t("void with no standing issue opens one", "void-open", decide_void(outcome="void", state="none"))
    t("void with an open standing issue comments", "void-comment", decide_void(outcome="void", state="open"))
    t("void with a CLOSED standing issue reopens it", "void-reopen", decide_void(outcome="void", state="closed"))
    t("a pass after a void MARKS recovery", "void-recovered", decide_void(outcome="pass", state="open"))
    t("recovery does NOT close", True, decide_void(outcome="pass", state="open") != "close")
    t("a pass with no standing issue does nothing", "noop", decide_void(outcome="pass", state="none"))
    t("a fail says nothing about the instrument", "noop", decide_void(outcome="fail", state="open"))
    t("the void table abstains on an UNKNOWN search", "abstain", decide_void(outcome="void", state=None))
    t("the void table NEVER closes, in any cell", True,
      all("close" not in decide_void(outcome=o, state=st)
          for o in ("void", "pass", "fail") for st in ("none", "open", "closed")))

    t("the title is keyed on the version", True,
      "0.85.1" in version_title("0.85.1"))
    t("two versions get different titles", True,
      version_title("0.85.1") != version_title("0.85.0"))
    t("a plain version validates", True, valid_version("0.85.1"))
    t("a v-prefixed version does not", False, valid_version("v0.85.1"))
    t("an empty version does not", False, valid_version(""))
    t("a two-part version does not", False, valid_version("0.85"))

    width = max(len(c[0]) for c in checks)
    failed = 0
    for name, want, got, good in checks:
        print("  {:<{w}}  {}".format(
            name, "PASS" if good else "FAIL want={!r} got={!r}".format(want, got), w=width))
        failed += 0 if good else 1
    print("\n  {} passed, {} failed".format(len(checks) - failed, failed))
    if failed:
        print("\nFAIL: the decision table is wrong, so no report would be trustworthy",
              file=sys.stderr)
        return 1
    print("  OK: every cell of the decision table discriminates, including the "
          "unknown-search column")
    return 0


def main(argv):
    if len(argv) > 1 and argv[1] == "--self-test":
        print("== the decision table (no network)\n")
        return self_test()

    dry = "--dry-run" in argv[1:]
    args = [a for a in argv[1:] if a != "--dry-run"]
    if len(args) < 3:
        print(__doc__)
        print("usage: upstream-watch-report.py [--dry-run] <pass|fail|void> <version> <run-url>",
              file=sys.stderr)
        return 2
    outcome, version, run_url = args[0], args[1], args[2]

    if outcome not in ("pass", "fail", "void"):
        print(f"unknown outcome {outcome!r}", file=sys.stderr)
        return 2
    if outcome != "void" and not valid_version(version):
        print(f"not a plain X.Y.Z version: {version!r}", file=sys.stderr)
        return 2

    print("== the decision table, before any GitHub call\n", file=sys.stderr)
    if self_test():
        return 1

    # BOTH TABLES ARE CONSULTED, because a pass can mean two things at once:
    # this version renders (close its issue) AND the instrument recovered from a
    # void (mark it). A fail means only the first; a void only the second.
    def act(action, title, number, body, note):
        if action in ("open", "void-open"):
            attempts = create_attempts(title=title, body=body)
            rc, out, err = run(attempts[0])
            # THE LABEL IS A CONVENIENCE; THE ISSUE IS THE SIGNAL. Measured on
            # run 44916780758: the first real reporting attempt failed with
            #
            #     could not create: could not add label: 'upstream-watch' not found
            #
            # so a missing label suppressed the entire alert. The label does not
            # exist in this repo and nothing creates it, and requiring repo state
            # for an alert to land is the wrong dependency: the title is the key,
            # the de-duplication reads the title, and a human reads the title.
            # So retry without it and SAY that the label was dropped, rather than
            # losing the signal over a filter nobody has set up yet.
            if rc != 0 and "label" in (err or "").lower():
                print(f"  the label {LABEL!r} does not exist; retrying without it",
                      file=sys.stderr)
                rc, out, err = run(attempts[1])                   # the label-free attempt
                if rc == 0:
                    print(f"  opened WITHOUT the {LABEL!r} label (create it to get "
                          f"filtering): {out.strip()}", file=sys.stderr)
                    return 0
            if rc != 0:
                print(f"  could not create: {err.strip()[:160]}", file=sys.stderr)
                return 3
            print(f"  opened: {out.strip()}", file=sys.stderr)
        elif action in ("comment", "void-comment", "void-recovered"):
            rc, out, err = run(["gh", "issue", "comment", str(number), "--repo", REPO,
                                "--body", note])
            if rc != 0:
                print(f"  could not comment: {err.strip()[:160]}", file=sys.stderr)
                return 3
            print(f"  commented on #{number}", file=sys.stderr)
        elif action in ("reopen", "void-reopen"):
            rc, out, err = run(["gh", "issue", "reopen", str(number), "--repo", REPO,
                                "--comment", note])
            if rc != 0:
                print(f"  could not reopen: {err.strip()[:160]}", file=sys.stderr)
                return 3
            print(f"  REOPENED #{number} rather than opening a duplicate", file=sys.stderr)
        elif action == "close":
            rc, out, err = run(["gh", "issue", "close", str(number), "--repo", REPO,
                                "--comment", note])
            if rc != 0:
                print(f"  could not close: {err.strip()[:160]}", file=sys.stderr)
                return 3
            print(f"  closed #{number}", file=sys.stderr)
        else:
            print(f"  {action}: nothing to do", file=sys.stderr)
        return 0

    worst = 0

    # --- the version issue (not consulted for a void) ---
    if outcome != "void":
        vt = version_title(version)
        vstate, vnum, verr = find_issue(vt)
        if verr:
            print(f"\n  the version-issue search FAILED: {verr}", file=sys.stderr)
        vaction = decide(outcome=outcome, state=vstate)
        print(f"\n  version issue: {vt}", file=sys.stderr)
        print(f"    state={vstate} number={vnum} -> {vaction}", file=sys.stderr)
        if vaction == "abstain":
            print("    ABSTAINING: the search could not say whether an issue exists, so\n"
                  "    nothing was written. Opening one anyway would duplicate it every\n"
                  "    day a flaky lookup persists, and a channel that cries wolf is how a\n"
                  "    real break goes unread.", file=sys.stderr)
            worst = 3
        elif dry:
            print(f"    DRY RUN -- would {vaction}"
                  f"{' #%s' % vnum if vnum else ''}", file=sys.stderr)
        else:
            body = (
                f"`upstream-watch` installed **oasgen-provider {version}** on a throwaway "
                f"kind cluster with this chart pointed at an unroutable endpoint, and the "
                f"chart did not reconcile.\n\n"
                f"- run: {run_url}\n"
                f"- this is a statement about **upstream**, not about any pull request here.\n"
                f"- our declared floor (`0.52.0`) and the platform pin (`0.74.0`) are tested "
                f"separately by `smoke.yaml` and are unaffected.\n\n"
                f"The run log carries the RestDefinition conditions and any "
                f"`schemaWarnings`, which usually say whether this is an upstream "
                f"regression or a genuine incompatibility with this chart.\n\n"
                f"Keyed on the version: a later failing run on {version} comments here, a "
                f"green run closes this, and a failure after that **reopens** it rather "
                f"than splitting the history across two issues."
            )
            note = {
                "comment": f"Still failing on `{version}` as of {run_url}",
                "reopen": (f"Failing again on `{version}` as of {run_url}. Reopened rather "
                           f"than opening a second issue, so this version's history stays "
                           f"in one place."),
                "close": (f"`upstream-watch` now renders this chart cleanly on `{version}`: "
                          f"{run_url}"),
            }.get(vaction, f"{vaction} on `{version}`: {run_url}")
            worst = max(worst, act(vaction, vt, vnum, body, note))

    # --- the standing void issue ---
    zstate, znum, zerr = find_issue(VOID_TITLE)
    if zerr:
        print(f"\n  the void-issue search FAILED: {zerr}", file=sys.stderr)
    zaction = decide_void(outcome=outcome, state=zstate)
    print(f"\n  void issue: {VOID_TITLE}", file=sys.stderr)
    print(f"    state={zstate} number={znum} -> {zaction}", file=sys.stderr)
    if zaction == "abstain":
        print("    ABSTAINING on the void issue for the same reason.", file=sys.stderr)
        worst = max(worst, 3)
    elif zaction == "noop":
        print("    nothing to do", file=sys.stderr)
    elif dry:
        print(f"    DRY RUN -- would {zaction}{' #%s' % znum if znum else ''}",
              file=sys.stderr)
    else:
        zbody = (
            f"`upstream-watch` could not resolve a version to test, so **no question was "
            f"asked**.\n\n"
            f"- run: {run_url}\n"
            f"- this is NOT 'no new release' and NOT a statement about this chart.\n"
            f"- the resolver's own positive control (`0.74.0` must pull) or the retraction "
            f"list read refused; the run log says which.\n\n"
            f"**A dead instrument is as serious as a finding**, which is why this is an "
            f"issue rather than a silent skip. **It is never closed automatically** -- but "
            f"a recovery is commented here, so you can tell at a glance whether this state "
            f"is current or historical, and close it yourself once you have read that."
        )
        znote = {
            "void-comment": f"Still unable to resolve a version as of {run_url}",
            "void-reopen": (f"Unable to resolve a version again as of {run_url}. Reopened "
                            f"rather than opening a second standing issue."),
            "void-recovered": (f"**Instrument working again** since {run_url} -- a version "
                               f"resolved and the chart rendered. Deliberately NOT closed "
                               f"automatically: close it yourself once you have read this, "
                               f"and a later void will reopen it."),
        }.get(zaction, f"{zaction}: {run_url}")
        worst = max(worst, act(zaction, VOID_TITLE, znum, zbody, znote))

    return worst


if __name__ == "__main__":
    sys.exit(main(sys.argv))
