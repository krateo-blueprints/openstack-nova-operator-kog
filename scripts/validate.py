#!/usr/bin/env python3
"""Repo guards that need no cluster, so they can run on every PR.

Each check here exists because the defect it screens for was SHIPPING in this
repository, not because it seemed plausible. The three were found on 2026-10-29
and reported across the Krateo blueprint repos; `aruba-provider-kog` hit all
three independently and contributed the original implementations.

Every check asserts it can detect a known-bad input AND that it does not
false-positive on a known-good one, before it trusts its own sweep. A screen
that reports "all clear" without proving it can catch a known-bad case is just
a function that returns false. That is not hypothetical here: a sweep for the
removed auth-bridge matched `auth-bridge` and `authBridge` but not "auth
bridge" with a space, reported clean, and had missed ten prose mentions
including the docs landing page. An INCOMPLETE pattern fails exactly like a
dead one.
"""
import glob
import json
import os
import re
import subprocess
import sys
import warnings

import yaml

# Docs that are dated evidence or a changelog: they record what was true at a
# past version ON PURPOSE, and rewriting them would destroy the record. The
# published articles additionally carry a superseded banner at the top, so a
# reader is told at the point of reading rather than having to notice a date.
HISTORICAL_DOCS = (
    "docs/medium-article.md",
    "docs/medium-openstack-compute-operator.md",
    "docs/log.md",
    "docs/release.md",
)


def _docs():
    for f in sorted(glob.glob("**/*.md", recursive=True)):
        if f.startswith(".git" + os.sep) or os.sep + "charts" + os.sep in f:
            continue
        yield f


def verify_composition_apiversion(errors):
    """No file may hardcode a `composition.krateo.io/<version>` apiVersion.

    core-provider derives the Composition CRD's served version from the chart
    version as `"v" + strings.ReplaceAll(chartVersion, ".", "-")` -- including
    the PATCH, so chart 0.1.3 is served at v0-1-3 and any literal is wrong the
    moment the chart is bumped. In THIS repo it is unconditionally wrong:
    chart/Chart.yaml carries `version: CHART_VERSION`, substituted at release,
    so at no commit does a correct literal exist to write.

    Writing one costs more than a stale doc. `no matches for kind` is what the
    API server says both when the apiVersion is wrong and when the CRD was
    never created, so the reader debugs the wrong layer entirely.

    The addressable form is to read what core-provider actually created:

        kubectl get compositiondefinition <name> -n <ns> \\
            -o jsonpath='{.status.apiVersion}'
        kubectl get compositiondefinition <name> -n <ns> \\
            -o jsonpath='{.status.resource}'    # the plural
    """
    pat = re.compile(r"composition\.krateo\.io/v[0-9A-Za-z]")
    assert pat.search("apiVersion: composition.krateo.io/v1alpha1")
    assert pat.search("composition.krateo.io/v0-3-8")
    assert not pat.search("apiVersion: core.krateo.io/v1alpha1")
    assert not pat.search("composition.krateo.io/<APIVERSION>")
    assert not pat.search("kubectl get compositiondefinition x -o json")

    files = list(_docs()) + [
        f for f in sorted(glob.glob("**/*.yaml", recursive=True))
        if not f.startswith(".git" + os.sep) and os.sep + "charts" + os.sep not in f
    ]
    for f in files:
        if f.replace(os.sep, "/") in HISTORICAL_DOCS:
            continue
        with open(f, encoding="utf-8", errors="replace") as fh:
            for n, line in enumerate(fh, 1):
                if pat.search(line):
                    errors.append(
                        f"{f}:{n}: hardcoded Composition apiVersion -- core-provider "
                        f"derives it from the FULL chart version, and this chart's "
                        f"version is a release-time placeholder, so no correct literal "
                        f"exists. Read it instead: kubectl get compositiondefinition "
                        f"<name> -n <ns> -o jsonpath='{{.status.apiVersion}}'")


def verify_documented_pin(errors):
    # KNOWN FALSE-POSITIVE SHAPE, measured by injection on 2026-11-21: a version
    # inside a DELIBERATELY-WRONG example counts as a documented pin. Appending
    #
    #     Do NOT do this:
    #     ```console
    #     $ helm install nova oci://.../openstack-nova-operator-kog --version 0.0.1
    #     ```
    #
    # makes this screen fail, naming 0.0.1. So this page cannot currently show a
    # bad install command as a cautionary example without tripping its own gate.
    #
    # NOT FIXED, DELIBERATELY, and the reasoning is the same as the filename
    # screen's: it fails SAFE and loudly, the failure names the version, and no
    # document here needs a negative install example. If one ever does, the fix
    # is a convention this screen can read -- not a looser match, which would
    # stop catching the real defect it was written for (six docs pinning 0.1.9
    # after 0.1.10 shipped).
    """Every documented oasgen-provider install must name a version this chart
    is actually correct on.

    THE PROPERTY IS THE DECLARED RANGE, NOT EQUALITY TO CI'S PIN, and the change
    is deliberate. This screen used to demand that every documented version equal
    OASGEN_VERSION in .github/workflows/validate.yaml. That was a PROXY for the
    thing worth protecting -- its own next paragraph says the hazard is paths
    *below their floor* -- and the proxy broke the moment the README began
    documenting the platform's `0.74.0` while validate keeps exercising the
    floor. Two correct versions serving two different purposes is not drift.
    What would be drift is a documented version below the floor, or one of the
    two versions the floor declaration excludes, and that is what is checked now.
    The floor and its exclusions are read from README.md's single machine-readable
    declaration -- the same line verify_provider_floor_declaration validates --
    so there is still exactly one place either can be changed.

    The docs spell versions out in prose, so nothing otherwise stops them
    drifting; `aruba-provider-kog` had four documented paths below their floor,
    with no stale registry involved at all.

    The pin is not a preference. apiKey/in-header arrived in 0.19.0, and 0.26.1
    is a hard floor: below it the `krateo.io/connector-verbose` request dump
    redacted only values it had been TOLD were sensitive, so credential HEADERS
    went to pod logs in cleartext -- and this chart's credential IS a header,
    X-Auth-Token. Verbose is precisely what you switch on to debug a 401.

    Also screens the install SOURCE. Three resolve and only one is right:
    krateo-platformops (live), krateoplatformops (abandoned, disjoint tags, and
    NOT archived so it is indistinguishable by measurement), and
    charts.krateo.io -- which serves appVersion 0.10.0, below both floors, from
    the dead org's GitHub Pages. See oasgen-provider#278.
    """
    wf = os.path.join(".github", "workflows", "validate.yaml")
    if not os.path.exists(wf):
        errors.append(f"{wf}: missing -- nothing defines OASGEN_VERSION to check "
                      f"the docs against")
        return
    m = re.search(r'^\s*OASGEN_VERSION:\s*"([^"]+)"', open(wf).read(), flags=re.M)
    if not m:
        errors.append(f"{wf}: no OASGEN_VERSION to check the docs against")
        return
    pin = m.group(1)

    chart = re.compile(r"charts/oasgen-provider(-crds)?\b")
    version = re.compile(r'--version\s+"?\$?\{?([0-9]+\.[0-9]+\.[0-9]+)')
    assert version.search("  --version 0.9.20 -n krateo-system").group(1) == "0.9.20"
    assert version.search('--version "0.40.0" -n x').group(1) == "0.40.0"
    assert version.search('--version "${OASGEN_VERSION}"') is None  # indirection is fine
    # PINNED: THE MATCHER IS CONTEXT-FREE BY DESIGN. A version inside a
    # deliberately-wrong example is collected like any other -- measured by
    # injection on 2026-11-21, where a "Do NOT do this" block pinning 0.0.1 made
    # this screen fail. Kept as an assert rather than only a comment, so that
    # teaching this screen about negative examples is a deliberate edit here
    # rather than an accident elsewhere.
    assert version.search("Do NOT do this: helm install x --version 0.0.1").group(1) == "0.0.1"
    assert chart.search("oci://ghcr.io/krateo-platformops/charts/oasgen-provider-crds")
    assert not chart.search("oci://ghcr.io/krateo-blueprints/charts/openstack-nova-operator-kog")

    bad_source = re.compile(r"ghcr\.io/krateoplatformops/|"
                            r"https?://charts\.krateo\.io|"
                            r"helm\s+repo\s+add\s+\S*\s+https?://charts\.krateo\.io")
    assert bad_source.search("helm install x oci://ghcr.io/krateoplatformops/charts/y")
    assert bad_source.search("helm repo add krateo https://charts.krateo.io")
    assert not bad_source.search("oci://ghcr.io/krateo-platformops/charts/oasgen-provider")

    # The range predicate, asserted against an EXPLICIT range so these do not
    # change meaning when README's declaration does.
    _r = ((0, 52, 0), [(0, 53, 0), (0, 54, 0)])
    assert _outside_declared_range("0.52.0", _r) == ""      # the floor itself
    assert _outside_declared_range("0.74.0", _r) == ""      # the platform pin
    assert "below the declared floor" in _outside_declared_range("0.51.0", _r)
    assert "excludes" in _outside_declared_range("0.53.0", _r)
    assert "excludes" in _outside_declared_range("0.54.0", _r)
    assert "SemVer" in _outside_declared_range("0.74", _r)
    # A MISSING DECLARATION IS NOT A PASS -- the one arm that keeps this screen
    # from going quiet if the floor line is ever deleted.
    assert "missing instrument" in _outside_declared_range("0.74.0", (None, []))

    for f in _docs():
        if f.replace(os.sep, "/") in HISTORICAL_DOCS:
            continue
        lines = open(f, encoding="utf-8", errors="replace").read().splitlines()
        for n, line in enumerate(lines):
            # Prose ABOUT the bad sources is the point of several docs, so only
            # flag them inside something that looks like a command.
            if bad_source.search(line) and re.search(r"\bhelm\b", line):
                errors.append(
                    f"{f}:{n+1}: install source is not ghcr.io/krateo-platformops -- "
                    f"that registry serves a build below the {pin} floor "
                    f"(oasgen-provider#278)")
            if not chart.search(line):
                continue
            # The --version may sit on the install line or a continuation. If it
            # cannot be located, say UNCHECKED rather than staying silent: a
            # silent skip reports "all clear" on an install it never examined,
            # which is the same way an incomplete pattern fails like a dead one.
            found = False
            for cand in lines[n:n + 3]:
                v = version.search(cand)
                if v:
                    found = True
                    why = _outside_declared_range(v.group(1))
                    if why:
                        errors.append(
                            f"{f}:{n+1}: documents oasgen-provider chart "
                            f"{v.group(1)}, which {why}")
                    break
            if not found and re.search(r"\bhelm\s+(install|upgrade)\b", line):
                errors.append(
                    f"{f}:{n+1}: installs the oasgen-provider chart but no --version "
                    f"within 3 lines -- this screen could not verify it against the "
                    f"declared range, so treat it as unchecked rather than clean")

    # CI'S OWN PIN IS HELD TO THE SAME RANGE. Before this, the docs were checked
    # against OASGEN_VERSION and OASGEN_VERSION was checked against nothing, so
    # the one value every document was compared to was the only one that could
    # drift below the floor unnoticed.
    why = _outside_declared_range(pin)
    if why:
        errors.append(f"{wf}: OASGEN_VERSION is {pin}, which {why}")


# The section in docs/configuration.md that every shared-cluster install must
# point at, as a link fragment. Kept as a constant so the screen below and the
# docs cannot drift apart silently.
PREFLIGHT_ANCHOR = "#installing-this-pin-onto-a-cluster-that-already-has-a-provider"
PREFLIGHT_DOC = "docs/configuration.md"


def verify_shared_cluster_preflight(errors):
    """An oasgen-provider install snippet must not assume the cluster is empty.

    `restdefinitions.ogen.krateo.io` is CLUSTER-SCOPED AND SINGLETON -- one
    object per cluster, shared by every KOG installed in it. So
    `helm upgrade --install oasgen-provider-crds --version <our pin>` is not a
    local act, and on a cluster the reader did not just create it can name an
    older build of an object other operators depend on. A newer CRD is a strict
    superset (39 field paths present at 0.74.0 and absent at 0.52.0, 0 the other
    way, measured 2026-11-18), so naming an older version can only lower it, and
    pruning is silent with a green apply.

    THIS SCREEN'S OWN PROSE USED TO SAY "installer 0.3.673 pins 0.74.0 while
    this repo pins 0.52.0". That stopped being true when the README's install
    snippet moved to 0.74.0, and it is recorded rather than quietly deleted
    because it is this repo's commonest defect in its own voice: a fact that was
    true, left standing as a current one. The requirement does not change with
    it -- a cluster running something NEWER than the documented version is
    downgraded just as silently, so the box is still required.

    This is the recurring shape in this repo, one layer out: not a wrong pin, a
    CORRECT pin in a snippet written as if the cluster were empty. Four of the
    five install snippets pointed at a reader-supplied cluster and said nothing;
    one of them asserted the versions already matched.

    THE EXEMPTION IS COMPUTED, NOT LISTED. A document that creates its own
    cluster earlier in its own text (`kind create cluster`) has nothing to check,
    and demanding the box there would be noise that teaches readers to skip
    boxes. Computing it means the exemption disappears by itself if that
    document stops creating a cluster -- a hardcoded filename would not.

    AND THE TARGET MUST EXIST. If the section in configuration.md were deleted,
    a screen that only looked for links would keep passing while every link it
    demanded pointed at nothing. The anchor is therefore verified against the
    document's own headings here, so this screen fails when the thing it
    requires is gone -- differently from how it fails when a link is missing.
    """
    heading_slug = PREFLIGHT_ANCHOR.lstrip("#")
    if not os.path.exists(PREFLIGHT_DOC):
        errors.append(f"{PREFLIGHT_DOC}: missing -- it holds the preflight every "
                      f"shared-cluster install snippet is required to link to")
        return
    # Reuses the slug helper verify_doc_link_fragments_resolve already uses, so
    # the two screens cannot disagree about what an anchor resolves to. A second
    # implementation here could call a link dead that the other calls live, or
    # the reverse, and the wrong one of those passes silently.
    slugs = set(_github_heading_slugs(
        open(PREFLIGHT_DOC, encoding="utf-8").read()))
    assert _github_heading_slugs(
        "### Installing this pin onto a cluster that already has a provider\n"
    ) == ["installing-this-pin-onto-a-cluster-that-already-has-a-provider"]
    assert _github_heading_slugs("## The Nova endpoint (`nova.*`)\n") \
        == ["the-nova-endpoint-nova"]
    if heading_slug not in slugs:
        errors.append(
            f"{PREFLIGHT_DOC}: no heading slugs to `{PREFLIGHT_ANCHOR}`, so every "
            f"install snippet this screen forces to link there would point at "
            f"nothing. Either restore the section or retire this screen -- do not "
            f"leave it demanding a dead link.")
        return

    installs = re.compile(r"charts/oasgen-provider(-crds)?\b")
    creates_own = re.compile(r"\bkind\s+create\s+cluster\b")
    assert installs.search("  oci://ghcr.io/krateo-platformops/charts/oasgen-provider \\")
    assert installs.search("charts/oasgen-provider-crds")
    assert not installs.search("charts/openstack-nova-operator-kog")
    assert creates_own.search("$ kind create cluster --name nova-kog")
    assert not creates_own.search("kind delete cluster --name nova-kog")

    for f in _docs():
        rel = f.replace(os.sep, "/")
        if rel in HISTORICAL_DOCS or rel == PREFLIGHT_DOC:
            continue
        text = open(f, encoding="utf-8", errors="replace").read()
        lines = text.splitlines()
        first = next((n for n, l in enumerate(lines) if installs.search(l)), None)
        if first is None:
            continue
        if any(creates_own.search(l) for l in lines[:first]):
            continue
        if PREFLIGHT_ANCHOR in text:
            continue
        errors.append(
            f"{f}:{first+1}: installs the cluster-scoped, singleton "
            f"oasgen-provider CRD on a cluster this document did not create, and "
            f"never links to `{PREFLIGHT_DOC}{PREFLIGHT_ANCHOR}`. On an "
            f"installer-managed cluster (installer 0.3.673 pins 0.74.0) this "
            f"snippet names an older build of an object every other KOG shares; "
            f"a downgrade prunes 39 measured field paths with a green apply. Add "
            f"the preflight box, or create a cluster in this document first.")

# FENCE, ADMONITION and the fence walker live in scripts/mdfence.py, because
# hack/check-doc-rendering.py needs the identical answer to "is this line inside
# a fence". Two copies of that answer is the defect this screen exists for, one
# layer up: the screens would disagree and the disagreement would show up as one
# passing a document the other failed.
from mdfence import ADMONITION, FENCE, fence_scan as _fence_scan  # noqa: E402

def verify_code_fences_pair(errors):
    """A fence carrying a language tag must be OPENING a block, never closing one.

    WHY THIS INVARIANT AND NOT A PARITY COUNT. docs/api.md had an EVEN number of
    fences and six code blocks rendering as prose: one unclosed ```bash shifted
    every later pair by one, so each block's opening fence was being consumed as
    the previous block's close. Parity cannot see that -- two missing fences, one
    open and one close, keep the count even. The tell is that `bash` and `go`
    tags ended up in CLOSING positions, which is meaningless: a language tag only
    says how to highlight what FOLLOWS.

    What the defect actually costs: a reader of a command they are meant to run
    sees it typeset as prose, and the paragraph explaining why sees itself
    typeset as code. Nothing errors. markdownlint's MD040 asks the opposite
    question (does every block declare a language) and passes on this.

    LIMIT, stated rather than discovered later: a fenced block that itself
    CONTAINS a line of three backticks AT THE SAME blockquote depth -- a fence
    shown as an example -- would be miscounted here. There is none in this repo
    today. If one is added, this screen is the thing to fix, not the document.
    A blockquoted `> ```` inside a top-level block is handled correctly, which
    it was not before 2026-11-18; see _fence_scan.

    WHAT THIS SCREEN DOES NOT CATCH, measured and not supposed: docs/usage.md
    carried a 37-line `[!NOTE]` admonition inside an open ```console block from
    #57 until 2026-11-18 and this screen passed it every run, correctly -- the
    fences DID pair. Pairing is not the same question as "is the right text
    inside". That one is verify_admonition_not_in_code_fence.
    """
    checked = 0
    for f in _docs():
        lines = open(f, encoding="utf-8", errors="replace").read().splitlines()
        inside, tagged_closers, unclosed_at, fences = _fence_scan(lines)
        checked += fences
        for n, tag in tagged_closers:
            errors.append(
                "{}:{}: ```{} sits in a CLOSING position. A language tag "
                "describes what follows, so this is the signature of an "
                "earlier unclosed fence shifting every later pair by one -- "
                "code renders as prose and prose as code, with nothing "
                "erroring and an even fence count hiding it. Find the "
                "block above that was never closed."
                .format(f, n + 1, tag))
        if unclosed_at is not None:
            errors.append(
                "{}:{}: this fence is never closed, so everything after it "
                "renders as code.".format(f, unclosed_at + 1))

    assert FENCE.match("> ```bash").group(2) == "bash"
    assert FENCE.match("```") .group(2) == ""
    assert FENCE.match("   ```yaml").group(2) == "yaml"
    assert FENCE.match("> ``` ") is not None
    assert FENCE.match("not a fence ```") is None
    # Prose that merely mentions a fence inline must not be read as one.
    assert FENCE.match("use ``` to open a block") is None

    if not checked:
        errors.append(
            "no code fences found in any document, so this screen examined "
            "nothing and would pass over a file of pure misnesting")


def verify_admonition_not_in_code_fence(errors):
    """A GitHub admonition marker must not sit inside a fenced code block.

    WHAT IT COSTS WHEN IT DOES, rendered by GitHub's own /markdown API rather
    than argued from the spec: docs/usage.md's "Install the chart" section came
    back as ONE `<pre class="notranslate">` whose first line is `&gt; [!NOTE]`,
    and ZERO `markdown-alert` divs. 37 lines of callout, plus the `helm install`
    command the section exists to give, typeset as a shell session. Introduced
    by #57 on 2026-11-07 (as a `[!CAUTION]`, inserted directly under an open
    fence), carried through #75 when the block was rewritten in place, and
    invisible for eleven merged PRs.

    WHY NO EXISTING CHECK SAW IT. verify_code_fences_pair asks whether fences
    pair; these did. markdownlint has no rule for it. A human reviewer reading
    the diff sees a correctly written admonition, because it IS correctly
    written -- the defect is four lines above it, in a fence nobody touched.
    The failure is silent in the strongest sense: the document still contains
    every word it is supposed to contain.

    LIMIT: a document that deliberately SHOWS an admonition as example code
    would trip this. None does. If one is added, exempt that span explicitly
    and say why at the line, rather than loosening the pattern.
    """
    checked = 0
    for f in _docs():
        lines = open(f, encoding="utf-8", errors="replace").read().splitlines()
        inside, _, _, _ = _fence_scan(lines)
        for n, line in enumerate(lines):
            if not inside[n]:
                continue
            m = ADMONITION.match(line)
            if m:
                errors.append(
                    "{}:{}: a [!{}] admonition sits INSIDE a fenced code "
                    "block, so GitHub renders the whole callout as code and "
                    "no alert box appears. The fence that swallowed it opened "
                    "earlier; close it above this line."
                    .format(f, n + 1, m.group(1)))
            checked += 1

    # Self-test: the exact shape that shipped, and the shape that fixes it.
    # Both arms are required -- an arm that only proves the matcher fires would
    # pass just as well if the screen flagged every admonition everywhere.
    broken = ["```console", "", "> [!NOTE]", "> **text**", "", "$ helm install", "```"]
    fixed = ["```console", "$ helm install", "```", "", "> [!NOTE]", "> **text**"]
    bad, good = [], []
    for lines, sink in ((broken, bad), (fixed, good)):
        ins, _, _, _ = _fence_scan(lines)
        for n, line in enumerate(lines):
            if ins[n] and ADMONITION.match(line):
                sink.append(n)
    assert bad == [2], "screen must flag the swallowed admonition, got %r" % (bad,)
    assert good == [], "screen must not flag a correctly placed one, got %r" % (good,)
    # And the GFM rule the scanner exists for: the inner `> ```` of a blockquoted
    # example cannot close the top-level block, so line 3 is still inside it.
    nested = ["```console", "> [!NOTE]", "> ```", "> x", "> ```", "```"]
    ins, _, unclosed, _ = _fence_scan(nested)
    assert ins[1] and ins[2] and ins[4], "blockquoted fences must stay literal"
    assert unclosed is None, "the top-level block closes on its own line"

    if not checked:
        errors.append(
            "no line inside any fenced block was examined, so this screen "
            "cannot have disagreed with anything")

# ---------------------------------------------------------------------------
# A PIPE INTO AN EARLY-EXIT CONSUMER, WHERE THE PRODUCER CAN BE LARGE.
#
# `grep -q`, `grep -m N` and `head` stop reading as soon as they are satisfied.
# The producer then takes EPIPE, exits non-zero, and `set -o pipefail` promotes
# that to the pipeline's status -- so a SUCCESSFUL match reads as a failure.
#
# MEASURED, because it is a race and a race that has not fired looks like a
# correct line: `tar -tzf <24 entries> | grep -qx <entry 3>` refused the 0.1.9
# release while the file was in the archive, and the same code passed at 0.1.8
# and 40/40 on a laptop. Forced: `seq 1 3000000 | grep -q '^1$'` -> rc=141;
# `seq 1 100 | grep -q '^1$'` -> rc=0.
#
# WHAT THIS SCREEN REFUSES, and why it is narrower than "every early-exit pipe".
# This repo has ~35 early-exit pipes whose producer is a version string, an `ls`
# of one directory or a `docker port` line -- none can outlive its consumer, and
# demanding 35 exemption markers would be 35 reasons reading "the producer is
# tiny". So it refuses the two shapes that have actually produced defects here:
#
#   1  a DIRECT pipe from a named unbounded producer (docker logs, kubectl logs,
#      curl, tar -t, cat of a file, journalctl) into an early-exit consumer
#   2  a pipe from a VARIABLE into an early-exit consumer, where that variable is
#      assigned from a named unbounded producer somewhere in the same file
#
# Rule 2 is the one that earned its place: it is what found two credential-leak
# checks that my own 61-site census had sorted into the safe column, because I
# had classified by which command fed the pipe rather than by what the value
# holds. Both were `logs=$(docker logs ...)` then `printf '%s\n' "$logs" |
# grep -qF 'dummy-not-a-credential'`, where the MATCH is the bad news -- so a
# race prints a clean bill of health over a leaked credential.
#
# THE LIMIT, stated rather than discovered: it cannot know a variable's size in
# general, so a large value from a producer not on the list passes. `printf` has
# no immunity -- a 1 MB variable matching on its first byte gives rc=141 -- the
# list is just where the unbounded values in THIS repo come from.
#
# THE SIMPLER DESIGN THIS DID NOT TAKE, named because it is better in a way that
# matters. The openstack-as-a-service session's guard keys ONLY on the CONSUMER:
# `| grep -q`, `| grep -m`, `| head` anywhere on a line, substitutions and
# continuation lines included. It has no producer analysis, so it has zero false
# negatives AND no producer analysis to get wrong -- and all four of the
# iterations this screen needed were in that analysis, two of them because the
# analysis itself was wrong (it paired an unbounded producer with the wrong
# consumer, then skipped `$( )` and passed everything).
#
# The reason this repo took the harder design is a count, not a principle: ~35
# early-exit pipes here have genuinely tiny producers, and the consumer-only
# rule makes each one a finding needing an exemption that reads "the producer is
# tiny". A screen whose findings are mostly noise gets exempted wholesale, and
# then a real finding is exempted with them. In a repo already at zero sites the
# consumer-only rule is strictly better and costs nothing. That makes this a
# per-repo choice rather than a universal one, and Diego decides it.
EARLY_EXIT = re.compile(r"(?<!\|)\|(?!\|)\s*(?:grep\s+(?:-\w+\s+)*-\w*q"
                        r"|grep\s+(?:-\w+\s+)*-m\s*[0-9]|head\b)")
UNBOUNDED = re.compile(r"\b(?:docker\s+logs|kubectl[^|]*\blogs\b|K\s+-n[^|]*\blogs\b"
                       r"|curl\b|tar\s+-\w*t|journalctl\b|cat\s+[\"'$/])")
PIPED_VAR = re.compile(r"(?:printf|echo)\s+(?:'[^']*'\s+)?\"\$\{?(\w+)")
ASSIGNED_UNBOUNDED = re.compile(r"(\w+)=\$\(\s*([^)]*)\)")

