#!/usr/bin/env python3
"""Resolve the newest oasgen-provider release that is actually usable.

WHY THIS EXISTS. Our smoke matrix pins two versions: the declared floor
(`0.52.0`) and the platform pin (`0.74.0`). Both are frozen, and **every break
that cost anyone time this week was in a BRAND-NEW release**:

  0.85.0   `- name: health` indented one level too deep -> the rdc Deployment
           asset does not parse -> 0 of 25 RestDefinitions regenerate
  0.85.1   `rdc.livenessProbe` is `{}` rather than absent -> the apiserver
           refuses a probe with no handler -> no generated controller at all

A fixed-version arm cannot see either. The first reader of both was a consumer's
cluster, and in the second case this repo's own dispatched probe. So the thing
worth running on a schedule is **the newest release, resolved at run time**.

WHAT "USABLE" MEANS HERE, and each clause is a defect we actually hit:

  1. NOT RETRACTED. The provider publishes `retracted-versions.yaml`; a version
     listed there must never be pinned. Read it, and VOID if it cannot be parsed
     rather than assuming nothing is retracted -- an unreadable retraction list
     is the one input whose absence must stop the run.
  2. ACTUALLY PUBLISHED. A tag is not a release. `0.83.0` is retracted with the
     reason "TAGGED AND NEVER PUBLISHED", and `0.85.1` existed as a tag for ~36
     minutes before its charts appeared. So resolve from what the registry
     SERVES, never from the tag list.
  3. BOTH CHARTS, IN STEP. `oasgen-provider` and `oasgen-provider-crds` are
     declared members of one version line; a version where only one is pullable
     is not installable.

Exit codes: 0 a version was resolved (printed to stdout), 2 usage,
            3 VOID (nothing could be resolved, or an input was unreadable).
            VOID IS NOT "no new version" -- it means the question was not asked.
"""

import json
import re
import subprocess
import sys

REPO = "krateo-platformops/oasgen-provider"
CHARTS = ("oasgen-provider", "oasgen-provider-crds")
REGISTRY = "oci://ghcr.io/krateo-platformops/charts"

# A version we KNOW is published, used as a positive control on every run: if it
# does not pull, the instrument is broken and no absence below means anything.
CONTROL_VERSION = "0.74.0"

PLAIN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def vkey(v):
    """Sort key. STRING SORT IS WRONG and the repo has the counterexample:
    '0.85.10' sorts BELOW '0.85.9' lexically, and this provider has already
    shipped a .10 (our own chart is at 0.1.10)."""
    m = PLAIN.match(v)
    if not m:
        return None
    return tuple(int(p) for p in m.groups())


def plain_versions(names):
    """Only X.Y.Z. A pre-release or a 'v'-prefixed tag is not a candidate."""
    return [n for n in names if vkey(n) is not None]


def newest(versions):
    ok = [v for v in versions if vkey(v) is not None]
    return max(ok, key=vkey) if ok else None


def exclude(versions, retracted):
    return [v for v in versions if v not in set(retracted)]


def classify_pull(rc, out_len):
    """pullable | absent. An error body is short; a chart's metadata is not."""
    return "pullable" if (rc == 0 and out_len > 40) else "absent"


def tags_cmd():
    """The tag listing, as a value. See pull_cmd for why these are factored out."""
    return ["gh", "api", f"repos/{REPO}/tags?per_page=100", "-q", ".[].name"]


def retraction_cmd():
    """The retraction-list read, as a value."""
    return ["gh", "api", f"repos/{REPO}/contents/retracted-versions.yaml",
            "-H", "Accept: application/vnd.github.raw"]


def pull_cmd(chart, version):
    """The chart pull, as a value -- and this is the one that matters.

    FACTORED OUT BECAUSE THE TESTS COULD NOT SEE IT. Measured on 2026-11-21,
    with these commands inline, two mutations passed all 13 arms:

      the registry pointed at a wrong org   ->  0 arms failed
      `--version` dropped from the pull     ->  0 arms failed

    The second is the dangerous one: without `--version`, `helm show chart`
    returns the LATEST chart regardless of what was asked, so `pullable()` would
    answer "yes" for every version string -- including a tag that was never
    published. That is precisely the defect the "actually published" clause
    exists to prevent, and the clause would have reported success while being
    structurally unable to fail.

    THE RULE, in the provider session's words, which are better than mine: make
    the impure boundary a VALUE you can assert on, rather than an argument you
    accept. The arms below do exactly that.
    """
    return ["helm", "show", "chart", f"{REGISTRY}/{chart}", "--version", version]


