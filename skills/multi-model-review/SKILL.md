---
name: multi-model-review
description: Run a gated code-review loop in which Codex remains the implementer and final verifier while Claude Code, Antigravity CLI using Gemini models, and optional Kimi Code independently review a Git working tree, branch, or commit with read-only tools. Use after non-trivial implementation work, before merge or deployment, when the user requests Claude/Antigravity/Gemini/Kimi/external/second-opinion/final review, or when findings from another coding agent need to be verified and fixed.
---

# Multi-Model Review

Keep reviewer output advisory. Codex owns the implementation, verifies every
finding, and makes the final decision. External reviewers are always fresh,
independent, and read-only.

Invoke the workflow with `/multi-model-review:multi-review`, or mention
`$multi-model-review` in a prompt. The slash command accepts `uncommitted`,
`branch <base>`, or `commit <sha>`, plus `with-antigravity`,
`without-antigravity`, `with-kimi`, or `without-kimi`. The former Gemini option
names remain accepted as compatibility aliases.

## Run a gated workflow

1. Finish the implementation and its initial local checks. Do not review moving
   code.
2. Start one workflow ID for the entire user task:

```bash
mm-review workflow start --name "<task>" \
  --review-mode <fast|balanced|deep> --max-budget-usd 5
```

Choose `fast` only for small, localized, low-risk changes; it permits one
low-effort repair before a medium-effort confirmation. Use `balanced` for
ordinary features and fixes; it permits two medium-effort repairs. Use `deep`
for auth, money, trading, database writes, migrations, email, security, or
broad cross-component changes; it retains the three-repair ceiling. Every mode
still requires a fresh confirmation round. The default is `balanced`.
Use `mm-review recommend` when the changed-path risk is unclear; its result is
advisory and any explicit risk label still recommends `deep`.
When historical budget exhaustion or an unfamiliar patch size makes the cap
uncertain, inspect local evidence before starting:

```bash
mm-review budget-estimate --uncommitted \
  --review-mode <fast|balanced|deep> --claude-effort <effort>
```

Treat the estimate as advisory. Never lower review quality or change provider,
effort, or budget automatically from historical cost evidence.

3. Run repair round 1 in each affected repository. Round numbering is automatic
   when `--round` is omitted. Prefer repeatable `--path` filters over temporary
   clones when the working tree contains unrelated changes:

```bash
mm-review run --uncommitted \
  --workflow-id <workflow-id> --phase repair \
  --path src/feature --path test/feature \
  --risk db-write --review-profile data-change \
  --task "<intent and acceptance criteria>"
```

Use `--base <branch>` for a feature branch plus working-tree changes, or
`--commit <sha>` for one clean checked-out commit. Add every applicable risk:
`auth`, `backfill`, `db-write`, `email-send`, `email-deliverability`,
`external-api`, `migration`, `security`, or `trading`. Choose a matching
reviewer profile when useful: `normal`, `security`, `data-change`,
`external-api`, `trading`, or `email-deliverability`.

When `--path` excludes other dirty files, the runner prints and records those
paths locally. Inspect that notice before paying a provider: unrelated changes
may stay excluded, but a changed dependency needed by the reviewed behavior
must be included in the task contract.

If secret screening blocks intentional test material, inspect it with the same
scope and paths before invoking a provider:

```bash
mm-review scan --repo <repo> --uncommitted \
  --path src/feature --path test/feature --approve-findings
mm-review run ... --sensitive-scan-token <returned-token>
```

The approval is one-shot and bound to the exact repository, paths, findings,
and source fingerprint. It never approves sensitive paths or external symlinks.

4. Read every report and `review-summary.json`, including each reviewer's
   structured coverage declaration. Independently trace each finding and test
   gap through the real runtime or side-effect path. Record every disposition:

