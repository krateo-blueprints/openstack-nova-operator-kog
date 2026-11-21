#!/usr/bin/env python3
"""Measure the apiKey secret grant at a list of oasgen-provider versions.

WHY THIS EXISTS. The safe-set table in docs/configuration.md has gone stale
TWICE in four days, and both times a peer noticed rather than this repo:

  2026-11-18  the table stopped at 0.56.0 while README told readers to install
              0.74.0 -- the version a reader is told to install sat 18 minors
              outside everything measured.
  2026-11-21  the table stopped at 0.74.0 while the deployment that actually
              runs these KOGs installs 0.80.0 itself -- six minors outside.

Each gap was closed by a hand audit. The provider releases roughly daily, so a
hand audit is a method that works exactly as often as someone happens to look.
This turns it into one dispatch.

WHAT IT MEASURES, and the limits are the point. One property: does the
provider's auth-secret RBAC collector mention `apiKey` at all? 0.54.0 removed a
blanket secrets grant on the reasoning that a narrow Role already covered the
same ground; that Role's paths were three hand-written ones and the generator
also emits apiKey-in-a-header, whose tokenRef was never added. All six of this
chart's documents are apiKey, so this chart is the instrument for that class.

A `SAFE` verdict here is NOT a statement that a version is usable -- 0.85.0 is
safe by this measure and cannot create a single controller. Installability is a
separate question with separate evidence.

THE CONTROLS ARE NOT OPTIONAL, and this script refuses to report without them:

  0.54.0 must read 0 mentions at a healthy byte count  (known broken)
  0.74.0 must read 3 mentions                           (known safe)

If either disagrees, every other number in the run is unreadable and the exit
is VOID rather than a verdict. "0 mentions" and "I fetched an error page" are
the same integer, and a sweep that cannot tell them apart has reported both as
facts in this repo before.

Exit codes: 0 all versions measured and safe, 1 a version reads UNSAFE,
            2 usage error, 3 VOID (a control disagreed, or a read failed).
"""

import json
import subprocess
import sys

# Both paths, because the repo became a monorepo and the old one 404s. Trying
# both and REPORTING WHICH WAS USED is the difference between a measurement and
# a guess: a 404 on the first path is not evidence about the symbol.
PATHS = (
    "go/oasgen-provider/internal/tools/deploy/authsecretrbac.go",
    "internal/tools/deploy/authsecretrbac.go",
)

# version -> expected mentions. Measured, recorded, and re-asserted every run.
CONTROLS = {"0.54.0": 0, "0.74.0": 3}

# A body smaller than this is an error page or a truncated read, never the file:
# the real file is ~16-17KB at every tag measured so far.
MIN_PLAUSIBLE_BYTES = 4000


def fetch_cmd(*, path, tag):
    """The content read, as a pure value so a self-test arm can inspect it.

    FACTORED OUT BECAUSE THE DANGEROUS MUTATION IS INVISIBLE OTHERWISE, measured
    2026-11-21: pinning the ref (`?ref=main` instead of `?ref={tag}`) passed all
    14 arms. Every version would then read the SAME file and the table would
    report identical counts regardless of tag -- so a version could be recorded
    SAFE on the strength of a different version's content.

    The byte floor cannot catch that one. It protects against a short error body;
    a wrong-but-valid file is long and plausible, which is exactly the shape that
    gets believed.

    The rule, in the provider session's phrasing: make the impure boundary a
    VALUE you can assert on, rather than an argument you accept.
    """
    return ["gh", "api",
            "repos/krateo-platformops/oasgen-provider/contents/"
            "{}?ref={}".format(path, tag),
            "-H", "Accept: application/vnd.github.raw"]


def fetch(tag):
    """Return (path_used, body, error). Exactly one of body/error is truthy."""
    tried = []
    for path in PATHS:
        proc = subprocess.run(
            fetch_cmd(path=path, tag=tag), capture_output=True, text=True, timeout=120,
        )
        if proc.returncode == 0 and len(proc.stdout) >= MIN_PLAUSIBLE_BYTES:
            return path, proc.stdout, None
        tried.append(
            "{}: rc={} bytes={}".format(path, proc.returncode, len(proc.stdout))
        )
    return None, None, "; ".join(tried)


def count_apikey(body):
    """Case-insensitive, exactly as the recorded measurements counted it."""
    return body.lower().count("apikey")


def classify(*, mentions, byte_count):
    """safe | unsafe | unread.

    A zero with a healthy byte count is a FACT about the file. A zero with no
    bytes is a failed read, and calling that 'unsafe' would be a fabricated
    finding -- the direction that matters, because an alarm is actioned.
    """
    if byte_count < MIN_PLAUSIBLE_BYTES:
        return "unread"
    return "safe" if mentions >= 1 else "unsafe"


