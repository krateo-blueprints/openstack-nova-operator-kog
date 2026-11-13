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
                                                        # also screens chart/samples/
                                                        # for pruned and missing fields
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

**Before writing an invariant, ask which input distinguishes it from its own
negation — and whether that input is in the corpus.** This is cheaper than a
mutation test and catches the same thing earlier.
`hack/check-restdefinition-shape.py` asserts that a suppressed self-referencing
param must produce a C row. Stated the other way round — *every* C row must
have a suppressed E row — it would flag a plain typo as the screen contradicting
itself, turning the most ordinary real finding into noise about the instrument.
The sound form was written, and the arm **still could not tell it from the
unsound one**: every document in the corpus named fields that existed, so over
that corpus the two forms are identical and the arm passed either way. The
discriminating input is a mapping naming a field that does not exist, it was
sitting in a *separate* arm that called the subject directly, and it never
passed through the invariant at all. **Testing the rule and testing the arm that
polices the rule are different tests.** The corpus now contains that input and
the arm fails if it ever leaves — a green arm over a corpus that cannot fail it
is not evidence. Found by `openstack-ironic-operator-kog` in their own screen
first; the hole was identical in both.

**A screen whose first run contradicts a settled measurement is wrong until
proven otherwise.** `check-restdefinition-shape.py` opened with four actionable
rows against `ComputeQuota`, which this repo has measured working on a real
cluster. The screen was wrong: Nova's quota set is addressed by a path-level
parameter no request body declares, and the resolver read only request bodies.
The contradiction is the only reason it was investigated rather than filed.
A new instrument disagreeing with an old measurement is evidence about the
instrument first.