# Each exemption must be LOAD-BEARING: the screen asserts the exempted line
# still matches, so a rotted entry fails instead of sitting there.
PIPE_EXEMPT = {
    "hack/lib/wait-self-test.sh":
        "the pipeline IS the fixture -- it proves wait_for works with a piped "
        "predicate, so rewriting it deletes the test",
    "hack/probe-590-restart.sh":
        "the pipeline is inside `bash -c`, a fresh shell with no pipefail, so "
        "the EPIPE is discarded and the pipeline returns the consumer's status",
}


# A stage that BOUNDS its own output stops largeness propagating past it.
BOUNDING = re.compile(r"\b(?:head\b|grep\s+(?:-\w+\s+)*-m\s*[0-9]|wc\b|cksum\b)")


def _why_large(prod, big):
    if UNBOUNDED.search(prod):
        return "the producer is an unbounded command"
    m = PIPED_VAR.search(prod)
    if m and m.group(1) in big:
        return "$%s is assigned from an unbounded producer in this file" % m.group(1)
    return ("its input comes from an unbounded producer earlier in the same "
            "pipeline, and this stage does not bound its output")


def _all_pipelines(line):
    """Every pipeline on the line as a list of stages: the outer one and each $( )."""
    out = [_stages(line)]
    for body in _subst_bodies(line):
        out.append(_stages(body))
    return [p for p in out if len(p) > 1]


def _subst_bodies(line):
    """The body of each $( ) on this line, innermost included."""
    out, i = [], 0
    while True:
        i = line.find("$(", i)
        if i < 0:
            return out
        depth, j = 1, i + 2
        while j < len(line) and depth:
            if line.startswith("$(", j):
                depth += 1
                j += 2
                continue
            if line[j] == ")":
                depth -= 1
            j += 1
        body = line[i + 2:j - 1]
        out.append(body)
        out.extend(_subst_bodies(body))
        i = j
    return out


def _all_stage_pairs(line):
    """Stage pairs of the outer pipeline AND of every pipeline inside a $( )."""
    pairs = _stage_pairs(line)
    for body in _subst_bodies(line):
        pairs.extend(_stage_pairs(body))
    return pairs


def _stage_pairs(line):
    """[(producer, consumer)] for each adjacent stage of the OUTER pipeline."""
    st = _stages(line)
    return [(st[k], st[k + 1]) for k in range(len(st) - 1)]


def _stages(line):
    """The outer pipeline's stages. A pipe inside "$( )" belongs to the inner
    pipeline, and `||` is not a pipe."""
    stages, buf, i, depth = [], [], 0, 0
    while i < len(line):
        c = line[i]
        if c in "'\"":
            j = i + 1
            while j < len(line):
                if line[j] == "\\":
                    j += 2
                    continue
                if line[j] == c:
                    break
                j += 1
            buf.append(line[i:j + 1])
            i = j + 1
            continue
        if line.startswith("$(", i):
            depth += 1
        elif c == ")" and depth:
            depth -= 1
        if c == "|" and depth == 0:
            if line.startswith("||", i):
                buf.append("||")
                i += 2
                continue
            stages.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    stages.append("".join(buf))
    return stages


def _unbounded_vars(text):
    out = set()
    for m in ASSIGNED_UNBOUNDED.finditer(text):
        if UNBOUNDED.search(m.group(2)):
            out.add(m.group(1))
    return out


def verify_no_pipe_from_large_producer(errors):
    """No early-exit consumer may be fed by a producer that can outlive it."""
    import glob
    checked, exempt_hit = 0, set()
    for d in (os.path.join(".github", "workflows"), "scripts", "hack",
              os.path.join("hack", "lib")):
        for f in sorted(glob.glob(os.path.join(d, "*"))):
            if not os.path.isfile(f) or not f.endswith((".sh", ".yaml", ".yml")):
                continue
            text = open(f, encoding="utf-8", errors="replace").read()
            big = _unbounded_vars(text)
            for n, line in enumerate(text.split("\n"), 1):
                if line.strip().startswith("#") or not EARLY_EXIT.search(line):
                    continue
                checked += 1
                if f in PIPE_EXEMPT:
                    exempt_hit.add(f)
                    continue
                # PAIR THE PRODUCER WITH ITS OWN CONSUMER, not with anything
                # left of the first pipe. The coarse version flagged
                #
                #   K -n "$NS" logs "$(... | grep -m1 x)" | grep -o ... | tail -4
                #
                # because an unbounded command appeared before SOME pipe -- but
                # the early-exit `grep -m1` there is fed by a tiny `get deploy`
                # listing, and the unbounded `logs` feeds `grep -o`, which reads
                # to EOF. A screen whose findings are mostly false gets exempted
                # wholesale, and then a real one is exempted with them.
                # LARGENESS PROPAGATES THROUGH FILTERS, and this was the
                # fourth shape this screen needed. `printf "$recs" | awk ... |
                # head -3` has `awk` as the early-exit consumer's producer --
                # and awk is not an unbounded COMMAND, but its input is a large
                # variable and awk does not bound its output. Checking only the
                # immediately preceding stage missed it. A bounding stage
                # (head, grep -m, wc, cksum) stops the propagation.
                why = None
                for stages in _all_pipelines(line):
                    large = False
                    for k, st in enumerate(stages):
                        if k and EARLY_EXIT.search("|" + st) and large:
                            why = _why_large(stages[k - 1], big)
                            break
                        if UNBOUNDED.search(st):
                            large = True
                        else:
                            m = PIPED_VAR.search(st)
                            if m and m.group(1) in big:
                                large = True
                        if BOUNDING.search(st):
                            large = False
                    if why:
                        break
                if why:
                    errors.append(
                        "{}:{}: an early-exit consumer (grep -q / grep -m / head) "
                        "is fed by a pipe where {}. Under pipefail the producer "
                        "takes EPIPE and a SUCCESSFUL match reads as a failure -- "
                        "measured at rc=141. Capture first and match the capture "
                        "(`grep -q P <<< \"$var\"`), or read from a file."
                        .format(f, n, why))

    for f, reason in PIPE_EXEMPT.items():
        if f not in exempt_hit:
            errors.append(
                "the pipe exemption for {} is not load-bearing: that file no "
                "longer contains an early-exit pipe, so the entry excuses "
                "nothing. Remove it. (It claimed: {})".format(f, reason))

    if not checked:
        errors.append(
            "no early-exit pipe was examined anywhere, so this screen checked "
            "nothing -- which is not the same as a clean repo")

    # Self-test: planted forms that must be caught, and safe ones that must not.
    assert EARLY_EXIT.search("foo | grep -q x")
    assert EARLY_EXIT.search("foo | grep -Eq x")
    assert EARLY_EXIT.search("foo | grep -m 1 x")
    assert EARLY_EXIT.search("foo | grep -m1 x")
    assert EARLY_EXIT.search("foo | head -1")
    assert not EARLY_EXIT.search("foo | grep -c x"), "grep -c reads to EOF"
    assert not EARLY_EXIT.search("foo | grep -o x"), "grep -o reads to EOF"
    assert not EARLY_EXIT.search("foo | grep x"), "plain grep reads to EOF"
    assert not EARLY_EXIT.search("a || grep -q x"), "`||` is not a pipe"
    assert UNBOUNDED.search('docker logs "$CID" 2>&1')
    assert UNBOUNDED.search("curl -s http://x")
    assert UNBOUNDED.search("tar -tzf a.tgz")
    assert not UNBOUNDED.search('printf "%s" "$v"')
    assert _unbounded_vars('logs=$(docker logs "$CID" 2>&1)') == {"logs"}
    assert _unbounded_vars('v=$(printf "%s" x)') == set()

    # Stage pairing: the arm that stops the false positive this screen had.
    pairs = _stage_pairs('K logs "$(K get deploy -o name | grep -m1 x)" | grep -o y | tail -4')
    assert len(pairs) == 2, pairs
    assert "grep -m1" not in pairs[0][1], \
        "a pipe inside $( ) belongs to the inner pipeline, not this one"
    assert "grep -o" in pairs[0][1] and "tail -4" in pairs[1][1], pairs
    # and the real shapes still pair correctly
    pairs = _stage_pairs('docker logs "$CID" 2>&1 | grep -qF secret')
    assert len(pairs) == 1 and UNBOUNDED.search(pairs[0][0]), pairs
    pairs = _stage_pairs('printf "%s" "$logs" | awk \'$1=="x"\' | head -3')
    assert len(pairs) == 2 and EARLY_EXIT.search("|" + pairs[1][1]), pairs
    assert _stage_pairs("a || b") == [], "`||` must not split a pipeline"

    # A pipeline INSIDE a substitution is still a pipeline. This arm is the one
    # the second attempt failed: it passed a real race because the construct was
    # skipped rather than descended into.
    inner = _all_stage_pairs("""head=$(printf '%s\\n' "$log" | head -1 | cksum)""")
    assert any(EARLY_EXIT.search("|" + c) for _p, c in inner), inner
    assert any("$log" in p for p, _c in inner), inner
    # and the outer/inner distinction still holds for the false-positive shape
    outer = _all_stage_pairs('K logs "$(K get deploy -o name | grep -m1 x)" | grep -o y')
    bad = [(p, c) for p, c in outer
           if EARLY_EXIT.search("|" + c) and UNBOUNDED.search(p)]
    assert bad == [], ("the unbounded `logs` must not be paired with the inner "
                       "`grep -m1`: %r" % (bad,))


# ---------------------------------------------------------------------------
# THE PROVIDER-FLOOR DECLARATION MUST HAVE A READER.
#
# README.md carries a machine-readable floor and a comment saying it is "Read by
# the create-reachability screen (hack/validate-findby-identifiers.py), which
# requires exactly ONE such declaration and refuses a floor below 0.52.0, an
# exclusion at or below the floor, or a duplicate exclusion."
#
# THAT SCREEN DOES NOT EXIST IN THIS REPOSITORY. Measured 2026-11-18: the file is
# absent and nothing anywhere references it. So the declaration had no reader and
# the comment asserted a gate that runs nowhere -- the same defect this repo
# found in its own CI description, where README and docs/release.md both claimed
# a shellcheck step that `grep -ri shellcheck .github/` could not find.
#
# It was going to arrive as a copy from another repository, which this session
# was denied permission to make. Rather than leave a false claim standing until
# that is resolved, the four rules are implemented here from the specification
# the comment itself gives. The findby half of that other screen has nothing to
# do here anyway: 0 findby verbs across all six RestDefinitions, rendered and
# counted with a 6-of-6 control.
#
# WHY 0.52.0 AND NOT ">= anything": 0.53.0 and 0.54.0 broke apiKey RBAC and all
# six of this chart's documents are apiKey, so the safe set is {0.52.0} union
# [0.54.1, inf) -- which is not an interval, and a bare ">= 0.52.0" would be a
# FALSE statement about this repository.
FLOOR_LINE = re.compile(
    r"^\*\*Minimum provider version:\*\*\s*`oasgen-provider\s*>=\s*"
    r"([0-9]+\.[0-9]+\.[0-9]+)((?:\s*except\s*.*)?)`\s*$")
FLOOR_MIN = (0, 52, 0)


def _declared_range(readme="README.md"):
    """(floor, exclusions) read from README's one machine-readable declaration.

    ONE SOURCE. verify_provider_floor_declaration validates that line; this
    reads it. A second copy of the floor anywhere else is the defect both are
    there to prevent.
    """
    if not os.path.exists(readme):
        return None, []
    for line in open(readme, encoding="utf-8", errors="replace").read().split("\n"):
        m = FLOOR_LINE.match(line)
        if not m:
            continue
        excl = []
        raw = (m.group(2) or "").strip()
        if raw:
            for part in re.sub(r"^\s*except\s*", "", raw).split(","):
                v = _ver(part)
                if v:
                    excl.append(v)
        return _ver(m.group(1)), excl
    return None, []


def _outside_declared_range(text, rng=None):
    """"" if this version is inside the declared range, else WHY it is not.

    An absent declaration returns a REASON, not "". A version cannot be
    pronounced safe by an instrument that is missing, and a screen whose
    instrument has gone must fail differently from one whose subject is clean.
    """
    floor, excl = rng if rng is not None else _declared_range()
    if floor is None:
        return ("cannot be checked: README.md carries no machine-readable "
                "provider-floor declaration, so there is no range to check it "
                "against -- a missing instrument, not a clean result")
    v = _ver(text)
    if v is None:
        return "is not a plain SemVer triple"
    if v < floor:
        return "is below the declared floor %d.%d.%d (README.md)" % floor
    if v in excl:
        return ("is one of the versions README.md's declaration excludes (%s): "
                "they broke apiKey RBAC and all six of this chart's documents "
                "are apiKey" % ", ".join("%d.%d.%d" % e for e in excl))
    return ""


def _ver(text):
    m = re.fullmatch(r"\s*([0-9]+)\.([0-9]+)\.([0-9]+)\s*", text or "")
    return tuple(int(g) for g in m.groups()) if m else None


def verify_provider_floor_declaration(errors):
    """The declaration exists exactly once, and says something true."""
    lines = open("README.md", encoding="utf-8", errors="replace").read().split("\n")
    found = [(n + 1, m) for n, l in enumerate(lines) for m in [FLOOR_LINE.match(l)] if m]

    if len(found) != 1:
        errors.append(
            "README.md carries %d machine-readable provider-floor declaration(s) "
            "and must carry exactly one. Zero means the gate below reads nothing "
            "while a comment says it does; more than one means a reader cannot "
            "know which is authoritative." % len(found))
        return

    n, m = found[0]
    floor = _ver(m.group(1))
    if floor is None:
        errors.append("README.md:%d: the floor %r is not a plain SemVer triple." % (n, m.group(1)))
        return
    if floor < FLOOR_MIN:
        errors.append(
            "README.md:%d: the declared floor %d.%d.%d is BELOW %d.%d.%d. Under "
            "0.26.1 `krateo.io/connector-verbose` wrote credential HEADERS to pod "
            "logs in cleartext and this chart's credential IS a header; apiKey "
            "support itself only arrived in 0.19.0."
            % ((n,) + floor + FLOOR_MIN))

    raw = m.group(2) or ""
    excl = []
    if raw.strip():
        body = re.sub(r"^\s*except\s*", "", raw.strip())
        for part in body.split(","):
            v = _ver(part)
            if v is None:
                errors.append("README.md:%d: exclusion %r is not a plain SemVer triple."
                              % (n, part.strip()))
                continue
            excl.append(v)

    for v in excl:
        if v <= floor:
            errors.append(
                "README.md:%d: exclusion %d.%d.%d is at or below the floor "
                "%d.%d.%d, so it excludes nothing the floor had admitted. Either "
                "the floor or the exclusion is wrong."
                % ((n,) + v + floor))
    dupes = {v for v in excl if excl.count(v) > 1}
    for v in sorted(dupes):
        errors.append("README.md:%d: exclusion %d.%d.%d appears %d times."
                      % ((n,) + v + (excl.count(v),)))

    # THE PROSE AND THE DECLARATION MUST AGREE, because the comment above the
    # declaration says they say the same thing, and two statements of one fact
    # is how this repo's defects usually arrive.
    prose = "\n".join(lines)
    for v in excl:
        dotted = "%d.%d.%d" % v
        if dotted not in prose:
            errors.append(
                "README.md:%d: the declaration excludes %s and no prose in this "
                "file mentions it, so a reader is told a different rule than the "
                "gate enforces." % (n, dotted))

    # Self-test: every rule driven, both directions.
    def _rows(text):
        out = []
        _floor_line_rows(text, out)
        return out
    assert _rows("**Minimum provider version:** `oasgen-provider >= 0.52.0 except 0.53.0, 0.54.0`") == []
    assert _rows("**Minimum provider version:** `oasgen-provider >= 0.40.0`"), "below the minimum must fail"
    assert _rows("**Minimum provider version:** `oasgen-provider >= 0.52.0 except 0.52.0`"), \
        "an exclusion AT the floor must fail"
    assert _rows("**Minimum provider version:** `oasgen-provider >= 0.52.0 except 0.51.0`"), \
        "an exclusion BELOW the floor must fail"
    assert _rows("**Minimum provider version:** `oasgen-provider >= 0.52.0 except 0.53.0, 0.53.0`"), \
        "a duplicate exclusion must fail"
    assert _rows("nothing here"), "a missing declaration must fail, not pass"
    assert FLOOR_LINE.match("**Minimum provider version:** `oasgen-provider >= 1.2.3`")
    assert FLOOR_LINE.match("**Minimum provider version:** `oasgen-provider >=0.52.0 except 0.53.0`")
    assert not FLOOR_LINE.match("Minimum provider version: oasgen-provider >= 0.52.0")


def _floor_line_rows(text, errors):
    """The rule set, over TEXT rather than the file, so the self-test can drive it."""
    lines = text.split("\n")
    found = [(n + 1, m) for n, l in enumerate(lines) for m in [FLOOR_LINE.match(l)] if m]
    if len(found) != 1:
        errors.append("declaration count is %d, not 1" % len(found))
        return
    n, m = found[0]
    floor = _ver(m.group(1))
    if floor is None:
        errors.append("floor is not SemVer")
        return
    if floor < FLOOR_MIN:
        errors.append("floor below the minimum")
    excl = []
    raw = (m.group(2) or "").strip()
    if raw:
        for part in re.sub(r"^\s*except\s*", "", raw).split(","):
            v = _ver(part)
            if v is None:
                errors.append("exclusion not SemVer")
            else:
                excl.append(v)
    for v in excl:
        if v <= floor:
            errors.append("exclusion at or below the floor")
    if len({v for v in excl if excl.count(v) > 1}):
        errors.append("duplicate exclusion")


LOCAL_GATE_DOC = os.path.join("hack", "README.md")
LOCAL_GATE_HEADING = "## Run the gates locally before pushing"
SELFTEST_CMD = re.compile(r"(bash|python3)\s+([A-Za-z0-9/_.-]+)\s+(--self-?test)\b")


def verify_local_gate_list_matches_ci(errors):
    """The "run these before pushing" list must not be a SUBSET of CI.

    WHY THIS EXISTS, and it is about a specific miss. On 2026-11-18 a change
    turned smoke.yaml into a matrix. `scripts/validate.py` passed locally and CI
    failed -- on `bash hack/render-check.sh --selftest`, which reads
    OASGEN_VERSION out of smoke.yaml and got back `${{ matrix.oasgen }}`. The
    local checklist in hack/README.md named SIX of the ELEVEN self-tests CI
    runs, and the missing five included that one.

    A checklist that is a subset of CI is worse than no checklist. With no list
    you know you have not checked. With a list you consult it, it comes back
    clean, and you believe the clean answer -- which is this repo's recurring
    defect wearing a checklist: a reassuring result produced by not looking.

    SO THE REQUIREMENT IS DERIVED FROM THE WORKFLOWS, not maintained beside
    them. Adding a self-test step to CI without adding it here now fails, which
    makes the drift impossible rather than detectable-in-principle.

    DIRECTION MATTERS, and only one direction is an error. Every self-test CI
    runs must appear in the doc. A command in the doc that CI does NOT run is
    fine and deliberately not flagged: `check-restdefinition-shape.py` with no
    flag, and the two docker-backed mocks, are there precisely because a human
    with docker can run more than a PR job should.
    """
    wf_dir = os.path.join(".github", "workflows")
    if not os.path.isdir(wf_dir):
        errors.append(f"{wf_dir}: missing -- nothing to derive the local gate "
                      f"list from")
        return
    in_ci = set()
    for f in sorted(glob.glob(os.path.join(wf_dir, "*.y*ml"))):
        for m in SELFTEST_CMD.finditer(open(f, encoding="utf-8").read()):
            in_ci.add("{} {} {}".format(*m.groups()))

    assert SELFTEST_CMD.search("        run: bash hack/render-check.sh --selftest")
    assert SELFTEST_CMD.search("python3 scripts/check-schema-warnings.py --self-test")
    assert SELFTEST_CMD.search(
        "run: bash hack/probe-401.sh --self-test").group(2) == "hack/probe-401.sh"
    assert not SELFTEST_CMD.search("bash hack/probe-401.sh --dry-run")

    # A derived requirement that derives NOTHING passes over any document,
    # including an empty one. That is the failure this whole screen is about, so
    # it must not be the way this screen itself fails.
    if not in_ci:
        errors.append(
            f"{wf_dir}: found ZERO self-test invocations, so this screen would "
            f"pass over any checklist including an empty one. Either the "
            f"workflows stopped running self-tests -- a much bigger problem -- "
            f"or SELFTEST_CMD no longer matches how they are written.")
        return

    if not os.path.exists(LOCAL_GATE_DOC):
        errors.append(f"{LOCAL_GATE_DOC}: missing -- it holds the checklist "
                      f"this screen keeps in step with CI's {len(in_ci)} "
                      f"self-tests")
        return
    text = open(LOCAL_GATE_DOC, encoding="utf-8").read()
    if LOCAL_GATE_HEADING not in text:
        errors.append(f"{LOCAL_GATE_DOC}: no {LOCAL_GATE_HEADING!r} section, so "
                      f"there is no checklist to keep in step with CI")
        return
    section = text.split(LOCAL_GATE_HEADING, 1)[1].split("\n## ", 1)[0]

    missing = sorted(c for c in in_ci if c not in section)
    if missing:
        errors.append(
            "{}: the local checklist under {!r} is a SUBSET of CI -- it omits "
            "{} of {} self-test(s): {}. A list that is consulted and comes back "
            "clean without having run these is how the break it was supposed to "
            "catch reaches CI. Add them in the form CI invokes them."
            .format(LOCAL_GATE_DOC, LOCAL_GATE_HEADING, len(missing),
                    len(in_ci), ", ".join(repr(m) for m in missing)))

# The status contexts branch protection requires on `main`. A LITERAL, read
# with:
#
#     gh api repos/krateo-blueprints/openstack-nova-operator-kog/branches/main/protection \
#       --jq .required_status_checks.contexts        -> ["validate", "smoke"]
#
# measured 2026-11-18. Not read live: this screen must run on a PR with no
# network and no token, and a gate that silently skips when it cannot reach the
# API is the failure mode this repo keeps finding. If protection changes, this
# list goes stale and the screen keeps testing the old names -- harmless. What
# it catches is the live failure mode: a JOB RENAME.
REQUIRED_CHECKS = ("validate", "smoke")


