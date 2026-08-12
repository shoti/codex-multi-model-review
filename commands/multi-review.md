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
   not ready. Use `mm-review doctor --live` before the first allowance-consuming run
   in a session or after a provider failure.
   Antigravity readiness must prove authenticated model access, not only that
   `agy` exists on PATH.
   If a crashed process leaves an orphaned running artifact, verify the recorded
   process is gone and use `mm-review recover --run <run-dir>`.
6. Derive task-specific `--path` filters so unrelated dirty files are excluded.
   Inspect the runner's excluded-path notice and add any changed dependency the
   reviewed behavior needs.
   If an unchanged tracked recognized sensitive file such as `.npmrc` is
   irrelevant but present in the full snapshot, use the exact
   `--exclude-snapshot-path` flag. Inspect and record its provenance; never
   exclude a changed or task-relevant path.
7. Select applicable risks and a review profile. Risks include auth, backfill,
   db-write, email-send, email-deliverability, external-api, migration,
   security, and trading.
8. Select `--review-mode fast` only for a small, localized, low-risk change;
   use `balanced` for ordinary work and `deep` whenever a risk label applies or
   the behavior spans components. Every mode retains mandatory confirmation.
   `mm-review recommend` may be used as a conservative advisory; explicit risk
   labels always keep the recommendation at `deep`.
9. If the target, intended behavior, or production impact is ambiguous, ask
   before proceeding.

## Plan

State repositories, path filters, scope, risk profiles, and enabled reviewers.
Create one workflow ID for the task with a deliberate per-provider attempt
ceiling. Successful and failed attempts follow every linked successor;
supersession never resets task usage.
Claude, Antigravity, and Kimi receive fresh independent read-only sessions when
enabled. Codex remains implementer, finding verifier, and final gate.
Run the bundled runner with Python 3.12 or newer; verify the interpreter rather
than assuming the macOS `/usr/bin/python3` is supported.

## Commands

1. Run `mm-review workflow start --review-mode <fast|balanced|deep>
   --max-provider-attempts <count>`, then run
   `--phase repair` in each affected
   repository with the same workflow ID, explicit `--path` filters, task
   intent, risks, review profile, and any provider override. Round numbering is
   automatic.
   If secret screening reports intentional test material, inspect it using
   `mm-review scan` with the same scope and paths, then use its one-shot token.
   The scan covers and fingerprints the complete outgoing repository snapshot,
   including unchanged tracked files; direct IDs and broad overrides are not
   accepted.
   On later lineage rounds, use `--reuse-lineage-sensitive-approvals` only for
   exact schema-11 content-hash matches; new or changed findings need a new
   one-shot token.
2. Read every reviewer report completely.
   If a run is `partial`, keep the source unchanged and use `mm-review resume`
   so only failed reviewers are retried. If Claude reached its native
   API-equivalent stop, pass
   a larger one-resume `--claude-max-budget-usd` and/or lower
   `--claude-effort` without reducing the existing stop; do not repeat a
   weaker cap blindly.
   The USD denomination is Claude CLI's native stop and an API-price equivalent,
   not proof of subscription billing.
3. Independently trace every finding, test gap, and structured observation
   against the actual code path.
   Record every result with `mm-review decide` or one atomic
   `mm-review decide-batch`; model agreement alone is not evidence.
4. Fix only accepted findings, keeping changes surgical.
5. Run relevant tests and behavior-level checks. For DB writes, migrations,
   backfills, and external APIs, verify exact operation shape and postconditions.
   After a fix, run formatter, lint/static checks, and the full relevant local
   suite before another provider call, then record the results with
   `--local-verification`.
6. After a fix changes the scoped source, update the original accepted finding
   to `fixed` with verification (or an accepted test gap to `covered`). The
   runner rejects either resolved transition while the reviewed scoped bytes
   are unchanged. Confirm the repair has no pending, accepted, or uncertain
   decisions. Any scoped source change makes the round stale; run another repair
   if needed.
7. After the selected mode's repair limit, run one `--phase confirmation
   --reuse-contract` with no planned source changes. Only that confirmation can
   be finalized. If scoped source changes after confirmation, explicitly use
   `workflow supersede` and review under its successor. Contract reuse and
   post-fix local-verification requirements follow the linked lineage; use
   `--reuse-contract` when the successor intentionally keeps the same scope.
   The successor inherits every repository from the superseded lineage; each
   inherited repository needs a fresh successor final before workflow closure.
   Evidence satisfies the gate for the exact reviewed fingerprint and must be
   refreshed after later source changes.
   Keep scope, path filters, risks, review profile, and task text identical
   within one workflow.
   Check `continue` for confirmation recovery-headroom warnings. If needed,
   explicitly increase the ceiling with `workflow raise-provider-attempt-limit
   --to <count> --reason <reason>`; never hide the change or lower the ceiling.
8. Use `mm-review continue <workflow-id>` for a read-only next-action plan.
   Add `--execute-review` only when provider allowance may be consumed. Use
   `mm-review gate <workflow-id>` to consolidate Codex finalization,
   verification, optional commit attestation, and workflow closure.

Never allow an external reviewer to edit the working tree. Do not commit, push,
open a PR, deploy, or change production state without separate authorization.

## Verification

Before finishing:

- confirm no accepted blocker or high-severity finding remains;
- verify the relevant checks pass, or state exact failures and test gaps;
- inspect the final diff for correctness, scope, secrets, and unintended files;
- perform the final Codex review;
- run `mm-review gate <workflow-id>` after confirmation triage and Codex's
  evidence-backed final review; the equivalent manual sequence remains
  `finalize`, `verify`, then `workflow finalize`;
- if a reviewed working tree is later committed with user authorization, run
  `mm-review attest-commit --run <run-dir> --commit HEAD` and verify again.
- distinguish a fresh local `ready` gate from `deployment_ready`; neither state
  proves live runtime or external-side-effect success without separate evidence.
- when the user explicitly authorizes commit and push, provide a concise,
  paste-ready GitHub PR description after pushing. Do not treat that authority
  as permission to open or update a PR.

## Summary

Report:

- workflow ID, final round, reviewer models that actually succeeded, scope,
  paths, exclusions, and risk profiles;
- aggregate reviewer runtime, attempts, tokens, API-price equivalent, and turns
  from workflow status;
- accepted and fixed findings;
- rejected, uncertain, covered, or deferred items with evidence;
- verification commands and results;
- the final freshness-checked gate status;
- remaining risks or test gaps.

For an additional focused question about an unchanged fresh final, use one
`run --supplemental-of <final-run> --task <question>`. Triage, finalize, and
verify its `supplemental.json`, but never present it as a replacement final
gate. Historical evidence memory may assist Codex only after fresh reviewers
finish; do not expose it in reviewer prompts.

## Next Steps

Suggest commit or merge only when the result is ready, but do not perform either
without explicit permission.

After an authorized commit and push, select the PR-description structure that
matches the work:

- evidence-based fix: `Production evidence` and `Fix`;
- fix without production evidence: `Summary` and `Fix`;
- new feature: `Summary` and `What changed`.

Keep only verified, durable context: the problem and impact, causal evidence or
root cause, and the resulting behavior or invariants. Omit routine test lists,
file inventories, review metadata, and incidental implementation detail. Use
exact timestamps, counts, or identifiers only when they materially prove
causality or scope and are safe to publish. Never invent production evidence.
