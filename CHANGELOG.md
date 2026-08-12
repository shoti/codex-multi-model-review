# Changelog

All notable user-visible changes are documented here. This project follows
[Semantic Versioning](https://semver.org/).

## Unreleased

### Fixed

- Block repair before provider invocation when the remaining attempt allowance
  cannot reach mandatory confirmation, or when a last-chance Claude call is
  below a medium/high-confidence historical stop recommendation. Preserve the
  evidence in the preflight artifact without consuming an attempt, including
  for an underfunded final confirmation attempt.
- Make external-I/O amplification inside changed loops an explicit reviewer and
  Codex-triage concern instead of an unbenchmarked note or speculative test gap.
- Isolate every provider in a per-attempt input directory so resumed reviewers
  cannot read peer reports, Codex triage, workflow summaries, or run metadata.
- Restore tracked `export-ignore` files to immutable snapshots from raw Git
  blobs without invoking repository-configured checkout filters.
- Clear all matching ancestor deferrals when a later lineage decision resolves
  the item, normalize legacy analytics timestamps, and make missing-workflow
  gate errors actionable.
- Reclaim dead provider-attempt reservations, expose live reservations in
  status/headroom, release them during stale-run recovery, and serialize review
  creation so concurrent preflights cannot duplicate a round.
- Redact configured secret shapes from persisted provider failures and stderr,
  scan complete non-binary files plus deleted YAML separator lines, and keep
  exact sensitive-approval identities stable.
- Cache workflow-audit metadata and lineage indexes for one report, batch
  evidence-memory searches, report malformed rebuild inputs as skipped, and
  preserve the original finding ID when a legacy finding becomes an
  observation.
- Compile every runtime module in CI and buffer unittest output so failures are
  not buried in successful command diagnostics.

### Added

- Record the exact plugin name/version, bundle root, runner path, and runner
  SHA-256 in diagnostics, status output, and every run artifact.
- Produce concise, evidence-aware GitHub PR descriptions after an explicitly
  authorized commit and push, with separate shapes for production-evidenced
  fixes, ordinary fixes, and new features.
- Treat an explicit commit-and-push request as authority to create the branch's
  PR with the generated description or update its open PR, including target
  verification, existing-body protection, private body files, post-write
  verification, and strict exclusion of unrelated PR mutations or extra scopes.
- Add provenance-bound `--exclude-snapshot-path` support for exact unchanged
  tracked sensitive files, with pinned contracts, freshness/resume validation,
  manifest disclosure, and mandatory Codex coverage compensation.
- Add schema-11 content-hash identities and explicit lineage reuse for already
  inspected sensitive-content findings; new or changed findings still require a
  one-shot scan token.
- Add structured reviewer observations with mandatory Codex acknowledgment so
  risk-relevant concerns cannot disappear into free-form Notes.
- Add confirmation recovery-headroom warnings and an audited, increase-only
  `workflow raise-provider-attempt-limit` command.
- Add explicit working-tree, immutable-commit, and attested-commit bindings,
  plus separate source-gate `ready` and `deployment_ready` status.
- Require recorded formatter, lint/static-check, and full relevant local-test
  evidence after fixes before another provider call, including across linked
  successor workflows, and allow successors to reuse the pinned lineage
  contract without resetting that gate. Evidence satisfies the gate for the
  exact reviewed fingerprint and is required again after later source changes.

- Add usage-aware `continue` and consolidated `gate` commands. Continuation is
  read-only by default, can consume one provider-review step only with explicit
  authorization or an `auto` provider-use policy, and never fabricates triage
  or a Codex verdict.
- Add provider authentication/resource metadata, quota cooldown evidence,
  per-provider attempt ceilings and atomic reservations across successor
  lineages, plus explicit `unknown` remaining allowance when a CLI does not
  expose it.

- Scan every file in the complete outgoing reviewer snapshot, bind approved
  redacted findings to that snapshot's content fingerprint, and reject direct
  reusable finding IDs and broad sensitive-path overrides.
- Protect a 10% Claude provider-overrun reserve and enforce a history-backed
  minimum viable provider budget before launch.
- Classify legacy finals without the structured Codex verdict contract as
  untrusted in verification, workflow status, and workflow audit.

- Add structured reviewer coverage declarations, persist reviewer notes and
  unreviewed changed paths, expose coverage analytics, and require explicit
  Codex compensation before incomplete confirmation coverage can finalize.
- Separate preflight-blocked attempts from failed reviews and provider
  invocation failures in workflow status and analytics.

- Add lineage-wide budget accounting so linked successors retain every
  ancestor's reported spend and active reservations.
- Add a private, dependency-free SQLite evidence index with rebuild, status,
  and search commands; retrieval remains Codex-only and post-review.
- Add one-round supplemental rechecks for focused questions about unchanged
  finalized content, with explicit non-authoritative `supplemental.json` output.
- Add conservative review-mode recommendations and lineage-level analytics for
  outcomes, cost, duration, and single-repository run finals.
- Add explicit workflow states and completion so finalized work no longer
  appears indefinitely active.
- Separate closed-workflow analytics from workflows that merely have a
  repository run final, and reject `supersede --by` budget mismatches.
- Normalize explicitly no-impact/no-action low-severity items into non-gating
  observations instead of requiring false triage work, while retaining medium,
  high, and blocker items as findings, persisting observations for audit, and
  treating observation-only `PASS_WITH_FINDINGS` reports as clean.
- Add pinned `fast`, `balanced`, and `deep` workflow modes that adapt repair
  limits and Claude effort while retaining mandatory confirmation.
- Break out review-mode runtime, spend, success, finding, and test-gap metrics
  in local analytics so the adaptive policy can be tuned from real outcomes.
- Add `disable <provider> --lock` so an unavailable provider cannot be restored
  accidentally by a one-run override.
- Add provider-reported token analytics by provider, model, review mode, and
  phase, plus artifact-byte totals by mode and phase, without estimating
  unavailable usage.
- Add opt-in compact output for analytics, workflow status, and evidence search;
  full JSON remains the default and compact rendering falls back when larger.
- Rank evidence memory across titles, locations, evidence, actions, and
  verification so concise symbol/path/topic queries can retrieve verified work.
- Separate explicit adaptive-mode lineage evidence from modes inferred for
  legacy workflows, and report unclassified run records and distributions.
- Add a read-only `workflow audit` that identifies pending triage, unclosed run
  finals, failed workflows, and stale incomplete work without mutating history.
- Add advisory historical API-equivalent estimates for the current patch and
  record the estimate before allowance-consuming reviews without changing
  effort or stops automatically.
- Add explicit useful/irrelevant/mixed feedback for attached memory candidates
  and report memory recall, structural matches, similarity, and assessments.
- Extract dependency-free token, artifact, and distribution primitives into a
  focused metrics module while keeping workflow orchestration in the runner.

- Preserve every failed/resumed reviewer attempt and archive its provider
  artifacts so analytics and workflow spend include paid retries.
- Add explicit one-resume Claude effort and budget overrides, plus a typed
  `budget_exhausted` failure category.
- Add `run --reuse-contract` for drift-proof confirmation rounds and record
  changed paths excluded by task filters.
- Flag legacy resumed artifacts whose overwritten attempt history cannot be
  reconstructed, instead of presenting their spend as complete.
- Resume partial runs against their original immutable snapshot without
  reinvoking successful reviewers.
- Enforce a configurable cumulative Claude budget across every workflow.
- Add explicit successor workflow lineage for intentional post-confirmation or
  contract changes.
- Add fingerprint-bound, one-shot approvals for inspected secret-scan findings.
- Add local evidence analytics for workflows, providers, failures, spend, and
  decisions.
- Validate the configured Kimi model alias before a review starts.

### Changed

- Disabled providers now report `disabled`, enabled but unprobed providers
  report `not_probed`, and neither is labeled ready. Successful Claude runs
  persist redacted authentication/resource classification, while workflow
  coverage headlines name only providers that actually returned evidence.
- Reviewer Notes are limited to neutral context; possible defects, missing
  tests, risks, and coverage limitations must use their structured sections.

- New workflows use provider attempts and observed quota/readiness state rather
  than a cumulative dollar gate. Claude's required USD-denominated print-mode
  cap is retained and labeled as a client-side API-price equivalent, not
  subscription billing. Legacy workflows retain their original semantics for
  verification compatibility.

### Security

- Fail runs closed when Claude reports an API-price equivalent beyond either
  the protected legacy lineage reservation or, for allowance-aware workflows,
  its per-call emergency stop plus the 10% safety reserve. Preserve other
  reviewers' reports as audit evidence and give non-resumable recovery guidance
  that matches the lifecycle contract.

- Overlay task-scoped working-tree changes onto `--base` snapshots even when a
  branch file is reverted exactly to its base content, preventing reviewers
  from inspecting the stale `HEAD` version, and apply the same sensitive-path,
  symlink, content, and manifest coverage to those overlay-only files.
- Refuse to start Claude when less than `$0.25` remains in the lineage budget,
  avoiding paid overhead-only attempts that cannot produce a valid report.
- Prevent resumed attempts from replacing earlier reported usage and thereby
  weakening the cumulative workflow budget.
- Refuse a blind Claude resume after budget exhaustion until the operator
  chooses an explicit retry policy.
- Require Claude's JSON-schema report contract and normalize contradictory
  clean verdicts fail-closed.
- Preserve typed provider failures when terminal handling records an error.
- Avoid duplicate secret findings from overlapping patch and source scans while
  retaining coverage for deleted lines.
- Reject new runs and resumes under superseded workflows, and preserve partial
  state when an unexpected exception interrupts resume.
- Reserve workflow budget atomically across concurrent runs, recreate stale
  ephemeral snapshots on resume, narrow model-error classification, and retain
  simultaneous provider and report-contract failures in analytics.
- Serialize workflow supersession with budget reservation and release so
  lineage changes cannot lose concurrent accounting updates.
- Serialize overlapping resume attempts for one artifact across snapshot,
  provider, and evidence writes.

### Changed

- Make the documented `--reuse-contract` confirmation command scope-neutral so
  it works for pinned `--base` and `--commit` workflows as well as
  `--uncommitted` workflows.
- Allow a clean reviewed `--base` branch to resolve as committed-equivalent and
  be attested to its checked-out HEAD, with actionable failure diagnostics.

- Skip CLI and readiness probes for disabled providers in `status` and plain
  `doctor`.
- Reject budget-exhaustion retries that lower both Claude effort and budget.

- Accept an explicit scope selector with `--reuse-contract` when it resolves to
  the pinned scope, matching the documented confirmation command while still
  rejecting scope drift.
- Let path-filtered commit reviews ignore unrelated working-tree changes while
  retaining task-scoped cleanliness checks.
- Report newly started or superseding workflows with zero runs as active in
  `workflow status`, while still rejecting genuinely unknown workflow IDs.
- Extracted the reviewer report contract and parser into a focused module.
- Clarified the reviewer report contract so inspection-budget limitations are
  recorded in Notes instead of producing an empty `PASS_WITH_FINDINGS`.
- Recognize the first root commit as content-equivalent to a reviewed unborn
  working tree when paths and file contents match exactly.

## 0.1.0 - 2026-07-30

### Added

- Independent, read-only Claude, Antigravity, and optional Kimi review adapters.
- Immutable repository snapshots with task-scoped patches and fingerprints.
- Persistent, lock-safe finding and test-gap triage.
- Bounded repair rounds followed by mandatory confirmation.
- Commit-aware freshness verification and multi-repository workflow gates.
- Provider readiness, usage, cost, failure, and active-run diagnostics.
- Secret and sensitive-path screening with exact-match waivers.
- Public Codex marketplace metadata, documentation, and dependency-free CI.
- MIT licensing.

### Security

- External snapshot symlinks fail closed and cannot be bypassed by the broad
  sensitive-path override.
- Live doctor probes run from an empty temporary directory.