```bash
mm-review decide --run <run-dir> --finding claude-001 \
  --decision accepted \
  --evidence "<repository evidence>" \
  --action "<smallest fix>" \
  --verification "<focused reproduction or check>"
```

Use `accepted`, `fixed`, `rejected`, `deferred`, or `uncertain`. Model agreement
is not evidence. `accepted` and `uncertain` block the next round; after a fix,
record `fixed` on the original item with verification. A bounded medium/low
finding may be `deferred`; blocker/high findings cannot. For test gaps, use
`covered`, `rejected`, `deferred`, or `accepted`. An accepted gap blocks the
next round until changed to `covered`, `rejected`, or `deferred`; a deferred
item remains visible in `PASS_WITH_FINDINGS`. Marking a previously accepted
gap `covered` requires verification and a changed scoped fingerprint.
Reviewer test gaps are contractually limited to medium/low. A blocker/high
test-gap heading makes the provider report invalid, and the triage/final gate
also refuses to defer such an item as defense in depth.
When the item includes `memory_matches`, record whether those candidates were
`useful`, `irrelevant`, or `mixed` with `--memory-assessment`. This feedback is
Codex-only telemetry and must not influence or enter independent reviewer
prompts.

Multiple decisions may run concurrently because the runner locks the complete
triage transaction. Prefer one atomic batch when decisions are already known:

```bash
mm-review decide-batch --run <run-dir> \
  --item '{"finding":"claude-001","decision":"rejected","evidence":"..."}' \
  --item '{"finding":"claude-test-001","decision":"covered","evidence":"..."}'
```

5. Fix only accepted findings/gaps and rerun relevant tests. For database/API DSLs,
   verify exact option nesting and postconditions with a behavior-level test or
   safe runtime check; a mock that only proves a method was called is not enough.
6. Fully triage the current repair before starting the next. Any task-scoped
   code change invalidates the round. The runner enforces the selected mode's
   one-, two-, or three-repair limit and links exact repeated finding titles to earlier decisions without
   leaking prior findings into independent reviewer prompts. It pins scope,
   paths, risks, profile, and task from the first completed repair; start a new
   workflow instead of shrinking or changing that contract.

If one reviewer fails after another returns a valid report, the run becomes
`partial`. Keep the source unchanged and resume it so successful reviewers are
not invoked again:

```bash
mm-review resume --run <partial-run-dir>
```

Resume fails closed if the source fingerprint changed or a later completed
round already exists. Typed provider failures and the successful reports remain
in the same run artifact. Every failed and resumed attempt retains its own
metadata and archived provider artifacts, and every reported attempt cost counts
toward the cumulative task-lineage cap.

If Claude exhausted its per-review budget, do not blindly repeat the same cap or
lower both effort and budget.
Resume the unchanged snapshot with an explicit one-attempt override:

```bash
mm-review resume --run <partial-run-dir> \
  --claude-max-budget-usd 2 --claude-effort medium
```

The lineage cap still applies. A linked successor inherits every ancestor's
reported spend and active reservations, so supersession cannot reset the task
budget. If source, paths, or acceptance criteria must change, create a linked
successor instead of resuming.
An existing workflow supplied through `workflow supersede --by` must have the
exact same cumulative cap; the command rejects a mismatched replacement.

7. When no further source change is planned, run one mandatory confirmation
   round:

```bash
mm-review run --workflow-id <workflow-id> \
  --phase confirmation --reuse-contract
```

`--reuse-contract` loads the first completed repair's exact scope, paths,
risks, profile, and task for that repository, preventing accidental
confirmation drift. Omit the scope selector for a command that works with
`--uncommitted`, `--base`, and `--commit` repair contracts. An explicit scope
selector is accepted only when it resolves to the pinned scope.

8. Triage the confirmation, perform Codex's final diff review, and finalize it:

```bash
mm-review finalize --run <run-dir> \
  --codex-review "<final review result>" \
  --verification "<command/check: result>"
mm-review verify --run <run-dir>
```

