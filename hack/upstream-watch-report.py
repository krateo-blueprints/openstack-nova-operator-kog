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


def decide(outcome, found):
    """(outcome, found) -> action. `found` is True, False or None (unknown).

    Pure, total, and the only place the policy lives.
    """
    if outcome == "void":
        return "void-open" if found is not True else "void-comment"
    if outcome == "fail":
        if found is None:
            return "abstain"
        return "comment" if found else "open"
    if outcome == "pass":
        if found is None:
            return "abstain"
        return "close" if found else "noop"
    return "abstain"


def valid_version(v):
    return bool(PLAIN.match(v or ""))


def run(cmd, timeout=120):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def find_open_issue(title):
    """Returns (found, number, error). found is None when the search FAILED."""
    rc, out, err = run([
        "gh", "issue", "list", "--repo", REPO, "--state", "open",
        "--search", title, "--json", "number,title", "--limit", "50",
    ])
    if rc != 0:
        return None, None, err.strip()[:200]
    try:
        items = json.loads(out or "[]")
    except Exception as exc:                                   # noqa: BLE001
        return None, None, f"unparseable: {exc}"
    for it in items:
        if it.get("title", "").strip() == title.strip():
            return True, it.get("number"), None
    return False, None, None


def self_test():
    checks = []

    def t(name, want, got):
        checks.append((name, want, got, want == got))

    # THE TABLE, every cell, both directions.
    t("fail with no issue opens one", "open", decide("fail", False))
    t("fail with an issue COMMENTS, never duplicates", "comment", decide("fail", True))
    t("pass with an issue closes it", "close", decide("pass", True))
    t("pass with no issue does nothing", "noop", decide("pass", False))
    t("void opens the standing issue", "void-open", decide("void", False))
    t("void comments when the standing issue exists", "void-comment", decide("void", True))
    # THE UNKNOWN COLUMN is the one that matters: a failed search must not be
    # read as "no issue exists", or a flaky lookup opens a duplicate every day.
    t("fail with an UNKNOWN search abstains", "abstain", decide("fail", None))
    t("pass with an UNKNOWN search abstains", "abstain", decide("pass", None))
    t("an unrecognised outcome abstains", "abstain", decide("banana", False))
    # VOID NEVER CLOSES: a dead instrument stays visible.
    t("void never produces a close", True, "close" not in
      {decide("void", f) for f in (True, False, None)})

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

    title = VOID_TITLE if outcome == "void" else version_title(version)
    found, number, err = find_open_issue(title)
    if err:
        print(f"\n  the issue search FAILED: {err}", file=sys.stderr)
    print(f"\n  title:   {title}", file=sys.stderr)
    print(f"  existing: found={found} number={number}", file=sys.stderr)

    action = decide(outcome, found)
    print(f"  action:   {action}", file=sys.stderr)

    if action == "abstain":
        print("\nEXIT 3: the search could not say whether an issue exists, so nothing\n"
              "  was written. Opening one anyway would duplicate it every day a flaky\n"
              "  lookup persists, and an alerting channel that cries wolf is how a real\n"
              "  break goes unread.", file=sys.stderr)
        return 3

    body_fail = (
        f"`upstream-watch` installed **oasgen-provider {version}** on a throwaway kind "
        f"cluster with this chart pointed at an unroutable endpoint, and the chart did "
        f"not reconcile.\n\n"
        f"- run: {run_url}\n"
        f"- this is a statement about **upstream**, not about any pull request here.\n"
        f"- our declared floor (`0.52.0`) and the platform pin (`0.74.0`) are tested "
        f"separately by `smoke.yaml` and are unaffected by this result.\n\n"
        f"The run log carries the RestDefinition conditions and any `schemaWarnings`, "
        f"which usually say whether this is an upstream regression or a genuine "
        f"incompatibility with this chart.\n\n"
        f"This issue is keyed on the version: a later failing run on {version} adds a "
        f"comment rather than a second issue, and a green run on {version} closes it."
    )
    body_void = (
        f"`upstream-watch` could not resolve a version to test, so **no question was "
        f"asked**.\n\n"
        f"- run: {run_url}\n"
        f"- this is NOT 'no new release' and NOT a statement about this chart.\n"
        f"- the resolver's own positive control (`0.74.0` must pull) or the retraction "
        f"list read refused; the run log says which.\n\n"
        f"**A dead instrument is as serious as a finding**, which is why this is an "
        f"issue rather than a silent skip. It is never closed automatically."
    )

    if dry:
        print("\n  DRY RUN -- no GitHub write performed. Would have:", file=sys.stderr)
        print(f"    {action} on {'#%s' % number if number else 'a new issue'}", file=sys.stderr)
        return 0

    if action in ("open", "void-open"):
        body = body_void if outcome == "void" else body_fail
        rc, out, err = run(["gh", "issue", "create", "--repo", REPO, "--title", title,
                            "--body", body, "--label", LABEL])
        if rc != 0:
            print(f"\nEXIT 3: could not create the issue: {err.strip()[:200]}", file=sys.stderr)
            return 3
        print(f"  opened: {out.strip()}", file=sys.stderr)
    elif action in ("comment", "void-comment"):
        note = (f"Still failing on `{version}` as of {run_url}" if outcome == "fail"
                else f"Still unable to resolve a version as of {run_url}")
        rc, out, err = run(["gh", "issue", "comment", str(number), "--repo", REPO,
                            "--body", note])
        if rc != 0:
            print(f"\nEXIT 3: could not comment: {err.strip()[:200]}", file=sys.stderr)
            return 3
        print(f"  commented on #{number}", file=sys.stderr)
    elif action == "close":
        rc, out, err = run(["gh", "issue", "close", str(number), "--repo", REPO,
                            "--comment",
                            f"`upstream-watch` now renders this chart cleanly on "
                            f"`{version}`: {run_url}"])
        if rc != 0:
            print(f"\nEXIT 3: could not close: {err.strip()[:200]}", file=sys.stderr)
            return 3
        print(f"  closed #{number}", file=sys.stderr)
    else:
        print("  nothing to do", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
