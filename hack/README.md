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
    bash hack/validate-401-mock.sh          # needs docker, not a cluster
    bash hack/validate-redirect-mock.sh     # needs docker, not a cluster

`SC2016` ("expressions don't expand in single quotes") fires on every awk
program held in a shell variable, because `$2` is awk's field reference and
*must* reach awk unexpanded. Disable it with the reason on the line above, as
the existing probes do — do not switch to double quotes to quiet it, which
would make the shell substitute its own empty positional parameters and the
program would silently match nothing.

## The rule behind all of it

**If the reassuring answer and the broken-instrument answer are the same
string, add an arm that fails differently.** Every check here that reports an
absence — no warnings, zero requests, nothing changed — first proves on
traffic it generated itself that it could have seen something. A planted
control is what makes a zero a measurement instead of a silence.