def run(cmd, timeout=180):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def tags():
    rc, out, err = run(tags_cmd())
    if rc != 0:
        return None, err.strip()[:200]
    return [l.strip() for l in out.split("\n") if l.strip()], None


def retracted_versions():
    """Returns (list, error). The list may be empty ONLY if the file parsed and
    genuinely held no entries -- which would itself be suspicious, so the caller
    treats an empty list as unreadable."""
    rc, out, err = run(retraction_cmd())
    if rc != 0 or len(out) < 200:
        return None, f"rc={rc} bytes={len(out)} {err.strip()[:120]}"
    try:
        import yaml
        doc = yaml.safe_load(out)
    except Exception as exc:                                  # noqa: BLE001
        return None, f"unparseable: {exc}"
    ents = (doc or {}).get("retracted")
    if not ents:
        return None, "parsed but holds no 'retracted' entries"
    vs = []
    for e in ents:
        v = e.get("versions") or e.get("version")
        vs.extend(v if isinstance(v, list) else [v])
    return [str(v) for v in vs if v], None


def pullable(version):
    """Both charts must pull. Returns (bool, detail)."""
    detail = []
    for c in CHARTS:
        rc, out, _ = run(pull_cmd(c, version))
        state = classify_pull(rc, len(out))
        detail.append(f"{c}={state}")
        if state != "pullable":
            return False, " ".join(detail)
    return True, " ".join(detail)


def self_test(stream=None):
    """Arms, printed to `stream` (default stdout).

    THE STREAM IS A PARAMETER FOR ONE MEASURED REASON. In the resolve path,
    STDOUT CARRIES THE ANSWER -- the workflow does `v=$(... resolve ...)` and
    writes `version=$v` to $GITHUB_OUTPUT. With the arms also on stdout, `$v`
    captured 20 lines of self-test output and GitHub refused the write:

        ##[error]Invalid format '  string sort would get this wrong   PASS'

    Measured on run 44916780758, the first run that resolved for real. The
    resolution itself was correct -- it read nine retracted versions, excluded
    0.85.0 and 0.85.1, confirmed both charts of 0.85.2 and printed `RESOLVED
    0.85.2` -- and the answer was destroyed on its way out.
    """
    out = stream or sys.stdout
    checks = []

    def t(name, want, got):
        checks.append((name, want, got, want == got))

    # THE SORT. The counterexample is not hypothetical: our own chart is 0.1.10.
    t("0.85.10 is newer than 0.85.9", "0.85.10", newest(["0.85.9", "0.85.10"]))
    t("string sort would get this wrong", True, "0.85.10" < "0.85.9")
    t("newest across minors", "0.86.0", newest(["0.85.9", "0.86.0", "0.9.0"]))
    t("newest of one", "1.2.3", newest(["1.2.3"]))
    t("newest of none is None", None, newest([]))
    t("a non-plain version is not a candidate", None, newest(["v1.2.3", "1.2.3-rc1"]))

    # THE FILTER, both directions.
    t("plain_versions drops v-prefixed and pre-release", ["1.2.3"],
      plain_versions(["v1.2.3", "1.2.3", "1.2.3-rc1", "1.2"]))
    t("exclude removes a retracted version", ["0.84.0"],
      exclude(["0.84.0", "0.85.0"], ["0.85.0"]))
    t("exclude removes SEVERAL", ["0.84.0"],
      exclude(["0.84.0", "0.85.0", "0.85.1"], ["0.85.1", "0.85.0"]))
    t("exclude with nothing retracted keeps all", ["0.84.0", "0.85.0"],
      exclude(["0.84.0", "0.85.0"], []))

    # THE COMMAND BOUNDARIES, because every arm above takes its inputs as
    # ARGUMENTS and therefore cannot see what builds them. Measured: with these
    # commands inline, pointing the registry at a wrong org failed 0 arms, and
    # dropping `--version` from the pull failed 0 arms -- the latter making every
    # version read as published.
    pc = pull_cmd("oasgen-provider", "1.2.3")
    t("the pull asks for the VERSION requested", "1.2.3",
      pc[pc.index("--version") + 1] if "--version" in pc else "<no --version>")
    t("the pull names the krateo-platformops registry", True,
      any("ghcr.io/krateo-platformops/charts" in a for a in pc))
    t("the pull names the chart it was given", True,
      any(a.endswith("/oasgen-provider") for a in pc))
    t("a different version produces a different command", True,
      pull_cmd("oasgen-provider", "1.2.3") != pull_cmd("oasgen-provider", "1.2.4"))
    t("the retraction read targets the right file", True,
      any("retracted-versions.yaml" in a for a in retraction_cmd()))
    t("the retraction read asks for RAW, not the JSON envelope", True,
      any("vnd.github.raw" in a for a in retraction_cmd()))
    t("the tag listing targets the provider repo", True,
      any("krateo-platformops/oasgen-provider" in a for a in tags_cmd()))

    # PULLABILITY: the third outcome is the point -- an error body is short, and
    # a short body must never read as a published chart.
    t("rc=0 with real output is pullable", "pullable", classify_pull(0, 400))
    t("rc=1 is absent", "absent", classify_pull(1, 400))
    t("rc=0 with a tiny body is ABSENT, not pullable", "absent", classify_pull(0, 12))

    width = max(len(c[0]) for c in checks)
    failed = 0
    for name, want, got, good in checks:
        print("  {:<{w}}  {}".format(
            name, "PASS" if good else "FAIL want={!r} got={!r}".format(want, got), w=width),
            file=out)
        failed += 0 if good else 1
    print("\n  {} passed, {} failed".format(len(checks) - failed, failed), file=out)
    if failed:
        print("\nFAIL: the sort, the filter or the pull classifier is wrong", file=sys.stderr)
        return 1
    print("  OK: version ordering, retraction filtering and pull classification all discriminate",
          file=out)
    return 0


