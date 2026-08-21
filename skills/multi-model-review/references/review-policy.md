# Review Policy

## Triage standard

Accept a finding only when repository evidence shows that the changed behavior
can violate an intended contract. Prefer a focused reproduction or regression
test. Reject style preferences, speculative future requirements, and findings
that ignore an existing guard elsewhere in the call path.

A low-severity observation that explicitly has no reachable impact and needs no
action is not a finding. Preserve it as non-gating audit evidence without
creating triage work. Never suppress a medium, high, or blocker item this way.

Use these severities:

- `blocker`: credential exposure, destructive data loss, unauthorized live
  side effect, or a change that is unsafe to merge under normal use.
- `high`: reachable correctness, security, concurrency, money, auth, migration,
  email-delivery, or production-operability defect.
- `medium`: reachable regression with bounded impact or an important missing
  test for changed risk-sensitive behavior.
- `low`: worthwhile but non-blocking improvement. Do not extend scope for it
  unless the user asks.

For every finding, record:

```text
Finding:
Reviewer:
Verdict: accepted | fixed | rejected | deferred | uncertain
Evidence:
Action:
Verification:
```

Persist that record with `mm-review decide` or `decide-batch`; do not leave the
only triage copy in chat. The runner locks concurrent decisions so one update
cannot overwrite another. Every parsed finding and test gap must have a
disposition before another round or finalization.

Use these test-gap decisions:

- `covered`: the required behavior assertion exists and passes;
- `rejected`: repository evidence shows the proposed gap is irrelevant;
- `accepted`: the gap is real and must be addressed before final PASS;
- `deferred`: the gap is real but explicitly retained as visible risk, producing
  at most `PASS_WITH_FINDINGS`.

If a gap was first accepted, `covered` requires concrete verification and a
changed task-scoped fingerprint. Use `rejected` instead when later evidence
shows that no new test was needed.

For findings, `accepted` means work remains and blocks the next round. After
the scoped source changes, update that original item to `fixed` with concrete
verification. Use `rejected` when evidence disproves it, or `deferred` only for
a bounded medium/low risk that should remain visible. Blocker/high findings
cannot be deferred. `uncertain` also blocks the next round.

## Behavior-level verification

For database writes, migrations, backfills, external APIs, framework DSLs,
email sends, auth, and trading paths, inspect exact payload and option nesting.
Verify the intended postcondition, not merely that a method was invoked.

Examples:

- a database write test should show the expected row/document changed, or
  assert the exact adapter contract including nested options;
- a backfill dry run should report bounded counts before any apply;
- an external API test should validate field names and response semantics;
- money, email, auth, and live side effects require the repository's existing
  safety gates and focused tests.

A green mock-based suite is not proof when the production adapter can silently
accept a malformed call as a no-op.

When changed control flow introduces or expands a loop, trace the helpers inside
it for hidden database, network, filesystem, broker, queue, or model I/O. Bound
the call count across the real cardinalities (for example recipients, cohorts,
steps, variants, retries, and pages). Reachable N-times or N-by-M external-I/O
amplification is a production-operability finding even without a benchmark; do
not reject the corresponding test gap as speculative until the helper I/O and
cardinality have been inspected.

## Coverage completeness

Every new reviewer report must declare whether it read every changed file and
completed the task-level trace. It must list unreviewed changed paths and any
time, context, budget, or tool limitation. These disclosures are evidence, not
repository findings, and do not manufacture a false test gap.

An incomplete repair may proceed to an independent confirmation, which can
close the missing coverage. An incomplete confirmation cannot be finalized
silently. Either obtain another review or use `--coverage-verification` to
record concrete Codex inspection of every uncovered path or behavior. A generic
statement such as "reviewed manually" is not sufficient evidence.

## Claim-to-evidence assurance

When a workflow pins acceptance criteria or critical invariants, treat them as
an immutable task contract separate from findings and file coverage. Reviewers
must declare criterion-level coverage, but their `verified` status is advisory
and never proves a claim by itself. Codex must attach a concrete repository,
test, artifact, or safe runtime evidence record for every claim against the
reviewed source fingerprint.