**An "actionable" row is advice, and advice can be backwards.** Everything above
is about whether a screen *finds* the right things. This one is about what it
then tells you to do. `check-restdefinition-shape.py`'s check D reported an
identifier missing from a findby collection item as actionable — "findby can
NEVER match" — which is right for a kind the runtime manages, and exactly
inverted for an **action-only** kind (`create`, no `get`, no `update`). Such a
kind reaches its create only while findby does *not* match: supply the
identifier and Observe answers `exists=true, upToDate=true`, the create is never
dispatched, and the CR reports Ready having done nothing. `openstack-ironic-
operator-kog` followed that advice and every bare-metal transition — deploy,
undeploy, manage, provide, inspect, clean — became a silent no-op for a day
(their #65). Nothing errored.

Three things this changed here, none of which a passing test would have forced:

- D is **informational** on an action-only kind, and says why supplying the
  identifier would be the harmful fix. The row stays, because the identifier
  really is unreadable and `additionalStatusFields` under it really will not
  populate — unreadable and *must be fixed* are different claims.
- Check **F** is new: an action-only kind whose findby *can* match has an
  unreachable create. It counts a `responseTransform` as making findby
  matchable, because adding one is precisely what broke ironic.
- D's "can NEVER match" is a claim about the **match policy**, not an
  identifier. `identifiersMatchPolicy` defaults to OR, which "adopts on the
  FIRST declared identifier whose value agrees and never looks at the rest"
  (`rest-dynamic-controller`'s `restResources.go`). Under OR with another
  identifier readable, findby still matches — the cost is discrimination, not
  function, and that is informational.

**And the fixture is where this hid.** Both of the D self-test arms this repo
shipped were `create` + `findby` and nothing else — the action-only shape — and
they asserted D actionable *and* asserted a `responseTransform` as the remedy.
The test did not merely miss the defect; it would have **defended** it against a
correct patch, and it read as coverage because the fixture was minimal and the
assertion was green. A fixture that is minimal is not neutral: it picks a shape,
and the shape it picks is the one the assertion is really about. The block is
six arms now, one of which exists only to stop F collapsing into "no
`responseTransform` allowed" — same findby, same transform, a kind that also
declares `get`, not flagged. All six were mutation-tested: five separate
mutations of the new logic, each killed by the arm that claims to pin it.

## A field's LOCATION is part of the claim (the same day, the same function)

#148 fixed check D's advice and, in the same patch, read
`identifiersMatchPolicy` from the **wrong level of the document**. It is a
per-verb field:

```
spec.resource.verbsDescription[].identifiersMatchPolicy   enum [AND, OR]
spec.resource.identifiersMatchPolicy                      <- what #148 read
```

Measured on the generated CRD (`ogen.krateo.io_restdefinitions.yaml`) at the
version this repo pins, 0.52.0, and again at 0.61.0, 0.62.0 and `main` —
identical at all four, with a CEL rule on the verb item that only makes sense
on a per-verb field: `self.action == 'findby' || !has(self.identifiersMatchPolicy)`,
message *"identifiersMatchPolicy can only be set for 'findby' actions"*.

The resource level has no such property, and the resource object sets no
`x-kubernetes-preserve-unknown-fields` (the CRD's five are all on
`*ApiRef.extras` and `fieldMapping.defaultIfAbsent`). So a key placed there is
an unknown field in a structural schema: **the apiserver prunes it, and the
apply succeeds.** Two separate failures, in the same direction:

- the screen could never see a declared `AND`, so every manifest screened as
  `OR`, and D printed its *non-actionable* "findby can still match via the other
  identifier" over manifests where findby could never match at all. Fail-quiet —
  the exact direction #148 existed to fix.
- an author who writes the key at the resource level gets a successful apply and
  no policy. `kubectl apply` returning 0 is a claim about admission, not about
  what the cluster kept.

**And the fixture encoded the same wrong location**, so the arm that asserted
`AND` asserted nothing. That is the third instance in one day of a fixture
defending the bug it was supposed to catch, and it happened *after* the rule
above was written — knowing the rule is not the same as applying it to the patch
in front of you. `_rd()` now takes `policy_at`, so a test can place the key where
the apiserver prunes it, where CEL refuses it, or where it works; the arm that
would have caught this is the one placing `AND` where #148 read it and asserting
the screen does **not** treat it as a policy.

Check **G** is new, and it is about the *declaration*, never about matching: a
pruned key, a key on a non-findby verb, or a value outside the enum. Its three
messages must differ by cause, asserted in the self-test — "your policy is doing
nothing" and "your manifest will be rejected" are not the same news. An invalid
value also **suppresses D and F** for that kind and says so: advice conditioned
on a policy the cluster will never accept is advice about a document that cannot
exist.

**A typo'd value is not a fallback.** `rest-dynamic-controller` lowercases the
policy and normalises anything unrecognised to `or`, and its own comment says
that branch is unreachable because the enum reaches both the generated CRD and
the chart's copy. Reading that branch as a fallback is what made the old code
`.upper()` the value — which screened `and`, a value the enum **refuses**, as a
working `AND` policy. The comment there notes that *"two readers reached the
opposite conclusion from this code in one day"*; we were both of them.

**A flag typo must not run a different, passing program.** `--self-test` (the
hyphenated spelling, which is what every other script in this directory uses)
matched no branch in the hand-rolled `sys.argv` scan, fell through to the real
screen, printed a green result and exited 0. It cost an hour of believing six
new arms had passed. Now `argparse` with `parse_args()` — not
`parse_known_args()`, which reintroduces exactly the fall-through — so an
unrecognised flag exits 2 before anything runs (measured: `--self-test` → 2,
`--selftest` → 0). Note the inconsistency that caused it is still here: every
`.sh` probe takes `--self-test` and this script takes `--selftest`. The hard
failure is the fix; matching spellings would be a second change.

Eight mutations of the new logic, each killed by the arm that claims to pin it,
run in-repo against a baseline asserted clean **in the same location** — the
contamination from the last round was mutants running from the scratchpad, where
unrelated arms fail in every run. Whole loop: 0.6 CPU-seconds.

## A document can be SILENT, and this screen read silence as agreement (check H)

A peer ran a read-only probe against a real Nova 2026.1 (`min_version 2.1 /
version 2.103`) and measured three mismatches against what this chart declares.
All three were invisible to a green screen, and they split into **two classes
with different cures** — which is the part worth keeping, because only one of
them is screenable at all.

**Class 1: the document says nothing where the field would be.** `GET /flavors`
returns only `[id, links, name]`, not the `ram`/`vcpus`/`disk` we declare — they
live on the item GET and on `/flavors/detail`. And `quota_set` comes back with
`id`, not the `tenant_id` we declare as ComputeQuota's identifier. Neither is a
live defect: we declare no list verb and no findby, and identifiers are read
from the CR rather than from the vendor's response. But neither was *caught*
either, and the reason is the same for both — **our OAS declares nothing at
those two locations**, so every check comparing a declaration against them
passed because there was nothing to disagree with.

That is now **check H**, informational by construction. It fires when a declared
identifier or `additionalStatusField` does not resolve in the read response AND
that response contains a node the document leaves empty. On this chart it fires
exactly once, on `quota tenant_id`.

**A scalar leaf is not silence**, and that distinction is why `silent_nodes` is
a function rather than a `not schema.get("properties")` test. `{"type":
"string"}` has no properties because strings do not have properties — it is
fully declared. A predicate that missed that would fire on every leaf of every
document and bury the one row that means something. The self-test pins it, along
with the inverse: a field genuinely **absent** from a populated schema is a
disagreement, check B owns it, and it stays actionable. Only an empty location
is unverifiable.

**And H fires on four fixtures written for other checks, which is a finding
about this file.** Each of them stubs its response body — `{"type": "object"}`,
or `{"quota_set": {"type": "object"}}` — because each was written to exercise a
different check and the body was beside the point. That stub *is* the shape H
reports. So this corpus was full of the silent-document shape and not one arm
had ever noticed: ironic's refinement of the fixture rule, playing out on the
fixtures themselves. Their expectations gained H rather than the fixtures
gaining filler, because the silence is real and padding it out would have
removed the only evidence that H reaches the shape it was built for.

**Class 2: the two documents agree, and both are wrong about the vendor. Nothing
here can ever catch this one.** We declared `server_group.project_id` as an
`additionalStatusField`, and the OAS declared `project_id` on
`ServerGroupSummary`. They agreed. Nova gates `project_id` and `user_id` behind
**microversion 2.13**, and this chart sends no microversion header on any path —
0 occurrences, against 28 for `X-Auth-Token` — so Nova serves every request at
`DEFAULT_API_VERSION = _MIN_API_VERSION = '2.1'` and both keys are absent from
every response we will ever see. Read from the guard, not from the API
reference, which documents the newest microversion:
`nova/api/openstack/compute/server_groups.py` for the 2.13 gate and
`nova/api/openstack/wsgi.py` for what a header-less request resolves to. The
field is now removed, with that citation beside it.

`policies` is **correct** and stays: the same function switches to `policy` +
`rules` only at 2.64, so at 2.1 Nova returns the `policies` list we declare.
That question had been left open pending a write to a live cloud; it was
answerable from the vendor's source all along, which is the cheaper authority
and the one that does not need anybody's cluster.

**The runtime is silent in the same direction, which is what makes a declared
field that never arrives cost nothing visible.**
`rest-dynamic-controller`'s `populateStatusFields` reads each declared field and,
when it is not found, simply continues — no error, no event, no log line, at
either of its two sites. So the CR reported Ready and Synced with a status field
that could never populate. A silent document and a silent runtime compose into a
claim nothing can contradict, which is this directory's whole subject in one
sentence. The upstream session is filing the signal; H is our half.

**What the two classes mean for how much a green screen is worth.** Class 1 the
screen can now name. Class 2 it cannot, ever — generation cannot either, because
the document *does* declare the field. Only the vendor's response or the
vendor's source settles it. So the honest reading of a clean run on this chart
is "no document-to-document disagreement", which is what the last line of the
output already says, and is strictly less than "the manifests match Nova".

**Eight mutations of H, run in-repo against a baseline asserted clean in the
same location. Seven killed by the arm that claims to pin them; one killed by a
crash, which is a different and weaker guarantee** — removing the `reader is not
None` guard makes the message's own `reader.get("method")` raise, so that guard
is enforced structurally and its arm documents the intent rather than defending
it. Worth saying plainly instead of reporting 8/8 and moving on.

The run also caught a **vacuous arm of exactly the kind this file keeps
relearning**. "declared SCALAR leaves are not silence" first declared both
identifiers in the read body — so under the mutation that makes scalars count as
silence, every node went silent, the unconfirmed list came out empty anyway, no
row appeared, and the arm **passed with the guard it is named after removed**.
Review did not catch it; mutation did. The fixture now keeps one identifier out
of the read body, which is what makes the row appear the moment scalars are
miscounted. An arm that cannot express its own defect is worse than no arm,
because it licenses the words "mutation-tested".