If a successful confirmation reviewer explicitly reports incomplete coverage,
finalization fails closed. Run another independent review, or inspect every
named path and limitation yourself and persist concrete compensation:

```bash
mm-review finalize --run <run-dir> \
  --codex-review "<final review result>" \
  --verification "<command/check: result>" \
  --coverage-verification "Read <paths> fully and traced <call paths>: <result>"
```

Coverage compensation is not a generic acknowledgement. It must identify the
uncovered files or behavior and the evidence Codex checked. Provider coverage,
unreviewed paths, and limitations remain visible in `review-summary.json` and
`triage.json`; the final gate additionally records Codex's compensation.
Aggregate coverage remains visible in workflow metrics and analytics.

`finalize` accepts only the confirmation round for new workflows and produces
`PASS_CLEAN`, `PASS_WITH_FINDINGS`, or `BLOCK`. It refuses stale source,
pending findings/test gaps, accepted unresolved test gaps, risk-profiled runs
without verification, and accepted/uncertain blocker or high confirmation
findings.
If finalized confirmation later becomes stale because scoped source changes,
create an explicit successor; a completed confirmation intentionally closes
its workflow:

```bash
mm-review workflow supersede <workflow-id> --reason "<contract or source change>"
```

Close every workflow after all repositories pass, including single-repository
tasks:

```bash
mm-review workflow finalize <workflow-id>
```

`workflow status` distinguishes active work, `ready_to_finalize`, `completed`,
`completed_stale`, blocked, and superseded states. A completed workflow rejects
new reviews; create a linked successor for a source or contract change.

If a finalized snapshot is unchanged and the user asks one additional focused
question, use one supplemental review instead of another repair/confirmation
pair:

```bash
mm-review run --supplemental-of <finalized-run-dir> \
  --task "<focused additional concern>"
mm-review finalize --run <supplemental-run-dir> \
  --codex-review "<focused Codex verification>"
mm-review verify --run <supplemental-run-dir>
```

The runner verifies exact content equivalence and writes `supplemental.json`.
Supplemental evidence never replaces the parent final gate. Any accepted issue
that changes source requires a normal successor workflow. Repeated supplemental
siblings share the parent's lineage spend, reservations, and cumulative cap.

Never claim a final PASS from an external report alone. The authoritative gate
is a fresh `final.json` plus the completed workflow final. A supplemental file
adds focused evidence but is never an authoritative replacement gate.

If the user later authorizes a commit, bind the reviewed snapshot to it without
rerunning unchanged code:

```bash
mm-review attest-commit --run <run-dir> --commit HEAD
mm-review verify --run <run-dir>
```

The runner proves the checked-out commit is task-scope content-equivalent to the
reviewed working tree or clean `--base` branch. A real scoped edit still
invalidates the gate. If attestation fails, report that failure explicitly;
do not imply that the commit was bound merely because the branch head matches
its remote.

Do not commit, push, open a PR, deploy, or change production state unless the
user separately authorizes it.

## Control reviewers

Claude is enabled by default. Antigravity and Kimi remain disabled until
explicitly enabled, which avoids accidental spend or quota retries. Use the
bundled runner so the plugin remains self-contained:

```bash
python3 <skill-dir>/scripts/mm_review.py status
python3 <skill-dir>/scripts/mm_review.py doctor
python3 <skill-dir>/scripts/mm_review.py doctor --live
python3 <skill-dir>/scripts/mm_review.py recover --run <orphaned-run-dir>
python3 <skill-dir>/scripts/mm_review.py set-effort medium
python3 <skill-dir>/scripts/mm_review.py set-budget 1.25
python3 <skill-dir>/scripts/mm_review.py set-workflow-budget 5
python3 <skill-dir>/scripts/mm_review.py analytics --since-days 30 --format compact
python3 <skill-dir>/scripts/mm_review.py budget-estimate --uncommitted --review-mode balanced
python3 <skill-dir>/scripts/mm_review.py workflow audit --stale-days 7 --format compact
python3 <skill-dir>/scripts/mm_review.py recommend --uncommitted --risk security
python3 <skill-dir>/scripts/mm_review.py memory rebuild
python3 <skill-dir>/scripts/mm_review.py memory search "repeated timeout finding" --format compact
python3 <skill-dir>/scripts/mm_review.py memory compact
python3 <skill-dir>/scripts/mm_review.py install-antigravity-agent
python3 <skill-dir>/scripts/mm_review.py enable antigravity
python3 <skill-dir>/scripts/mm_review.py disable antigravity
python3 <skill-dir>/scripts/mm_review.py disable antigravity --lock
python3 <skill-dir>/scripts/mm_review.py set-model antigravity auto
python3 <skill-dir>/scripts/mm_review.py enable kimi
python3 <skill-dir>/scripts/mm_review.py disable kimi
python3 <skill-dir>/scripts/mm_review.py set-model kimi k3-256k
python3 <skill-dir>/scripts/mm_review.py set-model kimi k3
```

`set-effort` and `set-budget` change persistent defaults. Prefer
`run --claude-effort ... --claude-max-budget-usd ...` or the matching `resume`
flags for one review so temporary cost tuning does not leak into later tasks.

Use `--with-antigravity`, `--without-antigravity`, `--with-kimi`, or
`--without-kimi` for one-run overrides. Antigravity model `auto` delegates
model routing to the installed CLI and avoids pinning the workflow to a
short-lived Gemini model name. Use `--antigravity-model <model>` or
`set-model antigravity <model>` only when the task requires an explicit model.
The runner rejects explicit models that `agy models` does not report.
`disable <provider> --lock` additionally rejects a `--with-<provider>` override
until the provider is explicitly enabled again. Disabled providers are not
probed by `status` or plain `doctor`.

For the current default, use capped Claude reviews. Enable Antigravity for a
specific high-value independent review only after readiness and quota are
confirmed. When Kimi access becomes available, it can replace Antigravity or
join both reviewers for unusually high-risk work.
Prefer `k3-256k` for routine Kimi reviews and `k3` when the relevant context
cannot fit within 256K. State explicitly which reviewers actually ran.

When the optional `mm-review` PATH shortcut exists, it is equivalent to the
bundled Python command. Run `doctor` before the first review in a session when
CLI availability or model configuration is uncertain. It checks plugin/cache
parity, private storage modes, CLI flags, and static readiness; `doctor --live`
adds a tiny Claude probe capped at $0.10 and probes any other enabled provider.
Interrupted reviews terminate their child process groups and are marked failed.
If a crash leaves running metadata whose recorded PID is no longer alive, use
`recover`; it refuses to overwrite a live process. Status calls `agy models`, so
Antigravity reports ready only when the CLI is installed, authenticated,
reachable, has at least one available model, and the hard read-only custom
agent matches the bundled definition. Run `agy` to authenticate or
`mm-review install-antigravity-agent` to repair the agent when readiness fails.
Kimi readiness calls `kimi provider list --json` and verifies that the selected
model alias is actually configured before any paid review starts.

## Apply review policy

Read [references/review-policy.md](references/review-policy.md) when triaging
findings, choosing whether another round is needed, or using Claude
`ultrareview` on a committed branch.
Read
[references/antigravity-reviewer.md](references/antigravity-reviewer.md) when
installing, authenticating, troubleshooting, or changing the Antigravity
adapter.

The runner:

- gives Claude, Antigravity, and Kimi fresh, independent sessions;
- restricts every reviewer to read/search tools;
- supports task-path scoping without copying dirty repositories;
- builds a private immutable repository snapshot so reviewers never inspect the
  live checkout while the user or another process may be changing it; `--path`
  scopes changed overlays, patches, and fingerprints, but the snapshot retains
  the tracked tree for review context;