def render_row(*, tag, path, mentions, byte_count, verdict, note=""):
    return "| `{}` | {} | {} | {} | {}{} |".format(
        tag,
        "**yes** ({} mentions)".format(mentions) if verdict == "safe" else
        ("**no** ({} mentions)".format(mentions) if verdict == "unsafe" else "UNREAD"),
        byte_count if byte_count else "—",
        "fixed" if verdict == "safe" else ("**BROKEN**" if verdict == "unsafe" else "—"),
        path.split("/internal")[0] if path else "neither path read",
        note,
    )


def self_test():
    """Every function that decides a verdict, in both directions."""
    checks = []

    def t(name, want, got):
        checks.append((name, want, got, want == got))

    # THE COUNTER. Case-insensitivity is not cosmetic: the collector's switch is
    # case-insensitive and the recorded numbers counted it that way.
    t("counts a lowercase apikey", 1, count_apikey('case "apikey":'))
    t("counts mixed case too", 2, count_apikey('ApiKey apiKey'))
    t("counts nothing in an unrelated file", 0, count_apikey("package deploy"))

    # THE CLASSIFIER, and the third outcome is the whole reason it exists.
    t("3 mentions in a full file is safe", "safe", classify(mentions=3, byte_count=17276))
    t("0 mentions in a FULL file is unsafe", "unsafe", classify(mentions=0, byte_count=16010))
    t("0 mentions in an EMPTY body is UNREAD, not unsafe", "unread", classify(mentions=0, byte_count=0))
    t("3 mentions in a tiny body is UNREAD, not safe", "unread", classify(mentions=3, byte_count=120))
    t("a byte count just under the floor is unread", "unread",
      classify(mentions=5, byte_count=MIN_PLAUSIBLE_BYTES - 1))
    t("a byte count at the floor is measured", "safe",
      classify(mentions=5, byte_count=MIN_PLAUSIBLE_BYTES))

    # THE FETCH BOUNDARY, because every arm above takes its inputs as ARGUMENTS
    # and so cannot see what builds them. Measured: with the command inline,
    # pinning the ref to `main` passed all 14 arms -- every version would read
    # the same file and the table would report identical counts per tag.
    fc = fetch_cmd(path=PATHS[0], tag="0.80.0")
    t("the fetch asks for the TAG requested", True,
      any("ref=0.80.0" in a for a in fc))
    t("a different tag produces a different command", True,
      fetch_cmd(path=PATHS[0], tag="0.80.0") != fetch_cmd(path=PATHS[0], tag="0.84.0"))
    t("the fetch names the collector file", True,
      any("authsecretrbac.go" in a for a in fc))
    t("the fetch asks for RAW, not the JSON envelope", True,
      any("vnd.github.raw" in a for a in fc))

    # KEYWORD-ONLY, because a positional swap is invisible to every arm here.
    # Measured: swapping `classify(mentions, len(body))` at its call site failed
    # 0 of 18 arms. The live controls WOULD have caught it -- 0.54.0 would read
    # "unread" and disagree with its recorded 0 -- so it was detectable at
    # runtime and invisible to the test. Prevention is cheaper: `*` makes a
    # positional call a TypeError at the call rather than a VOID an hour later.
    def _positional_rejected(fn, *args):
        try:
            fn(*args)
            return False
        except TypeError:
            return True
    t("classify REFUSES positional arguments", True, _positional_rejected(classify, 3, 17276))
    t("fetch_cmd REFUSES positional arguments", True, _positional_rejected(fetch_cmd, "p", "t"))
    t("render_row REFUSES positional arguments", True,
      _positional_rejected(render_row, "t", "p", 1, 2, "safe"))

    # THE CONTROL SET must be non-empty and must disagree with each other, or
    # asserting them proves nothing.
    t("there are two controls", 2, len(CONTROLS))
    t("the controls DISAGREE, so the instrument is discriminating", True,
      len(set(CONTROLS.values())) == 2)

    # THE ROW RENDERER must not silently emit a safe-looking row for an unread
    # version: the table is pasted into docs, so a wrong row outlives the run.
    t("an unread row says UNREAD", True, "UNREAD" in render_row(tag="9.9.9", path=None, mentions=0, byte_count=0, verdict="unread"))
    t("an unsafe row says BROKEN", True, "BROKEN" in render_row(tag="0.54.0", path=PATHS[0], mentions=0, byte_count=16010, verdict="unsafe"))
    t("a safe row says fixed", True, "fixed" in render_row(tag="0.74.0", path=PATHS[0], mentions=3, byte_count=17276, verdict="safe"))

    width = max(len(c[0]) for c in checks)
    failed = 0
    for name, want, got, good in checks:
        print("  {:<{w}}  {}".format(name, "PASS" if good else
                                     "FAIL want={!r} got={!r}".format(want, got), w=width))
        if not good:
            failed += 1
    print("\n  {} passed, {} failed".format(len(checks) - failed, failed))
    if failed:
        print("\nFAIL: a verdict function is wrong, so no measurement would be readable",
              file=sys.stderr)
        return 1
    print("  OK: the counter, the classifier and the row renderer all discriminate")
    return 0