def main(argv):
    if len(argv) > 1 and argv[1] == "--self-test":
        if len(argv) > 2:
            print("--self-test takes no further arguments", file=sys.stderr)
            return 2
        print("== pure functions (no network)\n")
        return self_test()
    if len(argv) > 1:
        print(__doc__)
        print("usage: resolve-upstream-oasgen.py [--self-test]", file=sys.stderr)
        return 2

    # STDERR, because stdout is the ANSWER. See self_test's docstring.
    print("== pure functions, before any network read\n", file=sys.stderr)
    if self_test(stream=sys.stderr):
        return 1

    print("\n== resolving", file=sys.stderr)

    # CONTROL FIRST. If a version known to be published does not pull, every
    # "absent" below is a statement about the instrument, not about a release.
    ok, detail = pullable(CONTROL_VERSION)
    print(f"  CONTROL {CONTROL_VERSION}: {detail}", file=sys.stderr)
    if not ok:
        print("\nVOID: the control version does not pull, so the registry read is broken.\n"
              "  No conclusion about any newer release is possible.", file=sys.stderr)
        return 3

    rets, err = retracted_versions()
    if rets is None:
        print(f"\nVOID: could not read the retraction list ({err}).\n"
              "  Resolving a 'newest' release without it could select a version the\n"
              "  publisher has withdrawn, which is worse than resolving nothing.",
              file=sys.stderr)
        return 3
    print(f"  retracted ({len(rets)}): {sorted(rets)}", file=sys.stderr)

    names, err = tags()
    if names is None:
        print(f"\nVOID: could not list tags ({err}).", file=sys.stderr)
        return 3
    cands = sorted(exclude(plain_versions(names), rets), key=vkey, reverse=True)
    print(f"  candidates, newest first: {cands[:8]}", file=sys.stderr)
    if not cands:
        print("\nVOID: no plain X.Y.Z tag survives the retraction filter.", file=sys.stderr)
        return 3

    # Walk down from the newest until one is actually SERVED. A tag is not a
    # release, and this loop is the only place that distinction is enforced.
    for v in cands[:10]:
        ok, detail = pullable(v)
        print(f"  {v}: {detail}", file=sys.stderr)
        if ok:
            print(f"\n  RESOLVED {v} -- newest tag that is not retracted and whose "
                  f"two charts both pull", file=sys.stderr)
            print(v)                                    # stdout: the answer
            return 0

    print("\nVOID: none of the 10 newest non-retracted tags has both charts published.\n"
          "  That is a statement about the registry, not about our chart.", file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv))
