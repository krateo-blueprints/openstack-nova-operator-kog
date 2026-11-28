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

IT READS TWO FILES PER VERSION, because one file only CONSUMES the paths:

  deploy/authsecretrbac.go            CONSUMES the auth-secret paths (the
                                      collector the controls below were taken on)
  oas2jsonschema/auth_secret_refs.go  DERIVES them: since at least 0.74.0 it
                                      builds the path list by reflecting over the
                                      auth-method shapes, one path per field of
                                      type SecretKeySelector, for every method,
                                      sorted. authsecretrbac.go:41 is just
                                      `var authSecretRefPaths =
                                      oas2jsonschema.AuthSecretRefPaths()`.

A change in the deriver would move the grant while a consumer-only reading kept
saying "safe" -- the same shape as the 0.54.0 break itself, where a hand-written
list covered basic's two refs and bearer's one, under a comment that accurately
said "basic and bearer" while the generator already emitted a third shape.
Reading the consumer alone cannot see that, so both are read and each gets a row.

A file that cannot be read is UNREAD, and a file that is not THERE is ABSENT;
neither is "0 mentions", and neither is safe. A version is `safe` only when BOTH
files were actually measured and both meet their expectations.

A `SAFE` verdict here is NOT a statement that a version is usable -- 0.85.0 is
safe by this measure and cannot create a single controller. Installability is a
separate question with separate evidence.

THE CONTROLS ARE NOT OPTIONAL, and this script refuses to report without them:

  consumer: 0.54.0 must read 0 mentions at a healthy byte count (known broken)
            0.74.0 must read 3 mentions                          (known safe)
  deriver:  0.54.0 must be ABSENT (404), 0.74.0 must be PRESENT

THE DERIVER'S CONTROL IS PRESENCE, NOT CONTENT, on purpose. Its sha and mention
counts are printed as DATA for comparison and asserted nowhere: a content
assertion would fail the day upstream legitimately edits the file, and this repo
has had three pins go stale exactly that way. Presence is the one property that
cannot move without the question itself changing.

If any control disagrees, every other number in the run is unreadable and the exit
is VOID rather than a verdict. "0 mentions" and "I fetched an error page" are
the same integer, and a sweep that cannot tell them apart has reported both as
facts in this repo before.

Exit codes: 0 all versions measured and safe, 1 a version reads UNSAFE,
            2 usage error, 3 VOID (a control disagreed, or a read failed, or the
            deriver is absent/unread at a version that is not a control).
