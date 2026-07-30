# Review Policy

## Triage standard

Accept a finding only when repository evidence shows that the changed behavior
can violate an intended contract. Prefer a focused reproduction or regression
test. Reject style preferences, speculative future requirements, and findings
that ignore an existing guard elsewhere in the call path.

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

`mm-review verify` must confirm the final gate is fresh. For tasks spanning
repositories, `mm-review workflow finalize` must confirm the latest round in
each repository and the complete triage history.

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

## Claude ultrareview

`claude ultrareview` is a cloud-hosted multi-agent branch or PR review. Use it
only when:

- the relevant changes are committed on a branch or available as a PR;
- the user wants the additional cost and latency;
- uploading that branch to the configured Claude service is acceptable; and
- the normal local read-only review is not sufficient for the risk.

Do not use it as the default local working-tree reviewer. The normal runner can
review uncommitted changes without requiring a commit or push.

## Final human gate

External reviewers and Codex reduce risk but do not authorize merge, deploy,
schema changes, backfills, email sends, or live trades. Preserve the user's
existing approval boundaries.
