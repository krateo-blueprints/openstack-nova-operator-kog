#!/usr/bin/env python3
r"""Catch regex escapes whose meaning differs between grep implementations.

WHY THIS EXISTS. `grep -E '^[A-Za-z]+Configuration\t'` matched on this
machine and matched nothing on CI, because in an ERE BSD grep reads `\t` as a
tab and GNU grep reads it as a literal `t`. hack/probe-apikey-rbac.sh shipped
with that pattern and every result it had ever produced came from a Mac; on a
Linux runner it reported a confident VOID about a healthy cluster. CI found it
the first time it executed that path.

TWO MODES, AND THE SECOND IS THE POINT:

  (default)   scan the files that RUN in CI and fail on a divergent escape.
  --measure   re-derive the divergence table by RUNNING each grep that is
              reachable, so the allowlist below is a measurement and not a
              belief. Takes --docker-image to add a Linux grep.

THE ALLOWLIST IS DERIVED, NOT ASSUMED. `\\s` is permitted because it was
measured to mean whitespace in every implementation tested, not because it
looks safe. Re-run --measure after any toolchain change; if a permitted escape
starts diverging, this file is where that belief is recorded and must change.
"""
from __future__ import annotations
import argparse, pathlib, re, shutil, subprocess, sys, tempfile

ROOTS = ("hack", "scripts", ".github")
EXTS = {".sh", ".bash", ".yaml", ".yml", ".py"}
TOOLCHAIN = re.compile(r"\b(grep|egrep|fgrep|sed|awk)\b")

# Escapes measured to diverge between implementations -> flagged.
DIVERGENT = "tdD"
# Escapes measured to mean the same thing everywhere tested -> permitted.
PERMITTED = "sSwWbB<>"
ESCAPE_RE = re.compile(r"\\([a-zA-Z<>])")

# A `\t` inside a kubectl `-o jsonpath=...` is JSONPath's OWN escape, parsed by
# the client before any grep sees it, and it emits a real tab on every platform.
# Flagging it would churn correct code; this is the distinction oasgen-provider
# raised and it is why "a \t in a script" is not one category.
JSONPATH_CTX = re.compile(r"jsonpath|go-template|-o\s+json")


def strip_comment(line: str) -> str:
    r"""Return the executable part of a line, with any trailing comment removed.

    QUOTE-AWARE ON PURPOSE. `grep -E '#'` is legitimate code and cutting at the
    first `#` would silently stop scanning it. Shell and YAML both start a
    comment at a `#` that is outside quotes and at line start or preceded by
    whitespace, which is the rule implemented here. Python's `#` follows the
    same shape closely enough for this scanner's purpose.

    This function has its own arms in _self_check() below, because a stripper
    that cut too much would make the whole scan silently vacuous -- it would
    report a clean repository by having read almost none of it.
    """
    out, quote, prev = [], None, ""
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "#" and (prev == "" or prev.isspace()):
            break
        else:
            out.append(ch)
        prev = ch
    return "".join(out)


def _self_check() -> list[str]:
    r"""Arms for strip_comment, in both directions. Returns a list of failures.

    WRITTEN AS RAW STRINGS DELIBERATELY. The first version of this block was
    generated through a non-raw Python literal and every `\t` in it became a
    real tab, so the arms tested the stripper against a character the scanner
    does not look for and all four code arms failed. The arms caught it, which
    is the only reason it is not in the repository.
    """
    cases = [
        # (line, must a backslash-t survive in the executable part?)
        (r"grep -E '\t' f.txt", True),
        (r"grep -E '\t' f.txt  # trailing note about \d", True),
        (r"# grep -E '\t' -- a comment, not code", False),
        (r"    # indented comment mentioning \t", False),
        (r"grep -E '#\t' f.txt", True),         # '#' inside quotes is data
        (r'echo "a#b" | grep -E ' + r"'\t'", True),  # '#' inside double quotes
        (r"X=1 # set it; later grep -E '\t'", False),
    ]
    fails = []
    for line, want in cases:
        got = r"\t" in strip_comment(line)
        if got != want:
            fails.append(f"strip_comment({line!r}) kept-backslash-t={got}, want {want}")
    return fails


