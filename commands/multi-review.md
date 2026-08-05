---
description: Run independent Claude, Antigravity, and optional Kimi reviews, record verified finding decisions, rerun after fixes, and produce a freshness-checked final gate.
argument-hint: "[uncommitted | branch <base> | commit <sha>] [with-antigravity | without-antigravity] [with-kimi | without-kimi] [paths ...]"
---

# Multi-Model Review

Use `$multi-model-review` to complete the review-and-fix loop for the current
repository. Do not stop after collecting reviewer reports.

## Preflight

1. Read the applicable `AGENTS.md` instructions.
2. Identify every affected Git repository and inspect each branch and
   working-tree state.
3. Select the scope from `$ARGUMENTS`:
   - default or `uncommitted`: staged, unstaged, and untracked changes;
   - `branch <base>`: the feature branch relative to `<base>`, plus current
     working-tree changes;
   - `commit <sha>`: one committed change.
4. Honor `with-antigravity`, `without-antigravity`, `with-kimi`, or
   `without-kimi` as one-run overrides. Otherwise use the saved reviewer
   configuration. The old Gemini names remain accepted as compatibility aliases.
   - Claude is the capped default. Enable Antigravity only after confirming
     quota/readiness for a review where the additional model is useful.
   - When Kimi is available, it can replace Antigravity or join both reviewers
     for unusually high-risk work.
   - State explicitly which reviewers actually ran.
5. Run `mm-review doctor` and stop if the plugin/cache or an enabled reviewer is
   not ready. Use `mm-review doctor --live` before the first paid-provider run
   in a session or after a provider failure.
   Antigravity readiness must prove authenticated model access, not only that
   `agy` exists on PATH.
   If a crashed process leaves an orphaned running artifact, verify the recorded
   process is gone and use `mm-review recover --run <run-dir>`.
6. Derive task-specific `--path` filters so unrelated dirty files are excluded.
   Inspect the runner's excluded-path notice and add any changed dependency the
   reviewed behavior needs.
7. Select applicable risks and a review profile. Risks include auth, backfill,
   db-write, email-send, email-deliverability, external-api, migration,
   security, and trading.
8. Select `--review-mode fast` only for a small, localized, low-risk change;
   use `balanced` for ordinary work and `deep` whenever a risk label applies or
   the behavior spans components. Every mode retains mandatory confirmation.
9. If the target, intended behavior, or production impact is ambiguous, ask
   before proceeding.

## Plan

State repositories, path filters, scope, risk profiles, and enabled reviewers.
Create one workflow ID for the task with a deliberate cumulative Claude budget.
Claude, Antigravity, and Kimi receive fresh independent read-only sessions when
enabled. Codex remains implementer, finding verifier, and final gate.

## Commands

1. Run `mm-review workflow start --review-mode <fast|balanced|deep>
   --max-budget-usd <cap>`, then run
   `--phase repair` in each affected
   repository with the same workflow ID, explicit `--path` filters, task
   intent, risks, review profile, and any provider override. Round numbering is
   automatic.
   If secret screening reports intentional test material, inspect it using
   `mm-review scan` with the same scope and paths, then use its one-shot token.
2. Read every reviewer report completely.
   If a run is `partial`, keep the source unchanged and use `mm-review resume`
   so only failed reviewers are retried. If Claude exhausted its budget, pass
   a larger one-resume `--claude-max-budget-usd` and/or lower
   `--claude-effort` without reducing the existing budget; do not repeat a
   weaker cap blindly.
3. Independently trace every finding and test gap against the actual code path.
   Record every result with `mm-review decide` or one atomic
   `mm-review decide-batch`; model agreement alone is not evidence.
4. Fix only accepted findings, keeping changes surgical.
5. Run relevant tests and behavior-level checks. For DB writes, migrations,
   backfills, and external APIs, verify exact operation shape and postconditions.
6. After a fix, update the original accepted finding to `fixed` with
   verification (or an accepted test gap to `covered`). Confirm the repair has
   no pending, accepted, or uncertain decisions. Any scoped source change makes
   it stale; run another repair if needed.
7. After the selected mode's repair limit, run one `--phase confirmation
   --reuse-contract` with no planned source changes. Only that confirmation can
   be finalized. If scoped source changes after confirmation, explicitly use
   `workflow supersede` and review under its successor. Keep scope, path
   filters, risks, review profile, and task text identical within one workflow.

Never allow an external reviewer to edit the working tree. Do not commit, push,
open a PR, deploy, or change production state without separate authorization.

## Verification

Before finishing:

- confirm no accepted blocker or high-severity finding remains;
- verify the relevant checks pass, or state exact failures and test gaps;
- inspect the final diff for correctness, scope, secrets, and unintended files;
- perform the final Codex review;
- run `mm-review finalize` and `mm-review verify` for every repository;
- for multi-repository work, run `mm-review workflow finalize`.
- if a reviewed working tree is later committed with user authorization, run
  `mm-review attest-commit --run <run-dir> --commit HEAD` and verify again.

## Summary

Report:

- workflow ID, final round, reviewer models, scope, paths, and risk profiles;
- aggregate reviewer runtime, reported cost, and turns from workflow status;
- accepted and fixed findings;
- rejected, uncertain, covered, or deferred items with evidence;
- verification commands and results;
- the final freshness-checked gate status;
- remaining risks or test gaps.

## Next Steps

Suggest commit or merge only when the result is ready, but do not perform either
without explicit permission.