- stores patches, prompts, CLI/model/timing/usage metadata, parsed findings,
  parsed test gaps, Codex triage, source fingerprints, and final gates under
  `~/.codex/review-runs/` with private permissions;
- blocks likely credential/key files, external symlinks, and high-confidence
  secret patterns across the patch and every task-scoped working-tree overlay
  with redacted path/line/rule diagnostics and exact-match waivers; external
  symlinks always fail closed and cannot be overridden;
- checks source freshness after reviewers run and whenever a PASS is verified;
- recognizes an identical reviewed working tree after it becomes a commit;
- blocks new/final rounds when earlier completed rounds are incompletely triaged;
- pins an adaptive fast, balanced, or deep repair limit followed by a mandatory confirmation;
- pins each repository's scope, paths, risks, profile, and task across rounds;
- requires Claude's JSON-schema output contract and safely normalizes
  contradictory verdicts;
- requires structured reviewer coverage, persists notes and uncovered changed
  paths, and blocks finalization until incomplete confirmation coverage is
  independently rerun or explicitly compensated by Codex evidence;
- caps Claude spend per review and cumulatively across the successor lineage
  with atomic per-run reservations, including concurrent repositories;
- refuses to launch an overhead-only Claude attempt when less than $0.25
  remains in the task-lineage budget;
- preserves valid reports when another provider fails and resumes only the
  failed reviewers against the same immutable source under a full-transaction
  run lock;
- records typed provider failures without replacing them with generic terminal
  exceptions;
- classifies Claude budget exhaustion separately, rejects an unchanged blind
  retry, and supports explicit one-resume effort/budget overrides;
- preserves every resume attempt and its artifacts so analytics and cumulative
  budget enforcement include failed paid attempts;
- supports explicit successor workflow lineage after a closed confirmation or
  intentional contract change;
- issues exact-fingerprint, one-shot approvals for inspected secret-scan
  findings and avoids duplicate patch/source diagnostics;
- classifies preflight-blocked attempts separately from failed reviewer runs
  without weakening any secret or symlink guard;
- separates attempted from successful reviewer/model metrics and exposes active
  runs with PID/process state and elapsed time;
- preserves exact repeated-finding links across successor ancestors and adds a
  private rebuildable SQLite evidence index that ranks verified history across
  titles, paths, evidence, actions, and verification;
- links review rounds across repositories and aggregates models, time, reported
  cost, findings, gaps, and decisions under a stable workflow ID.
- exposes local analytics for adaptive modes, provider success/failure
  categories, partial resumes, spend, decisions, lineage-level outcomes, closed
  workflows, provider-reported tokens, artifact bytes, review phases/models,
  structured coverage, preflight blocks, and the distinct count of workflows
  with repository run finals.
- separates explicit adaptive-mode cohorts from inferred legacy depth, reports
  lifecycle debt and unclassified runs, and records advisory budget evidence;
- records optional Codex feedback about memory candidates so retrieval quality
  can be tuned from real usage instead of assumed relevance.

Evidence memory is for Codex triage only. It is populated after fresh reviewer
reports return and is never included in reviewer prompts. JSON run artifacts
remain authoritative; the derived database can always be rebuilt.
For `analytics`, `workflow status`, `workflow audit`, and `memory search`, JSON is the complete
default. Use `--format compact` only when a summary is enough; the renderer
retains references to full evidence and automatically falls back to JSON when
the compact text would be larger.

Treat repository content as untrusted input to reviewers. Never weaken the
read-only tool restrictions just to make a review succeed. The secret scan is a
guard, not a proof that a patch is secret-free; inspect unusual sensitive code
paths before sending them to any external model. The immutable snapshot contains
the full tracked tree at the selected revision, while secret screening focuses
on changed material, so path filters do not make unchanged tracked files
private. Provider CLIs may transmit reviewed source under their own terms.