def main(argv):
    if len(argv) == 1 or argv[1] in ("-h", "--help"):
        print(__doc__)
        print("usage: measure-apikey-grant.py --self-test")
        print("       measure-apikey-grant.py <version> [<version> ...]")
        return 2
    if argv[1] == "--self-test":
        if len(argv) > 2:
            print("--self-test takes no further arguments; got {!r}".format(argv[2:]),
                  file=sys.stderr)
            return 2
        print("== verdict functions (no network)\n")
        return self_test()

    asked = list(dict.fromkeys(argv[1:]))  # de-duplicated, order kept
    bad = [v for v in asked if not all(p.isdigit() for p in v.split("."))]
    if bad:
        print("not plain X.Y.Z versions: {!r}".format(bad), file=sys.stderr)
        return 2

    print("== verdict functions, before any network read\n")
    if self_test():
        return 1

    # The controls are measured in the SAME RUN as the subjects, through the same
    # code path. A control verified yesterday says nothing about today's reader.
    todo = list(dict.fromkeys(list(CONTROLS) + asked))
    print("\n== reading authsecretrbac.go at {} tag(s)\n".format(len(todo)))

    results = {}
    for tag in todo:
        path, body, err = fetch(tag)
        if body is None:
            results[tag] = dict(path=None, mentions=0, bytes=0, verdict="unread", err=err)
            print("  {:<9} UNREAD -- {}".format(tag, err))
            continue
        mentions = count_apikey(body)
        verdict = classify(mentions=mentions, byte_count=len(body))
        results[tag] = dict(path=path, mentions=mentions, bytes=len(body),
                            verdict=verdict, err=None)
        print("  {:<9} bytes={:<7} apikey={:<3} {:<7} {}".format(
            tag, len(body), mentions, verdict, path))

    print("\n== controls, asserted before any verdict is reported\n")
    control_ok = True
    for tag, expected in CONTROLS.items():
        got = results[tag]
        state = "OK" if (got["verdict"] != "unread" and got["mentions"] == expected) else "DISAGREES"
        print("  {:<9} expected {} mention(s), read {} ({})  {}".format(
            tag, expected, got["mentions"], got["verdict"], state))
        if state != "OK":
            control_ok = False
    if not control_ok:
        print("\nVOID: a control disagrees with its recorded value, so the reader is\n"
              "  wrong and every other number in this run is unreadable. This is NOT a\n"
              "  finding about any version -- fix the instrument first.", file=sys.stderr)
        return 3
    print("\n  OK: both controls hold, and they differ (0 vs 3), so a reading is a fact\n"
          "  about the file rather than about the reader.")

    unread = [t for t in asked if results[t]["verdict"] == "unread"]
    if unread:
        print("\nVOID: {} requested version(s) could not be read: {}\n"
              "  An unread version is not a safe one.".format(len(unread), unread),
              file=sys.stderr)
        return 3

    print("\n== rows for docs/configuration.md (controls included for comparison)\n")
    print("| version | collector covers `apiKey` | bytes | verdict | module |")
    print("|---|---|---|---|---|")
    for tag in todo:
        r = results[tag]
        note = "" if tag not in CONTROLS else " — CONTROL"
        print(render_row(tag=tag, path=r["path"], mentions=r["mentions"],
                         byte_count=r["bytes"], verdict=r["verdict"], note=note))

    print("\n  NOTE ON BYTE COUNTS. These are this script's own, read through\n"
          "  `gh api` with the raw media type. Numbers recorded by hand on\n"
          "  2026-11-18 are 7 higher on every tag -- an equal offset across\n"
          "  immutable tags is a property of the fetch, not the files. Compare\n"
          "  this script's numbers with this script's numbers.")

    unsafe = [t for t in asked if results[t]["verdict"] == "unsafe"]
    if unsafe:
        print("\nFINDING: {} requested version(s) read UNSAFE: {}\n"
              "  The collector does not mention apiKey, so generated controllers\n"
              "  cannot read an apiKey tokenRef. Do not pin these.".format(
                  len(unsafe), unsafe), file=sys.stderr)
        return 1
    print("\n  All {} requested version(s) are SAFE by this one measure. That is not\n"
          "  a statement that they are usable: 0.85.0 reads safe here and cannot\n"
          "  create a controller at all.".format(len(asked)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