"""

import hashlib
import re
import subprocess
import sys

# Both paths, because the repo became a monorepo and the old one 404s. Trying
# both and REPORTING WHICH WAS USED is the difference between a measurement and
# a guess: a 404 on the first path is not evidence about the symbol.
PATHS = (
    "go/oasgen-provider/internal/tools/deploy/authsecretrbac.go",
    "internal/tools/deploy/authsecretrbac.go",
)

# Where the paths are derived. Same monorepo-then-old-layout order as PATHS; at
# 0.54.0 neither exists, and that ABSENCE is the deriver's negative control.
DERIVER_PATHS = (
    "go/oasgen-provider/internal/tools/oas2jsonschema/auth_secret_refs.go",
    "internal/tools/oas2jsonschema/auth_secret_refs.go",
)

# version -> expected mentions in the CONSUMER. Measured, recorded, and
# re-asserted every run.
CONTROLS = {"0.54.0": 0, "0.74.0": 3}

# version -> expected state of the DERIVER. PRESENCE only; see the docstring for
# why no sha or count is asserted.
DERIVER_CONTROLS = {"0.54.0": "absent", "0.74.0": "measured"}

# A body smaller than this is an error page or a truncated read, never the file:
# the real file is ~16-17KB at every tag measured so far.
MIN_PLAUSIBLE_BYTES = 4000
# The deriver is ~3.7KB, so it needs its own floor; a 404's JSON body is ~100.
DERIVER_MIN_BYTES = 1500

_PACKAGE_LINE = re.compile(rb"^package ", re.MULTILINE)


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


def read_result(*, rc, stdout, stderr, min_bytes):
    """Classify ONE fetch as measured | absent | unread. A pure value.

    Three outcomes because two of them look identical as a count:

      measured  rc 0, a `^package ` line, and at least min_bytes. A 404's JSON body
                and a real Go file are both "some bytes"; only the package line
                says it is Go source.
      absent    the server said 404 and there is no Go source in the body. This is
                a FACT about the tag (the file does not exist there), never "0
                mentions".
      unread    anything else: a rate limit, a timeout, a 5xx, a truncated body.
                Says nothing about the file in either direction.
    """
    has_package = bool(_PACKAGE_LINE.search(stdout))
    if rc == 0 and has_package and len(stdout) >= min_bytes:
        return "measured"
    if not has_package and (
        b"HTTP 404" in stderr or b'"status":"404"' in stdout
        or b'"status": "404"' in stdout
    ):
        return "absent"
    return "unread"


def fetch_file(paths, tag, min_bytes):
    """Return dict(state, path, body, bytes, err). Tries each path in order.

    measured if any path measured; absent only if EVERY path was absent; any
    other mix is unread, because "one layout 404ed and the other timed out" is
    not evidence the file is missing.
    """
    states, tried = [], []
    for path in paths:
        proc = subprocess.run(
            fetch_cmd(path=path, tag=tag), capture_output=True, timeout=120,
        )
        state = read_result(rc=proc.returncode, stdout=proc.stdout,
                            stderr=proc.stderr, min_bytes=min_bytes)
        if state == "measured":
            body = proc.stdout.decode("utf-8", "replace")
            # `bytes` is len() of the DECODED text, as every recorded count in
            # docs/configuration.md is (17276 for the consumer at 0.74.0). The
            # raw byte length is 8 higher there (non-ASCII comments); changing
            # the unit would make this run disagree with every number on record.
            return dict(state=state, path=path, body=body,
                        raw=proc.stdout, bytes=len(body), err=None)
        states.append(state)
        tried.append("{}: {} rc={} bytes={}".format(
            path, state, proc.returncode, len(proc.stdout)))
    state = "absent" if all(x == "absent" for x in states) else "unread"
    return dict(state=state, path=None, body=None, raw=b"", bytes=0,
                err="; ".join(tried))


def fetch(tag):
    """The consumer, as (path_used, body, error). Kept for its callers."""
    r = fetch_file(PATHS, tag, MIN_PLAUSIBLE_BYTES)
    return r["path"], r["body"], r["err"]


def version_verdict(*, tag, consumer_state, consumer_class, deriver_state):
    """safe | unsafe | void, from BOTH files.

    void whenever either read is unmeasured. The deriver counts as measured when
    it was read, or when it is ABSENT at a tag whose control says absent -- there
    the 404 is the expected fact. Absent anywhere else is void: a file that
    should be there and is not is a question the instrument could not answer.
    """
    if consumer_state != "measured" or consumer_class == "unread":
        return "void"
    expected_absent = DERIVER_CONTROLS.get(tag) == "absent"
    if not (deriver_state == "measured" or (deriver_state == "absent" and expected_absent)):
        return "void"
    return consumer_class


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


def sha_prefix(raw):
    return hashlib.sha256(raw).hexdigest()[:12]


# NOTE ON "apiKey mentions" for the deriver: this counts OCCURRENCES, so
# auth_secret_refs.go reads 7 (line 19 holds both `"apiKey"` and `APIKeyAuth`),
# while `grep -ic` counts LINES and reads 6. Both are right about different
# things; the consumer's recorded 3 is an occurrence count, so this matches it.
def render_deriver_row(*, tag, state, byte_count, sha, apikey, selectors, note=""):
    """One row for the DERIVER. Counts are DATA, never a verdict: only presence is
    asserted, and ABSENT is printed as ABSENT rather than as zeros."""
    if state == "measured":
        cells = "present | {} | `{}` | {} | {}".format(byte_count, sha, apikey, selectors)
    elif state == "absent":
        cells = "**ABSENT** (404) | — | — | — | —"
    else:
        cells = "UNREAD | — | — | — | —"
    return "| `{}` | {}{} |".format(tag, cells, note)


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

    # THE SECOND FILE. Each arm below fails if its subject is broken; the mutation
    # that proves it is in the comment.
    pkg = b"package oas2jsonschema\n" + b"x" * 3000
    notfound = b'{"message":"Not Found","status":"404"}'
    t("a 200 Go body with a package line is measured", "measured",
      read_result(rc=0, stdout=pkg, stderr=b"", min_bytes=DERIVER_MIN_BYTES))
    # Mutation: drop the 404 branch -> this reads "unread", not "absent".
    t("a 404-shaped JSON body reads ABSENT, not 0 mentions", "absent",
      read_result(rc=1, stdout=notfound, stderr=b"gh: Not Found (HTTP 404)",
                  min_bytes=DERIVER_MIN_BYTES))
    t("a 404 body even at rc 0 is ABSENT, not measured", "absent",
      read_result(rc=0, stdout=notfound, stderr=b"", min_bytes=DERIVER_MIN_BYTES))
    # Mutation: drop the package requirement -> a long HTML/JSON body reads measured.
    t("a long body with NO package line is UNREAD, not measured", "unread",
      read_result(rc=0, stdout=b"<html>" + b"y" * 5000, stderr=b"", min_bytes=DERIVER_MIN_BYTES))
    t("a rate limit is UNREAD, not absent", "unread",
      read_result(rc=1, stdout=b'{"message":"API rate limit exceeded"}',
                  stderr=b"gh: API rate limit exceeded (HTTP 403)", min_bytes=DERIVER_MIN_BYTES))
    t("a package line under the byte floor is UNREAD", "unread",
      read_result(rc=0, stdout=b"package x\n", stderr=b"", min_bytes=DERIVER_MIN_BYTES))
    t("a real Go file that merely mentions HTTP 404 is still measured", "measured",
      read_result(rc=0, stdout=pkg + b"// HTTP 404", stderr=b"", min_bytes=DERIVER_MIN_BYTES))
    t("read_result REFUSES positional arguments", True,
      _positional_rejected(read_result, 0, b"", b"", 1))

    # THE VERDICT ACROSS BOTH FILES.
    t("both measured and consumer safe -> safe", "safe",
      version_verdict(tag="0.98.0", consumer_state="measured", consumer_class="safe",
                      deriver_state="measured"))
    t("consumer unsafe with a deriver present -> unsafe", "unsafe",
      version_verdict(tag="0.54.1", consumer_state="measured", consumer_class="unsafe",
                      deriver_state="measured"))
    t("the deriver ABSENT at the 0.54.0 control is EXPECTED, not void", "unsafe",
      version_verdict(tag="0.54.0", consumer_state="measured", consumer_class="unsafe",
                      deriver_state="absent"))
    t("the deriver ABSENT at a NON-control version is VOID, not safe", "void",
      version_verdict(tag="0.98.0", consumer_state="measured", consumer_class="safe",
                      deriver_state="absent"))
    t("the deriver ABSENT at the 0.74.0 control (expected present) is VOID", "void",
      version_verdict(tag="0.74.0", consumer_state="measured", consumer_class="safe",
                      deriver_state="absent"))
    # The arm that makes the second read non-decorative: the consumer alone says
    # safe, and only the deriver's state changes the answer.
    t("consumer reads safe but the deriver is UNREAD -> VOID", "void",
      version_verdict(tag="0.98.0", consumer_state="measured", consumer_class="safe",
                      deriver_state="unread"))
    t("the two files can DISAGREE: same consumer, different deriver, different verdict", True,
      version_verdict(tag="0.98.0", consumer_state="measured", consumer_class="safe",
                      deriver_state="measured")
      != version_verdict(tag="0.98.0", consumer_state="measured", consumer_class="safe",
                         deriver_state="unread"))
    t("an unread consumer is VOID even with the deriver present", "void",
      version_verdict(tag="0.98.0", consumer_state="unread", consumer_class="unread",
                      deriver_state="measured"))
    t("version_verdict REFUSES positional arguments", True,
      _positional_rejected(version_verdict, "t", "measured", "safe", "measured"))

    # THE DERIVER CONTROLS must be non-empty and must DISAGREE, and the deriver
    # fetch must really ask for the deriver's file.
    t("the deriver has two controls", 2, len(DERIVER_CONTROLS))
    t("the deriver controls DISAGREE (absent vs measured)", True,
      len(set(DERIVER_CONTROLS.values())) == 2)
    t("the deriver fetch names auth_secret_refs.go", True,
      any("auth_secret_refs.go" in a for a in fetch_cmd(path=DERIVER_PATHS[0], tag="0.98.0")))
    t("the deriver fetch is pinned to the tag requested", True,
      any("ref=0.98.0" in a for a in fetch_cmd(path=DERIVER_PATHS[0], tag="0.98.0")))
    t("a measured deriver row says present", True,
      "present" in render_deriver_row(tag="0.98.0", state="measured", byte_count=3714,
                                      sha="d904185f55f0", apikey=6, selectors=1))
    t("an absent deriver row says ABSENT and prints no zeros", True,
      "ABSENT" in render_deriver_row(tag="0.54.0", state="absent", byte_count=0,
                                     sha="", apikey=0, selectors=0)
      and " 0 " not in render_deriver_row(tag="0.54.0", state="absent", byte_count=0,
                                          sha="", apikey=0, selectors=0))
    t("an unread deriver row says UNREAD", True,
      "UNREAD" in render_deriver_row(tag="9.9.9", state="unread", byte_count=0,
                                     sha="", apikey=0, selectors=0))

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
    print("  OK: the counter, the classifier, both-file verdict and the row renderers all discriminate")
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
    print("\n== reading authsecretrbac.go (consumer) and auth_secret_refs.go (deriver) at {} tag(s)\n".format(len(todo)))

    results = {}
    for tag in todo:
        c = fetch_file(PATHS, tag, MIN_PLAUSIBLE_BYTES)
        d = fetch_file(DERIVER_PATHS, tag, DERIVER_MIN_BYTES)
        if c["state"] == "measured":
            mentions = count_apikey(c["body"])
            cls = classify(mentions=mentions, byte_count=c["bytes"])
        else:
            mentions, cls = 0, "unread"
        dm = dict(apikey=0, selectors=0, sha="")
        if d["state"] == "measured":
            dm = dict(apikey=count_apikey(d["body"]),
                      selectors=d["body"].count("SecretKeySelector"),
                      sha=sha_prefix(d["raw"]))
        results[tag] = dict(
            path=c["path"], mentions=mentions, bytes=c["bytes"], verdict=cls,
            c_state=c["state"], err=c["err"],
            d_state=d["state"], d_path=d["path"], d_bytes=d["bytes"], d_err=d["err"], **dm)
        results[tag]["overall"] = version_verdict(
            tag=tag, consumer_state=c["state"], consumer_class=cls, deriver_state=d["state"])
        print("  {:<9} consumer {:<9} bytes={:<7} apikey={:<3} {:<7} | deriver {:<9} bytes={:<6} sha={:<12} apikey={} selector={}".format(
            tag, c["state"], c["bytes"], mentions, cls, d["state"], d["bytes"],
            dm["sha"] or "-", dm["apikey"], dm["selectors"]))
        for who, r in (("consumer", c), ("deriver", d)):
            if r["state"] != "measured":
                print("            {} {}: {}".format(who, r["state"].upper(), r["err"]))

    print("\n== controls, asserted before any verdict is reported\n")
    control_ok = True
    for tag, expected in CONTROLS.items():
        got = results[tag]
        state = "OK" if (got["verdict"] != "unread" and got["mentions"] == expected) else "DISAGREES"
        print("  {:<9} consumer: expected {} mention(s), read {} ({})  {}".format(
            tag, expected, got["mentions"], got["verdict"], state))
        if state != "OK":
            control_ok = False
    for tag, expected in DERIVER_CONTROLS.items():
        got = results[tag]["d_state"]
        state = "OK" if got == expected else "DISAGREES"
        print("  {:<9} deriver:  expected {}, read {}  {}".format(tag, expected.upper(), got.upper(), state))
        if state != "OK":
            control_ok = False
    if not control_ok:
        print("\nVOID: a control disagrees with its recorded value, so the reader is\n"
              "  wrong and every other number in this run is unreadable. This is NOT a\n"
              "  finding about any version -- fix the instrument first.", file=sys.stderr)
        return 3
    print("\n  OK: every control holds. The consumer's differ (0 vs 3) and the deriver's\n"
          "  differ (absent vs present), so a reading is a fact about the file rather\n"
          "  than about the reader.")

    void = [t for t in asked if results[t]["overall"] == "void"]
    if void:
        print("\nVOID: {} requested version(s) could not be fully measured: {}\n"
              "  A version is safe only when BOTH files were read. An unread or absent\n"
              "  file is not a safe one, and absence is not '0 mentions'.".format(len(void), void),
              file=sys.stderr)
        return 3

    print("\n== rows for docs/configuration.md (controls included for comparison)\n")
    print("CONSUMER, deploy/authsecretrbac.go\n")
    print("| version | collector covers `apiKey` | bytes | verdict | module |")
    print("|---|---|---|---|---|")
    for tag in todo:
        r = results[tag]
        note = "" if tag not in CONTROLS else " — CONTROL"
        print(render_row(tag=tag, path=r["path"], mentions=r["mentions"],
                         byte_count=r["bytes"], verdict=r["verdict"], note=note))
    print("\nDERIVER, oas2jsonschema/auth_secret_refs.go -- counts are DATA, only presence is a control\n")
    print("| version | file | bytes | sha256[:12] | apiKey mentions | SecretKeySelector |")
    print("|---|---|---|---|---|---|")
    for tag in todo:
        r = results[tag]
        note = "" if tag not in DERIVER_CONTROLS else " — CONTROL"
        print(render_deriver_row(tag=tag, state=r["d_state"], byte_count=r["d_bytes"],
                                 sha=r["sha"], apikey=r["apikey"],
                                 selectors=r["selectors"], note=note))

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
          "  create a controller at all. And a safe reading is a READ of two\n"
          "  source files, not a run.".format(len(asked)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
