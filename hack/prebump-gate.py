#!/usr/bin/env python3
"""Refuse an oasgen-provider bump that could move this chart's generated GVK.

Usage:
    hack/prebump-gate.py 0.48.0 0.49.0

WHY THIS IS A SCRIPT AND NOT A HABIT. These checks were run by hand on every
bump in this repo and recorded in commit messages. That protects nothing: the
next bump is one `sed` away, and a check that lives in a previous commit message
is a check nobody runs. A gate in the repo is a gate.

WHAT IT PROTECTS, precisely. The resource CRD's apiVersion is DERIVED from the
OAS `info.version` by `crdgen.NormalizeVersionName`, reached through
`TargetVersion`/`DefaultVersion` in oasgen's `render.go`. This chart's seven
resource CRs are written against `v2-1`, and its Configuration CRs against
`v1alpha1`. If that derivation moves, every CR in `chart/samples/`,
`examples/` and `hack/probe-361.sh` becomes unapplicable with
`no matches for kind` -- which is indistinguishable from a missing CRD, and cost
three rounds of investigation the one time it happened.

So the gate asserts the two things that derivation depends on:

    go/oasgen-provider/internal/tools/render/render.go   unchanged
    the `plumbing` version in go.mod                     unchanged
                                                         (the normalizer itself
                                                          lives in that module)

WHAT IT DELIBERATELY DOES NOT DO. It does not require the whole `render/`
directory to be byte-identical. That was the gate as originally practised, and
0.49.0 broke it by adding `cel_cost_generated_test.go` -- 150 lines of test, no
behaviour. Blocking a bump on that would have been wrong, and loosening the
gate to "the directory is close enough" would have been a workaround that
silently stops protecting anything. The honest fix is for the gate to name what
it actually protects, so it is stated as files rather than as a directory.

A changed TEST file under render/ is reported as information and does not fail.

WHAT IT REFUSES TO DO -- report a pass it cannot support. Every one of these is
the same defect: a negative result indistinguishable from not having run.

  compare returns no files     exit 3 VOID. Two tags with no differences means
                               either they are the same commit or the query did
                               not do what was asked. "No protected file
                               changed" would be true and worthless.
  a protected path is absent   exit 3 VOID. A file that does not exist cannot
    at either tag               change, so "unchanged" would be vacuous. This
                               is not hypothetical: oasgen's Go module moved
                               from the repo root to `go/oasgen-provider/`
                               between 0.19.0 and 0.20.0, so one path cannot
                               address both eras.
  the API call fails           exit 2. Never silence.

Needs `gh` authenticated. Reads only; touches nothing.
"""
import hashlib
import json
import subprocess
import sys

REPO = "krateo-platformops/oasgen-provider"

# Files whose change could move the derived apiVersion. Stated as exact paths,
# because a directory would catch test files and a glob would hide a rename.
PROTECTED = (
    "go/oasgen-provider/internal/tools/render/render.go",
)
GOMOD = "go/oasgen-provider/go.mod"
PLUMBING = "github.com/krateo-platformops/plumbing"


# A REVIEWED DIFF MAY BE ACKNOWLEDGED, AND THE ACK IS PINNED TO THAT EXACT DIFF.
#
# The gate is mechanical: a protected file changed, so it refuses. It cannot
# know whether the change moved the derived apiVersion or merely moved code
# around. Without a way to record "I read this specific diff and here is what it
# was", the only ways past a refusal are to weaken the gate or to skip the step
# -- and both of those are permanent, silent and apply to every future bump.
#
# So an ack is: one version pair, one path, and the SHA-256 of the patch as the
# compare API returns it. If upstream changes that file again between the same
# two tags (a re-tag), or if the ack is copied to a different pair, the hash no
# longer matches and the gate REFUSES AGAIN -- it does not fall back to the ack.
# An ack can therefore never become a blanket exemption, which is the failure
# mode of every "allowlist" that is keyed by filename alone.
#
# `finding` is the reason, written to be read by whoever hits the next refusal.
ACKS = {
    ("0.50.0", "0.52.0", "go/oasgen-provider/internal/tools/render/render.go"): {
        "patch_sha256": "8e33ed3dc4af8d5a07bd45ef54f4fd30475019920dd3c624768d112decf4d9ff",
        "finding": (
            "PURE EXTRACTION, no version expression changed. The inline "
            "VerbsDescription->Verb and ConfigurationField shims moved out to "
            "toDomainVerbs() and toDomainResourceConfig() in helper.go (+68, "
            "which is why that file grew). `Version: DefaultVersion` is "
            "untouched; the one changed expression is "
            "`len(configurationFields)` -> `len(resourceConfig.ConfigurationFields)`, "
            "the same value through the new struct. CONFIRMED FROM THE "
            "ARTIFACTS, not only the diff: generated on 0.52.0 in kind, all "
            "twelve CRDs served, resource halves at v2-1 and Configuration "
            "halves at v1alpha1 -- the split this gate exists to protect."
        ),
    },
}


def patch_sha256(patch):
    return hashlib.sha256(patch.encode("utf-8")).hexdigest()

def gh_json(*args):
    try:
        out = subprocess.run(("gh",) + args, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"gh invocation failed ({e}). This is VOID, not a pass.", file=sys.stderr)
        sys.exit(2)
    if out.returncode != 0:
        print(f"gh {' '.join(args)} failed:\n{out.stderr.strip()}\n"
              f"This is VOID, not a pass.", file=sys.stderr)
        sys.exit(2)
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError as e:
        print(f"gh returned non-JSON ({e}). This is VOID, not a pass.", file=sys.stderr)
        sys.exit(2)


