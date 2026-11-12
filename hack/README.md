# `hack/` — probes, gates and the platform facts they depend on

These scripts measure one claim each against a real cluster, and the gates in
`.github/workflows/validate.yaml` prove each probe can still report every one
of its verdicts before a cluster is spent on it. The notes below are the
cross-platform facts the scripts were *measured* against, not the ones they
were assumed to hold.

## Regex escapes are not portable, and the axis is not macOS-vs-Linux

`grep -E '^[A-Za-z]+Configuration\t'` matched on a Mac and matched nothing in
CI. `hack/probe-apikey-rbac.sh` shipped that pattern, and every result it had
ever produced came from a Mac; on a Linux runner it reported a confident VOID
about a healthy cluster. CI found it the first time it executed that path.

Re-derive this table with `hack/check-regex-portability.py --measure`; the row
labelled `control` must read `BOTH` in every column or that column measured
nothing:

| pattern | ugrep 7.8.4 | BSD grep 2.6.0-FreeBSD | GNU grep 3.11 | busybox grep |
|---|---|---|---|---|
| `a\tb` | tab | tab | **literal `t`** | tab |
| `a\db` | digit | digit | **literal `d`** | digit |
| `a\sb` | whitespace | whitespace | whitespace | whitespace |
| `a.b` *(control)* | both lines | both lines | both lines | both lines |

**GNU grep is the outlier; the other three agree with each other.** So "it
works on Linux" is not a side either: busybox reads `\t` as a tab, so the same
pattern can pass inside a busybox-based container step and fail on the ubuntu
runner in the same workflow.

Two consequences:

- **Use a literal character.** `TAB=$(printf '\t')`, then `grep -E "...$TAB"`.
- **A `\t` inside `kubectl -o jsonpath=` is a different thing.** That escape is
  JSONPath's own, parsed by the client before any grep sees it, and it emits a
  real tab everywhere. "A `\t` in a script" is not one category, which is why
  `check-regex-portability.py` exempts JSONPath contexts rather than flagging
  them.

`\s` is permitted because it was *measured* to mean whitespace in every
implementation tested — not because it looks safe. That allowlist lives in
`check-regex-portability.py` and `--measure` fails if a permitted escape starts
diverging.

## Shell facts these scripts were bitten by

- **`export LC_ALL=C`** at the top of anything doing arithmetic on command
  output. A comma decimal separator turns a comparison into a syntax error.
- **Never read `$?` after a pipe.** It is the last command's status, not the
  one you meant. Redirect to a file and read `rc` on the next line. The same
  trap wearing a different hat: `cmd | head -20 && echo OK` gates `echo` on
  `head`, so it prints OK over a failed `cmd`.
- **`cat -A` is GNU-only.** On macOS it fails and silently empties a pipeline,
  which looks exactly like "no output to show".
- **zsh does not word-split unquoted parameters.** A helper called as
  `classify $row` receives one argument in zsh and several in bash. Re-run
  anything you reason about under the shell the script declares.
- **A report line physically above the computation it reports** is a latent
  crash under `set -u`, and a `--self-test` that stops before it will never
  reach it. Two probes died that way on a billing GKE cluster.

## Run the gates locally before pushing

CI's lint step is reproducible on a laptop, and not running it cost a red CI
run on the commit that added this file:

    shopt -s nullglob
    shellcheck -x scripts/*.sh hack/*.sh hack/lib/*.sh
    python3 scripts/validate.py
    python3 hack/check-regex-portability.py
    bash hack/probe-401.sh --self-test
    bash hack/probe-redirect.sh --self-test
    bash hack/probe-portonly.sh --self-test
    bash hack/probe-548-delete.sh --self-test
    python3 scripts/check-schema-warnings.py --self-test
    python3 hack/check-restdefinition-shape.py --selftest
    python3 hack/check-restdefinition-shape.py          # needs helm, not a cluster
    bash hack/validate-401-mock.sh          # needs docker, not a cluster
    bash hack/validate-redirect-mock.sh     # needs docker, not a cluster

`SC2016` ("expressions don't expand in single quotes") fires on every awk
program held in a shell variable, because `$2` is awk's field reference and
*must* reach awk unexpanded. Disable it with the reason on the line above, as
the existing probes do — do not switch to double quotes to quiet it, which
would make the shell substitute its own empty positional parameters and the
program would silently match nothing.

## The rules behind all of it

**If the reassuring answer and the broken-instrument answer are the same
string, add an arm that fails differently.** Every check here that reports an
absence — no warnings, zero requests, nothing changed — first proves on
traffic it generated itself that it could have seen something. A planted
control is what makes a zero a measurement instead of a silence.

**The field nobody asserts is the field that breaks.** `probe-548-delete.sh`'s
parser truncated every HTTP status — `500` parsed as `"50"`, `404` as `"40"` —
and *fifteen* assertions passed over it, because every one of them checked the
method, the caller or the token state. A malformed value that keeps its type
stays invisible to every check that only touches its neighbours: a two-digit
status still sorts, still prints, still compares. Found by a dry run printing a
table a human looked at. When a parser emits N fields, assert N.

**Asserting a finding's letter and severity is not asserting its message.** Two
mutants survived `check-restdefinition-shape.py` for this reason. Dropping the
"path is not declared" branch still produced an A row — the method branch
catches an absent path, because an absent path has no methods — but told the
reader the method was wrong when the path did not exist. And collapsing the
branch on a mapping's source root said an author-set field had been *kept* when
it is omitted. Same letter, same severity, opposite repair. Where two cases
share a row type, assert that their messages **differ**.

**A screen whose first run contradicts a settled measurement is wrong until
proven otherwise.** `check-restdefinition-shape.py` opened with four actionable
rows against `ComputeQuota`, which this repo has measured working on a real
cluster. The screen was wrong: Nova's quota set is addressed by a path-level
parameter no request body declares, and the resolver read only request bodies.
The contradiction is the only reason it was investigated rather than filed.
A new instrument disagreeing with an old measurement is evidence about the
instrument first.
