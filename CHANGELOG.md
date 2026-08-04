# Changelog

All notable user-visible changes are documented here. This project follows
[Semantic Versioning](https://semver.org/).

## Unreleased

### Added

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

### Security

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