def path_exists_at(ref, path):
    """Precondition: a path that does not exist cannot meaningfully be 'unchanged'.

    The ref goes in the QUERY STRING, not as `-f ref=...`. `gh api -f` sends a
    request BODY field, which makes the call a POST; the endpoint then answers
    about the default branch and the `.sha` jq filter yields nothing, so every
    path looks absent. That is exactly how the first version of this gate
    failed -- and it failed LOUDLY, refusing with VOID rather than reporting
    "nothing protected changed", which is the only reason it was caught here
    instead of in a bump that silently moved the apiVersion.
    """
    out = subprocess.run(
        ("gh", "api", f"repos/{REPO}/contents/{path}?ref={ref}", "--jq", ".sha"),
        capture_output=True, text=True, timeout=120)
    return out.returncode == 0 and out.stdout.strip() != ""


def main():
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 64
    old, new = sys.argv[1], sys.argv[2]
    print(f"  comparing {REPO} {old}...{new}")

    # Precondition before any verdict: every protected path must exist at BOTH
    # refs. Checked first, because the whole gate is vacuous otherwise.
    missing = []
    for path in PROTECTED + (GOMOD,):
        for ref in (old, new):
            if not path_exists_at(ref, path):
                missing.append(f"{path} @ {ref}")
    if missing:
        print(f"  VOID: these protected paths do not exist, so 'unchanged' would "
              f"be vacuous:\n" + "\n".join(f"    {m}" for m in missing) +
              f"\n  The upstream layout has moved before (module root -> "
              f"go/oasgen-provider/ between 0.19.0 and 0.20.0). Update PROTECTED "
              f"rather than trusting this run.", file=sys.stderr)
        return 3
    print(f"  precondition: all {len(PROTECTED) + 1} protected paths exist at both refs")

    cmp = gh_json("api", f"repos/{REPO}/compare/{old}...{new}")
    files = cmp.get("files") or []
    if not files:
        print(f"  VOID: the comparison reports NO changed files. Either {old} and "
              f"{new} are the same commit or the query did not ask what I think "
              f"it asked. 'No protected file changed' would be true and "
              f"worthless.", file=sys.stderr)
        return 3
    changed = {f.get("filename"): f for f in files}
    print(f"  {len(changed)} files changed between the tags")

    problems, notes = [], []

    for path in PROTECTED:
        if path not in changed:
            print(f"  OK   {path} unchanged")
            continue
        detail = (f"{path} CHANGED (+{changed[path].get('additions', '?')} "
                  f"-{changed[path].get('deletions', '?')}) -- this file carries "
                  f"TargetVersion/DefaultVersion, so the derived resource "
                  f"apiVersion may have moved.")
        ack = ACKS.get((old, new, path))
        if ack is None:
            problems.append(f"{detail} Read the diff before bumping; every CR in "
                            f"this repo depends on it. If you have read it and it "
                            f"does not move the apiVersion, record an ACK in "
                            f"hack/prebump-gate.py -- the ack is pinned to this "
                            f"exact patch's SHA-256, so it cannot cover a later "
                            f"change to the same file.")
            continue
        patch = changed[path].get("patch") or ""
        if not patch:
            problems.append(f"{detail} An ACK exists for this pair, but the API "
                            f"returned NO patch, so it cannot be checked against "
                            f"the diff it claims to cover. VOID for this check "
                            f"rather than honoured.")
            continue
        got = patch_sha256(patch)
        if got != ack["patch_sha256"]:
            problems.append(f"{detail} An ACK exists for this pair but it is "
                            f"STALE: it was recorded against patch sha256 "
                            f"{ack['patch_sha256'][:16]}... and the diff now "
                            f"hashes to {got[:16]}.... The file changed again "
                            f"between the same two refs, so the ack's finding no "
                            f"longer describes it. Re-read the diff and re-record "
                            f"-- this gate does not fall back to a stale ack.")
            continue
        print(f"  ACK  {path} changed (+{changed[path].get('additions','?')} "
              f"-{changed[path].get('deletions','?')}), and the diff matches the "
              f"recorded ack (sha256 {got[:16]}...):")
        for line in ack["finding"].split(". "):
            if line.strip():
                print(f"         {line.strip().rstrip('.')}.")

    gomod = changed.get(GOMOD)
    if gomod is None:
        print(f"  OK   {GOMOD} unchanged, so the plumbing pin is too")
    else:
        patch = gomod.get("patch") or ""
        if not patch:
            problems.append(f"{GOMOD} changed but the API returned no patch, so "
                            f"the plumbing pin cannot be read. VOID for this "
                            f"check rather than assumed safe.")
        else:
            touched = [ln for ln in patch.splitlines()
                       if ln[:1] in "+-" and ln[1:2] != ln[:1] and PLUMBING in ln]
            if touched:
                problems.append(f"{GOMOD} moves the plumbing pin, which OWNS "
                                f"NormalizeVersionName:\n" +
                                "\n".join(f"      {ln}" for ln in touched))
            else:
                print(f"  OK   {GOMOD} changed, but not its {PLUMBING} line")

    for path in sorted(changed):
        if path.startswith("go/oasgen-provider/internal/tools/render/") and path not in PROTECTED:
            notes.append(f"{path} changed (+{changed[path].get('additions','?')}) "
                         f"-- under render/ but not protected"
                         f"{' ; a test file, no behaviour' if path.endswith('_test.go') else ''}")

    if notes:
        print("  notes:")
        for n in notes:
            print(f"    {n}")

    if problems:
        print("\n".join(f"  REFUSE: {p}" for p in problems), file=sys.stderr)
        return 1
    print(f"  PASS: nothing the derived apiVersion depends on changed "
          f"between {old} and {new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
