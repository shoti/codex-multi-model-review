# Changelog

All notable user-visible changes are documented here. This project follows
[Semantic Versioning](https://semver.org/).

## Unreleased

### Changed

- Clarified the reviewer report contract so inspection-budget limitations are
  recorded in Notes instead of producing an empty `PASS_WITH_FINDINGS`.

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
