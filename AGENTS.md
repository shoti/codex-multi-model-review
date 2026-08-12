# Repository guidance

This repository contains a Codex plugin and one bundled skill. Keep changes
small, auditable, and compatible with Python 3.12 or newer on Linux and macOS.

## Important paths

- `.codex-plugin/plugin.json`: plugin identity and install-surface metadata.
- `.agents/plugins/marketplace.json`: repository marketplace entry.
- `skills/multi-model-review/SKILL.md`: the user-facing workflow contract.
- `skills/multi-model-review/scripts/mm_review.py`: the runner.
- `skills/multi-model-review/scripts/test_mm_review.py`: the dependency-free
  test suite.
- `skills/multi-model-review/references/`: reviewer and policy contracts.

## Change rules

- Keep Codex as implementer and final verifier; external reviewers are
  read-only and advisory.
- Preserve private immutable snapshots, explicit task scoping, source
  fingerprints, locked triage, bounded repair rounds, and mandatory
  confirmation.
- Do not weaken secret scanning, path containment, process cleanup, provider
  spend caps, or final freshness checks.
- Do not add a runtime dependency without discussing it first.
- Never invoke a paid provider from automated tests or CI. Use the existing
  fake CLI fixtures and isolated temporary homes.
- When changing a CLI contract, update help text, the skill, README, fixtures,
  and tests together.
- When changing the plugin source used by a local installation, refresh its
  cache-busted version and reinstall it before the final end-to-end check.

## Verification

Run:

```bash
python3 -m py_compile \
  skills/multi-model-review/scripts/mm_review.py \
  skills/multi-model-review/scripts/review_contract.py \
  skills/multi-model-review/scripts/evidence_memory.py \
  skills/multi-model-review/scripts/review_metrics.py
python3 skills/multi-model-review/scripts/test_mm_review.py
python3 -m json.tool .codex-plugin/plugin.json >/dev/null
python3 -m json.tool .agents/plugins/marketplace.json >/dev/null
git diff --check
```

Provider-backed smoke tests are optional, cost money, and require explicit
credentials. Do not run them during ordinary contribution checks.
