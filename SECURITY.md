# Security policy

## Supported versions

Security fixes are applied to the latest version on the `main` branch. Until
the project publishes stable releases, older snapshots are not maintained.

## Report a vulnerability

Please use GitHub's private vulnerability reporting flow:

https://github.com/shoti/codex-multi-model-review/security/advisories/new

Include:

- the affected command and version or commit;
- a minimal reproduction;
- the security impact;
- whether external provider data, credentials, or local files are exposed;
- any suggested mitigation.

Do not include live credentials, customer data, or proprietary source code.
Use synthetic examples and redact tokens.

## Scope and trust model

This plugin sends immutable repository snapshots to external model providers
only when a review is run. A `--path` filter scopes changed overlays, patches,
and fingerprints, but the snapshot retains the tracked Git tree for review
context. Provider CLIs use their own authentication, data-handling terms,
quotas, and billing.

The built-in secret scan focuses on changed material. It reduces accidental
disclosure but cannot prove that a snapshot is safe to share or detect every
secret in unchanged tracked files.

Reviewers are restricted to read/search operations and work from private
immutable snapshots. Codex remains responsible for validating findings and for
the final gate. Users should still inspect sensitive changes before invoking an
external provider.