def scan(roots=ROOTS) -> tuple[list[tuple[str, int, str, str]], int, bool]:
    files: list[pathlib.Path] = []
    for r in roots:
        p = pathlib.Path(r)
        if p.exists():
            files += [f for f in p.rglob("*") if f.suffix in EXTS and f.is_file()]

    # PLANTED CONTROL. Without it, a zero is indistinguishable from a scanner
    # whose pattern never fires -- which is the exact defect this scanner is
    # for. The control file is scanned like any other and must be reported.
    ctrl_dir = pathlib.Path(tempfile.mkdtemp())
    ctrl = ctrl_dir / "_portability_control.sh"
    ctrl.write_text(
        "grep -E '^Alpha\\t' f.txt\n"      # \t -> must be flagged
        "grep -E 'id-\\d+' f.txt\n"        # \d -> must be flagged
        "grep -E '^\\s*x' f.txt\n"         # \s -> must be permitted, not flagged
        "kubectl get crd -o jsonpath='{.a}{\"\\t\"}{.b}'\n"  # jsonpath -> exempt
    )
    files.append(ctrl)

    hits, ctrl_hits = [], 0
    for f in files:
        try:
            lines = f.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for i, raw in enumerate(lines, 1):
            # SELF-EXEMPTION. This file documents the escapes it flags, in prose
            # and in its own planted control, so scanning itself would report a
            # permanent failure about text that never executes. A detector that
            # matches its own pattern is a defect this repo has already shipped
            # once.
            if f.name == pathlib.Path(__file__).name:
                continue
            line = strip_comment(raw)
            if not TOOLCHAIN.search(line):
                continue
            found = {e for e in ESCAPE_RE.findall(line) if e in DIVERGENT}
            if not found:
                continue
            if JSONPATH_CTX.search(line):
                continue
            if f == ctrl:
                ctrl_hits += 1
            else:
                hits.append((str(f), i, "".join(sorted(found)), line.strip()[:96]))
    shutil.rmtree(ctrl_dir, ignore_errors=True)
    # 2 of the control's 4 lines must be flagged: \t and \d. The \s line must
    # NOT be (it is permitted) and the jsonpath line must NOT be (it is exempt),
    # so this number checks the scanner in BOTH directions at once.
    return hits, ctrl_hits, ctrl_hits == 2


# --------------------------------------------------------------------------
# --measure: run the escapes rather than believe them.
# --------------------------------------------------------------------------
CASES = [
    # name, pattern, line matching the CLASS reading, line matching the LITERAL reading
    ("\\t", r"a\tb", "a\tb", "atb"),
    ("\\d", r"a\db", "a1b", "adb"),
    ("\\s", r"a\sb", "a b", "asb"),
    ("\\w", r"a\wb", "a_b", "awb"),
    ("\\b", r"\bzz\b", "zz", "bzzb"),
    ("\\<", r"\<zz", "zz", "<zz"),
]
CONTROL = ("control a.b", "a.b", "a-b", "a+b")


def verdict(runner, pattern, class_line, literal_line) -> str:
    r"""CLASS / LITERAL / BOTH / NEITHER -- a label encoding TWO match bits.

    It is a pair, not a name, on purpose. For some escapes the two readings
    cannot be separated by any single line: the literal character of `\w` is
    itself a word character, so no line matches the literal reading without
    also matching the class one, and `\w` reads BOTH everywhere. That is still
    a usable signal, because divergence is detected as a DIFFERENCE between
    columns: an implementation that read `\w` literally would fail the
    class-only line and report LITERAL against the others' BOTH. A label that
    collapsed to "supported/unsupported" would lose that.
    """
    c = runner(pattern, class_line)
    l = runner(pattern, literal_line)
    if c is None or l is None:
        return "ERR"
    if c and not l:
        return "CLASS"
    if l and not c:
        return "LITERAL"
    if c and l:
        return "BOTH"
    return "NEITHER"