def verify_required_checks_are_produced(errors):
    """Something must publish a check for every required context.

    HOW THIS BROKE, because the symptom is uniquely unhelpful. Turning the
    `smoke` job into a matrix renamed its checks: GitHub publishes a matrix
    job's checks as `<job name> (<matrix values>)`, so `smoke` became
    `smoke (floor / oasgen 0.52.0)` and `smoke (platform / oasgen 0.74.0)`. The
    required context `smoke` then does not fail -- it NEVER REPORTS. Every pull
    request sits at `mergeStateStatus: BLOCKED` with `mergeable: MERGEABLE`,
    every visible check green, and nothing to point at. The pull request that
    introduced the matrix could not merge itself, which is the only reason it
    was found before it blocked everything else.

    So a matrix job CANNOT satisfy a bare required context, and this screen
    knows that rather than just checking the name exists -- a name check alone
    would have passed on the broken workflow and called it covered.

    THE FIX IS A JOB, NOT A SETTING. Widening protection to the two new names
    would encode version numbers in a repository setting, so every pin change
    would re-break it somewhere nobody is looking. An aggregator job named
    `smoke` keeps one stable context over any number of arms.
    """
    producers = {}
    for f in sorted(glob.glob(os.path.join(".github", "workflows", "*.y*ml"))):
        try:
            doc = yaml.safe_load(open(f, encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            errors.append(f"{f}: not parseable as YAML, so the required-check "
                          f"screen cannot tell what it publishes: {exc}")
            return
        for job_id, job in (doc.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            name = job.get("name") or job_id
            matrixed = bool((job.get("strategy") or {}).get("matrix"))
            # A later job with the same effective name does not un-matrix an
            # earlier one, so keep the BEST (non-matrix) producer.
            producers[name] = producers.get(name, True) and matrixed

    assert REQUIRED_CHECKS, "an empty required list would pass over anything"
    if not producers:
        errors.append(
            ".github/workflows: no jobs found at all, so this screen would "
            "pass for lack of anything to contradict it")
        return

    for ctx in REQUIRED_CHECKS:
        if ctx not in producers:
            errors.append(
                "branch protection requires a status check named {!r} and no "
                "job publishes that name. The context does not FAIL in this "
                "state -- it never reports, so every PR sits at BLOCKED with "
                "mergeable=MERGEABLE, all visible checks green and nothing to "
                "point at. Job names present: {}."
                .format(ctx, ", ".join(sorted(producers))))
        elif producers[ctx]:
            errors.append(
                "branch protection requires a status check named {!r}, and the "
                "only job publishing that name is a MATRIX job -- whose checks "
                "are published as {!r} with the matrix values appended, never "
                "as the bare name. Add a non-matrix aggregator job named {!r} "
                "that `needs` the arms, rather than widening protection to "
                "names that carry version numbers."
                .format(ctx, ctx + " (...)", ctx))

# Kinds and plurals a stock Kubernetes API server already serves. Hand-written
# on purpose: this must run on a PR with no cluster, which is the only place
# the collision is still cheap to fix. It is not exhaustive and does not need
# to be. The AUTHORITATIVE screen is `kubectl api-resources` minus CRD-backed
# groups on a FRESH cluster; this moves the common cases earlier.
BUILTIN_KINDS = {
    "Binding", "ComponentStatus", "ConfigMap", "Endpoints", "Event", "LimitRange",
    "Namespace", "Node", "PersistentVolume", "PersistentVolumeClaim", "Pod",
    "PodTemplate", "ReplicationController", "ResourceQuota", "Secret", "Service",
    "ServiceAccount", "ControllerRevision", "DaemonSet", "Deployment", "ReplicaSet",
    "StatefulSet", "CronJob", "Job", "Ingress", "IngressClass", "NetworkPolicy",
    "ClusterRole", "ClusterRoleBinding", "Role", "RoleBinding", "Lease",
    "CustomResourceDefinition", "APIService", "StorageClass", "VolumeAttachment",
    "CSIDriver", "CSINode", "PriorityClass", "RuntimeClass", "PodDisruptionBudget",
    "HorizontalPodAutoscaler", "MutatingWebhookConfiguration",
    "ValidatingWebhookConfiguration", "CertificateSigningRequest", "EndpointSlice",
}
BUILTIN_LOWER = {k.lower() for k in BUILTIN_KINDS} | {
    "bindings", "componentstatuses", "configmaps", "endpoints", "events",
    "limitranges", "namespaces", "nodes", "persistentvolumes",
    "persistentvolumeclaims", "pods", "podtemplates", "replicationcontrollers",
    "resourcequotas", "secrets", "services", "serviceaccounts",
    "controllerrevisions", "daemonsets", "deployments", "replicasets",
    "statefulsets", "cronjobs", "jobs", "ingresses", "ingressclasses",
    "networkpolicies", "clusterroles", "clusterrolebindings", "roles",
    "rolebindings", "leases", "customresourcedefinitions", "apiservices",
    "storageclasses", "volumeattachments", "csidrivers", "csinodes",
    "priorityclasses", "runtimeclasses", "poddisruptionbudgets",
    "horizontalpodautoscalers", "certificatesigningrequests", "endpointslices",
}


def _collides(kind):
    """The names this kind would occupy, intersected with the built-ins.

    The plural is DERIVED here, which is the weak point: oasgen derives the real
    one and irregular stems do not survive a naive +s (`Kaas` -> `kaas`, not
    `kaass`). So this screens the forms a collision could take and accepts a
    false positive over a miss -- a false positive is one comment to resolve, a
    miss is a chart that installs on an upgraded cluster and refuses on a fresh
    one.
    """
    low = kind.lower()
    return ({kind} & BUILTIN_KINDS) | ({low, low + "s", low + "es"} & BUILTIN_LOWER)


def verify_no_builtin_shadowing(errors):
    """No generated CRD may shadow a Kubernetes built-in, on EITHER of its CRDs.

    oasgen refuses such a kind from 0.32.0 (#200) -- but only on a FRESH
    cluster. Where the CRD already exists it downgrades to a warning, so a
    developer who has been upgrading one cluster for weeks sees nothing and the
    chart fails for the first person who installs it clean.
    `github-provider-kog` lost a chart to exactly this with a `Deployment` kind.

    Reads chart/values.yaml rather than chart/templates/rd-*.yaml: those are
    Helm templates and are not parseable as YAML.

    Screens BOTH CRDs per RestDefinition -- `<Kind>` and the companion
    `<Kind>Configuration` -- because oasgen emits two and the guard does not
    care which one you intended.
    """
    for probe in ("Node", "Service", "Secret", "Deployment", "Event", "Job"):
        assert _collides(probe), f"screen cannot detect the built-in {probe}"
    for ok in ("Instance", "ComputeFlavor", "ComputeKeypair", "ComputeServerGroup",
               "ComputeAggregate", "ComputeQuota"):
        assert not _collides(ok), f"screen false-positives on {ok}"

    vals = yaml.safe_load(open(os.path.join("chart", "values.yaml"))) or {}
    rds = (vals.get("restdefinitions") or {})
    if not rds:
        errors.append("chart/values.yaml: no restdefinitions -- the shadowing screen "
                      "found nothing to check, which is a failure, not a pass")
        return
    for name, cfg in sorted(rds.items()):
        kind = (cfg or {}).get("resourceKind")
        if not kind:
            errors.append(f"chart/values.yaml: restdefinitions.{name} has no "
                          f"resourceKind to screen")
            continue
        for k in (kind, kind + "Configuration"):
            hit = _collides(k)
            if hit:
                errors.append(
                    f"chart/values.yaml: restdefinitions.{name}.resourceKind -> "
                    f"'{k}' shadows a Kubernetes built-in ({', '.join(sorted(hit))}). "
                    f"oasgen refuses it on a fresh cluster (#200) and only warns where "
                    f"the CRD already exists, so this is invisible until a clean "
                    f"install.")


def _declared_auth_fields():
    """The `spec.authentication.<field>` names oasgen will generate, read from
    the OAS security schemes this chart actually ships.

    The assets are Helm templates (`servers[].url` is a `{{ include }}`), so they
    do not parse as YAML -- hence the textual read. `type: apiKey` and
    `type: http` appear only on security schemes in an OAS; schema types are
    string/object/integer/..., so this does not collide with property types.
    """
    fields = set()
    for path in sorted(glob.glob(os.path.join("chart", "assets", "*.yaml"))):
        text = open(path).read()
        for m in re.finditer(r"\btype:\s*(apiKey|http)\b", text):
            if m.group(1) == "apiKey":
                fields.add("apiKey")
                continue
            tail = text[m.end():m.end() + 200]
            sm = re.search(r"\bscheme:\s*([A-Za-z0-9_-]+)", tail)
            if sm:
                fields.add(sm.group(1).lower())
    return fields


def _auth_methods_in_text(text):
    """Yield (lineno, method) for the first key under each `authentication:`.

    Deliberately textual so it also reaches YAML inside fenced markdown blocks --
    which is where two of the three broken examples lived.
    """
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        m = re.match(r"^(\s*)authentication:\s*(#.*)?$", ln)
        if not m:
            continue
        indent = len(m.group(1))
        for j in range(i + 1, min(i + 12, len(lines))):
            nxt = lines[j]
            if not nxt.strip() or nxt.strip().lstrip("#").strip() == "":
                continue
            if nxt.strip().startswith("#"):
                continue
            km = re.match(r"^(\s*)([A-Za-z][A-Za-z0-9_]*):", nxt)
            if not km or len(km.group(1)) <= indent:
                break
            yield j + 1, km.group(2)
            break


def verify_auth_scheme_consistency(errors):
    """Every shipped `authentication.<method>` must exist in the generated CRD.

    This screens the gap that let a real defect ship. PR #13 converted the OAS
    from `http/bearer` to an `apiKey` scheme and updated samples/nova-auth.yaml,
    but left `samples/extra-resources.yaml`, `docs/api.md` and
    `docs/e2e-krateo-openstack.md` carrying `authentication.bearer` blocks. The
    generated CRDs are apiKey-ONLY -- measured in a kind cluster, all five
    report `methods=[apiKey]` with no bearer field to fall back to -- so the API
    server refuses those examples outright:

        strict decoding error: unknown field "spec.authentication.bearer"

    The failure mode this guards is not drift but a verification gap: the
    generated schema was asserted correct, and the shipped examples were
    asserted to apply, and the two were never checked AGAINST EACH OTHER. Both
    halves passed in isolation, which is why good CI did not catch it.

    Prose is deliberately NOT screened. Legitimate past-tense narrative lives in
    current docs ("the generator then accepted only http/basic and http/bearer"),
    so a prose pattern would need a per-line allowlist -- and an allowlist keyed
    to position silently stops matching when a line moves, which is how a
    sibling repo's long-allowlisted placeholder reddened its scanner. A
    structural screen has no such failure mode.
    """
    declared = _declared_auth_fields()
    if not declared:
        errors.append("chart/assets/*.yaml: no security scheme found -- the auth "
                      "consistency screen has nothing to check, which is a "
                      "failure, not a pass")
        return

    # Prove the screen works before trusting it: it must reject a method that is
    # not declared and accept one that is.
    bad = "spec:\n  authentication:\n    bearer:\n      tokenRef: {}\n"
    good = "spec:\n  authentication:\n    apiKey:\n      tokenRef: {}\n"
    bad_found = [k for _, k in _auth_methods_in_text(bad)]
    good_found = [k for _, k in _auth_methods_in_text(good)]
    if "bearer" not in bad_found:
        errors.append("validate.py: the auth screen failed its own known-BAD "
                      f"assertion (saw {bad_found!r}) -- it cannot be trusted")
        return
    if "apiKey" not in good_found:
        errors.append("validate.py: the auth screen failed its own known-GOOD "
                      f"assertion (saw {good_found!r}) -- it cannot be trusted")
        return
    if "apiKey" not in declared:
        errors.append(f"chart/assets/*.yaml declares {sorted(declared)} but the "
                      f"known-good fixture assumes apiKey; update the fixture")
        return

    targets = []
    for pat in ("**/*.md", "**/*.yaml", "**/*.yml"):
        targets.extend(glob.glob(pat, recursive=True))
    for path in sorted(set(targets)):
        if path.startswith(".git" + os.sep) or os.sep + "charts" + os.sep in path:
            continue
        if path.replace(os.sep, "/") in HISTORICAL_DOCS:
            continue
        try:
            text = open(path).read()
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"{path}: unreadable, so its auth blocks go "
                          f"UNASSERTED ({type(exc).__name__}). A file this "
                          f"screen cannot read is a failure, not a skip.")
            continue
        for lineno, method in _auth_methods_in_text(text):
            if method not in declared:
                errors.append(
                    f"{path}:{lineno}: `authentication.{method}` is not a field the "
                    f"generated CRD has. This chart's OAS declares {sorted(declared)}, "
                    f"so oasgen emits only those -- the API server rejects this with "
                    f"`strict decoding error: unknown field "
                    f"\"spec.authentication.{method}\"`.")


# The oasgen-provider release below which a credential-carrying HEADER could be
# written to pod logs in cleartext. `krateo.io/connector-verbose` redacted only
# values it had been TOLD were sensitive; since 0.26.1 it redacts by header name.
# For an X-Auth-Token chart the credential IS a header, and verbose is what a
# user enables to debug the 401 they are getting -- so anything below this is a
# credential-exposure recommendation, not a version-drift one.
HARD_SECURITY_FLOOR = (0, 26, 1)


def _ver_tuple(s):
    return tuple(int(x) for x in s.split("."))


def verify_no_under_floor_recommendation(errors):
    """No file may RECOMMEND an oasgen-provider version below the hard floor.

    Distinct from verify_documented_pin, which compares `helm ... --version`
    invocations in markdown against OASGEN_VERSION. This caught nothing when
    `kagent/agent-nova-expert.yaml` advised "bump oasgen-provider to >= 0.20.0",
    because that line is in a YAML file (the pin check sweeps only `**/*.md`) and
    carries neither `helm` nor `--version`. Two independent reasons to miss it,
    which is why the scope AND the pattern both widen here.

    Deliberately narrow: it fires only on an imperative -- bump/upgrade/install/
    pin/use/require/need/minimum/at least -- near an oasgen-provider mention and
    a version below the floor.

    KNOWN LIMIT, stated rather than implied: the scan is LINE BY LINE, so a
    version on a different line from its subject is NOT detected. That is how
    the quickstart instance survived even once "need" was in the list -- the
    version had wrapped onto the next line. Joining lines into paragraphs would
    fix it and introduce a worse bug: the version table in
    docs/configuration.md legitimately pairs `0.19.0` with the word "minimum"
    two rows apart, so a joined window would flag a correct table forever. A
    false positive in a table nobody can silence is how a guard gets disabled. Stating a fact about history ("apiKey arrived in 0.19.0", "0.26.1 is
    the floor") is correct and must not trip it, and the known-good assertion
    below pins that distinction.
    """
    # "need" and "minimum"/"at least" were MISSING, and a live instance slipped
    # through because of it: docs/quickstart.md said "`apiKey` needs >= 0.20.0",
    # below the 0.26.1 floor, and this screen passed. An incomplete pattern
    # fails exactly like a dead one -- which is the lesson in this file's own
    # module docstring, committed by the same hand that then wrote this list.
    _IMP = r"(?:bump|upgrade|install|pin|use|require|need|minimum|at least)"
    imperative = re.compile(
        r"\b" + _IMP + r"\w*\b[^.\n]{0,120}?"
        r"oasgen-provider\b[^.\n]{0,120}?([0-9]+\.[0-9]+\.[0-9]+)"
        r"|oasgen-provider\b[^.\n]{0,120}?\b" + _IMP + r"\w*\b"
        r"[^.\n]{0,120}?([0-9]+\.[0-9]+\.[0-9]+)",
        re.I)

    def _under_floor(line):
        for m in imperative.finditer(line):
            v = m.group(1) or m.group(2)
            if v and _ver_tuple(v) < HARD_SECURITY_FLOOR:
                return v
        return None

    # Prove it before trusting it.
    known_bad = "the upgrade is: bump oasgen-provider to >= 0.20.0, switch the OAS"
    known_good_fact = "`apiKey` / `in: header` has been supported by oasgen-provider since 0.19.0"
    known_good_rec = "bump oasgen-provider to >= 0.40.0 and set nova.url"
    if _under_floor(known_bad) != "0.20.0":
        errors.append("validate.py: the under-floor screen failed its known-BAD "
                      f"assertion (got {_under_floor(known_bad)!r})")
        return
    if _under_floor(known_good_fact) is not None:
        errors.append("validate.py: the under-floor screen false-positives on a "
                      "statement of HISTORICAL FACT, which must stay writable "
                      f"(flagged {_under_floor(known_good_fact)!r})")
        return
    if _under_floor(known_good_rec) is not None:
        errors.append("validate.py: the under-floor screen false-positives on a "
                      f"compliant recommendation (flagged {_under_floor(known_good_rec)!r})")
        return

    targets = []
    # A SECOND PATTERN: a bare `>=` BOUND, with no imperative at all.
    #
    # The imperative list above is deliberately narrow so historical prose
    # ("before 0.26.1 the dump leaked", "apiKey arrives in 0.19.0") cannot trip
    # it. That narrowness let a real case through, in SHIPPED content:
    #
    #     chart/assets/server.yaml:  "oasgen-provider (>= 0.43.0) generates ..."
    #
    # No bump, no install, no "at least" -- so the screen swept the file and
    # found nothing. The asset ships INSIDE the chart and its text surfaces in
    # the generated CRD's descriptions, so an under-floor bound there reaches a
    # user's cluster. Mutating that line to `>= 0.20.0`, below the hard security
    # floor, passed validation.
    #
    # `>=` is a recommendation BY CONSTRUCTION: it states a minimum a reader is
    # invited to satisfy. Historical statements use "before", "below", "since"
    # or "at", never a `>=` bound, so this pattern can be added without
    # reintroducing the false positives the imperative list exists to avoid.
    bound = re.compile(
        r"oasgen-provider\b[^.\n]{0,120}?>=\s*v?([0-9]+\.[0-9]+\.[0-9]+)"
        r"|>=\s*v?([0-9]+\.[0-9]+\.[0-9]+)[^.\n]{0,120}?oasgen-provider\b",
        re.I)

    def _under_floor_bound(line):
        for m in bound.finditer(line):
            v = m.group(1) or m.group(2)
            if v and _ver_tuple(v) < HARD_SECURITY_FLOOR:
                return v
        return None

    # Prove THIS pattern too, with the shipped line that escaped as known-bad.
    bound_bad = "directly as an `apiKey` security scheme. oasgen-provider (>= 0.20.0)"
    bound_good = "directly as an `apiKey` security scheme. oasgen-provider (>= 0.49.0)"
    bound_hist = "before 0.26.1 the verbose dump wrote credential headers in cleartext"
    if _under_floor_bound(bound_bad) != "0.20.0":
        errors.append("validate.py: the bare-bound screen failed its known-BAD "
                      f"assertion (got {_under_floor_bound(bound_bad)!r}); the "
                      f"line it was written for would escape again")
        return
    if _under_floor_bound(bound_good) is not None:
        errors.append("validate.py: the bare-bound screen false-positives on an "
                      "ABOVE-floor bound, so it would fire on correct content")
        return
    if _under_floor_bound(bound_hist) is not None:
        errors.append("validate.py: the bare-bound screen false-positives on "
                      "HISTORICAL prose, which is exactly what the imperative "
                      "list was kept narrow to protect")
        return

    for pat in ("**/*.md", "**/*.yaml", "**/*.yml"):
        targets.extend(glob.glob(pat, recursive=True))
    for path in sorted(set(targets)):
        if path.startswith(".git" + os.sep) or os.sep + "charts" + os.sep in path:
            continue
        if path.replace(os.sep, "/") in HISTORICAL_DOCS:
            continue
        try:
            lines = open(path).read().splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"{path}: unreadable, so it is NOT screened for "
                          f"under-floor recommendations ({type(exc).__name__}). "
                          f"A file this screen cannot read is a failure.")
            continue
        for i, line in enumerate(lines, 1):
            # Either pattern is a finding: an imperative, or a bare `>=` bound.
            v = _under_floor(line) or _under_floor_bound(line)
            if v:
                errors.append(
                    f"{path}:{i}: recommends oasgen-provider {v}, below the "
                    f"{'.'.join(map(str, HARD_SECURITY_FLOOR))} HARD security floor. "
                    f"Below that, connector-verbose wrote credential HEADERS to pod "
                    f"logs in cleartext -- and this chart's credential IS a header "
                    f"(X-Auth-Token). Recommend the pinned version instead.")


def verify_cr_apiversion_consistency(errors):
    """Resource CRs must all share one apiVersion, Configuration CRs another.

    DELIBERATELY DOES NOT DERIVE THE VALUE. This screen used to port oasgen's
    `crdgen.NormalizeVersionName` and assert it against known inputs, to catch
    the seven examples that declared `v1alpha1` for resource kinds where the CRD
    is served at `v2-1`. That worked, and it was a reimplementation of another
    project's internal rule, maintained here, able to drift from it silently
    while still passing its own self-test.

    oasgen publishes both halves on the RestDefinition and always has:

        {.status.resource.apiVersion}       -> the derived resource version
        {.status.configuration.apiVersion}  -> the fixed v1alpha1

    So the honest check is a COMPARISON, which needs a cluster. The smoke
    workflow now does it on every PR via `hack/assert-sample-gvks.py`, and the
    port is gone rather than maintained -- the commitment made in its own
    docstring when it was written.

    What is left here is the part a cluster-free check CAN do honestly: internal
    consistency. If one sample's apiVersion drifts from its siblings, this
    catches it in seconds without a cluster. It cannot catch a UNIFORMLY wrong
    value -- the original defect was uniform -- and that is precisely what the
    smoke comparison is for. Stating the limit rather than implying coverage.
    """
    group = "nova.openstack.krateo.io"
    api_re = re.compile(r"apiVersion:\s*(" + re.escape(group) + r"/[\w.-]+)")
    res, cfg = {}, {}
    res_src, cfg_src = set(), set()
    # hack/*.sh is swept too: hack/probe-361.sh embeds a ComputeFlavor and a
    # ComputeFlavorConfiguration in a heredoc, and a CR in a shell script is as
    # applicable as one in a sample -- it just is not where anyone looks. Adding
    # a file with CR apiVersions that this screen did not cover would have left
    # an unguarded copy of the exact defect the screen exists for. The regex
    # reads raw lines, so a heredoc needs no special handling.
    # Markdown is swept too, EXCEPT the historical narratives. docs/api.md
    # prescribes CRs a reader copies, and its Instance at `v2-1` plus its
    # InstanceConfiguration at `v1alpha1` were correct but UNGUARDED: flipping
    # every `v2-1` in that file to `v1alpha1` passed validation. That is the
    # same defect class that once made seven examples unapplicable, in the one
    # document a reader follows most closely.
    #
    # HISTORICAL_DOCS are excluded on purpose, and this is not a loophole. The
    # medium articles print a reconciled `Instance` at `v1alpha1` under the words
    # "here's the proof", and that was CORRECT when written -- oasgen's
    # TargetVersion landed in 0.28.1, and before it both halves really were
    # `v1alpha1`. Rewriting those blocks would falsify a record of a real run.
    # They carry a reader warning instead.
    doc_crs = [f for f in _docs()
               if f.replace(os.sep, "/") not in HISTORICAL_DOCS]
    for path in sorted(glob.glob("chart/samples/*.yaml")
                       + glob.glob("examples/**/*.yaml", recursive=True)
                       + glob.glob("hack/*.sh")
                       + doc_crs):
        lines = open(path, encoding="utf-8").read().splitlines()
        for i, ln in enumerate(lines):
            m = api_re.search(ln)
            if not m:
                continue
            kind = None
            for j in range(i + 1, min(i + 4, len(lines))):
                km = re.match(r"^\s*kind:\s*([A-Za-z]+)", lines[j])
                if km:
                    kind = km.group(1)
                    break
            if not kind:
                # Underivable expectation. Every CR in a sample or example has
                # `kind` adjacent to `apiVersion`; if it cannot be found the
                # file is malformed, and dropping the line from both buckets
                # would remove it from the consistency comparison silently.
                errors.append(
                    f"{path}:{i + 1}: found `{m.group(1)}` but no `kind` within "
                    f"3 lines, so this apiVersion cannot be classified as a "
                    f"resource or a Configuration and goes UNCHECKED. Either "
                    f"the document is malformed or it is prose that belongs "
                    f"outside a sample file.")
                continue
            ver = m.group(1).split("/", 1)[1]
            is_cfg = kind.endswith("Configuration")
            (cfg if is_cfg else res).setdefault(
                ver, []).append(f"{path}:{i + 1} ({kind})")
            (cfg_src if is_cfg else res_src).add(path)

    # Coverage gate for the hack/*.sh glob. `res`/`cfg` stay populated from
    # chart/samples/ even if hack/*.sh matched nothing, so the combined
    # emptiness check below cannot notice this sweep going blind -- a rename, a
    # moved heredoc or a changed glob would all keep this screen green while
    # silently dropping the file it was extended for. Absence is only readable
    # once presence is established: assert only if the file IS there, so
    # deleting the probe relaxes the gate instead of breaking the build.
    probe = "hack/probe-361.sh"
    if os.path.exists(probe):
        covered = {pth for pth in list(res_src) + list(cfg_src)}
        if not any(c.startswith("hack/") for c in covered):
            errors.append(
                f"{probe} exists and declares CRs in a heredoc, but this screen "
                f"found none in hack/ -- the sweep that was added for it is no "
                f"longer reaching it. Scanned: {sorted(covered)}. A guard whose "
                f"corpus silently empties reports a clean pass, which is the "
                f"defect this screen exists to catch.")

    if not res and not cfg:
        errors.append("no sample CRs in the group were found to check -- that is "
                      "a failure, not a pass")
        return
    for label, seen in (("resource", res), ("Configuration", cfg)):
        if len(seen) > 1:
            detail = "; ".join(f"{v}: {', '.join(w)}" for v, w in sorted(seen.items()))
            errors.append(
                f"{label} CRs disagree on apiVersion ({detail}). Every {label} CR "
                f"in this chart is served at the same version, so at most one of "
                f"these can be right. The authoritative value is "
                f"`.status.{'configuration' if label == 'Configuration' else 'resource'}"
                f".apiVersion` on the RestDefinition -- read it, do not derive it.")
    if res and cfg and set(res) == set(cfg):
        errors.append(
            f"resource and Configuration CRs share apiVersion {sorted(res)}, but the "
            f"two halves of a generated pair are served at DIFFERENT versions "
            f"(resource = NormalizeVersionName(info.version), Configuration = a "
            f"fixed v1alpha1). One of them is wrong.")


def verify_composition_spec_against_schema(errors):
    """A Composition CR's `spec` IS the chart's values, so it must satisfy
    chart/values.schema.json -- no unknown fields, no missing required ones.

    This exists because the Composition example shipped with:

        spec.nova.enabled: true
        spec.nova.upstreamUrl: https://...

    Neither field exists. `upstreamUrl` was the nginx auth-bridge's upstream,
    and the bridge was deleted when the OAS moved to a native apiKey scheme;
    the schema allows exactly one property there, `url`, required. core-provider
    generates the Composition CRD from values.schema.json and the API server
    decodes strictly, so the example was rejected with

        strict decoding error: unknown field "spec.nova.upstreamUrl"

    It survived the rename because the example had never been applied -- the
    fourth hand-written artifact in this repo with that same cause.

    The smoke job catches this against a real cluster, which is how it was
    found. This screen catches it in seconds without one, and unlike the
    apiVersion case there is nothing to reimplement: the schema is IN this repo
    and is the same file core-provider reads. Comparing two local files is not
    a port of anyone's internal rule.
    """
    schema_path = os.path.join("chart", "values.schema.json")
    if not os.path.exists(schema_path):
        errors.append(f"{schema_path}: missing -- the Composition spec screen "
                      f"has nothing to validate against, which is a failure, "
                      f"not a pass")
        return
    try:
        schema = json.load(open(schema_path, encoding="utf-8"))
    except ValueError as e:
        errors.append(f"{schema_path}: not valid JSON ({e})")
        return

    def unknown_fields(obj, sch, path=""):
        out = []
        allowed = sch.get("properties") or {}
        for key, val in (obj or {}).items():
            here = f"{path}.{key}" if path else key
            if key not in allowed:
                out.append(here)
            elif isinstance(val, dict):
                out.extend(unknown_fields(val, allowed[key], here))
        return out

    # Prove it both ways before trusting the sweep.
    probe_schema = {"properties": {"nova": {"properties": {"url": {}},
                                            "required": ["url"]}}}
    if unknown_fields({"nova": {"upstreamUrl": "x"}}, probe_schema) != ["nova.upstreamUrl"]:
        errors.append("validate.py: the Composition spec screen failed its "
                      "known-BAD assertion and cannot be trusted")
        return
    if unknown_fields({"nova": {"url": "x"}}, probe_schema) != []:
        errors.append("validate.py: the Composition spec screen false-positives "
                      "on a valid spec and cannot be trusted")
        return

    checked = 0
    for path in sorted(glob.glob("examples/**/*.yaml", recursive=True)
                       + glob.glob("examples/*.yaml")):
        try:
            docs = [d for d in yaml.safe_load_all(open(path, encoding="utf-8")) if d]
        except (yaml.YAMLError, OSError, UnicodeDecodeError) as exc:
            # NOT a skip. An example this screen cannot read is an example it
            # cannot assert, and a rationalised `continue` here would hide a
            # future breakage behind a reason that is no longer true: the
            # comment used to claim placeholder apiVersions make some examples
            # unparseable, and on this tree NONE are -- so the path had never
            # run. Credit: krateo-arubacloud-provider-kog, who shipped the same
            # shape in an assertion built to prevent silent passes, and whose
            # one-line check is "find where the derivation can return no answer
            # and confirm the caller treats it as a failure".
            errors.append(f"{path}: cannot be read or parsed, so its Composition "
                          f"spec goes UNASSERTED ({type(exc).__name__}: "
                          f"{str(exc).splitlines()[0]}). A file this screen "
                          f"cannot check is a failure, not a skip.")
            continue
        for doc in docs:
            av = str(doc.get("apiVersion") or "")
            if not av.startswith("composition.krateo.io/"):
                continue
            checked += 1
            spec = doc.get("spec") or {}
            for field in unknown_fields(spec, schema):
                errors.append(
                    f"{path}: Composition spec sets `{field}`, which is not in "
                    f"chart/values.schema.json. A Composition's spec IS the "
                    f"chart's values and the API server decodes it strictly, so "
                    f"this is rejected as `unknown field \"spec.{field}\"`.")
            for prop, sub in (schema.get("properties") or {}).items():
                for req in (sub.get("required") or []):
                    if prop in spec and req not in (spec.get(prop) or {}):
                        errors.append(
                            f"{path}: Composition spec is missing required "
                            f"`{prop}.{req}` (chart/values.schema.json marks it "
                            f"required).")
    if not checked:
        errors.append("no Composition examples found to validate -- that is a "
                      "failure, not a pass")


def _compile_problem(src, name):
    """Compile src with SyntaxWarning promoted to an error; return a message or None.

    BOTH exception types must be caught. With `simplefilter("error",
    SyntaxWarning)` the interpreter raises the escalated diagnostic as a
    **SyntaxError** at compile time, not as a SyntaxWarning -- measured on
    CPython 3.14.6 for `return` in `finally`, invalid escapes, assert-on-tuple
    and `is` with a literal, all four of which arrived as SyntaxError. A version
    of this that caught only SyntaxWarning would catch nothing and report
    success: a check that cannot fail.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("error", SyntaxWarning)
        try:
            compile(src, name, "exec")
        except SyntaxWarning as w:          # some interpreters/diagnostics
            return f"SyntaxWarning: {w}"
        except SyntaxError as e:            # escalated warnings land here
            return f"SyntaxError: {e}"
    return None


def verify_python_compiles_without_warnings(errors):
    """Every Python file must compile with SyntaxWarning promoted to an error.

    Turns diagnostics nobody reads into a hard failure: `return` in a `finally`
    block (which discards an in-flight exception), invalid escape sequences,
    assert-on-a-tuple (always true), `is` against a literal. Each is a defect
    that runs, and each is reported by the compiler and then ignored.

    Adopted from `krateo-arubacloud-provider-kog`, who converted exactly this
    rule from a comment into a gate after finding a `return` in a `finally`
    thirty lines below a comment explaining why not to write one. The general
    lesson does not mechanise -- there is no check for "a comment explains a
    hazard and the next function ignores it" -- so this converts the one
    instance that can be, and claims nothing about the class.

    The self-test compiles the known-bad pattern from a STRING, not a file, on
    purpose: a negative test that writes a file somewhere can break its own
    imports, produce no output, and read exactly like a pass. A string has no
    path to break.
    """
    # WHICH diagnostics exist is version-dependent, so no single fixture proves
    # the mechanism everywhere. `return` in a `finally` block only became a
    # diagnostic in CPython 3.14; on the 3.12 this repo's CI pins it compiles
    # silently. A gate asserting that one fixture alone passed locally and failed
    # in CI -- correctly, loudly, which is the whole reason the assertion is here.
    # Without it the gate would have run in CI detecting nothing and reported
    # success: a check that cannot fail.
    #
    # So: several known-bad fixtures, and the mechanism is proven if ANY is
    # caught. Each one independently exercises promote -> raise -> catch, which
    # is the thing under test; the set only buys portability. The detected ones
    # are printed so a reader sees which diagnostics this interpreter has.
    known_bad = {
        "invalid escape sequence": "x = '\\d'\n",
        "assert on a tuple": "assert (1, 'always true')\n",
        "`is` with a literal": "x = 1\nif x is 1:\n    pass\n",
        "`return` in `finally`": "def f():\n    try:\n        return 1\n    finally:\n        return 2\n",
    }
    caught = sorted(k for k, src in known_bad.items()
                    if _compile_problem(src, "<selftest-bad>") is not None)
    if not caught:
        errors.append(
            "validate.py: the syntax gate failed its known-BAD assertion -- NONE "
            f"of {sorted(known_bad)} was rejected on Python "
            f"{sys.version.split()[0]}, so the gate detects nothing and cannot be "
            "trusted. Either SyntaxWarning promotion stopped working or this "
            "interpreter has none of these diagnostics; add one it does have.")
        return
    print(f"  syntax gate: diagnostics available here -> {caught}")

    good = "def f():\n    try:\n        return 1\n    finally:\n        pass\n"
    problem = _compile_problem(good, "<selftest-good>")
    if problem is not None:
        errors.append("validate.py: the syntax gate false-positives on clean "
                      f"code ({problem})")
        return

    found = 0
    for path in sorted(glob.glob("**/*.py", recursive=True)):
        parts = path.split(os.sep)
        if ".git" in parts or "__pycache__" in parts:
            continue
        try:
            src = open(path, encoding="utf-8").read()
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"{path}: unreadable, so it is NOT compiled and its "
                          f"syntax warnings go undetected ({type(exc).__name__}). "
                          f"A Python file this gate cannot read is a failure.")
            continue
        found += 1
        problem = _compile_problem(src, path)
        if problem:
            errors.append(f"{path}: {problem}")
    if not found:
        errors.append("validate.py: the syntax gate found no Python files to "
                      "compile, which is a failure, not a pass")



def verify_every_script_is_exercised(errors):
    """Every repo script must be invoked by CI, or exempted here with a reason.

    FOUND BY HAND, WHICH IS THE PROBLEM. `scripts/get-token.sh` -- the script a
    quickstart reader actually runs, and the one that handles credentials -- had
    ZERO CI coverage, while every other script in hack/ and scripts/ was
    referenced by at least one workflow. Nothing would have reported that, and
    nothing would report the next one.

    A script nobody invokes is the same defect as a check nobody invokes, one
    level up: its absence from CI is indistinguishable from it passing.

    EXEMPTIONS ARE NAMED, NOT PATTERNED. Each carries the reason it cannot run
    in CI, so a future reader can judge whether the reason still holds rather
    than inheriting a silent allowlist.
    """
    exempt = {
        "hack/probe-361.sh":
            "needs a REAL CLUSTER it does not create -- it takes a kubeconfig "
            "and context and reconciles against an in-cluster mock for ~10 "
            "minutes; run by hand to answer a specific upstream question. (It "
            "used to create its own kind cluster; that was removed when Diego "
            "directed all sessions onto disposable GKE, and this reason was "
            "updated with it rather than left saying 'creates a kind cluster'.)",
        "scripts/get-token.sh":
            "INVOKED IN CI, by scripts/test-get-token.sh, in a form no "
            "syntactic detector can see: `SCRIPT=\"$(dirname \"$0\")/get-token.sh\"` "
            "and then `\"$SCRIPT\"`. The invocation is real -- validate.yaml runs "
            "the test, the test runs this -- so this entry records a LIMIT OF "
            "THE INSTRUMENT, not a gap in CI. Resolving it properly means "
            "following a shell variable, which is a static analyser; an "
            "exemption with the reason written down is the honest alternative.",
    }
    # DELETED 2026-11-13, both measured as covered rather than argued about:
    #   hack/probe-redirect.sh    validate.yaml: `run: bash hack/probe-redirect.sh --self-test`
    #   scripts/test-get-token.sh validate.yaml: `run: bash scripts/test-get-token.sh`
    # Each had a carefully written reason that had stopped being true -- the
    # redirect one said "the measuring device is covered even though the
    # measurement is not", while CI now runs the probe's own self-test, and the
    # get-token one argued a circularity the screen no longer has. They were
    # found by asking a question this registry could not answer: does removing
    # this entry change anything? Credit openstack-ironic-operator-kog, who hit
    # the identical shape -- an accepted-warning entry whose stated basis
    # ("a false positive on both kinds") had been invalidated by their own
    # later change, so it was classifying a true warning as benign.
    scripts = sorted(
        set(glob.glob("hack/*.sh")) | set(glob.glob("hack/*.py"))
        | set(glob.glob("scripts/*.sh")) | set(glob.glob("scripts/*.py"))
    )
    if not scripts:
        errors.append("verify_every_script_is_exercised found NO scripts to "
                      "check -- its globs reach nothing, so it would pass "
                      "whatever the repo contained")
        return

    workflows = glob.glob(".github/workflows/*.yaml") + glob.glob(".github/workflows/*.yml")
    if not workflows:
        errors.append("verify_every_script_is_exercised found NO workflows, so "
                      "every script would look uncovered -- or, inverted, "
                      "covered. Either way it is not measuring")
        return
    def run_commands(paths):
        """Collect only the `run:` scripts from the workflows, STRUCTURALLY.

        THIS SCREEN WAS DEFEATED THREE TIMES BY TEXTUAL MATCHING, each time by
        its own documentation, and each time the false pass was indistinguishable
        from a real one:

          1. validate.py's docstring named `scripts/get-token.sh`, and
             validate.py is itself referenced by a workflow, so the screen read
             its own prose and called the script covered.
          2. the explanatory COMMENT beside the new CI step named it, so
             deleting the step entirely still passed.
          3. the step's `name:` label named it, so neutering `run:` while
             leaving the label still passed.

        A mention is not an invocation, and no amount of comment-stripping
        separates them -- a label, a description or an `if:` expression can all
        contain a filename. So this parses the YAML and takes `run:` values
        only. A step that names a script in its label but does not run it is now
        correctly uncovered.
        """
        cmds = []

        def walk(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    if k == "run" and isinstance(v, str):
                        cmds.append(v)
                    else:
                        walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        for path in paths:
            try:
                walk(yaml.safe_load(open(path, encoding="utf-8")))
            except (OSError, yaml.YAMLError) as e:
                errors.append(f"{path}: cannot be parsed, so the scripts it "
                              f"runs cannot be read ({e}). Calling that "
                              f"'nothing is covered' flags every script and "
                              f"'everything is' flags none. Neither measures.")
        return "\n".join(cmds)

    blob = run_commands(workflows)
    if not blob.strip():
        errors.append("no `run:` commands found in any workflow, so every "
                      "script would look uncovered. That is a parse failure, "
                      "not a finding.")
        return

    def named_in(text, path):
        """Is `path` INVOKED in `text`? An invocation, not a mention.

        Matched as a whole filename, because a bare `in` check would make
        `get-token.sh` look covered the moment `test-get-token.sh` appeared
        anywhere -- right here by accident, and the same accident would hide a
        genuinely uncovered script whose name is a substring of a covered one.

        AND IT HAS TO REQUIRE INVOCATION SYNTAX, which the first version did
        not. That version matched the basename anywhere in the text, and the
        text includes the FULL CONTENTS of every transitively-covered script --
        prose included. Measured 2026-11-13: `hack/probe-361.sh` is invoked by
        no workflow and by no script, and it passed this screen anyway, because
        hack/probe-redirect.sh (genuinely covered, it has a `run:` line) carries

            bad "Run hack/probe-361.sh against the same version: its mock answers the"

        -- a human-readable ERROR MESSAGE, read as an invocation. So the screen
        written to find "a script CI never runs" passed on a script CI never
        runs, which is its own subject one level up. The file already recorded
        this hazard once, for validate.py, and excluded it as a transitive
        source; the exclusion fixed the instance and not the class.

        The forms below are the ones that cannot be prose. A form missing from
        this list makes a real invocation read as uncovered -- which FAILS THE
        SCREEN LOUDLY and gets fixed. The opposite error is silent, which is the
        whole reason this is a list of shapes rather than a substring test.
        """
        base = re.escape(os.path.basename(path))
        name = r"(?:[\w./\-\"'$(){}]*/)?" + base + r"(?![\w.-])"
        forms = (
            r"(?:^|[\s|&;(`])(?:bash|sh|zsh|python3?|uv run)\s+" + name,  # interpreter
            r"(?:^|[\s|&;(`])\./" + name,                                 # ./script
            r"(?:^|\s)run:\s*" + name,                                    # a bare run: step
            r"(?:^|\s)(?:source|\.)\s+" + name,                          # sourced
        )
        return any(re.search(f, text, re.M) for f in forms)

    # PROVE THE JUDGEMENT BOTH WAYS BEFORE TRUSTING IT. This screen's failure
    # mode is a mention passing as coverage, so a detector that only ever says
    # yes would make it vacuous while looking identical.
    for probe, want, why in (
        ("        run: bash hack/thing.sh --self-test", True, "an interpreter invocation"),
        ("      run: hack/thing.sh", True, "a bare run: step"),
        ("  ./hack/thing.sh --flag", True, "a direct ./ invocation"),
        ("  source hack/thing.sh", True, "a sourced script"),
        ('  bad "Run hack/thing.sh against the same version"', False,
         "a filename inside an ERROR MESSAGE"),
        ("# see hack/thing.sh for the manual procedure", False, "a filename in prose"),
        ('MSG="please run hack/thing.sh by hand"', False, "a filename in an assigned string"),
    ):
        if named_in(probe, "hack/thing.sh") is not want:
            errors.append(
                "validate.py: the script-coverage detector reads %s as %s. "
                "Probe: %r. A detector that cannot tell an invocation from a "
                "mention makes this screen pass on the gap it exists to find."
                % (why, "a mention" if want else "an invocation", probe))
            return

    # Transitive, one level: a script invoked BY a covered script is covered.
    # scripts/test-get-token.sh runs get-token.sh, which is real coverage and
    # should be recognised deliberately rather than by substring luck.
    #
    # validate.py is EXCLUDED as a transitive source, and the reason is the best
    # instance of this screen's own subject found so far. validate.py is named
    # in the workflows, so it counted as covered; its contents were then added
    # to the transitive reach; and this screen's docstring and exemption notes
    # MENTION `scripts/get-token.sh` by name. So the screen read its own
    # documentation, found the script named there, and reported it as covered.
    #
    # The measurement included the measurer, and the first version of this
    # screen passed cleanly on the very gap it was written to find. validate.py
    # names scripts as DATA -- it lints them -- never as invocations, so it can
    # never be a legitimate source of coverage.
    covered_scripts = [s for s in scripts
                       if s != "scripts/validate.py" and named_in(blob, s)]
    indirect = ""
    for s in covered_scripts:
        try:
            indirect += "\n" + open(s, encoding="utf-8").read()
        except OSError:
            pass
    reach = blob + indirect

    uncovered = []
    for s in scripts:
        if s in exempt or s == "scripts/validate.py":
            continue
        if not named_in(reach, s):
            uncovered.append(s)
    if uncovered:
        errors.append(
            "these scripts are invoked by NO workflow and are not exempted: "
            f"{uncovered}. Either wire one into CI, or add it to `exempt` in "
            f"this screen WITH THE REASON it cannot run there. An uncovered "
            f"script's absence from CI is indistinguishable from it passing.")

    stale = [s for s in exempt if not os.path.exists(s)]
    if stale:
        errors.append(f"these exemptions name scripts that no longer exist: "
                      f"{stale}. A stale exemption is a hole kept open for a "
                      f"file that is gone.")

    # EVERY EXEMPTION MUST STILL DO SOMETHING. An entry whose script is now
    # genuinely covered is not harmless: it reads as "CI does not exercise
    # this", which is then false, and it carries a written reason a future
    # reader will believe. Two of the three entries here were exactly that on
    # 2026-11-13, and nothing reported it -- the registry had no question it
    # could fail. This is that question.
    #
    # Deliberately NOT the same test as `stale`: that one asks whether the file
    # exists, this one asks whether the exemption changes the outcome.
    unnecessary = [s for s in exempt if named_in(reach, s)]
    if unnecessary:
        errors.append(
            "these exemptions are NOT load-bearing -- CI reaches each of these "
            f"scripts, so the entry changes nothing: {unnecessary}. Delete them. "
            f"An exemption that does nothing still asserts that CI does not "
            f"exercise the script, and a reader has no way to tell that claim "
            f"from a live one.")


def verify_doc_filenames_exist(errors):
    """A backticked repo filename in the docs must name a file that exists.
    # KNOWN FALSE-POSITIVE SHAPE, measured by injection on 2026-11-21: this
    # screen cannot tell a REFERENCE to a file from a WARNING about a filename.
    # Appending to a doc
    #
    #     Never write a path like `totally-nonexistent-probe.sh` in these docs.
    #
    # makes it fail, naming that path. The same equivalence-class error landed in
    # a peer's own check twice today: they asserted `'fixed in 0.85.1' not in
    # reason` and it fired, because their correction QUOTES the wording it
    # corrects.
    #
    # NOT FIXED, DELIBERATELY. It fails in the SAFE direction -- a screen
    # complaining about something fine, loudly, with the path named -- and the
    # `exempt` dict below is the documented escape, which forces the reason to be
    # written down. Teaching it a "this is a negative example" convention would
    # invent a convention no document here uses, and the convention would then
    # need its own verification.

    FOUND BY AUDIT, WHICH IS THE PROBLEM. `docs/release.md` -- the runbook a
    maintainer follows to cut a release -- named `release-chart.yaml` three
    times. No such workflow existed; the file was `release-tag.yaml`, since
    retired in favour of `release.yaml`. The README named it too, plus
    `ci.yaml`, which has never existed either. Nothing
    reported any of it: `hack/preflight-refs.py` resolves OCI and image
    references, not filenames in prose, and never claimed otherwise.

    The runbook was wrong in a way that would have cost something. It said to
    bump `chart/Chart.yaml`'s `version` before tagging, and described a
    "tag-matches-Chart.yaml guard" that is in no workflow. The real mechanism
    stamps `CHART_VERSION` FROM the tag, so committing a concrete version would
    have silently removed the placeholder and made `smoke.yaml`'s stamp check
    vacuous -- that check fires when the placeholder SURVIVES, so a file that
    never had one always passes.

    EXEMPTIONS ARE NAMED WITH REASONS, not patterned, so a reader can judge
    whether each still holds.
    """
    exempt = {
        "/creds/key.json": "a path INSIDE a container, not a repo file",
        "key.json": "a service-account key the operator supplies themselves",
        "openrc.sh": "downloaded from the OpenStack dashboard by the reader; "
                     "deliberately gitignored so it can never be committed",
        "release-oci.yaml":
            "the ORG'S SHARED release workflow, which lives upstream at "
            "krateo-platformops/.github .github/workflows/release-oci.yaml and "
            "is CALLED from this repository's release.yaml at a pinned SHA "
            "(2.1.0). Named in the runbook because the mechanism it documents IS "
            "that workflow: a reader who cannot tell which file packages and "
            "pushes the chart has no way to check the pin it runs at. Nothing in "
            "this repo ships it",
        "ogen.krateo.io_restdefinitions.yaml":
            "oasgen-provider's GENERATED RestDefinition CRD, which lives "
            "upstream at go/oasgen-provider/crds/ and is cited at a PINNED tag "
            "(0.52.0, and cross-checked at 0.61.0/0.62.0/main). Nothing in this "
            "repo ships it, and citing it by name is the point: it is the "
            "authority that says identifiersMatchPolicy is a per-verb field, "
            "which is what hack/check-restdefinition-shape.py had wrong",
        "nova/api/openstack/compute/server_groups.py":
            "OpenStack Nova's own source at stable/2026.1, cited for the guard "
            "`is_supported(req, '2.13')` that gates project_id and user_id out "
            "of the response at the 2.1 this chart requests. Named because the "
            "API reference documents the NEWEST microversion and would have "
            "confirmed the field we had to remove",
        "nova/api/openstack/compute/views/servers.py":
            "OpenStack Nova's own source at stable/2026.1, cited for "
            "`_get_image`: its else branch is `return \"\"`, so a server with "
            "no image_ref reports `image` as the EMPTY STRING while an "
            "image-backed one reports an object. That single line is why "
            "`server.image` cannot be a CR status field at all, and naming the "
            "file is the point -- it is the authority for an ABSENCE, which is "
            "the hardest kind of decision for a later reader to re-derive",
        "nova/api/openstack/wsgi.py":
            "Nova's source at stable/2026.1, cited for set_api_version_request: "
            "a request with no microversion header resolves to "
            "DEFAULT_API_VERSION, which is the API MINIMUM. That one line is "
            "why this chart is 2.1 everywhere without asking to be",
    }
    token = re.compile(r"`([A-Za-z0-9_.\-/]+\.(?:yaml|yml|py|sh|json|tpl))`")
    # PINNED RATHER THAN DESCRIBED. The comment above explains that this screen
    # cannot tell a WARNING about a filename from a REFERENCE to one; these three
    # lines make that behaviour executable, so a later "improvement" that changes
    # it must change this line deliberately instead of silently.
    #
    # The peer who hit this same class resolved it the same way -- their fix's own
    # head became a must-pass case for their gate, so the trap is a test rather
    # than a footnote. A footnote is a claim about behaviour; an assert IS the
    # behaviour.
    assert token.findall("See `scripts/validate.py` for details") == ["scripts/validate.py"]
    assert token.findall("Never write a path like `nope.sh` in docs") == ["nope.sh"]
    assert token.findall("a bare word.yaml outside backticks") == []

    # Prove the pattern before trusting the sweep.
    if token.findall("see `release.yaml` and `scripts/validate.py`") != \
            ["release.yaml", "scripts/validate.py"]:
        errors.append("validate.py: the doc-filename screen's pattern does not "
                      "extract the names it is built to check")
        return
    if token.findall("a bare word.yaml outside backticks"):
        errors.append("validate.py: the doc-filename screen matches outside "
                      "backticks, so prose would trip it")
        return

    present = set()
    for root, dirs, files in os.walk("."):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "charts")]
        present.update(files)
    if not present:
        errors.append("the doc-filename screen walked the repo and found NO "
                      "files, so every reference would look broken")
        return

    docs = [f for f in _docs()]
    if not docs:
        errors.append("the doc-filename screen found NO markdown, so it has "
                      "nothing to check and would pass regardless")
        return

    broken, seen_any = [], False
    for path in docs:
        try:
            lines = open(path, encoding="utf-8").read().splitlines()
        except OSError as e:
            errors.append(f"{path}: unreadable, so its references are NOT "
                          f"checked ({e}). A file this screen cannot read is a "
                          f"failure, not a skip.")
            continue
        for i, line in enumerate(lines, 1):
            for tok in token.findall(line):
                seen_any = True
                if tok in exempt:
                    continue
                if os.path.exists(tok) or os.path.basename(tok) in present:
                    continue
                broken.append(f"{path}:{i}: `{tok}`")
    if not seen_any:
        errors.append("the doc-filename screen matched NO filenames in any doc, "
                      "which cannot be right for this repo -- the pattern has "
                      "stopped reaching the content")
        return
    if broken:
        errors.append(
            "these docs name files that do not exist in this repo: "
            f"{broken}. A runbook naming a workflow that is not there sends a "
            f"maintainer looking for it, or worse, describes a mechanism that "
            f"does not run. Fix the name, or exempt it in this screen WITH THE "
            f"REASON it is not a repo file.")

    resolved = [t for t in exempt if os.path.exists(t)]
    if resolved:
        errors.append(f"these exemptions now resolve to real files: {resolved}. "
                      f"An exemption that is no longer needed hides a check.")


def verify_no_unstampable_install(errors):
    """No live doc may tell a reader to `helm install ./chart`.

    IT DOES NOT WORK, AND NOTHING CAUGHT IT. `chart/Chart.yaml` carries
    `version: CHART_VERSION`, which the release tag stamps, so `helm` refuses the
    working tree outright:

        Error: validation: chart.metadata.version "CHART_VERSION" is invalid

    SIX live documents instructed exactly that, including README.md and
    docs/quickstart.md -- the first command a new reader runs. CI never noticed
    because every workflow installs a STAMPED STAGED COPY (`smoke.yaml`: "Stage a
    stamped chart"), never `./chart` as the docs said. The documented path and
    the tested path had diverged, and only the tested one was exercised.

    HISTORICAL_DOCS are exempt because they are accurate: Chart.yaml held a
    literal version until 2026-08-11, and the medium articles were written
    2026-05-28. They carry a reader warning instead of a rewrite.

    The screen is line-scoped: a finding is a `helm install`/`helm upgrade`
    naming `./chart` ON ONE LINE. overview.md's prose survives because it says
    "Installing from a clone instead (`./chart`)" without `helm install` beside
    it.

    BUT IT WILL FIRE ON A WARNING THAT QUOTES THE COMMAND VERBATIM. The
    historical articles' new note says "The `helm install ./chart` commands below
    no longer work" -- which matches, and would be flagged if those files were
    not exempt. That is deliberate: the alternative is to exempt lines containing
    "no longer" or similar, which hands any author a phrase that silences a real
    finding. A false positive here fails loudly and is fixed by splitting the
    line or fencing the command; an escape hatch fails silently forever.

    So: if this screen trips on correct prose, rephrase the prose. Do not widen
    the exemption.
    """
    cmd = re.compile(r"\bhelm\b[^\n]*\b(?:install|upgrade)\b[^\n]*\./chart\b")

    bad = 'helm upgrade --install nova-kog ./chart -n krateo-system \\'
    good_oci = ('helm upgrade --install nova-kog '
                'oci://ghcr.io/krateo-blueprints/charts/openstack-nova-operator-kog '
                '--version 0.1.5 \\')
    good_prose = "Installing from a clone instead (`./chart`) needs the placeholder stamped"
    if not cmd.search(bad):
        errors.append("validate.py: the unstampable-install screen fails its "
                      "known-BAD assertion; the six commands it was written for "
                      "would escape again")
        return
    if cmd.search(good_oci):
        errors.append("validate.py: the unstampable-install screen fires on the "
                      "CORRECT published-chart command")
        return
    if cmd.search(good_prose):
        errors.append("validate.py: the unstampable-install screen fires on "
                      "prose about ./chart, which overview.md needs in order to "
                      "explain the prerequisite")
        return

    hits = []
    for path in _docs():
        if path.replace(os.sep, "/") in HISTORICAL_DOCS:
            continue
        try:
            lines = open(path, encoding="utf-8").read().splitlines()
        except OSError as e:
            errors.append(f"{path}: unreadable, so it is NOT screened for an "
                          f"unstampable install ({e})")
            continue
        for i, line in enumerate(lines, 1):
            if cmd.search(line):
                hits.append(f"{path}:{i}")
    if hits:
        errors.append(
            f"these docs instruct an install from the working tree: {hits}. "
            f"`helm` refuses it -- chart.metadata.version \"CHART_VERSION\" is "
            f"invalid -- so the command fails on the reader's first attempt. "
            f"Point at the published chart with an explicit --version instead.")


def verify_documented_chart_version(errors):
    """Every documented `--version` for THIS chart must equal the newest tag.

    A RISK INTRODUCED BY A FIX. Six live docs told a reader to
    `helm install ./chart`, which `helm` refuses outright because Chart.yaml
    carries the CHART_VERSION placeholder. The fix points them at the published
    chart with an explicit `--version`, which works -- and creates a new way to
    be wrong: on the next release all six go stale, and a reader installs an
    older chart than the document claims to describe.

    `verify_documented_pin` guards the OASGEN pin, not ours, so nothing covered
    this. The newest SemVer tag is the only honest source: the release workflow
    derives the published version from exactly that.

    MATCHES OUR CHART ONLY. The docs also pin oasgen (0.49.0) and the kagent
    chart (0.9.6) with `--version`; those are different artifacts and this screen
    must not touch them, so the pattern requires this chart's name on the line.

    IT ERRORS RATHER THAN SKIPS when it cannot find a tag. A skip would read as a
    pass, which is the defect this repo has found seven times -- and a missing
    tag in CI has a specific cause worth naming (a shallow checkout), so the
    message names it.
    """
    tags = []
    try:
        out = subprocess.run(("git", "tag", "--list"), capture_output=True,
                             text=True, timeout=30)
        if out.returncode == 0:
            tags = [t for t in out.stdout.split()
                    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", t)]
    except (OSError, subprocess.SubprocessError) as e:
        errors.append(f"the chart-version screen could not run `git tag` ({e}), "
                      f"so it cannot compare. That is a failure, not a pass.")
        return
    if not tags:
        errors.append("the chart-version screen found NO SemVer tags, so it has "
                      "nothing to compare the docs against. In CI this usually "
                      "means a shallow checkout -- validate.yaml sets "
                      "fetch-depth: 0 for exactly this reason. Erroring rather "
                      "than skipping, because a skip would read as a pass.")
        return
    newest = max(tags, key=lambda t: tuple(int(x) for x in t.split(".")))

    pat = re.compile(r"openstack-nova-operator-kog\b[^\n]*?--version\s+"
                     r"([0-9]+\.[0-9]+\.[0-9]+)")

    # Prove the pattern discriminates before trusting the sweep.
    ours = ("helm install nova oci://ghcr.io/krateo-blueprints/charts/"
            "openstack-nova-operator-kog --version 0.1.5 \\")
    theirs = ("helm upgrade --install oasgen-provider oci://ghcr.io/"
              "krateo-platformops/charts/oasgen-provider --version 0.49.0 \\")
    if pat.findall(ours) != ["0.1.5"]:
        errors.append("validate.py: the chart-version screen's pattern does not "
                      "match this chart's own install command")
        return
    if pat.findall(theirs):
        errors.append("validate.py: the chart-version screen matches ANOTHER "
                      "chart's --version, so it would demand our tag of theirs")
        return

    stale, found = [], 0
    for path in _docs():
        if path.replace(os.sep, "/") in HISTORICAL_DOCS:
            continue
        try:
            lines = open(path, encoding="utf-8").read().splitlines()
        except OSError as e:
            errors.append(f"{path}: unreadable, so its chart version is NOT "
                          f"checked ({e})")
            continue
        for i, line in enumerate(lines, 1):
            for v in pat.findall(line):
                found += 1
                if v != newest:
                    stale.append(f"{path}:{i} says {v}")
    if found == 0:
        errors.append("the chart-version screen matched NO documented version "
                      "for this chart, which cannot be right -- the docs install "
                      "it in several places. The pattern has stopped reaching "
                      "the content, so its silence means nothing.")
        return
    if stale:
        errors.append(f"the newest tag is {newest}, but these docs pin a "
                      f"different version of this chart: {stale}. A reader would "
                      f"install an older chart than the document describes. "
                      f"Update them, or cut the release the docs already claim.")


def verify_no_credential_following_redirects(errors):
    """No curl in this repo may follow redirects. The credential would follow.

    `scripts/get-token.sh` POSTs the operator's password -- or application
    credential secret -- to `$OS_AUTH_URL`, which the operator supplies. It
    passes no `-L`, so curl stops at a 302 and the script exits 1 naming the
    missing X-Subject-Token. That safety currently rests on a DEFAULT, and a
    default is a weak place to keep a security property: adding `-L` for
    convenience would be a one-character change with no visible consequence
    until a redirecting endpoint appeared.

    WHY A SUBDOMAIN HOP IS THE REAL RISK, not an exotic one. Go's net/http
    documents it in `shouldCopyHeaderOnRedirect`: "Permit sending auth/cookie
    headers from foo.com to sub.foo.com", comparing url.Hostname() so the PORT is
    dropped as well. That is deliberate stdlib behaviour no Go release will
    change, and curl's --location-trusted has the same shape. oasgen-provider
    measured a Bearer token arriving at evil.krateo.test from krateo.test on a
    plain http.Client for exactly this reason (their #401).

    This chart ships no Go, so the stdlib row does not reach it -- but it ships a
    credential-posting shell script, which has the same exposure through `-L`.

    THE CONTROL MATTERS MORE THAN THE SWEEP HERE, because "no match" is the
    reassuring answer: a typo'd pattern produces it too. So the regexes are
    asserted against planted known-bad lines in both spellings before use.
    """
    follows = re.compile(r"curl\b[^|;&\n]*(?:\s-L\b|\s--location(?:-trusted)?\b)")

    bad_short = 'body=$(curl -sS -L -D "$tmp" --url "$u")'
    bad_long = 'body=$(curl -sS --location -D "$tmp")'
    bad_trusted = 'curl --location-trusted -H "X-Auth-Token: $t" "$u"'
    good = 'body=$(curl -sS -D "$tmp_headers" -X POST "$url" -d "$payload")'
    for label, line in (("-L", bad_short), ("--location", bad_long),
                        ("--location-trusted", bad_trusted)):
        if not follows.search(line):
            errors.append(f"validate.py: the redirect-following screen does not "
                          f"detect {label}, so its silence over the repo would "
                          f"mean nothing")
            return
    if follows.search(good):
        errors.append("validate.py: the redirect-following screen fires on a "
                      "curl that does NOT follow redirects, so it would flag "
                      "the correct form")
        return

    targets = sorted(set(glob.glob("scripts/*.sh")) | set(glob.glob("hack/*.sh"))
                     | set(glob.glob(".github/workflows/*.yaml")))
    if not targets:
        errors.append("the redirect-following screen found NO shell or workflow "
                      "files, so it swept nothing and would pass regardless")
        return

    def logical_commands(lines):
        """Join backslash continuations, so a multi-line invocation is one unit.

        A line-scoped match MISSED a real case. The probes run curl through
        `kubectl run --image=curlimages/curl:8.11.0 --`, with a trailing
        continuation and the flags on following lines, so the word `curl` and
        the flag are never on the same line. Planting `--location-trusted` there
        was NOT caught until this joined them -- found by mutation, after the
        single-line case passed and made the screen look finished.
        """
        out, buf, start = [], "", 1
        for i, line in enumerate(lines, 1):
            if line.lstrip().startswith("#"):
                continue   # the rationale comments quote the flags on purpose
            if not buf:
                start = i
            stripped = line.rstrip()
            if stripped.endswith("\\"):
                buf += stripped[:-1] + " "
                continue
            out.append((start, buf + stripped))
            buf = ""
        if buf:
            out.append((start, buf))
        return out

    hits = []
    for path in targets:
        try:
            lines = open(path, encoding="utf-8").read().splitlines()
        except OSError as e:
            errors.append(f"{path}: unreadable, so it is NOT screened for "
                          f"redirect-following curl ({e})")
            continue
        for i, command in logical_commands(lines):
            if follows.search(command):
                hits.append(f"{path}:{i}")
    if hits:
        errors.append(
            f"these invocations follow redirects: {hits}. A credential follows a "
            f"redirect to a SUBDOMAIN or another port -- documented, deliberate "
            f"behaviour in both net/http and curl --location-trusted -- so the "
            f"operator's password can reach a host nobody declared. Drop the "
            f"flag; a 302 should fail loudly instead.")

def verify_chart_icon_is_org_controlled(errors):
    """`chart/Chart.yaml`'s icon must be https and under a path this org controls.

    WHY THIS EXISTS, measured not hypothetical. The icon shipped in this chart
    was the OpenStack Nova mascot on `www.openstack.org`, and it returns **404**
    -- confirmed three ways (plain, browser User-Agent, and following redirects)
    with `https://www.openstack.org/` itself returning 200 as the control, so
    the site moved the file rather than being down. It was found one command
    before cutting the first tag in five releases, which would have published a
    dead icon into the live helm index and rendered it as broken art on the
    portal Marketplace card.

    NOTHING COULD HAVE CAUGHT IT EARLIER, and that is the point: a dead
    third-party URL is still a syntactically valid https icon, `helm lint` has
    no opinion on it, and this chart had not been published since 2026-08-19, so
    no card had ever tried to render it. The failure needed a release to become
    visible, which is the worst time to find it.

    THIS SCREEN DOES NOT CHECK THAT THE URL IS LIVE, and says so rather than
    implying coverage it lacks. A network fetch here would redden CI for
    reasons that have nothing to do with this repo -- a proxy, a rate limit, an
    outage -- and a screen that fails for unrelated reasons gets disabled, which
    is worse than one with a stated limit.

    WHAT IT CHECKS INSTEAD is the property that actually prevents a recurrence:
    the icon lives under a path the Krateo org controls, so it cannot rot
    because a third party reorganised their CDN. That is fully checkable
    offline and is the real lesson of the 404.

    IT ALSO REFUSES THE UNHYPHENATED ORG. `raw.githubusercontent.com/
    krateoplatformops/...` (no hyphen) resolves **200** through GitHub's
    org-rename redirect, so it fails silently and forever. The hyphenated
    `krateo-platformops` is the live org. See the hyphen-trap note in the repo
    docs -- this is the same trap in a different field.

    HOW WIDESPREAD, WITH THE SELECTOR NAMED, because an earlier version of this
    docstring said "385 entries" with no selector and I quoted "roughly 39" for
    the same thing in the same hour. Both were real; they count different
    populations, and without the selector written beside them they looked like a
    contradiction. Measured in the live blueprints index on 2026-11-10:

        37  distinct charts whose NEWEST entry's icon is unhyphenated
       289  distinct charts where ANY version mentions it
       397  raw occurrences, i.e. one per affected chart VERSION

    THE FIRST IS THE DECISION-RELEVANT ONE: portal #383 reads only the newest
    entry, so 37 is the number of charts actually rendering a redirected icon
    today. The other two describe accumulated history that no publish can undo,
    because a helm index keeps every version it has ever served.

    AND ALL THREE DRIFT. The raw count was 385 when first measured and 397 a few
    hours later; charts publish continuously. So treat any figure here as a
    point-in-time reading, not a fact -- which is the reason the selector matters
    more than the number.
    """
    path = "chart/Chart.yaml"
    try:
        raw = open(path, encoding="utf-8").read()
    except OSError as e:
        errors.append(f"{path}: unreadable, so the chart icon is NOT checked "
                      f"({e}). That is a failure, not a pass.")
        return

    OK_PREFIXES = ("https://raw.githubusercontent.com/krateo-platformops/",
                   "https://raw.githubusercontent.com/krateo-blueprints/")
    DEAD_ORG = "krateoplatformops"

    def judge(url):
        """Return a complaint about `url`, or None if it is acceptable.

        The real check and the self-tests below both call THIS, so a passing
        self-test is evidence about the code that runs. A self-test that
        exercised a different expression would prove nothing -- which is a
        mistake this repo has made and does not intend to repeat.
        """
        if not url:
            return "the chart declares no icon, which the channel gate requires"
        if not url.startswith("https://"):
            return f"the icon is not https: {url!r}"
        if DEAD_ORG in url:
            return (f"the icon names the UNHYPHENATED org, which resolves 200 "
                    f"through a rename redirect and so fails silently: {url!r}")
        if not url.startswith(OK_PREFIXES):
            return (f"the icon is not under a Krateo-controlled path, so it can "
                    f"rot without warning -- the previous value did exactly "
                    f"that (openstack.org, HTTP 404): {url!r}")
        return None

    # Prove the judgement discriminates before trusting it on the real value.
    cases = (
        ("https://www.openstack.org/themes/openstack/images/project-mascots/"
         "Nova/OpenStack_Project_Nova_vertical.png", True),
        ("https://raw.githubusercontent.com/krateoplatformops/krateo/main/"
         "docs/media/logo.svg", True),
        ("http://raw.githubusercontent.com/krateo-platformops/.github/main/"
         "brand/logo.svg", True),
        ("", True),
        ("https://raw.githubusercontent.com/krateo-platformops/.github/main/"
         "brand/logo.svg", False),
        ("https://raw.githubusercontent.com/krateo-blueprints/"
         "openstack-nova-operator-kog/main/docs/images/icon.svg", False),
    )
    for url, want_complaint in cases:
        got = judge(url)
        if want_complaint and got is None:
            errors.append(f"validate.py: the icon screen ACCEPTS a value it "
                          f"must refuse ({url!r}), so its silence means "
                          f"nothing")
            return
        if not want_complaint and got is not None:
            errors.append(f"validate.py: the icon screen refuses a legitimate "
                          f"value ({url!r}): {got}")
            return

    m = re.search(r"^icon:\s*(\S*)\s*$", raw, re.M)
    if not m:
        errors.append(f"{path}: no `icon:` key found. The blueprints channel "
                      f"gate requires an https icon, so its absence fails the "
                      f"publish rather than defaulting to anything.")
        return
    complaint = judge(m.group(1))
    if complaint:
        errors.append(f"{path}: {complaint}")

    # A self-hosted icon must point at a file that is actually committed.
    url = m.group(1)
    own = ("https://raw.githubusercontent.com/krateo-blueprints/"
           "openstack-nova-operator-kog/main/")
    if url.startswith(own):
        rel = url[len(own):]
        if rel and not os.path.exists(rel):
            errors.append(f"{path}: the icon is self-hosted at {rel!r}, but no "
                          f"such file is committed, so the published icon would "
                          f"404 exactly as the previous one did.")


def verify_no_header_parameters_in_assets(errors):
    """No OAS asset may declare an `in: header` PARAMETER.

    WHY, and it is a structural immunity worth keeping rather than a style rule.
    The openstack-ironic-operator-kog session measured this in their own repo:
    a required `in: header` PARAMETER can be **dropped with no error** -- the
    request goes out without it, and their workaround is a static per-verb header
    on the RestDefinition instead.

    THE MECHANISM, VERIFIED HERE IN THE PROVIDER'S SOURCE. This paragraph has had
    three states -- asserted, retracted, restored -- so it is written from a read
    of the code rather than from anyone's summary, including mine:

        rest-dynamic-controller/internal/tools/client/builder/builder.go
        applyConfigSpec, BEFORE commit 1da37f6:
            process("path",    req.Parameters)
            process("query",   req.Query)
            process("headers", req.Headers)     <- PLURAL
            process("cookies", req.Cookies)     <- PLURAL
        AFTER:
            process("header",  req.Headers)
            process("cookie",  req.Cookies)

    The key is passed to `unstructured.NestedMap(configSpec, key, action)`, and
    the Configuration schema is generated from the OpenAPI location verbatim --
    and OpenAPI's locations are SINGULAR. So `path` and `query` worked by
    coincidence of spelling, while every `configurationFields` header or cookie
    was generated into the CRD and then never applied, with no error. The fix's
    own commit message says it: "generated into the Configuration CRD and then
    NEVER APPLIED".

    THE VERSION WAS MISATTRIBUTED, NOT THE MECHANISM. Measured by ancestry with
    controls: `1da37f6` is absent at 0.43.0 and present from 0.44.0 (#304/#338,
    2026-10-31). A SECOND and distinct defect -- a *spec-field* header never
    populated from the CR -- is `4acbf8a`, absent at 0.49.0 and present from
    0.50.0 (#305/#392). THIS CHART'S PIN, 0.52.0, CARRIES BOTH. The two fail in
    opposite directions, which is the discriminator: the configurationFields one
    is SENT without the header and logs nothing, the spec-field one is never sent
    and logs `missing header: <name>`. Tracked upstream as oasgen-provider#561.

    The screen is kept, and would be kept even with the mechanism in doubt: a
    measured symptom is enough to avoid the shape, and oasgen-provider#391 gives
    an independent reason -- its missing-header check exempts a required header
    parameter only when the name contains "authorization", so a required
    `X-Auth-Token` parameter would be refused outright.

    THIS CHART IS IMMUNE FOR A STRUCTURAL REASON, NOT BY LUCK. Its credential is
    a `type: apiKey` SECURITY SCHEME, which the provider turns into
    `spec.authentication.apiKey.{tokenRef,header}` and attaches through its own
    auth path. A security scheme never enters the parameter-header code path at
    all. That is a different claim from "we grepped and found nothing", which
    describes today and stops being true the moment somebody adds a parameter.

    So this screen makes the structural property enforceable: an `in: header`
    inside `securitySchemes` is fine, and one anywhere else is refused with the
    reason, so whoever adds it finds out here rather than from a silently
    unsent header.

    THE FILES DO NOT PARSE AS YAML, so this is textual. OAS path templates like
    `/os-quota-sets/{tenant_id}:` are flow mappings used as keys, and
    `yaml.safe_load` raises "while constructing a mapping" on every one of these
    assets. A screen that tried to parse them would error on all six and the
    error would read as a finding.

    THE DISCRIMINATOR is a sibling `type: apiKey` at the same indentation. A
    security scheme has `type`, `in` and `name` as siblings; a header parameter
    has `name`, `in`, `required` and `schema`, with its `type` nested under
    `schema`. Siblings are read by indentation rather than by looking upward for
    a section header, because `securitySchemes` and a path-level `parameters`
    block sit at different depths in different files.
    """
    import glob

    def judge(text):
        """Return a list of (line_no, why) for every offending `in: header`.

        The real sweep and the self-tests below both call THIS, so a passing
        self-test is evidence about the code that runs.
        """
        out, lines = [], text.splitlines()
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped != "in: header":
                continue
            indent = len(line) - len(line.lstrip())
            # Walk the sibling block: same indent, until the block ends.
            sibling_type_apikey = False
            for j in range(i - 1, -1, -1):          # upward
                ln = lines[j]
                if not ln.strip() or ln.strip().startswith("#"):
                    continue
                ind = len(ln) - len(ln.lstrip())
                if ind < indent:
                    break
                if ind == indent and ln.strip() == "type: apiKey":
                    sibling_type_apikey = True
            for j in range(i, len(lines)):           # downward
                ln = lines[j]
                if not ln.strip() or ln.strip().startswith("#"):
                    continue
                ind = len(ln) - len(ln.lstrip())
                if ind < indent:
                    break
                if ind == indent and ln.strip() == "type: apiKey":
                    sibling_type_apikey = True
            if not sibling_type_apikey:
                out.append((i, "an `in: header` with no sibling `type: apiKey`, "
                               "so it is a header PARAMETER, not a security "
                               "scheme"))
        return out

    # Prove the judgement discriminates before trusting it on the real assets.
    scheme = ("components:\n"
              "  securitySchemes:\n"
              "    apiKey:\n"
              "      type: apiKey\n"
              "      in: header\n"
              "      name: X-Auth-Token\n")
    param = ("paths:\n"
             "  /thing:\n"
             "    parameters:\n"
             "      - name: X-Thing\n"
             "        in: header\n"
             "        required: true\n"
             "        schema:\n"
             "          type: string\n")
    if judge(scheme):
        errors.append("validate.py: the header-parameter screen refuses a "
                      "legitimate apiKey security scheme, so it would fail on "
                      "every asset this chart ships")
        return
    if not judge(param):
        errors.append("validate.py: the header-parameter screen ACCEPTS a real "
                      "header parameter, so its silence means nothing")
        return
    if not judge(scheme + param):
        errors.append("validate.py: the header-parameter screen misses a "
                      "parameter when a scheme is also present, which is the "
                      "only shape these assets could actually take")
        return

    assets = sorted(glob.glob(os.path.join("chart", "assets", "*.yaml")))
    if not assets:
        errors.append("the header-parameter screen found NO files under "
                      "chart/assets/, so it swept nothing. An empty sweep "
                      "passes vacuously; refusing.")
        return
    hits, schemes = [], 0
    for path in assets:
        try:
            text = open(path, encoding="utf-8").read()
        except OSError as e:
            errors.append(f"{path}: unreadable, so it is NOT screened for "
                          f"header parameters ({e})")
            continue
        schemes += text.count("in: header") - len(judge(text))
        for line_no, why in judge(text):
            hits.append(f"{path}:{line_no} {why}")
    if schemes == 0:
        errors.append("the header-parameter screen matched NO `in: header` at "
                      "all across chart/assets/. Every asset declares an apiKey "
                      "security scheme with one, so the pattern has stopped "
                      "reaching the content and its silence means nothing.")
        return
    if hits:
        errors.append(
            f"these assets declare an `in: header` PARAMETER: {hits}. "
            f"oasgen-provider emits the generated property as `header` while "
            f"rest-dynamic-controller reads `headers`, so the lookup misses and "
            f"the value is DROPPED WITH NO ERROR -- measured by the "
            f"openstack-ironic-operator-kog session. This chart's credential is "
            f"a security scheme precisely so it never enters that path. If you "
            f"need a header on the wire, pin it as a static per-verb header on "
            f"the RestDefinition instead.")


def verify_boot_index_has_no_lower_bound(errors):
    """`boot_index` must carry no numeric lower bound, and must stay nullable.

    ASKED FOR BY THE REVIEWER OF #215, who spotted that the field had no comment
    saying why it is unbounded -- the kind of omission an author does not see.

    Nova's own source at stable/2026.1,
    nova/api/openstack/compute/schemas/servers.py:

        # NOTE(mriedem): boot_index=None is also accepted for backward
        # compatibility with the legacy v2 API.
        'boot_index': {
            'type': ['integer', 'string', 'null'],
            'pattern': '^-?[0-9]+$',
        },

    The pattern admits a leading `-`, and `-1` is Nova's documented way to say
    "never boot from this device". So `minimum: 0` is exactly the tightening a
    later reader would reach for, and it would reject a documented value at
    `kubectl apply` -- before any request is made, with an error that blames the
    CRD rather than naming Nova. `exclusiveMinimum` is refused for the same
    reason, and `nullable` is required to stay because Nova accepts null.

    WHY A SCREEN RATHER THAN A COMMENT. The comment is there too, and comments
    do not fail CI. This chart's history is mostly cases where a reasonable
    "obvious fix" was wrong, and two of them were reintroduced after being
    removed once.

    SCOPED TO THE FIELD, NOT TO THE FILE. A lower bound elsewhere may be
    perfectly correct -- `volume_size` could legitimately carry `minimum: 1` --
    so this refuses one key under one property rather than banning a keyword.
    """
    import re

    asset = os.path.join("chart", "assets", "server.yaml")
    if not os.path.exists(asset):
        errors.append(f"{asset}: missing, so the boot_index screen checked nothing")
        return
    text = open(asset, encoding="utf-8", errors="replace").read()

    def judge(body):
        """(found_block, has_minimum, has_nullable) for a server.yaml text.

        The sweep AND the controls call this, so a passing control is evidence
        about the code that runs.
        """
        lines = body.splitlines()
        start = None
        for i, l in enumerate(lines):
            if re.match(r"^(\s+)boot_index:\s*$", l):
                start = i
                indent = len(l) - len(l.lstrip())
                break
        if start is None:
            return (False, False, False)
        has_min = has_null = False
        for l in lines[start + 1:]:
            if not l.strip():
                continue
            ind = len(l) - len(l.lstrip())
            if ind <= indent:
                break                      # left the boot_index block
            key = l.strip()
            if key.startswith("#"):
                continue                   # a comment NAMING minimum is not one
            if re.match(r"^(exclusiveMinimum|minimum):", key):
                has_min = True
            if re.match(r"^nullable:\s*true\s*$", key):
                has_null = True
        return (True, has_min, has_null)

    # CONTROLS: the reader must find the block, see a planted bound, and NOT be
    # fooled by a comment that merely names the keyword -- which is the exact
    # mistake made twice today in other screens.
    planted = "        boot_index:\n          type: integer\n          minimum: 0\n          nullable: true\n"
    commented = "        boot_index:\n          type: integer\n          # no minimum, deliberately\n          nullable: true\n"
    if judge(planted) != (True, True, True):
        errors.append("validate.py: the boot_index screen does not see a planted "
                      f"`minimum` (got {judge(planted)!r}); it cannot be trusted")
        return
    if judge(commented) != (True, False, True):
        errors.append("validate.py: the boot_index screen counts a COMMENT naming "
                      f"`minimum` as a bound (got {judge(commented)!r}); a mention "
                      f"is not a declaration")
        return

    found, has_min, has_null = judge(text)
    if not found:
        errors.append(f"{asset}: no `boot_index` property found, so this screen "
                      f"swept nothing. Either the field was renamed or the block "
                      f"shape changed -- an empty sweep is not a clean result.")
        return
    if has_min:
        errors.append(
            f"{asset}: `boot_index` carries a numeric lower bound. Nova's own "
            f"schema is `'type': ['integer','string','null'], 'pattern': "
            f"'^-?[0-9]+$'` -- the leading `-` is deliberate and `-1` means "
            f"\"never boot from this device\". A bound here rejects a documented "
            f"value at `kubectl apply`, before any request is made.")
    if not has_null:
        errors.append(
            f"{asset}: `boot_index` is no longer `nullable: true`. Nova accepts "
            f"null for legacy-API compatibility and says so in a NOTE in its own "
            f"schema; dropping it rejects a value the vendor takes.")


def verify_no_request_field_above_microversion_baseline(errors):
    """No REQUEST-direction property may be annotated above the 2.1 baseline.

    THIS SCREEN WAS CITED BEFORE IT EXISTED. chart/assets/keypair.yaml says, of
    the `type` field it had to remove, that
    `verify_no_request_field_above_microversion_baseline` in scripts/validate.py
    "is what keeps the next such field out". It did not exist: 27 `verify_*`
    screens were defined and none mentioned microversion. Found while preparing
    to add `block_device_mapping_v2`, whose `tag` (2.32) and `volume_type` (2.67)
    are exactly the next such fields -- i.e. the guard was missing at the moment
    it was needed, which is the second time this repo has named a screen that
    was never written (the README provider-floor declaration was the first,
    fixed in #190).

    WHAT IT ENFORCES. This chart sends NO microversion header on any path, and
    Nova resolves a header-less request to DEFAULT_API_VERSION, which is the API
    MINIMUM -- 2.1. So a request body carrying a field that only exists at a
    higher microversion is refused outright:

        HTTP/1.1 400
        X-Openstack-Nova-Api-Version: 2.1
        Additional properties are not allowed ('type' was unexpected)

    measured on a real cluster for keypair `type`. A RESPONSE may carry such a
    field annotated -- Nova simply omits it, and the annotation is the honest
    record of why `.status.keypair.type` is always empty. So the rule is
    directional: annotate freely on the way back, never on the way out.

    HOW DIRECTION IS DECIDED, AND THE LIMIT OF THAT. These files do not parse as
    YAML (OAS path templates like `/os-quota-sets/{tenant_id}:` are flow
    mappings used as keys; `yaml.safe_load` raises on all six, as
    verify_no_header_parameters_in_assets records), and `components.schemas`
    entries are shared by `$ref`, so true reachability needs resolution that is
    not available here. What IS available textually is the enclosing schema
    name, and this chart names request-direction schemas consistently: `*Spec`,
    `*CreateRequest`, `*UpdateRequest` -- 12 of 33 today. That convention is the
    screen's premise and is asserted below rather than assumed: if it ever stops
    holding, the controls fail loudly instead of the sweep going quiet.
    """
    import glob
    import re

    BASELINE = (2, 1)

    # A deliberate above-baseline request field goes here, keyed by
    # "<asset>:<schema>.<property>", with the reason it is safe -- which can
    # only be that this chart has started sending a microversion header, since
    # nothing else makes Nova accept it.
    exempt = {}

    ann = re.compile(r'^(\s*)x-nova-microversion:\s*"?([0-9]+)\.([0-9]+)"?\s*$')
    schema_key = re.compile(r'^    ([A-Za-z0-9_]+):\s*$')
    prop_key = re.compile(r'^(\s+)([A-Za-z0-9_]+):\s*$')
    REQUEST_SUFFIXES = ("Spec", "CreateRequest", "UpdateRequest")

    def judge(text):
        """(line, schema, version, direction) for every annotation found.

        The sweep AND the controls call THIS, so a passing control is evidence
        about the code that runs rather than about a retyped copy of it.
        """
        out = []
        cur_schema = None
        prop_stack = []
        for i, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            m = schema_key.match(line)
            if m:
                cur_schema = m.group(1)
                prop_stack = []
                continue
            pm = prop_key.match(line)
            if pm:
                ind = len(pm.group(1))
                prop_stack = [(d, n) for d, n in prop_stack if d < ind]
                prop_stack.append((ind, pm.group(2)))
            a = ann.match(line)
            if not a:
                continue
            ver = (int(a.group(2)), int(a.group(3)))
            direction = ("request"
                         if cur_schema and cur_schema.endswith(REQUEST_SUFFIXES)
                         else "response")
            prop = prop_stack[-1][1] if prop_stack else "<unknown>"
            out.append((i, cur_schema, prop, ver, direction))
        return out

    # CONTROL 1: the pattern fires on an above-baseline annotation inside a
    # request schema, and does NOT fire on the same annotation in a response
    # schema. A screen that cannot tell those apart would refuse the keypair
    # annotation this repo deliberately keeps.
    planted_req = ('components:\n  schemas:\n    FooSpec:\n      properties:\n'
                   '        tag:\n          type: string\n'
                   '          x-nova-microversion: "2.32"\n')
    planted_resp = planted_req.replace("FooSpec", "FooSummary")
    got_req = [r for r in judge(planted_req) if r[4] == "request"]
    got_resp = [r for r in judge(planted_resp) if r[4] == "request"]
    if len(got_req) != 1 or got_resp:
        errors.append(
            "validate.py: the microversion screen cannot tell a request schema "
            "from a response one (planted request=%d, planted response=%d; "
            "expected 1 and 0). Every result below would be unreadable."
            % (len(got_req), len(got_resp)))
        return

    rows = []
    schema_names = set()
    for f in sorted(glob.glob(os.path.join("chart", "assets", "*.yaml"))):
        text = open(f, encoding="utf-8", errors="replace").read()
        for line in text.splitlines():
            m = schema_key.match(line)
            if m:
                schema_names.add(m.group(1))
        for (i, sch, prop, ver, direction) in judge(text):
            rows.append((os.path.basename(f), i, sch, prop, ver, direction))

    # CONTROL 2: the corpus must contain the naming convention this screen
    # rests on. Zero request-direction schemas would make every annotation
    # read as "response" and the sweep would pass on anything.
    req_named = sorted(n for n in schema_names if n.endswith(REQUEST_SUFFIXES))
    if not req_named:
        errors.append(
            "validate.py: no schema in chart/assets/ is named *Spec, "
            "*CreateRequest or *UpdateRequest, so this screen classified every "
            "annotation as response-direction and could not fail. The naming "
            "convention it rests on has changed.")
        return

    # CONTROL 3: the sweep must SEE the annotation this repo deliberately
    # keeps. Zero annotations found means the pattern or the glob is wrong, not
    # that the chart is clean.
    if not rows:
        errors.append(
            "validate.py: the microversion sweep found NO x-nova-microversion "
            "annotation in chart/assets/, and at least one is kept on purpose "
            "(keypair `type`, 2.2, on the response). An empty sweep is a broken "
            "pattern, not a clean corpus.")
        return

    for (asset, line, sch, prop, ver, direction) in rows:
        if direction != "request" or ver <= BASELINE:
            continue
        if "%s:%s.%s" % (asset, sch, prop) in exempt:
            continue
        errors.append(
            "chart/assets/%s:%d: `%s.%s` is annotated microversion %d.%d and "
            "sits in a REQUEST-direction schema. This chart sends no "
            "microversion header, so Nova serves %d.%d and refuses the body "
            "with `Additional properties are not allowed`, leaving the CR at "
            "Ready=False/Creating indefinitely. Keep the field on the response "
            "side annotated, or raise the baseline deliberately."
            % (asset, line, sch, prop, ver[0], ver[1], BASELINE[0], BASELINE[1]))


def verify_no_schema_defaults_in_assets(errors):
    """No OAS asset may declare a `default:` SCHEMA KEYWORD.

    WHY, and it is a latent switch rather than a current defect. A `default` on
    a property reaches the generated CRD verbatim -- read in plumbing's crdgen
    on main, not inferred:

        transpile.go:359   t.copyDefault(out, node, path)        the GENERAL path
        transpile.go:582   copyDefault: out.Default = raw        copied verbatim;
                           warns only KindDefaultDropped, for an UNMARSHALABLE one
        transpile.go:70    materializeDefaultedParents(specProps, "spec")  SPEC ONLY

    The two warning kinds that exist are `KindParentDefaulted` (spec-side
    materialization) and `KindDefaultDropped`. There is NO warning kind for a
    default that lands under status -- the silence is a missing case, not a
    suppressed one. github-provider-kog measured the result on oasgen 0.63.1:
    a default on a response-schema property renders into the CRD's status with
    no warning at all.

    WHAT THE SWITCH DOES. The apiserver prunes a null for a non-nullable field
    (apiextensions .../schema/defaulting/prunenulls.go), and the guard is
    `x == nil && !s.Generic.Nullable && s.Default.Object == nil`. So:

        no default  ->  a vendor's explicit null is DELETED, key absent
        a default   ->  the null is REPLACED by that default and STORED

    The second is worse, because an absent key reads as absence while a stored
    default reads as data. crdgen says so itself for the spec side --
    *"once a defaulted value is persisted it cannot be told apart from an
    authored one"* (plumbing#84) -- and the status side has the same property
    with nothing watching it.

    WHY THIS REFUSES A DEFAULT ANYWHERE RATHER THAN ONLY UNDER A RESPONSE.
    The hazard is specifically a default that reaches STATUS, which means a
    default on a schema reachable from a 2xx response. Deciding that textually
    is not possible here: `components.schemas` entries are shared by reference,
    so `ServerGroupSummary` is response-only and `ServerGroupSpec` is
    request-only with nothing in the node itself to say which -- it takes $ref
    resolution, and THESE FILES DO NOT PARSE AS YAML (OAS path templates like
    `/os-quota-sets/{tenant_id}:` are flow mappings used as keys;
    `yaml.safe_load` raises on all six, as verify_no_header_parameters_in_assets
    records). A textual guess at which schemas are response-reachable would be
    a screen whose correctness nobody could check. Refusing the keyword outright
    is strictly safer, needs no resolution, and leaves the legitimate case --
    a deliberate default on a REQUEST property -- available through the
    exemption slot below, where it has to carry its reason.

    Zero today: 0 anchored `default:` keywords against 123 anchored `type:`.
    The first unanchored grep found FOUR and all four were PROSE -- "policies
    default to", "reset the quota set to defaults" -- which is why `judge`
    tracks block scalars rather than matching a bare word.
    """
    import glob
    import re

    # A deliberate default on a REQUEST property goes here, keyed by
    # "<asset basename>:<property path>", with the reason it is safe -- i.e.
    # why the schema it sits on can never be reached from a 2xx response.
    exempt = {}

    def judge(text):
        """(line_no, why) for every `default:` SCHEMA KEYWORD.

        The real sweep and the controls below both call THIS, so a passing
        control is evidence about the code that runs.
        """
        out = []
        block_indent = None     # inside a block scalar; deeper lines are literal prose
        block_scalar = re.compile(r"^(\s*)(?:-\s+)?[\w$./{}'\"-]+:\s*[|>][-+0-9]*\s*$")
        for i, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            indent = len(line) - len(line.lstrip())
            if block_indent is not None:
                if indent > block_indent:
                    continue            # description text, not a node
                block_indent = None
            m = block_scalar.match(line)
            if m:
                block_indent = len(m.group(1))
                continue
            key = line.strip()
            if key.startswith("- "):
                key = key[2:].strip()
            if not key.startswith("default:"):
                continue
            if indent == 0:
                continue                # a top-level `default:` is not a schema node here
            out.append((i, "a `default:` schema keyword, which crdgen copies "
                           "verbatim into the generated CRD"))
        return out

    # CONTROLS FIRST, each failing differently, because this screen's normal
    # result is SILENCE and a broken judge is indistinguishable from a clean
    # repo. A screen whose only output is "nothing found" has to prove it can
    # find something and prove it does not find prose.
    real = ("components:\n"
            "  schemas:\n"
            "    Thing:\n"
            "      properties:\n"
            "        state:\n"
            "          type: string\n"
            "          default: active\n")
    prose = ("info:\n"
             "  description: |\n"
             "    Quota writes require admin.\n"
             "    default: this line is PROSE inside a block scalar\n"
             "    Nova policies default to rule:context_is_admin.\n"
             "    DELETE resets the quota set to defaults.\n")
    if not judge(real):
        errors.append("validate.py: the schema-default screen ACCEPTS a real "
                      "`default:` on a property, so its silence means nothing")
        return
    if judge(prose):
        errors.append("validate.py: the schema-default screen flags PROSE "
                      "inside a block scalar, so it would fail on assets whose "
                      "descriptions merely discuss defaults -- which all six do")
        return
    if len(judge(prose + real)) != 1:
        errors.append("validate.py: the schema-default screen cannot find a "
                      "real default once a prose block is also present, which "
                      "is the only shape these assets actually take")
        return

    assets = sorted(glob.glob(os.path.join("chart", "assets", "*.yaml")))
    if not assets:
        errors.append("the schema-default screen found NO files under "
                      "chart/assets/, so it swept nothing. An empty sweep "
                      "passes vacuously; refusing.")
        return
    hits, nodes = [], 0
    for path in assets:
        try:
            text = open(path, encoding="utf-8").read()
        except OSError as e:
            errors.append(f"{path}: unreadable, so it is NOT screened for "
                          f"schema defaults ({e})")
            continue
        # The REACH CONTROL. This screen expects to find nothing, so "no hits"
        # has to be distinguishable from "never read a schema". Anchored
        # `type:` keywords are the cheapest proxy for schema nodes present.
        nodes += len([ln for ln in text.splitlines()
                      if ln.strip().startswith("type:") and ln != ln.lstrip()])
        for line_no, why in judge(text):
            key = f"{os.path.basename(path)}:{line_no}"
            if key in exempt:
                continue
            hits.append(f"{path}:{line_no} {why}")
    if nodes == 0:
        errors.append("the schema-default screen walked chart/assets/ and found "
                      "NO indented `type:` keyword at all, so it never reached a "
                      "schema node. Its silence about defaults means nothing; "
                      "refusing rather than reporting clean.")
        return
    if hits:
        errors.append(
            f"these assets declare a `default:` schema keyword: {hits}. crdgen "
            f"copies a default verbatim into the generated CRD, and there is NO "
            f"warning kind for one that lands under status -- so a default on "
            f"any schema reachable from a 2xx response silently changes what "
            f"happens to a vendor's explicit null: without a default the "
            f"apiserver DELETES the key, with one it REPLACES the null with the "
            f"default and stores it, where it cannot be told apart from a value "
            f"the vendor really sent. Measured on oasgen 0.63.1 by "
            f"github-provider-kog. If this default is on a REQUEST property and "
            f"its schema can never be reached from a response, add it to "
            f"`exempt` in this screen WITH THAT REASON.")


BARE_RC = re.compile(r"^\s*rc=\$\?\s*$")



_KEY_ANCHORED_STAMP = re.compile(
    r"""(?:\.replace\(\s*["']|s/|s\|)\s*[A-Za-z_]*[Vv]ersion:\s+CHART_VERSION""")



_STAMPS_PLACEHOLDER = re.compile(
    r"""(?:s[/|]CHART_VERSION"""
    r"""|\.replace\(\s*["'](?:[A-Za-z_]*[Vv]ersion:\s+)?CHART_VERSION"""
    r"""|re\.sub\([^)]*CHART_VERSION|s[/|][A-Za-z_]*[Vv]ersion:\s+CHART_VERSION)""")
_ASSERTS_NO_SURVIVOR = re.compile(
    r"""grep -rq CHART_VERSION|grep -q CHART_VERSION|["']CHART_VERSION["']\s+in\b""")


def verify_chart_version_stamp_is_verified(errors):
    """Every file that stamps CHART_VERSION must also assert that none survives.

    THIS IS THE SCREEN THAT CANNOT BE EVADED, and it exists because the one
    beside it can. verify_chart_version_stamp_is_not_key_anchored matches a
    FORM -- `.replace("version: CHART_VERSION", ...)` and its sed spellings.
    oasgen-provider reviewed that screen against eleven ways to write the same
    defect and it caught four. The misses were `re.sub(`, a `^`-anchored
    MULTILINE sub, a `startswith("version:")` line filter, variable
    indirection, `sed -i '1s/.../'`, `yq -i '.version = strenv(V)'`, and worst:

        s.replace("CHART_VERSION", "0.0.0", 1)

    which names no key at all -- so there is nothing for a key-anchored pattern
    to find -- and stamps only the first occurrence, which in Chart.yaml is
    `version`. It is the defect with the evidence removed. A form-based screen
    gains a new blind spot every time the language gains a way to edit one line,
    so the list grows with the language and not with anyone's care.

    The defect is SEMANTIC: fewer than all occurrences stamped. The one check
    that sees it regardless of spelling is asking the staged file afterwards,
    which is why this screen requires that question to be asked at every site
    rather than requiring any particular way of stamping.

    SIXTEEN files here stamp the placeholder; NINE asserted and SEVEN did not.
    That ratio is the finding: the assertion was not a convention, it was a
    habit that held wherever someone had been bitten. The seven included
    release-tag.yaml -- BOTH of its stamping loops, the CompositionDefinition
    one and the per-chart one -- which was then the only site whose output is
    published. (It stamped globally, so nothing was ever published wrong. The
    exposure was that nothing would have said so.) That workflow is now retired
    in favour of the shared release-oci; the published chart is stamped in ITS
    runner, and the only stamp left in this repository is the release asset's,
    in release.yaml.

    WHY ORDER IS NOT CHECKED, unlike the self-test guard: an assertion placed
    BEFORE its stamp fails loudly and immediately, because the placeholder is
    still there. The inert direction does not exist, so there is nothing to
    screen. That is the opposite of assert_arms_callable, where a guard sourced
    after the dispatch was silently inert in four of five probes.

    AND THIS SCREEN'S OWN ARM CAUGHT THIS SCREEN, which is the second time in
    one change. `_STAMPS_PLACEHOLDER` matched a bare `.replace("CHART_VERSION"`
    but not `.replace("version: CHART_VERSION"` -- so a file that stamped ONLY
    in the key-anchored form counted as not stamping at all, and escaped the
    requirement to assert. The screen written to cover the other screen's blind
    spot had the other screen's blind spot. Both patterns now accept an optional
    `<key>:` before the placeholder.

    LIMIT, stated because it is the kind of gap this file keeps finding: this
    screen is file-scoped. A file that stamps twice and asserts once satisfies
    it. NO FILE HERE STAMPS TWICE any more -- release-tag.yaml was the only one
    and it is retired; its successor release.yaml stamps once, in the release
    asset step, and asserts there. Re-measured at the migration with this
    screen's own regexes rather than carried forward: 16 stamping files, none
    with more than one substitution, none without an assertion. A per-stamp
    screen would need to pair each substitution with a following assertion in
    the same block, which the YAML nesting here does not make reliable to
    parse.
    """
    # Arms for both patterns, in both directions.
    assert _STAMPS_PLACEHOLDER.search('sed -i "s/CHART_VERSION/${V}/g" "$cf"')
    assert _STAMPS_PLACEHOLDER.search('body.replace("CHART_VERSION", "0.0.0")')
    assert _STAMPS_PLACEHOLDER.search('re.sub(r"CHART_VERSION", v, body)')
    assert _STAMPS_PLACEHOLDER.search('.replace("version: CHART_VERSION", "x")')
    assert not _STAMPS_PLACEHOLDER.search('grep -q CHART_VERSION "$f"')
    assert not _STAMPS_PLACEHOLDER.search('# CHART_VERSION is a placeholder')
    assert _ASSERTS_NO_SURVIVOR.search('grep -q CHART_VERSION "$STAGE/Chart.yaml"')
    assert _ASSERTS_NO_SURVIVOR.search('grep -rq CHART_VERSION "$STAGE/chart"')
    assert _ASSERTS_NO_SURVIVOR.search('if "CHART_VERSION" in fh.read():')
    assert not _ASSERTS_NO_SURVIVOR.search('sed -i "s/CHART_VERSION/1.2.3/g" x')
    targets = (sorted(glob.glob("hack/*.sh")) + sorted(glob.glob("hack/*.py"))
               + sorted(glob.glob("scripts/*.py"))
               + sorted(glob.glob(".github/workflows/*.yaml")))
    stampers = 0
    for p in targets:
        if p == "scripts/validate.py":
            continue
        body = open(p, encoding="utf-8").read()
        lines = body.split("\n")
        subs = [n for n, l in enumerate(lines, 1) if _STAMPS_PLACEHOLDER.search(l)]
        if not subs:
            continue
        stampers += 1
        if not _ASSERTS_NO_SURVIVOR.search(body):
            errors.append(
                f"{p}:{subs[0]}: this file stamps the CHART_VERSION placeholder "
                f"(line(s) {', '.join(str(n) for n in subs)}) but never asserts "
                f"that none survived. helm refuses an invalid `version` but "
                f"accepts ANY `appVersion`, so a stamp that misses the second "
                f"key produces no error, no warning and no event -- the staged "
                f"chart is simply not the one that was asked for. Add "
                f"`grep -q CHART_VERSION <staged Chart.yaml>` (or `grep -rq` "
                f"over the staged tree, if the stamper is a `find`) and fail as "
                f"SETUP, not as a finding.")
    # A screen that scanned nothing passes. Something stamps this chart.
    if stampers == 0:
        errors.append("validate.py: the stamp-verification screen found no file "
                      "that stamps CHART_VERSION, which cannot be true of this "
                      "repository -- its globs are wrong and it screened nothing")


def verify_chart_version_stamp_is_not_key_anchored(errors):
    """A CHART_VERSION substitution must not be anchored to a `version:` key.

    Chart.yaml carries the placeholder TWICE -- `version` and `appVersion`
    (REPOSITORY.md R4: a provider ships no image, so the chart version IS the
    application version, and a hand-maintained one drifts). A substitution
    written as

        .replace("version: CHART_VERSION", "version: 0.0.0-probe")

    stamps the FIRST and leaves the second as the literal string
    "CHART_VERSION".

    AND NOTHING FAILS. helm semver-validates `version` and refuses a bad one
    outright -- that is why every staging site stamps at all -- but `appVersion`
    is free-form, so helm accepts the placeholder without an error, a warning or
    an event. The probe then installs a chart it did not stage and reports on it
    confidently.

    Nine sites in this repo stamp the placeholder. Seven substitute it globally
    (`s/CHART_VERSION/.../g`) and pick up a new key for free; TWO were anchored
    this way -- probe-lease-readiness.sh and probe-volume-attachment-mapping.sh
    -- and neither asserted that no placeholder survived, so both would have
    staged a literal appVersion silently the moment the field was added. The
    shared release-oci.yaml stamps globally too (line 336), so the published
    chart was never at risk; only the probes were, which is worse in one
    respect: a probe is the instrument the other findings are measured with.

    The screen is on the FORM, not on the outcome, because the outcome is
    invisible: there is no error to detect. An anchored search term is the
    defect, and it is syntactically identifiable.

    THE KEY IS MATCHED CASE-INSENSITIVELY AT THE `V` (`[Vv]ersion:`), and this
    screen's own arm is why. Written as a lowercase `version:`, the pattern could
    not see `.replace("appVersion: CHART_VERSION", ...)` -- the mirror-image
    defect, which stamps appVersion and leaves `version` as the placeholder. That
    one does fail loudly (helm refuses an invalid semver `version`), so it is the
    less dangerous half; a screen that catches only the silent half of a
    two-sided defect is still a screen that reads as covering the defect.

    Prose is not matched -- the pattern requires substitution syntax
    (`.replace("`, `s/`, `s|`) immediately before the key -- so the many
    documents that quote `version: CHART_VERSION` as a fact about the file are
    untouched. This file is skipped because this docstring quotes the bad form.
    """
    # Arms. The known-bad form must match, and every good form must not --
    # a screen that cannot say "no" cannot say "yes" either.
    bad_py = ('s = io.open(p).read().replace("version: CHART_VERSION", '
              '"version: 0.0.0-probe")')
    bad_sed = 'sed -i "s/version: CHART_VERSION/version: 1.2.3/" Chart.yaml'
    bad_app = '.replace("appVersion: CHART_VERSION", "appVersion: 1.2.3")'
    good_global_sed = 'sed -i "s/CHART_VERSION/${VERSION}/g" "$cf"'
    good_global_py = 'body.replace("CHART_VERSION", "0.0.0-screen")'
    good_assert = 'grep -q CHART_VERSION "$STAGE/chart/Chart.yaml"'
    good_prose = ("`chart/Chart.yaml` keeps `version: CHART_VERSION`, which the "
                  "release tag stamps")
    if not _KEY_ANCHORED_STAMP.search(bad_py):
        errors.append("validate.py: the key-anchored-stamp screen fails its "
                      "known-BAD python assertion; the two probes it was "
                      "written for would escape again")
    if not _KEY_ANCHORED_STAMP.search(bad_sed):
        errors.append("validate.py: the key-anchored-stamp screen does not "
                      "match the sed form of the same defect")
    if not _KEY_ANCHORED_STAMP.search(bad_app):
        errors.append("validate.py: the key-anchored-stamp screen does not "
                      "match an appVersion-anchored stamp, which misses "
                      "`version` for the mirror-image reason")
    for good in (good_global_sed, good_global_py, good_assert, good_prose):
        if _KEY_ANCHORED_STAMP.search(good):
            errors.append("validate.py: the key-anchored-stamp screen flags a "
                          f"correct form, so it cannot be trusted: {good!r}")
    targets = (sorted(glob.glob("hack/*.sh")) + sorted(glob.glob("hack/*.py"))
               + sorted(glob.glob("scripts/*.py"))
               + sorted(glob.glob(".github/workflows/*.yaml")))
    # A screen that scans nothing passes. Chart.yaml is stamped by something, so
    # if no file in this repo mentions the placeholder at all, the glob is wrong.
    mentions = 0
    for path in targets:
        if path == "scripts/validate.py":
            continue
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().split("\n")
        if any("CHART_VERSION" in line for line in lines):
            mentions += 1
        for n, line in enumerate(lines, 1):
            if _KEY_ANCHORED_STAMP.search(line):
                errors.append(
                    f"{path}:{n}: this CHART_VERSION substitution is anchored to "
                    f"a `version:` key, so it stamps `version` and leaves "
                    f"`appVersion` as the literal placeholder. helm accepts any "
                    f"string in appVersion, so nothing fails -- the staged chart "
                    f"is simply not the chart that was asked for. Substitute "
                    f"CHART_VERSION globally instead, and assert that none "
                    f"survives.")
    if mentions == 0:
        errors.append("validate.py: the key-anchored-stamp screen found no file "
                      "mentioning CHART_VERSION, which cannot be true of this "
                      "repository -- its globs are wrong and it screened "
                      "nothing")


def verify_rc_capture_is_reachable(errors):
    """A bare `rc=$?` line in a workflow step is UNREACHABLE, so the branch that
    reads it is dead code.

    MEASURED, not reasoned. GitHub runs a `run:` block as `bash -e {0}`, and a
    `set -uo pipefail` written INSIDE the block does not remove that -e. So:

        bash hack/probe-lease-readiness.sh ...   <- exits 4
        rc=$?                                    <- never runs; -e killed the step
        case "$rc" in ...                        <- dead

    Run 44928163907 measured both channels successfully, exited 4, and emitted NO
    annotation at all -- the ::warning:: that was supposed to say "the teardown
    failed and that is this probe's fault" never fired. Worse, the run before it
    exited 1 and I reported to another team that the workflow had rendered
    "A REAL FINDING" about THEIR code. It had not. I had read my own case block
    instead of the log, and asserted an output I never observed.

    SIX of this repo's workflows carried the pattern, including the canary's VOID
    branch -- the one built specifically so a failed resolve would abstain rather
    than read as "nothing found". Every one of those non-zero branches was dead.

    The fix is `rc=0` then `cmd || rc=$?`, which this screen requires. Verified
    both directions before writing it:

        bash -e -c 'false; echo REACHED'              prints nothing
        bash -e -c 'rc=0; false || rc=$?; echo $rc'   prints 1

    LIMIT: this matches a bare capture on its own line, which is the form that
    appears here. `rc=$?` on the same line as a command, or after a command that
    cannot fail, is not flagged -- the first is unusual enough to review by hand
    and the second is harmless.
    """
    # Arms. A regex meant to find a dead capture must not pass the live form.
    assert BARE_RC.match("          rc=$?")
    assert BARE_RC.match("rc=$?")
    assert not BARE_RC.match("          rc=0")
    assert not BARE_RC.match("          cmd || rc=$?")
    assert not BARE_RC.match("          v=$(cmd) || rc=$?")
    assert not BARE_RC.match("          echo rc=$?")
    for path in sorted(glob.glob(".github/workflows/*.yaml")):
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().split("\n")
        for n, line in enumerate(lines, 1):
            if BARE_RC.match(line):
                errors.append(
                    f"{path}:{n}: a bare `rc=$?` is unreachable -- GitHub runs the "
                    f"step as `bash -e {{0}}`, so a non-zero exit kills it before "
                    f"this line and the branch reading $rc is dead code. Write "
                    f"`rc=0` on the line before and `<command> || rc=$?` instead.")


_SELF_TEST_CALL = re.compile(r"\b[a-z_]*self_test[a-z_]*\b")
_SELF_TEST_DEF = re.compile(r"^(function\s+)?[a-z_]*self_test[a-z_]*\s*\(\)")
_GUARD_CALL = re.compile(r"^assert_arms_callable\b")


def verify_self_test_guard_precedes_dispatch(errors):
    """Every self-test under hack/ must be guarded, and the guard must be sourced
    BEFORE the line that runs it.

    THE CLAIM MATTERS. Keyed only on scripts that already source the guard, this
    screen asserted "every GUARDED self-test is ordered correctly" -- which left
    coverage at "every probe someone remembered to wire". It now asserts "every
    self-test in hack/ IS guarded", which is the property worth having.

    MEASURED, in the same change that added the guard. Four of the five probes
    source `lib/wait.sh` well down the file, and the guard was appended next to
    it -- AFTER the `--self-test` dispatch:

        probe-401                        source@247  dispatch@190   INERT
        probe-extension-passthrough      source@207  dispatch@197   INERT
        probe-lease-readiness            source@359  dispatch@349   INERT
        probe-volume-attachment-mapping  source@125  dispatch@115   INERT
        probe-observe-rejection          source@64   dispatch@765   live

    Only the last one worked, which is why it was the only probe to report
    anything. A guard against ordering mistakes, installed in the wrong order --
    and it failed exactly the way the bug it guards against fails: by being an
    undefined command whose absence bash renders as silence.

    The failure is INVISIBLE at runtime: calling an undefined
    `assert_arms_callable` writes "command not found" to stderr and carries on,
    so the self-test still prints its arms and still exits 0. Nothing surfaces.
    Hence a static screen rather than a runtime one.
    """
    # Arms. A screen that cannot see a call site reports nothing, forever.
    assert _SELF_TEST_CALL.search("delete_self_test || { exit 1; }")
    assert _SELF_TEST_CALL.search('[ "$1" = "--self-test" ] && { self_test; exit 0; }')
    assert _SELF_TEST_CALL.search("  portonly_self_test || fail")
    assert not _SELF_TEST_DEF.match("delete_self_test || { exit 1; }")
    assert _SELF_TEST_DEF.match("delete_self_test() {")
    assert _SELF_TEST_DEF.match("portonly_self_test () ")
    assert _SELF_TEST_DEF.match("function helper_self_test () ")
    assert not _SELF_TEST_CALL.search("  assert_arms_callable")
    # the guard's own call carries a *self_test* name and must NOT be read as the
    # dispatch: it sits inside the definition, always after the source line.
    assert _GUARD_CALL.match("assert_arms_callable delete_self_test")
    assert _GUARD_CALL.match("assert_arms_callable")
    assert not _GUARD_CALL.match("delete_self_test || { exit 1; }")
    assert not _GUARD_CALL.match("# assert_arms_callable delete_self_test")

    guard = "lib/self-test-guard.sh"
    for path in sorted(glob.glob("hack/*.sh")) + sorted(glob.glob("hack/lib/*.sh")):
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().split("\n")
        src_at = dispatch_at = None
        for n, line in enumerate(lines, 1):
            st = line.strip()
            if src_at is None and guard in st and st.startswith(("source ", ". ")):
                src_at = n
            if dispatch_at is not None or st.startswith("#"):
                continue
            # THE CALL SITE, by NAME rather than by the `--self-test` flag. Two
            # probes invoke theirs as `delete_self_test || {...}` and
            # `portonly_self_test || {...}` with no flag on the line, so a screen
            # keyed on "--self-test" skipped both of them silently -- the same
            # shape as the guard it screens for. Anything matching *self_test*
            # that is not a DEFINITION counts.
            # AND NOT THE GUARD'S OWN CALL. `assert_arms_callable delete_self_test`
            # contains a *self_test* name, so it registered as the dispatch -- a
            # line INSIDE the function definition, always after the source, which
            # made the ordering comparison measure the wrong thing entirely. Found
            # by a mutant that renamed a function and still passed.
            if _GUARD_CALL.match(st):
                continue
            m = _SELF_TEST_CALL.search(st)
            if m and not _SELF_TEST_DEF.match(st):
                dispatch_at = n
        # THE MECHANISM CANNOT BE ITS OWN SUBJECT. lib/self-test-guard.sh defines
        # assert_arms_callable and cannot source itself; its own self-test sources
        # it through a variable inside a `bash -c` string. Both mention self_test
        # constantly. Skipped by path rather than by an exemption list, so a new
        # script anywhere under hack/ is still covered.
        if "self-test-guard" in path:
            continue
        if src_at is None and dispatch_at is None:
            continue
        # A DISPATCH WITH NO SOURCE is how probe-548-delete.sh and
        # probe-portonly.sh were unguarded before this check existed: they simply
        # did not source it. Returning early here made the screen's claim "every
        # GUARDED self-test is ordered correctly" when the claim worth making is
        # "every self-test in hack/ IS guarded" -- otherwise coverage is again
        # "every probe someone remembered".
        if src_at is None:
            errors.append(
                f"{path}: dispatches a self-test at line {dispatch_at} but never "
                f"sources lib/self-test-guard.sh, so its arms are unguarded -- a "
                f"function defined below the call site would make every arm using "
                f"it compare the empty string, silently.")
            continue
        if dispatch_at is None:
            errors.append(
                f"{path}: sources lib/self-test-guard.sh at line {src_at} but never "
                f"dispatches a self-test, so the guard is wired to nothing. Either "
                f"call the self-test or drop the source line.")
            continue
        if src_at > dispatch_at:
            errors.append(
                f"{path}: lib/self-test-guard.sh is sourced at line {src_at} but the "
                f"self-test dispatch is at line {dispatch_at}, so assert_arms_callable "
                f"does not exist when it is called. Bash renders that as silence and "
                f"the guard is inert. Source it near the top, before any dispatch.")


def verify_schema_covers_every_values_key(errors):
    """Every chart/values.yaml key must exist in chart/values.schema.json, with
    the same default -- because the schema, not values.yaml, is what a
    Composition can express.

    MEASURED LIVE, NOT HYPOTHETICAL. On 0.1.10 the schema's
    `restdefinitions.properties` listed six kinds and values.yaml defined seven:
    `volume_attachment` was absent. core-provider generates the Composition CRD
    from this schema and the API server decodes strictly, so a blueprint install
    that tried to switch the kind on was rejected --

        OpenstackNovaOperatorKog in version "v0-1-10" cannot be handled as a
        OpenstackNovaOperatorKog: strict decoding error: unknown field
        "spec.restdefinitions.volume_attachment"

    -- while `helm install --set restdefinitions.volume_attachment.enabled=true`
    worked perfectly. Two install paths, one of which could not reach a shipped
    feature at all, and nothing in CI compared the two files. Found by the
    openstack-as-a-service session's live #308 run on GKE, which had to tear a
    cluster down over it.

    THE KINDS THE SCHEMA OMITTED WERE EXACTLY THE ONES SHIPPED OFF BY DEFAULT,
    and that is the mechanism rather than a coincidence: a kind nobody enables is
    a kind nobody writes into a Composition, so the omission is invisible until
    the first consumer wants it. The exposure grows with every deliberately
    disabled kind -- the opposite of the intuition that an off-by-default feature
    is the low-risk one.

    WHY verify_composition_spec_against_schema DID NOT CATCH IT, stated because
    the names are nearly the same and a reader will reasonably ask. That screen
    validates the Composition EXAMPLE's spec against the schema: it catches an
    artifact naming a field the schema lacks. This one runs the other direction,
    chart against schema. The example never set `volume_attachment`, so there was
    nothing for the first screen to find. One direction was covered and read as
    both.

    DEFAULTS ARE COMPARED TOO, which is the second drift the same sweep found:
    `quota.enabled` was `true` in values.yaml and `false` in the schema, stale
    since the kind was unparked at the oasgen 0.52.0 pin. A helm install got the
    kind and a Composition install read a schema that said it was off.

    AND THAT SECOND DRIFT IS NOT COSMETIC: THE SCHEMA IS WHERE A COMPOSITION
    INSTALL'S DEFAULTS COME FROM. When this screen was written it said the
    question was unmeasured. It has since been measured, on a live GKE cluster
    (reported by the openstack-as-a-service session, 2026-11-26T00:07Z, nova-kog
    0.1.11, core-provider 2.13.130 -- second-hand, not run here): a Composition
    that set ONLY `restdefinitions.volume_attachment` had ALL SEVEN keys stored
    by the API server, each carrying `enabled` plus the resourceGroup and
    resourceKind FROM THE SCHEMA. So the CRD's defaults materialise absent
    objects whole, not merely absent fields inside objects the caller supplied.

    The consequence for the release before the fix is an INFERENCE from that
    reading rather than a measurement -- nobody re-installed 0.1.10 -- but it
    follows directly: 0.1.10's schema default `quota.enabled: false` would have
    been stored the same way, so a Composition install got ComputeQuota OFF no
    matter what values.yaml said, while every helm install got it ON. Making the
    two files agree is therefore not tidiness; a default in this file IS
    behaviour for one of the two install paths, and drift between them is a
    silent behavioural fork.

    THE SCHEMA-ONLY DIRECTION IS ALSO AN ERROR: a property in the schema with no
    values.yaml counterpart is a field a Composition may set and the chart will
    silently ignore, which is the same class of defect pointing the other way.
    """
    vpath = os.path.join("chart", "values.yaml")
    spath = os.path.join("chart", "values.schema.json")
    for path in (vpath, spath):
        if not os.path.exists(path):
            errors.append(f"{path}: missing, so the schema/values agreement is "
                          f"NOT checked. A file this screen cannot read is a "
                          f"failure, not a skip.")
            return
    try:
        values = yaml.safe_load(open(vpath, encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001 - any parse failure is a failure here
        errors.append(f"{vpath}: does not parse as YAML ({e}). VOID, not a pass.")
        return
    try:
        schema = json.load(open(spath, encoding="utf-8"))
    except ValueError as e:
        errors.append(f"{spath}: not valid JSON ({e}). VOID, not a pass.")
        return

    def compare(vals, sch, path=""):
        """(missing, drifted, schema_only, visited) for one subtree."""
        missing, drifted, schema_only, visited = [], [], [], 0
        allowed = sch.get("properties") or {}
        for key, val in (vals or {}).items():
            here = f"{path}.{key}" if path else key
            visited += 1
            if key not in allowed:
                missing.append(here)
                continue
            sub = allowed[key]
            if isinstance(val, dict):
                m, d, o, n = compare(val, sub, here)
                missing += m
                drifted += d
                schema_only += o
                visited += n
            elif "default" in sub and sub["default"] != val:
                drifted.append((here, val, sub["default"]))
        for key, sub in allowed.items():
            here = f"{path}.{key}" if path else key
            if not isinstance(vals, dict) or key not in (vals or {}):
                schema_only.append(here)
            elif isinstance(sub, dict) and not isinstance((vals or {}).get(key), dict):
                pass
        return missing, drifted, schema_only, visited

    # ARMS FIRST, on synthetic pairs, so a pass means the walk can SEE each
    # defect rather than that it found none. The clean arm is the control: if a
    # correct pair also reported something, every finding below is unreadable.
    clean_v = {"a": {"enabled": True}}
    clean_s = {"properties": {"a": {"properties": {"enabled": {"default": True}}}}}
    if any(compare(clean_v, clean_s)[:3]):
        errors.append("validate.py: the schema/values screen reports findings "
                      "against a CORRECT pair, so its findings mean nothing")
        return
    gone_s = {"properties": {}}
    if compare(clean_v, gone_s)[0] != ["a"]:
        errors.append("validate.py: the schema/values screen cannot see a "
                      "values key missing from the schema -- the exact defect "
                      "it was written for")
        return
    nested_s = {"properties": {"a": {"properties": {}}}}
    if compare(clean_v, nested_s)[0] != ["a.enabled"]:
        errors.append("validate.py: the schema/values screen does not descend, "
                      "so a NESTED missing key (which is what 0.1.10 shipped) "
                      "would pass")
        return
    drift_s = {"properties": {"a": {"properties": {"enabled": {"default": False}}}}}
    if not compare(clean_v, drift_s)[1]:
        errors.append("validate.py: the schema/values screen cannot see a "
                      "default that disagrees")
        return

    missing, drifted, schema_only, visited = compare(values, schema)
    if visited == 0:
        errors.append(f"{vpath}: the schema/values screen visited NO keys, so "
                      f"it screened nothing and passed. Its walk is wrong.")
        return
    if missing:
        errors.append(
            f"these chart/values.yaml keys are absent from chart/values.schema.json: "
            f"{missing}. core-provider generates the Composition CRD from the "
            f"schema and the API server decodes strictly, so `helm install --set` "
            f"reaches them and a Composition CANNOT -- the install path a "
            f"blueprint consumer uses is the one that breaks, with `unknown "
            f"field \"spec.<key>\"`. Add them to the schema.")
    if drifted:
        errors.append(
            f"these defaults disagree between chart/values.yaml and the schema "
            f"(key, values.yaml, schema): {drifted}. A helm install takes the "
            f"first and a Composition install is described by the second, so the "
            f"two paths document different behaviour.")
    if schema_only:
        errors.append(
            f"these chart/values.schema.json properties have no chart/values.yaml "
            f"counterpart: {schema_only}. A Composition may set them and the "
            f"chart will silently ignore them. Add them to values.yaml or drop "
            f"them from the schema.")


# Response fields whose TYPE depends on the resource's own shape, so that no
# single CRD status type can hold both answers. Each entry names the reason and
# the source it was read in; this is a short, deliberately hand-maintained list,
# not a heuristic, because a pattern that tried to infer polymorphism from an OAS
# would quietly stop matching the day a vendor changed how it spells an empty
# value.
_POLYMORPHIC_STATUS_FIELDS = {
    "server.image":
        "Nova returns `image` as an OBJECT for a server booted from an image and "
        "as the EMPTY STRING for one booted from a volume "
        "(nova/api/openstack/compute/views/servers.py `_get_image` at "
        "stable/2026.1: the else branch is `return \"\"`). A structural CRD "
        "property has exactly one type, so listing this field makes an entire "
        "class of server unobservable -- MEASURED as exactly that on a live "
        "cluster: a boot-from-volume Instance sat at "
        "Ready=Unknown/ObserveFailed for 900 s, its only written status field "
        "`server.id`, while the server itself was ACTIVE and reachable. The "
        "field stays in the `get` response schema, where it is true; it must "
        "not be mapped into status.",
}


def verify_no_polymorphic_status_fields(errors):
    """No RestDefinition may map a type-polymorphic response field into status.

    WHY THIS IS A SCREEN AND NOT A COMMENT. The fix for the defect above is the
    ABSENCE of one line, and an absence is exactly what a later reader
    re-introduces in good faith: `server.image` looks like an obviously useful
    status field, the OAS declares it as an object two files away, and adding it
    back produces no error here, no error at install, and no error until someone
    boots a server from a volume -- at which point that Instance never reports
    anything and the chart looks broken rather than misconfigured. A comment
    cannot fail; this can.

    IT READS THE TEMPLATE AS TEXT, deliberately. chart/templates/rd-*.yaml are
    Helm templates: `kind: {{ .Values... }}` is not valid YAML, so yaml.safe_load
    cannot be used, and rendering with helm would make a repo guard depend on a
    binary that may not be installed. The list items this needs are flat scalars
    under a known key, which text extraction reads exactly.
    """
    paths = sorted(glob.glob(os.path.join("chart", "templates", "rd-*.yaml")))
    if not paths:
        errors.append("the polymorphic-status screen found no "
                      "chart/templates/rd-*.yaml, so it screened nothing")
        return

    def status_fields(text):
        """The flat list items under each `additionalStatusFields:` key."""
        out, inside, indent = [], False, None
        for line in text.split("\n"):
            stripped = line.strip()
            if not inside:
                if stripped == "additionalStatusFields:":
                    inside, indent = True, len(line) - len(line.lstrip())
                continue
            if not stripped or stripped.startswith("#"):
                continue
            here = len(line) - len(line.lstrip())
            if stripped.startswith("- ") and here > indent:
                out.append(stripped[2:].strip())
                continue
            inside, indent = False, None
            if stripped == "additionalStatusFields:":
                inside, indent = True, here
        return out

    # ARMS, before trusting the extractor on the real files. The reassuring
    # answer ("no forbidden field found") and the broken-extractor answer are the
    # same empty list, so each arm has to fail differently.
    sample = ("spec:\n"
              "    identifiers:\n"
              "      - server.id\n"
              "    additionalStatusFields:\n"
              "      - server.status\n"
              "      # a comment inside the list\n"
              "      - server.image\n"
              "    verbsDescription:\n"
              "      - action: create\n")
    got = status_fields(sample)
    if got != ["server.status", "server.image"]:
        errors.append(f"validate.py: the polymorphic-status screen's extractor "
                      f"read {got} from its own fixture, expected "
                      f"['server.status', 'server.image'] -- it cannot be "
                      f"trusted on the real templates")
        return
    if status_fields(sample.replace("      - server.image\n", "")) != ["server.status"]:
        errors.append("validate.py: the polymorphic-status screen's extractor "
                      "reports a field its fixture does not contain")
        return
    if status_fields("spec:\n  identifiers:\n    - server.id\n") != []:
        errors.append("validate.py: the polymorphic-status screen's extractor "
                      "invents status fields where the key is absent")
        return

    seen_any = 0
    for path in paths:
        try:
            text = open(path, encoding="utf-8").read()
        except OSError as e:
            errors.append(f"{path}: unreadable, so its status fields are NOT "
                          f"checked ({e}). A failure, not a skip.")
            continue
        fields = status_fields(text)
        seen_any += len(fields)
        for field in fields:
            if field in _POLYMORPHIC_STATUS_FIELDS:
                errors.append(
                    f"{path}: additionalStatusFields lists `{field}`, which "
                    f"cannot be a CR status field. "
                    f"{_POLYMORPHIC_STATUS_FIELDS[field]}")
    # A screen that parsed nothing passes. Something here declares status fields.
    if seen_any == 0:
        errors.append("the polymorphic-status screen parsed ZERO status fields "
                      "across " + ", ".join(paths) + ", which cannot be true of "
                      "this chart -- its extractor has stopped matching")


def verify_chart_ci_render_values(errors):
    """chart/ci/*-values.yaml must exist and supply a renderable nova.url.

    WHY. The org release gate (krateo-platformops/.github chart-images.yaml)
    renders the chart with its DEFAULTS plus every chart/ci/*-values.yaml. This
    chart's defaults are deliberately not a valid configuration (nova.url has no
    default and the `novaUrl` helper refuses it), so without such a file the
    gate fails and the whole repository cannot publish. Measured on the first
    release-oci dry run (run 46096398425):

        chart did not render, so its images could NOT be checked ...
        nova.url must be set to the Nova Compute endpoint from your Keystone
        catalog

    The file looks unused (nothing in this repo reads it), which is exactly why
    it gets deleted in good faith; its absence would only surface as a failed
    release. The url must carry the `/v2.1/<segment>` path or the chart's own
    guard refuses it and the gate fails the same way.
    """
    url_re = re.compile(r"/v2\.1/[^/\s]+")

    def problem(files):
        """files: {path: text}. Returns a message, or None when it is fine."""
        if not files:
            return "no chart/ci/*-values.yaml exists"
        for path, text in sorted(files.items()):
            try:
                doc = yaml.safe_load(text)
            except yaml.YAMLError as e:
                return f"{path} is not valid YAML ({e})"
            nova = doc.get("nova") if isinstance(doc, dict) else None
            url = nova.get("url") if isinstance(nova, dict) else None
            if isinstance(url, str) and url_re.search(url):
                return None
        return ("no chart/ci/*-values.yaml sets nova.url to a value with a "
                "/v2.1/<projectId> path")

    # ARMS. "No problem found" and "the check stopped working" are the same
    # silence, so prove it can see each defect before it reads the tree.
    good = {"chart/ci/a-values.yaml":
            "nova:\n  url: https://x.invalid/v2.1/abc\n"}
    if problem(good) is not None:
        errors.append("validate.py: the ci-render-values screen rejects its "
                      "own valid fixture -- it cannot be trusted")
        return
    if problem({}) is None:
        errors.append("validate.py: the ci-render-values screen cannot see a "
                      "missing file")
        return
    if problem({"chart/ci/a-values.yaml":
                "nova:\n  url: https://x.invalid/\n"}) is None:
        errors.append("validate.py: the ci-render-values screen cannot see a "
                      "nova.url without the /v2.1/<projectId> segment")
        return
    if problem({"chart/ci/a-values.yaml":
                "nova:\n  url: https://x.invalid/v2.1/\n"}) is None:
        errors.append("validate.py: the ci-render-values screen accepts an "
                      "EMPTY /v2.1/ segment")
        return

    files = {}
    for path in sorted(glob.glob(os.path.join("chart", "ci", "*-values.yaml"))):
        try:
            files[path] = open(path, encoding="utf-8").read()
        except OSError as e:
            errors.append(f"{path}: unreadable ({e}). A failure, not a skip.")
            return
    msg = problem(files)
    if msg:
        errors.append(
            f"{msg}. The release image gate renders the chart with defaults "
            f"plus chart/ci/*-values.yaml; without a valid one it fails with "
            f"'chart did not render' and nothing publishes. See "
            f"docs/release.md.")


def verify_chart_helmignore_excludes_ci(errors):
    """chart/.helmignore must list BOTH `ci/` and `.helmignore`.

    WHY. chart/ci/render-values.yaml exists only so the org image gate can
    render this chart; its header says "NOT AN INSTALL EXAMPLE". Left unlisted
    it is packaged and shipped to every consumer at the next release. And
    `.helmignore` is itself packaged, so listing only `ci/` SWAPS one unwanted
    member for another. Measured with `helm package`:

        no .helmignore                    31 members, ci/render-values.yaml in
        .helmignore listing only `ci/`    31 members (ci out, .helmignore in)
        .helmignore listing both          30 members, neither present

    A later tidy-up removing the self-reference passes every other gate, so
    this reads the file. It cannot see packaging; lint.yaml's helm package
    step does.
    """
    def problem(text):
        """text: file contents, or None when the file is missing."""
        if text is None:
            return "chart/.helmignore does not exist"
        lines = {ln.strip() for ln in text.splitlines()
                 if ln.strip() and not ln.strip().startswith("#")}
        missing = [e for e in ("ci/", ".helmignore") if e not in lines]
        if missing:
            return "chart/.helmignore does not list " + ", ".join(missing)
        return None

    if problem("ci/\n.helmignore\n") is not None:
        errors.append("validate.py: the helmignore screen rejects its own "
                      "valid fixture -- it cannot be trusted")
        return
    if problem("# ci/\n.helmignore\n") is None:
        errors.append("validate.py: the helmignore screen accepts a "
                      "commented-out ci/ entry")
        return
    if problem("ci/\n") is None:
        errors.append("validate.py: the helmignore screen cannot see the "
                      "swap (a file listing only ci/)")
        return
    if problem(None) is None:
        errors.append("validate.py: the helmignore screen cannot see a "
                      "missing file")
        return

    try:
        text = open(os.path.join("chart", ".helmignore"),
                    encoding="utf-8").read()
    except FileNotFoundError:
        text = None
    except OSError as e:
        errors.append(f"chart/.helmignore: unreadable ({e}). A failure, "
                      "not a skip.")
        return
    msg = problem(text)
    if msg:
        errors.append(
            f"{msg}. Without both entries `helm package` ships "
            "ci/render-values.yaml (or .helmignore in its place) to every "
            "consumer. See the header of chart/.helmignore.")


def _registered_checks():
    return (verify_chart_ci_render_values,
                  verify_chart_helmignore_excludes_ci,
                  verify_no_polymorphic_status_fields,
                  verify_schema_covers_every_values_key,
                  verify_rc_capture_is_reachable,
                  verify_chart_version_stamp_is_not_key_anchored,
                  verify_chart_version_stamp_is_verified,
                  verify_self_test_guard_precedes_dispatch,
                  verify_composition_apiversion,
                  verify_documented_pin,
                  verify_no_builtin_shadowing,
                  verify_auth_scheme_consistency,
                  verify_no_under_floor_recommendation,
                  verify_cr_apiversion_consistency,
                  verify_python_compiles_without_warnings,
                  verify_composition_spec_against_schema,
                  verify_every_script_is_exercised,
                  verify_doc_filenames_exist,
                  verify_no_unstampable_install,
                  verify_documented_chart_version,
                  verify_no_credential_following_redirects,
                  verify_release_dry_run_gates_every_mutation,
                  verify_chart_icon_is_org_controlled,
                  verify_no_header_parameters_in_assets,
                  verify_no_schema_defaults_in_assets,
                  verify_no_request_field_above_microversion_baseline,
                  verify_boot_index_has_no_lower_bound,
                  verify_no_conflict_markers,
                  verify_doc_link_fragments_resolve,
                  verify_no_544_refused_path_forms,
                  verify_shared_cluster_preflight,
                  verify_code_fences_pair,
                  verify_admonition_not_in_code_fence,
                  verify_no_pipe_from_large_producer,
                  verify_provider_floor_declaration,
                  verify_local_gate_list_matches_ci,
                  verify_required_checks_are_produced)


def verify_no_conflict_markers(errors):
    """No tracked text file may contain a git conflict marker.

    WHY THIS EXISTS, and it is the plainest instance in this file. On
    2026-11-10 a `git merge origin/main` on a docs branch conflicted, printed
    its failure in Italian, and I edited the file and committed anyway --
    pushing all three marker lines into docs/architecture.md. **validate.py
    returned rc=0 on that tree**, and so did every other gate: markdown with
    conflict markers is still valid markdown, the YAML was untouched, and no
    test reads prose. The defect went past a green suite into a document a
    reader would act on, and what caught it was opening the file by hand.

    A gate that cannot see a half-merged file is not protecting the documents
    it claims to cover.

    Matched ANCHORED at line start, which is what git writes, and at git's
    exact widths (seven characters, then a space or end of line). Otherwise a
    diff quoted inside a fenced block would trip it. This file is skipped
    because this docstring discusses the markers.
    """
    pattern = re.compile(r"^(?:<{7}|>{7})[ \t]|^={7}$")
    exts = (".md", ".yaml", ".yml", ".py", ".sh", ".json", ".txt")
    skip = {"scripts/validate.py"}
    hits = []
    scanned = 0
    # os.walk, NOT glob("**/*"), and the reason is a gap this check found in
    # itself: Python's glob skips hidden names, so `**/*.yaml` never descends
    # into `.github/` -- the directory where a conflict marker would do the most
    # damage, since a half-merged workflow is a half-merged release gate. The
    # arm that exposed it used a dotfile fixture and silently scanned nothing.
    candidates = []
    for dirpath, dirnames, filenames in os.walk("."):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__", "node_modules")]
        for name in filenames:
            if name.endswith(exts):
                candidates.append(os.path.relpath(os.path.join(dirpath, name), "."))
    for rel in sorted(set(candidates)):
        if rel.startswith(".git/") or rel in skip:
            continue
        if not os.path.isfile(rel):
            continue
        try:
            with open(rel, encoding="utf-8") as fh:
                text = fh.read()
        except (UnicodeDecodeError, OSError):
            continue
        scanned += 1
        for n, line in enumerate(text.splitlines(), 1):
            if pattern.match(line):
                hits.append("{}:{}: {}".format(rel, n, line[:40]))
    if scanned == 0:
        errors.append(
            "verify_no_conflict_markers scanned 0 files, so it would pass on a "
            "tree full of markers. VOID, not a pass."
        )
        return
    for h in hits:
        errors.append("git conflict marker left in a tracked file -- {}".format(h))


def _github_heading_slugs(markdown_text):
    """Heading anchors as GitHub derives them, plus its duplicate suffixes.

    GitHub lowercases, drops everything that is not a letter, digit, space or
    hyphen, then turns spaces into hyphens. A repeated slug gets -1, -2, ... in
    document order. Both rules matter here: this repo has headings with
    parentheses and backticks, and two sections could easily collide.
    """
    slugs = []
    seen = {}
    in_fence = False
    for line in markdown_text.split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line.startswith("#"):
            continue
        title = line.lstrip("#").strip()
        base = re.sub(r"[^a-z0-9 -]", "", title.lower()).replace(" ", "-")
        if base in seen:
            seen[base] += 1
            slugs.append("{}-{}".format(base, seen[base]))
        else:
            seen[base] = 0
            slugs.append(base)
    return slugs


def verify_doc_link_fragments_resolve(errors):
    """A `](file.md#anchor)` link must name a heading that exists.

    WHY THIS EXISTS, and it was nearly shipped the hour it was written. A commit
    whose whole purpose was to stop three sentences asserting a mechanism
    without pointing at its source added three pointers to
    `architecture.md#the-request-path` -- an anchor that does not exist. The
    sibling screen verify_doc_filenames_exist passed them, because it checks
    backticked FILENAMES and says nothing about link FRAGMENTS.

    The failure is silent in the worst way: a dead anchor does not 404. The
    browser scrolls to the top of the page, so a reader lands on a real document
    and simply does not find the thing they were sent for, and concludes the
    reference was vague rather than broken.

    Same-repo markdown only. External URLs are never fetched, so this screen
    stays offline and cannot fail on network conditions.
    """
    import posixpath
    doc_files = sorted(glob.glob("**/*.md", recursive=True)) + sorted(
        glob.glob(os.path.join(".github", "**", "*.md"), recursive=True))
    slug_cache = {}
    checked = 0
    for src in sorted(set(doc_files)):
        try:
            with open(src, encoding="utf-8") as fh:
                text = fh.read()
        except (UnicodeDecodeError, OSError):
            continue
        for target, frag in re.findall(r"\]\(([^)\s#]*\.md)#([A-Za-z0-9_.-]+)\)", text):
            resolved = posixpath.normpath(
                posixpath.join(posixpath.dirname(src.replace(os.sep, "/")), target))
            if not os.path.isfile(resolved):
                continue   # verify_doc_filenames_exist owns the missing-file case
            if resolved not in slug_cache:
                with open(resolved, encoding="utf-8") as fh:
                    slug_cache[resolved] = _github_heading_slugs(fh.read())
            checked += 1
            if frag not in slug_cache[resolved]:
                near = [x for x in slug_cache[resolved] if frag.split("-")[0] in x][:2]
                errors.append(
                    "{}: link points at {}#{}, and that heading does not exist. "
                    "A dead anchor does not 404 -- the reader lands at the top of "
                    "a real page and concludes the reference was vague.{}".format(
                        src, resolved, frag,
                        " Nearest: {}".format(near) if near else ""))
        # same-file fragments
        own = _github_heading_slugs(text)
        for frag in re.findall(r"\]\(#([A-Za-z0-9_.-]+)\)", text):
            checked += 1
            if frag not in own:
                errors.append(
                    "{}: same-file link points at #{}, which is not a heading in "
                    "that file.".format(src, frag))
    if checked == 0:
        errors.append(
            "verify_doc_link_fragments_resolve checked 0 fragments, so it would "
            "pass on a document full of dead anchors. VOID, not a pass."
        )


def _refused_544_form(value):
    """True when a JSONPath value uses a form oasgen 0.57.0 (#544) refuses.

    SCOPE, from oasgen-provider's own differential test, 2026-11-11 -- and it is
    NARROWER than the advice that circulated first:

        REFUSED    a.0       against an ARRAY   dotted index        -- decidable
                   a['0']    against an ARRAY   quoted key as index -- decidable
                   a[0]      against a MAP      index into a map    -- NOT decidable
        UNCHANGED  a[0]      against an ARRAY   works
                   a['0']    against a MAP      works
                   spec.a.b  plain traversal, any depth
                   [?k=v]    predicate selectors

    So this screen flags ONLY the two forms decidable from the path text. It
    deliberately does NOT flag `a[0]`: against an array that is the working
    form, and against a map it is refused but no static screen can tell which,
    because it depends on the data's shape at that point. An earlier regex of
    mine matched any bracketed index and would have raised a false alarm on the
    working form; it reported 0 only because this corpus has none of either.
    """
    import re as _re
    # SEGMENT-WISE, not a regex over the whole string, and the self-test below is
    # why: `(^|\.)[0-9]+(\.|$)` flagged `2.72`, a Nova microversion, because it
    # only asked whether SOME segment was numeric. A version string is numeric in
    # EVERY segment; a path has non-numeric ones too. So require both.
    if _re.search(r"\[\s*'[0-9]+'\s*\]", value):           # a['0']
        return "quoted numeric key used as an index"
    segments = value.split(".")
    if len(segments) < 2:
        return None
    numeric = [x for x in segments if x.isdigit()]
    named = [x for x in segments if x and not x.isdigit()]
    if numeric and named:
        return "dotted numeric segment ({})".format(numeric[0])
    return None


CR_SCALAR_KEYS = ("inCustomResource", "nameFromCustomResource",
                  "keyFromCustomResource", "equalsFromCustomResource",
                  "customResourceFieldPresent", "customResourceFieldAbsent")
CR_LIST_KEYS = ("identifiers", "additionalStatusFields")


def _scan_544_lines(path, lines, errors):
    """Scan one document's lines for CR-reading paths in a refused form.

    Split out of the check so the WALKER can be tested on a planted fixture.
    Returns the number of paths examined.
    """
    checked = 0
    in_list, list_indent, list_key = False, 0, ""
    for n, line in enumerate(lines, 1):
        indent = len(line) - len(line.lstrip())
        m = re.match(r"\s*(" + "|".join(CR_SCALAR_KEYS) + r")\s*:\s*(.+?)\s*$", line)
        if m:
            in_list = False
            value = m.group(2).strip().strip("'\"")
            checked += 1
            why = _refused_544_form(value)
            if why:
                errors.append(
                    "{}:{}: {} = {!r} uses a path form oasgen 0.57.0 refuses "
                    "({}). It works on the current pin and breaks on upgrade "
                    "(#544).".format(path, n, m.group(1), value, why))
            continue
        m2 = re.match(r"\s*(" + "|".join(CR_LIST_KEYS) + r")\s*:\s*$", line)
        if m2:
            in_list, list_indent, list_key = True, indent, m2.group(1)
            continue
        if in_list:
            m3 = re.match(r"\s*-\s*(.+?)\s*$", line)
            if m3 and indent > list_indent:
                value = m3.group(1).strip().strip("'\"")
                checked += 1
                why = _refused_544_form(value)
                if why:
                    errors.append(
                        "{}:{}: {}[] entry {!r} uses a path form oasgen 0.57.0 "
                        "refuses ({}).".format(path, n, list_key, value, why))
            elif line.strip() and indent <= list_indent:
                in_list = False
    return checked


def verify_no_544_refused_path_forms(errors):
    """No CR-reading JSONPath in the assets may use a form 0.57.0 refuses.

    WHY A SCREEN RATHER THAN A ONE-OFF CHECK. The risk is on the UPGRADE, not on
    the tag existing: these paths work today and stop working when the pin moves
    to 0.57.0. A manual screen protects the upgrade I am about to do; a screen in
    CI protects the one after it, and the one someone else does.

    Textual, not YAML-parsed, for the same reason as
    verify_no_header_parameters_in_assets: the assets carry Helm template syntax
    (`{{ include ... }}`) and are not valid YAML. A YAML-based screen on them
    VOIDs -- measured.

    Scoped to the keys that read INTO the custom resource, because an unscoped
    search for a dotted numeric segment matches `/v2.1/flavors` and every
    microversion in the corpus.

    THE KEY LIST WAS WRONG AND A CROSS-CHECK FOUND IT. This docstring used to
    claim the list was "oasgen's full surface". On 2026-11-11 I compared it
    against oasgen-provider's own `check544.py` and mine was MISSING
    `equalsFromCustomResource` and `customResourceFieldAbsent` -- two of the
    three keys they independently reported getting wrong. My census had
    reported "39 CR-reading paths across 18 files" and a clean result, and that
    clean result was true of the four keys I happened to list. A screen scoped
    by an enumerated key list reports exactly as clean as its list is complete,
    and nothing in the output distinguishes "no refused forms" from "did not
    look". So the completeness of this tuple is now asserted below rather than
    claimed here: every key gets a planted refused value and must be caught.

    Still deliberately NOT flagged: `a[0]`, which 0.57.0 refuses against a MAP
    and accepts against an ARRAY. That depends on the schema the path resolves
    against, which is not in the RestDefinition -- so a clean run here means
    "no TEXT-DECIDABLE refusal", not a clean bill of health.
    """
    files = sorted(glob.glob(os.path.join("chart", "assets", "*.yaml")) +
                   glob.glob(os.path.join("chart", "templates", "*.yaml")))
    if not files:
        errors.append("verify_no_544_refused_path_forms found no assets to screen. "
                      "VOID, not a pass.")
        return
    checked = 0
    for path in files:
        with open(path, encoding="utf-8", errors="replace") as fh:
            checked += _scan_544_lines(path, fh.read().splitlines(), errors)
    if checked == 0:
        errors.append(
            "verify_no_544_refused_path_forms examined 0 CR-reading paths across "
            "{} asset file(s), so it would pass on a corpus full of refused "
            "forms. VOID, not a pass.".format(len(files)))
        return
    # Self-test the judge, with the forms that must and must NOT fire. The
    # screen's own 0 is only as good as this.
    must_fire = ("status.items.0.id", "a.0", "spec.a['0'].b", "status.0")
    must_not = ("status.aggregate.id", "spec.a.b.c", "a[0]", "a['x']",
                "spec.items[?name=x].id", "/v2.1/flavors", "2.72")
    for v in must_fire:
        if not _refused_544_form(v):
            errors.append("_refused_544_form missed {!r}, which 0.57.0 refuses "
                          "-- the screen above cannot be trusted.".format(v))
    for v in must_not:
        if _refused_544_form(v):
            errors.append("_refused_544_form flagged {!r}, which 0.57.0 accepts "
                          "-- a false alarm would send a reader to inspect a "
                          "working path.".format(v))
    # PLANTED CONTROL FOR THE WALKER, one needle per key. The judge self-test
    # above proves the CLASSIFIER works; it says nothing about whether the
    # walker ever hands it the value sitting under a given key. A key silently
    # not reached is exactly the defect the oasgen cross-check found, and it
    # reads identically to a clean corpus.
    # THE PLANTED ARM BELOW CANNOT CATCH A DELETED KEY, and saying so matters
    # more than the arm. Its fixture is built FROM these tuples, so dropping a
    # key drops the needle with it and the arm still passes -- a vacuous test,
    # which is worse than none because it licenses the word "controlled". What
    # it does catch is a key that is listed but unreachable: wrong regex, a list
    # key handled as a scalar, a walker that returns early. Mutation-tested by
    # excluding one key inside _scan_544_lines, which names the key it lost.
    #
    # A deleted key needs a source OUTSIDE the tuple. That source is
    # oasgen-provider's own check544.py, cross-checked by hand on 2026-11-11
    # (12/12 agreement on the classifier rows, and that is where these two keys
    # came from). A count is the cheapest honest proxy: edit the tuples and CI
    # fails until someone re-runs that comparison and updates the number
    # deliberately. It is a canary, not a proof.
    if len(CR_SCALAR_KEYS) != 6 or len(CR_LIST_KEYS) != 2:
        errors.append(
            "the #544 key list changed ({} scalar + {} list, expected 6 + 2). "
            "This screen reports exactly as clean as its key list is complete, "
            "so re-run the comparison against oasgen-provider's check544.py "
            "before updating these numbers -- do not just make CI green."
            .format(len(CR_SCALAR_KEYS), len(CR_LIST_KEYS)))
    planted = []
    for k in CR_SCALAR_KEYS:
        planted.append("  {}: spec.a.0.b".format(k))
    for k in CR_LIST_KEYS:
        planted.append("  {}:".format(k))
        planted.append("    - spec.a.0")
    probe = []
    n_examined = _scan_544_lines("<planted>", planted, probe)
    want = len(CR_SCALAR_KEYS) + len(CR_LIST_KEYS)
    if len(probe) != want:
        caught = set()
        for msg in probe:
            for k in CR_SCALAR_KEYS + CR_LIST_KEYS:
                if k in msg:
                    caught.add(k)
        missed = [k for k in CR_SCALAR_KEYS + CR_LIST_KEYS if k not in caught]
        errors.append(
            "the #544 walker caught {} of {} planted refused paths -- it does "
            "not reach: {}. Every key it cannot reach is a key the clean result "
            "above says nothing about.".format(len(probe), want,
                                               ", ".join(missed) or "unknown"))
    if n_examined != want:
        errors.append(
            "the #544 walker examined {} planted paths, expected {}. The "
            "fixture and the walker disagree, so the control is not measuring "
            "the walker.".format(n_examined, want))


def verify_release_dry_run_gates_every_mutation(errors):
    """Every mutating step in release.yaml must reference DRY_RUN.

    WHY THIS EXISTS, and it is not hypothetical. This repository's previous
    release workflow -- release-tag.yaml, retired when the org standard's shared
    release-oci was adopted (R5) -- gained a `workflow_dispatch` with `dry_run`
    so the release path could be exercised without burning a version. `helm push` was gated. The step that runs
    `gh release create` was NOT -- it had zero DRY_RUN references -- and
    `gh release create` creates a TAG. The first dry run therefore produced a
    real release 0.1.6 and tag 0.1.6 for a chart that was never pushed: a
    CompositionDefinition pinning a version absent from the registry, which is
    the defect PR #11 fixed once already, plus a repo-wide red `validate`
    because the newest tag stopped matching the documented version.

    "Dry run" is a property of EVERY mutating step, not of the one you had in
    mind when you added the flag. So this asserts the property rather than
    trusting the next author to remember it.

    WHAT COUNTS AS MUTATING is a deliberately short, explicit list rather than a
    clever heuristic: a pattern that tries to infer mutation would quietly stop
    matching when someone writes the same call differently, and this screen's
    whole value is that it cannot miss one silently. A new mutating verb must be
    added here on purpose -- and the VERBS list being wrong is caught by the
    self-test below, which requires each verb to be found somewhere in the file.
    """
    path = ".github/workflows/release.yaml"
    try:
        raw = open(path, encoding="utf-8").read()
    except OSError as e:
        errors.append(f"{path}: unreadable, so the dry-run gate is NOT checked "
                      f"({e}). A file this screen cannot read is a failure, not "
                      f"a skip.")
        return
    try:
        doc = yaml.safe_load(raw)
    except Exception as e:  # noqa: BLE001 - any parse failure is a failure here
        errors.append(f"{path}: does not parse as YAML ({e}), so its steps "
                      f"cannot be inspected. VOID, not a pass.")
        return

    # THE VERBS ARE THE MUTATIONS, NOT THE SCRIPTS THAT WRAP THEM.
    #
    # #171 moved `gh release create` / `gh release upload` into
    # hack/publish-release-assets.sh (it survives the release-notes race: an
    # "already exists" create means a concurrent creator, glance-kog 0.1.2) and
    # set VERBS to ("publish-release-assets.sh", "helm push"). That was the
    # correct minimal change -- the caller keeps its DRY_RUN conditional, and
    # gating the caller is equivalent to gating the verb -- but it narrowed what
    # this screen can see:
    #
    #   gh release create   1 occurrence in the workflow, A COMMENT   2 in the script
    #   gh release upload   0 in the workflow                         1 in the script
    #
    # So a FUTURE ungated `gh release upload` written directly into the workflow
    # would have passed unflagged. And keeping it in VERBS would have survived
    # `missing_verbs` only because the string still appears in a comment, which
    # is a fragile reason for a screen to keep working -- the same comment-vs-code
    # confusion this function already carries a warning about.
    #
    # So the verbs stay the verbs, and the screen FOLLOWS THEM into the scripts
    # the workflow invokes. A step is gated if it carries the conditional; a
    # script is gated if every step that invokes it is.
    # THE PUSH VERB CHANGED AND THIS SCREEN WENT QUIET, which is how it was
    # found: adopting rule 14 replaced `helm push` with the shared script, and
    # afterwards NONE of the three verbs had a live occurrence anywhere -- so the
    # screen passed having checked nothing about the one mutation that matters
    # most. `"${CHART_PUSH}"` is the invocation form, and it is listed here so
    # the gating check applies to it.
    # `helm push` IS DELIBERATELY ABSENT, as this screen's own message
    # instructed when it caught the change: rule 14 routes the push through the
    # shared script, so `helm push` no longer occurs in this repo outside the
    # comments that explain why. Leaving it listed would make the screen VOID on
    # every run; removing it without replacing it would make the screen pass
    # having checked nothing. Both were available and both are wrong.
    # AND THEN THE PUSH LEFT THIS REPOSITORY ENTIRELY, so `"${CHART_PUSH}"` is
    # gone from this list too -- the same deliberate removal, for the same
    # reason, one layer further out. Adopting R5 replaced the local chart-push
    # step with a call to the shared release-oci workflow, which performs the
    # push in ITS runner and gates it with ITS own dry_run. Nothing in this
    # repository pushes to the registry any more, so a verb looking for it would
    # make this screen VOID on every run.
    #
    # WHAT THIS SCREEN STILL CHECKS, stated because "removing a verb" is the
    # move that can hollow it out: the ONE mutation this repository still
    # performs itself, the GitHub release and its asset. That is what
    # release-oci cannot do (a called workflow is capped at its caller's
    # `contents: read`), it is the step whose first ungated dry run created a
    # real release 0.1.6, and both its verbs are live in
    # hack/publish-release-assets.sh, which release.yaml invokes. The registry
    # push is now gated upstream at a pinned SHA, not here.
    VERBS = ("gh release create", "gh release upload")

    # Scripts this workflow runs, and what each contains. Only repo-relative
    # paths under hack/ or scripts/ -- an absolute or fetched path is not
    # something this screen can reason about, and pretending otherwise would be
    # the kind of silent scope error it exists to prevent.
    SCRIPT_REF = re.compile(r"(?:bash|sh|python3)\s+((?:hack|scripts)/[A-Za-z0-9_.-]+)")
    assert SCRIPT_REF.search("bash hack/publish-release-assets.sh \"$V\"").group(1) \
        == "hack/publish-release-assets.sh"
    assert SCRIPT_REF.search("python3 scripts/validate.py").group(1) == "scripts/validate.py"
    assert SCRIPT_REF.search("bash /tmp/evil.sh") is None

    script_text = {}
    for m in SCRIPT_REF.finditer(raw):
        rel = m.group(1)
        if rel in script_text:
            continue
        try:
            script_text[rel] = open(rel, encoding="utf-8", errors="replace").read()
        except OSError:
            errors.append(
                f"{path} runs {rel}, which cannot be read, so whether it mutates "
                f"anything is unknown. VOID, not a pass.")
            return

    # SELF-TEST FIRST: a verb that appears NOWHERE -- not in the workflow, not in
    # any script it runs -- means this screen has silently stopped checking it.
    # The union is the point: #171's narrowing was invisible precisely because
    # the verb had moved somewhere the screen did not look.
    # LIVE LINES ONLY, because a verb that survives in PROSE satisfies a
    # substring test while no longer being invoked anywhere. That is exactly
    # what happened: after rule 14 the only `helm push` occurrences left in this
    # repo were comments explaining the gating, and they kept this guard quiet.
    def _live(text):
        return "\n".join(l for l in text.split("\n")
                          if not l.strip().startswith("#"))

    everywhere = _live(raw) + "\n" + "\n".join(
        _live(t) for t in script_text.values())
    missing_verbs = [v for v in VERBS if v not in everywhere]
    if missing_verbs:
        errors.append(
            f"{path}: the dry-run screen looks for {missing_verbs} and neither "
            f"the workflow nor any script it runs ({sorted(script_text)}) "
            f"contains them. Either the release path no longer mutates that way "
            f"-- in which case update VERBS deliberately -- or this screen is "
            f"now checking nothing. VOID, not a pass.")
        return

    # Which scripts mutate, so a step invoking one must be gated as if the verb
    # were written inline.
    mutating_scripts = sorted(rel for rel, txt in script_text.items()
                              if any(v in txt for v in VERBS))

    jobs = (doc.get("jobs") or {})
    ungated = []
    for job_name, job in jobs.items():
        for step in (job.get("steps") or []):
            run = step.get("run") or ""
            # A step is in scope if it writes a mutation itself OR invokes a
            # script that does. The second clause is what #171's shape needs.
            invokes_mutator = any(rel in run for rel in mutating_scripts)
            if not any(v in run for v in VERBS) and not invokes_mutator:
                continue
            # COMMENTS DO NOT GATE ANYTHING, and checking the raw body for the
            # string "DRY_RUN" was satisfied by the comment explaining the bug.
            # The first version of this screen passed WITH the defect injected:
            # a guard that reads prose as if it were code, inside a guard built
            # to stop a step being ungated. Strip comment lines first, so this
            # asks about executable text only.
            code = "\n".join(ln for ln in run.splitlines()
                              if not ln.lstrip().startswith("#"))
            # And require a CONDITIONAL on it, not a mention: `echo $DRY_RUN`
            # would satisfy a substring test while gating nothing.
            gated = any(pat in code for pat in
                        ('if [ "${DRY_RUN}"', 'if [ "$DRY_RUN"',
                         'if [[ "${DRY_RUN}"', 'if [[ "$DRY_RUN"'))
            if not gated:
                ungated.append(f"{job_name} / {step.get('name') or '(unnamed)'}")
    if ungated:
        errors.append(
            "these release steps MUTATE something (a release, a tag or the "
            "registry) and carry no `if [ \"${{DRY_RUN}}\" = \"true\" ]` "
            "conditional in their executable text, so a dry run would perform "
            "them: {}. A COMMENT mentioning DRY_RUN does not count, and neither "
            "does echoing it -- the first version of this screen accepted both. "
            "`gh release create` also creates a tag. Gate them, or the dispatch "
            "is not a dry run.".format(ungated))


def _verify_every_check_is_registered(errors):
    '''Every `verify_*` defined in this module must be in _registered_checks().

    A screen that exists and is never reached is the defect this whole file
    exists to prevent, and until this was added it was reachable right here:
    deleting a check from the registration tuple left "all checks passed",
    because each screen's self-tests live INSIDE the screen and a screen that
    is never called never runs them. Verified by mutation -- removing
    verify_auth_scheme_consistency from the tuple produced exit 0.

    Credit: oasgen-provider, who hit the identical shape fixing #361. Every
    test they wrote for a merge helper called it directly, so the suite stayed
    green when they reverted the call site -- which was the original bug. They
    found it by mutating each half separately; the first mutation passing had
    looked like adequate coverage.
    '''
    defined = {n for n, v in globals().items()
               if n.startswith("verify_") and callable(v)}
    registered = {f.__name__ for f in _registered_checks()}
    unregistered = defined - registered
    if unregistered:
        errors.append(
            "these screens are DEFINED but never registered, so they never run "
            "and their self-tests never execute: {}. A screen that exists and "
            "is not reached is worse than an absent one -- it reads as "
            "coverage.".format(sorted(unregistered)))
    stale = registered - defined
    if stale:
        errors.append("registered but not defined: {}".format(sorted(stale)))
    if not registered:
        errors.append("no checks registered at all -- a failure, not a pass")


def main():
    # THIS SCRIPT TAKES NO ARGUMENTS, AND SAYS SO RATHER THAN IGNORING THEM.
    # Measured 2026-11-21: `validate.py --this-flag-does-not-exist` exited 0,
    # having run every check. So a typo'd or invented flag ran a DIFFERENT
    # program than the caller asked for and reported success -- and the caller
    # cannot tell the difference, because the output is identical.
    #
    # hack/README.md holds the opposite standard for the probes ("an
    # unrecognised flag exits 2 before anything runs"), and the gate that
    # enforces this repo's standards is the last place that should fall short
    # of them. Exit 2, not 1: a usage error is not a validation failure, and a
    # CI step that cannot tell them apart will report the wrong thing.
    if len(sys.argv) > 1:
        print(
            "validate.py takes no arguments; got {!r}.\n"
            "It runs every registered screen over the repository and takes no "
            "flags, so an argument here means the caller expected a different "
            "program. Exiting 2 (usage) rather than 0, because a flag that is "
            "silently ignored reports success for work nobody asked for.".format(
                sys.argv[1:]
            ),
            file=sys.stderr,
        )
        return 2
    errors = []
    # Called explicitly, NOT through the tuple it validates: a check on the
    # registration cannot depend on being registered.
    _verify_every_check_is_registered(errors)
    for check in _registered_checks():
        check(errors)
    if errors:
        print(f"{len(errors)} problem(s):\n", file=sys.stderr)
        for e in errors:
            print(f"  {e}\n", file=sys.stderr)
        return 1
    print("validate.py: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
