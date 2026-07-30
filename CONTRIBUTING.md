# Contributing

Thanks for improving Multi-Model Review. Small, focused pull requests are the
easiest to verify and review.

## Development setup

The runner uses only the Python standard library. You need:

- Git;
- Python 3.12 or newer;
- Linux or macOS.

Claude Code, Antigravity, and Kimi Code are optional for development. The test
suite replaces them with local fake executables and never spends provider
credits.

Clone the repository and run the checks:

```bash
git clone https://github.com/shoti/codex-multi-model-review.git
cd codex-multi-model-review
python3 -m py_compile skills/multi-model-review/scripts/mm_review.py
python3 skills/multi-model-review/scripts/test_mm_review.py
git diff --check
```

## Pull requests

Before opening a pull request:

1. Explain the user-visible problem and the intended behavior.
2. Add or update a focused regression test for behavior changes.
3. Keep external reviewer tools read-only and keep paid calls out of tests.
4. Update the skill and README when a command or workflow contract changes.
5. Check the diff for secrets, credentials, private paths, and generated files.
6. List the exact verification commands and results in the pull request.

Please avoid unrelated refactors or formatting churn. New runtime dependencies
need discussion in an issue before implementation.

## Architecture invariants

The following are deliberate safety properties:

- Codex owns implementation, verification, and the final decision.
- Reviewers inspect immutable repository snapshots under task-scoped contracts
  with read-only tools.
- Every finding and test gap receives a persisted disposition.
- Repair rounds are bounded and followed by a mandatory confirmation.
- Source fingerprints make stale final gates fail closed.
- Credentials and likely secret material are blocked before provider review.
- Claude reviews have a configured spend cap; other providers are opt-in.

Changes that weaken an invariant need explicit rationale, tests, and a migration
story for existing review artifacts.

## Reporting security issues

Do not publish exploitable details in a normal issue. Follow
[SECURITY.md](SECURITY.md) instead.