def local_runner(binary):
    def run(pattern, line):
        try:
            r = subprocess.run([binary, "-E", pattern], input=line + "\n",
                               capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            return None
        return bool(r.stdout.strip())
    return run


def docker_runner(image):
    def run(pattern, line):
        try:
            r = subprocess.run(
                ["docker", "run", "--rm", "-i", "--entrypoint", "sh", image, "-c",
                 'grep -E "$0"', pattern],
                input=line + "\n", capture_output=True, text=True, timeout=180)
        except (OSError, subprocess.SubprocessError):
            return None
        return bool(r.stdout.strip())
    return run


def measure(docker_image: str | None) -> int:
    impls: list[tuple[str, object]] = []
    for label, path in (("PATH grep", "grep"), ("/usr/bin/grep", "/usr/bin/grep")):
        if shutil.which(path) or pathlib.Path(path).exists():
            impls.append((label, local_runner(path)))
    if docker_image:
        impls.append((f"docker {docker_image}", docker_runner(docker_image)))
    if not impls:
        print("VOID: no grep implementation was reachable, so nothing was measured.")
        return 9

    names = [lbl for lbl, _ in impls]
    print(f"  {'escape':<8}" + "".join(f"{n[:22]:>24}" for n in names))
    # CONTROL FIRST. `a.b` must read BOTH lines in every column; a column that
    # fails it is a broken instrument and its other cells mean nothing.
    lbl, pat, cl, ll = CONTROL
    ctl = [verdict(r, pat, cl, ll) for _, r in impls]
    print(f"  {lbl:<8}" + "".join(f"{v:>24}" for v in ctl))
    broken = [names[i] for i, v in enumerate(ctl) if v != "BOTH"]
    if broken:
        print(f"\nVOID: the control did not read both lines in: {', '.join(broken)}.")
        print("      Those columns measured nothing; do not read the table above.")
        return 9
    rows = {}
    for name, pat, cl, ll in CASES:
        vs = [verdict(r, pat, cl, ll) for _, r in impls]
        rows[name] = vs
        flag = "  <-- DIVERGES" if len(set(vs)) > 1 else ""
        print(f"  {name:<8}" + "".join(f"{v:>24}" for v in vs) + flag)
    print()
    diverge = {n.lstrip('\\') for n, vs in rows.items() if len(set(vs)) > 1}
    agreed = {n.lstrip('\\') for n, vs in rows.items() if len(set(vs)) == 1}
    print(f"  measured divergent: {sorted(diverge) or 'none'}")
    print(f"  measured in agreement: {sorted(agreed) or 'none'}")
    stale = sorted(diverge & set(PERMITTED))
    if stale:
        print(f"\nFAIL: {stale} is on this file's PERMITTED list but just measured as")
        print("      divergent. The allowlist is a recorded measurement and it is now")
        print("      wrong -- update DIVERGENT/PERMITTED here, then re-run the scan.")
        return 1
    missing = sorted(agreed & set(DIVERGENT))
    if missing:
        print(f"\nNOTE: {missing} is flagged as divergent but measured in agreement across")
        print("      the implementations reachable here. That is not a failure: an")
        print("      implementation this run could not reach may still differ.")
    print("\n  allowlist is consistent with what was just measured.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--measure", action="store_true",
                    help="re-derive the divergence table by running each reachable grep")
    ap.add_argument("--docker-image", default=None,
                    help="an image whose grep to include in --measure (e.g. a Linux GNU grep)")
    a = ap.parse_args()
    if a.measure:
        return measure(a.docker_image)

    sc = _self_check()
    if sc:
        print("VOID: strip_comment() does not behave as specified, so the scan would")
        print("      have read the wrong part of every line:")
        for m in sc:
            print(f"        {m}")
        return 9
    print(f"  CONTROL: strip_comment passed {7} arms (code kept, comments dropped, '#' in quotes kept)")
    hits, ctrl_hits, ctrl_ok = scan()
    print(f"  CONTROL: planted lines flagged: {ctrl_hits} of 2 expected "
          f"(\\t and \\d flagged; \\s permitted and jsonpath exempt must NOT be)")
    if not ctrl_ok:
        print("\nVOID: the scanner did not flag exactly its own planted control lines, so")
        print("      a clean result would be a statement about this scanner rather than")
        print("      about the repository. Nothing was checked.")
        return 9
    print(f"  flagged escapes: {sorted(DIVERGENT)}   permitted (measured equal): {sorted(PERMITTED)}")
    if not hits:
        print("  no divergent regex escape in any file that runs in CI.")
        return 0
    print(f"\nFAIL: {len(hits)} divergent regex escape(s) in files that run in CI.")
    for f, i, e, line in hits:
        print(f"  {f}:{i}  [\\{e}]  {line}")
    print("\n  Use a literal character instead: TAB=$(printf '\\t') and then \"...$TAB\".")
    print("  A JSONPath `\\t` inside -o jsonpath= is the client's escape and is exempt.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