A critical invariant must be `verified` with fresh evidence or finalization
blocks. A non-critical criterion may be deliberately `deferred` only with
concrete evidence locating the gap and an explicit rationale; it remains in the
assurance artifact and limits the result to `PASS_WITH_FINDINGS`. Never infer a
numeric confidence score or count model agreement as evidence. Contract drift
requires an explicit linked successor, and legacy artifacts remain honestly
unassured rather than receiving synthesized claim coverage.

## Convergence gate

Run another external round when an accepted blocker/high finding caused code to
change, or when any task-scoped source changed after the reports were created.
The old fingerprint is evidence about the old code, not the final code.

Repair until:

- no accepted blocker/high findings remain;
- the remaining findings are rejected with evidence; or
- three repair rounds have completed.

Then run one independent confirmation round against the intended final source.
Do not use confirmation as another open-ended design pass. An accepted or
uncertain blocker/high confirmation finding fails convergence; triage lower
severity items normally. A source change after confirmation requires a new
workflow and another policy-compliant repair/confirmation sequence;
confirmation intentionally closes the old workflow.

The final machine-readable gate is:

- `PASS_CLEAN`: all findings were rejected with evidence, or none existed;
- `PASS_WITH_FINDINGS`: only accepted/uncertain/deferred medium or low findings
  remain;
- `BLOCK`: an accepted/uncertain blocker or high finding remains.

Codex must record its own explicit structured verdict at finalization. The
machine status is the more conservative result of that verdict and reviewer
triage across every completed round for the repository workflow. A Codex block
can never be represented as a passing artifact. Deferred and otherwise
unresolved earlier-round items remain in the final gate under run-qualified
IDs even when the confirmation reviewer does not repeat them. Deferred items
also carry across superseded task-lineage ancestors until a later matching
decision resolves the same kind and title. The final gate hashes this complete
triage set; changing any contributing decision makes the gate stale.

`mm-review verify` must confirm the final gate is fresh. Every task must also
run `mm-review workflow finalize` to close the workflow after confirming the
latest round in each repository and the complete triage history.
Final artifacts created before the structured Codex verdict contract are
untrusted even if their stored status says PASS. Verification, workflow status,
and workflow audit must fail closed and require a fresh structured review.

A supplemental review is permitted only for an unchanged passing final and one
focused additional question. It performs one fresh review and writes a
non-authoritative `supplemental.json`. If Codex accepts an issue that requires a
source change, open a normal linked successor and repeat repair plus
confirmation. Supplemental evidence never replaces or upgrades the parent
gate. All supplemental siblings share the parent task lineage's provider
attempts and active reservations.

Committing an unchanged reviewed working tree is a state transition, not a code
change. Use `mm-review attest-commit` to bind the final gate to the checked-out
equivalent commit. The command must reject changed scoped content.

At the three-repair limit, proceed to confirmation only if no further source
change is planned. Otherwise present unresolved disagreements to the user. Do
not silently pick the most confident-sounding model.

## Reviewer independence

Do not give one reviewer another reviewer's findings. Give each a fresh session,
the same task intent, the same patch, and the same repository access. Parallel
execution is preferred because it prevents one result from biasing the prompt
for the other.

Codex may compare reports only after both reviewers finish. Agreement raises
priority for investigation but does not prove the finding.

Codex-only evidence memory follows the same boundary: retrieve prior decisions
only after fresh independent reports return. Never include memory results or
earlier findings in reviewer prompts.

## Claude ultrareview

`claude ultrareview` is a cloud-hosted multi-agent branch or PR review. Use it
only when:

- the relevant changes are committed on a branch or available as a PR;
- the user wants the additional provider-allowance use and latency;
- uploading that branch to the configured Claude service is acceptable; and
- the normal local read-only review is not sufficient for the risk.

Do not use it as the default local working-tree reviewer. The normal runner can
review uncommitted changes without requiring a commit or push.

## Final human gate

External reviewers and Codex reduce risk but do not authorize merge, deploy,
schema changes, backfills, email sends, or live trades. Preserve the user's
existing approval boundaries.
