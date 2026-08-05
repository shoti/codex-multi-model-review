#!/usr/bin/env python3
"""Focused regression tests for the multi-model review runner."""

from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from contextlib import redirect_stdout
from unittest import mock


SCRIPT_PATH = Path(__file__).with_name("mm_review.py")
SPEC = importlib.util.spec_from_file_location("mm_review", SCRIPT_PATH)
assert SPEC and SPEC.loader
MM = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MM
SPEC.loader.exec_module(MM)


def run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=check,
    )


def initialize_repo(root: Path) -> None:
    run(["git", "init", "-q"], cwd=root)
    run(["git", "config", "user.email", "review-test@example.invalid"], cwd=root)
    run(["git", "config", "user.name", "Review Test"], cwd=root)
    (root / "src").mkdir()
    (root / "src" / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "unrelated.txt").write_text("clean\n", encoding="utf-8")
    run(["git", "add", "src/feature.py", "unrelated.txt"], cwd=root)
    run(["git", "commit", "-qm", "initial"], cwd=root)


class RunnerUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.health_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.health_directory.cleanup)
        self.health_patch = mock.patch.object(
            MM,
            "PROVIDER_HEALTH_PATH",
            Path(self.health_directory.name) / "provider-health.json",
        )
        self.health_patch.start()
        self.addCleanup(self.health_patch.stop)

    def test_claude_review_has_effort_and_budget_caps(self) -> None:
        args = MM.build_parser().parse_args(
            [
                "run",
                "--with-claude",
                "--without-antigravity",
                "--without-kimi",
                "--claude-effort",
                "low",
                "--claude-max-budget-usd",
                "0.75",
            ]
        )
        config = json.loads(json.dumps(MM.DEFAULT_CONFIG))
        with (
            mock.patch.object(MM, "version_of", return_value="test"),
            mock.patch.object(MM.shutil, "which", return_value="/fake/claude"),
        ):
            reviewer = MM.reviewer_definitions(args, config)[0]
        self.assertEqual(
            reviewer.command[
                reviewer.command.index("--effort") + 1
            ],
            "low",
        )
        self.assertEqual(
            reviewer.command[
                reviewer.command.index("--max-budget-usd") + 1
            ],
            "0.75",
        )
        for invalid in ("nan", "inf"):
            invalid_args = MM.build_parser().parse_args(
                [
                    "run",
                    "--with-claude",
                    "--without-antigravity",
                    "--without-kimi",
                    "--claude-max-budget-usd",
                    invalid,
                ]
            )
            with self.assertRaisesRegex(MM.ReviewError, "positive finite"):
                MM.reviewer_definitions(invalid_args, config)

    def test_budget_exhaustion_retry_requires_a_material_policy_change(self) -> None:
        self.assertFalse(
            MM.materially_changes_claude_retry(
                previous_effort="medium",
                previous_budget=1.25,
                selected_effort="medium",
                selected_budget=1.25,
            )
        )
        self.assertFalse(
            MM.materially_changes_claude_retry(
                previous_effort="medium",
                previous_budget=1.25,
                selected_effort="high",
                selected_budget=1.0,
            )
        )
        self.assertTrue(
            MM.materially_changes_claude_retry(
                previous_effort="medium",
                previous_budget=1.25,
                selected_effort="medium",
                selected_budget=1.5,
            )
        )
        self.assertTrue(
            MM.materially_changes_claude_retry(
                previous_effort="medium",
                previous_budget=1.25,
                selected_effort="low",
                selected_budget=1.25,
            )
        )
        self.assertFalse(
            MM.materially_changes_claude_retry(
                previous_effort="medium",
                previous_budget=1.25,
                selected_effort="low",
                selected_budget=0.75,
            )
        )

    def test_non_finite_budget_is_rejected_from_config_and_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.json"
            config_path.write_text(
                '{"claude":{"max_budget_usd":NaN}}',
                encoding="utf-8",
            )
            with (
                mock.patch.object(MM, "CONFIG_PATH", config_path),
                self.assertRaisesRegex(MM.ReviewError, "positive number"),
            ):
                MM.load_config()
        with (
            mock.patch.object(
                MM,
                "load_config",
                return_value=json.loads(json.dumps(MM.DEFAULT_CONFIG)),
            ),
            mock.patch.object(MM, "write_config") as write_config,
            self.assertRaisesRegex(MM.ReviewError, "positive USD"),
        ):
            MM.set_budget_command(
                MM.build_parser().parse_args(["set-budget", "inf"])
            )
        write_config.assert_not_called()

    def test_doctor_live_reports_unready_provider_without_crashing(self) -> None:
        config = json.loads(json.dumps(MM.DEFAULT_CONFIG))
        config["antigravity"]["enabled"] = False
        config["kimi"]["enabled"] = False
        output = io.StringIO()
        with (
            mock.patch.object(MM, "load_config", return_value=config),
            mock.patch.object(
                MM,
                "plugin_install_parity",
                return_value=(True, "cache matches"),
            ),
            mock.patch.object(
                MM,
                "private_storage_permissions",
                return_value=(True, "private"),
            ),
            mock.patch.object(
                MM,
                "claude_cli_contract",
                return_value=(True, "flags verified"),
            ),
            mock.patch.object(
                MM,
                "provider_readiness",
                return_value=MM.ProviderReadiness(False, "quota cooldown"),
            ),
            mock.patch.object(
                MM,
                "reviewer_definitions",
                side_effect=MM.ReviewError("quota cooldown"),
            ),
            redirect_stdout(output),
        ):
            exit_code = MM.doctor_command(
                MM.build_parser().parse_args(["doctor", "--live"])
            )
        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 3)
        self.assertFalse(payload["ready"])
        live = next(
            check
            for check in payload["checks"]
            if check["name"] == "claude_live_probe"
        )
        self.assertFalse(live["ok"])
        self.assertEqual(live["failure_category"], "not_ready")

    def test_plugin_parity_accepts_non_personal_marketplace_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin_root = root / "source"
            cache_root = root / "cache"
            manifest_dir = plugin_root / ".codex-plugin"
            manifest_dir.mkdir(parents=True)
            manifest = {
                "name": "multi-model-review",
                "version": "0.1.0",
            }
            (manifest_dir / "plugin.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            (plugin_root / "payload.txt").write_text(
                "public marketplace fixture\n",
                encoding="utf-8",
            )
            installed = (
                cache_root
                / "codex-multi-model-review"
                / "multi-model-review"
                / "0.1.0"
            )
            installed.parent.mkdir(parents=True)
            shutil.copytree(plugin_root, installed)

            ok, detail = MM.plugin_install_parity(
                plugin_root=plugin_root,
                cache_root=cache_root,
            )

            self.assertTrue(ok)
            self.assertIn("marketplace codex-multi-model-review", detail)

    def test_plugin_parity_rejects_missing_and_mismatched_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin_root = root / "source"
            cache_root = root / "cache"
            manifest_dir = plugin_root / ".codex-plugin"
            manifest_dir.mkdir(parents=True)
            (manifest_dir / "plugin.json").write_text(
                json.dumps(
                    {
                        "name": "multi-model-review",
                        "version": "0.1.0",
                    }
                ),
                encoding="utf-8",
            )
            (plugin_root / "payload.txt").write_text(
                "current source\n",
                encoding="utf-8",
            )

            missing_ok, missing_detail = MM.plugin_install_parity(
                plugin_root=plugin_root,
                cache_root=cache_root,
            )
            self.assertFalse(missing_ok)
            self.assertIn("installed cache is missing", missing_detail)

            installed = (
                cache_root
                / "team-marketplace"
                / "multi-model-review"
                / "0.1.0"
            )
            installed.mkdir(parents=True)
            shutil.copytree(
                manifest_dir,
                installed / ".codex-plugin",
            )
            (installed / "payload.txt").write_text(
                "stale source\n",
                encoding="utf-8",
            )

            mismatch_ok, mismatch_detail = MM.plugin_install_parity(
                plugin_root=plugin_root,
                cache_root=cache_root,
            )
            self.assertFalse(mismatch_ok)
            self.assertIn("source differs", mismatch_detail)
            self.assertIn("team-marketplace", mismatch_detail)

    def test_doctor_live_uses_empty_probe_directory(self) -> None:
        config = json.loads(json.dumps(MM.DEFAULT_CONFIG))
        config["antigravity"]["enabled"] = False
        config["kimi"]["enabled"] = False
        seen_repositories: list[tuple[bool, list[Path]]] = []
        seen_budgets: list[float | None] = []

        def reviewer_definitions(
            args: argparse.Namespace, _config: dict[str, object]
        ) -> list[MM.Reviewer]:
            seen_budgets.append(args.claude_max_budget_usd)
            return [reviewer]

        def invoke(
            reviewer: MM.Reviewer,
            *,
            repo: Path,
            prompt: str,
            run_dir: Path,
            timeout_seconds: int,
        ) -> MM.ReviewResult:
            del prompt, timeout_seconds
            seen_repositories.append((repo == run_dir, list(repo.iterdir())))
            report_path = run_dir / f"{reviewer.name}.md"
            error_path = run_dir / f"{reviewer.name}.stderr.log"
            report_path.write_text(
                "# Verdict\nPASS_CLEAN\n\n# Findings\nNone.\n\n"
                "# Test gaps\nNone.\n",
                encoding="utf-8",
            )
            error_path.write_text("", encoding="utf-8")
            now = MM.utc_now()
            return MM.ReviewResult(
                name=reviewer.name,
                report_path=report_path,
                error_path=error_path,
                returncode=0,
                started_at=now,
                completed_at=now,
                duration_seconds=0.01,
                timed_out=False,
                usage=None,
            )

        reviewer = MM.Reviewer(
            name="claude",
            model="test",
            command=("claude",),
            environment={},
            cli_version="test",
        )
        output = io.StringIO()
        with (
            mock.patch.object(MM, "load_config", return_value=config),
            mock.patch.object(
                MM,
                "plugin_install_parity",
                return_value=(True, "cache matches"),
            ),
            mock.patch.object(
                MM,
                "private_storage_permissions",
                return_value=(True, "private"),
            ),
            mock.patch.object(
                MM,
                "claude_cli_contract",
                return_value=(True, "flags verified"),
            ),
            mock.patch.object(
                MM,
                "provider_readiness",
                return_value=MM.ProviderReadiness(True, "available"),
            ),
            mock.patch.object(
                MM,
                "reviewer_definitions",
                side_effect=reviewer_definitions,
            ),
            mock.patch.object(MM, "invoke_reviewer", side_effect=invoke),
            redirect_stdout(output),
        ):
            exit_code = MM.doctor_command(
                MM.build_parser().parse_args(["doctor", "--live"])
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(seen_repositories), 1)
        self.assertEqual(seen_repositories[0], (True, []))
        self.assertEqual(seen_budgets, [MM.DOCTOR_CLAUDE_BUDGET_USD])

    def test_claude_cli_contract_checks_required_flags(self) -> None:
        help_text = " ".join(
            (
                "--effort",
                "--max-budget-usd",
                "--json-schema",
                "--permission-mode",
                "--tools",
                "--safe-mode",
                "--no-session-persistence",
            )
        )
        with mock.patch.object(
            MM.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                ["claude", "--help"], 0, stdout=help_text, stderr=""
            ),
        ):
            ready, detail = MM.claude_cli_contract()
        self.assertTrue(ready, detail)

    def test_contract_fixtures_cover_success_and_provider_failures(self) -> None:
        fixture_dir = SCRIPT_PATH.parent / "fixtures"
        claude = json.loads(
            (fixture_dir / "claude-success.json").read_text(encoding="utf-8")
        )
        antigravity = json.loads(
            (fixture_dir / "antigravity-success.json").read_text(encoding="utf-8")
        )
        quota = (fixture_dir / "antigravity-quota-error.json").read_text(
            encoding="utf-8"
        )
        authentication = (fixture_dir / "provider-auth-error.json").read_text(
            encoding="utf-8"
        )
        empty = (fixture_dir / "provider-empty-success.json").read_text(
            encoding="utf-8"
        )
        self.assertEqual(
            MM.parse_review_report("claude", claude["result"])["verdict"],
            "PASS_CLEAN",
        )
        self.assertEqual(
            MM.parse_review_report(
                "antigravity", antigravity["response"]
            )["verdict"],
            "PASS_CLEAN",
        )
        self.assertEqual(
            MM.classify_provider_failure(
                returncode=1,
                timed_out=False,
                stdout=quota,
                stderr="",
            ),
            "quota",
        )
        self.assertEqual(
            MM.classify_provider_failure(
                returncode=1,
                timed_out=False,
                stdout=authentication,
                stderr="",
            ),
            "authentication",
        )
        self.assertEqual(
            MM.classify_provider_failure(
                returncode=0,
                timed_out=False,
                stdout="",
                stderr=empty,
            ),
            "empty_response",
        )
        reset_at = MM.quota_reset_at("Quota exhausted. Resets in 2h3m4s.")
        self.assertIsNotNone(reset_at)
        assert reset_at is not None
        remaining = (
            MM.dt.datetime.fromisoformat(reset_at)
            - MM.dt.datetime.now(MM.dt.timezone.utc)
        ).total_seconds()
        self.assertGreater(remaining, 2 * 60 * 60)
        MM.record_provider_failure(
            "antigravity",
            "quota",
            "Quota exhausted. Resets in 2h3m4s.",
        )
        self.assertIsNotNone(MM.active_provider_cooldown("antigravity"))
        MM.clear_provider_failure("antigravity")
        MM.record_provider_failure(
            "antigravity",
            "quota",
            "Quota exhausted. Resets in several hours.",
        )
        self.assertIsNotNone(MM.active_provider_cooldown("antigravity"))

    def test_confirmation_policy_limits_repairs_and_requires_sequence(self) -> None:
        repository = {"id": "repo-1"}
        repairs = [
            (
                Path(f"/private/tmp/run-{round_number}"),
                {
                    "run_id": f"run-{round_number}",
                    "repository": repository,
                    "status": "completed",
                    "round": round_number,
                    "phase": "repair",
                },
            )
            for round_number in range(1, 4)
        ]
        with mock.patch.object(MM, "workflow_runs", return_value=[]):
            with self.assertRaisesRegex(MM.ReviewError, "requires at least one"):
                MM.validate_workflow_phase(
                    "wf-test",
                    "repo-1",
                    phase="confirmation",
                    round_number=1,
                )
        with mock.patch.object(MM, "workflow_runs", return_value=repairs):
            with self.assertRaisesRegex(MM.ReviewError, "repair limit"):
                MM.validate_workflow_phase(
                    "wf-test",
                    "repo-1",
                    phase="repair",
                    round_number=4,
                )
            MM.validate_workflow_phase(
                "wf-test",
                "repo-1",
                phase="confirmation",
                round_number=4,
            )

    def test_review_modes_bound_repairs_and_keep_confirmation(self) -> None:
        self.assertEqual(
            MM.workflow_policy(2.5, "fast"),
            {
                "review_mode": "fast",
                "max_repair_rounds": 1,
                "confirmation_required": True,
                "repair_effort": "low",
                "confirmation_effort": "medium",
                "max_budget_usd": 2.5,
            },
        )
        repair = (
            Path("/private/tmp/run-1"),
            {
                "repository": {"id": "repo-1"},
                "status": "completed",
                "round": 1,
                "phase": "repair",
            },
        )
        with (
            mock.patch.object(MM, "workflow_runs", return_value=[repair]),
            mock.patch.object(MM, "workflow_max_repair_rounds", return_value=1),
            self.assertRaisesRegex(MM.ReviewError, "1-round repair limit"),
        ):
            MM.validate_workflow_phase(
                "wf-fast",
                "repo-1",
                phase="repair",
                round_number=2,
            )
        with (
            mock.patch.object(MM, "workflow_runs", return_value=[repair]),
            mock.patch.object(MM, "workflow_max_repair_rounds", return_value=1),
        ):
            MM.validate_workflow_phase(
                "wf-fast",
                "repo-1",
                phase="confirmation",
                round_number=2,
            )
        with tempfile.TemporaryDirectory() as temporary:
            workflows = Path(temporary)
            with mock.patch.object(MM, "WORKFLOWS_DIR", workflows):
                MM.create_workflow(
                    "wf-fast",
                    max_budget_usd=2.5,
                    review_mode="fast",
                )
                self.assertEqual(MM.workflow_max_repair_rounds("wf-fast"), 1)
                self.assertEqual(
                    MM.workflow_phase_effort("wf-fast", "repair"), "low"
                )
                self.assertEqual(
                    MM.workflow_phase_effort("wf-fast", "confirmation"),
                    "medium",
                )
                MM.safe_write_json(
                    workflows / "wf-legacy.json",
                    {
                        "workflow_id": "wf-legacy",
                        "policy": {
                            "max_budget_usd": 5.0,
                            "max_repair_rounds": 3,
                            "confirmation_required": True,
                        },
                    },
                )
                self.assertEqual(
                    MM.workflow_max_repair_rounds("wf-legacy"),
                    MM.MAX_REPAIR_ROUNDS,
                )
                self.assertIsNone(
                    MM.workflow_phase_effort("wf-legacy", "repair")
                )

    def test_initial_commit_is_equivalent_to_reviewed_unborn_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            run_dir = root / "run"
            repo.mkdir()
            run_dir.mkdir()
            run(["git", "init", "-q"], cwd=repo)
            run(
                ["git", "config", "user.email", "review-test@example.invalid"],
                cwd=repo,
            )
            run(["git", "config", "user.name", "Review Test"], cwd=repo)
            (repo / "release.txt").write_text(
                "reviewed initial content\n",
                encoding="utf-8",
            )
            scope = MM.Scope(
                "uncommitted",
                None,
                "staged, unstaged, and untracked changes",
            )
            paths = MM.changed_paths(repo, scope)
            source_fingerprint = MM.fingerprint(repo, scope, paths, ())
            metadata = {
                "repository": {
                    "root": str(repo),
                    "head": None,
                },
                "scope": {
                    "kind": scope.kind,
                    "value": scope.value,
                    "label": scope.label,
                },
                "path_filters": [],
                "paths": paths,
                "result_content_fingerprint": MM.content_fingerprint(repo, paths),
            }

            run(["git", "add", "release.txt"], cwd=repo)
            run(["git", "commit", "-qm", "initial"], cwd=repo)
            freshness = MM.freshness_status(
                run_dir,
                metadata,
                source_fingerprint,
            )

            self.assertTrue(freshness["fresh"])
            self.assertEqual(freshness["mode"], "committed-equivalent")
            self.assertEqual(
                freshness["commit"],
                run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip(),
            )

    def test_initial_commit_equivalence_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def prepare_case(
                name: str,
            ) -> tuple[Path, Path, dict[str, object], str]:
                repo = root / name
                run_dir = root / f"{name}-run"
                repo.mkdir()
                run_dir.mkdir()
                run(["git", "init", "-q"], cwd=repo)
                run(
                    [
                        "git",
                        "config",
                        "user.email",
                        "review-test@example.invalid",
                    ],
                    cwd=repo,
                )
                run(["git", "config", "user.name", "Review Test"], cwd=repo)
                (repo / "release.txt").write_text(
                    "reviewed initial content\n",
                    encoding="utf-8",
                )
                scope = MM.Scope(
                    "uncommitted",
                    None,
                    "staged, unstaged, and untracked changes",
                )
                paths = MM.changed_paths(repo, scope)
                source_fingerprint = MM.fingerprint(repo, scope, paths, ())
                metadata: dict[str, object] = {
                    "repository": {
                        "root": str(repo),
                        "head": None,
                    },
                    "scope": {
                        "kind": scope.kind,
                        "value": scope.value,
                        "label": scope.label,
                    },
                    "path_filters": [],
                    "paths": paths,
                    "result_content_fingerprint": MM.content_fingerprint(
                        repo, paths
                    ),
                }
                return repo, run_dir, metadata, source_fingerprint

            repo, run_dir, metadata, source_fingerprint = prepare_case(
                "path-mismatch"
            )
            (repo / "extra.txt").write_text("not reviewed\n", encoding="utf-8")
            run(["git", "add", "--all"], cwd=repo)
            run(["git", "commit", "-qm", "initial"], cwd=repo)
            self.assertFalse(
                MM.freshness_status(
                    run_dir, metadata, source_fingerprint
                )["fresh"]
            )

            repo, run_dir, metadata, source_fingerprint = prepare_case(
                "content-mismatch"
            )
            (repo / "release.txt").write_text(
                "tampered after review\n",
                encoding="utf-8",
            )
            run(["git", "add", "--all"], cwd=repo)
            run(["git", "commit", "-qm", "initial"], cwd=repo)
            self.assertFalse(
                MM.freshness_status(
                    run_dir, metadata, source_fingerprint
                )["fresh"]
            )

            repo, run_dir, metadata, source_fingerprint = prepare_case(
                "dirty-worktree"
            )
            run(["git", "add", "--all"], cwd=repo)
            run(["git", "commit", "-qm", "initial"], cwd=repo)
            (repo / "later.txt").write_text("unreviewed\n", encoding="utf-8")
            self.assertFalse(
                MM.freshness_status(
                    run_dir, metadata, source_fingerprint
                )["fresh"]
            )

            repo, run_dir, metadata, source_fingerprint = prepare_case(
                "non-root-head"
            )
            run(["git", "add", "--all"], cwd=repo)
            run(["git", "commit", "-qm", "initial"], cwd=repo)
            (repo / "second.txt").write_text("second commit\n", encoding="utf-8")
            run(["git", "add", "--all"], cwd=repo)
            run(["git", "commit", "-qm", "second"], cwd=repo)
            self.assertFalse(
                MM.freshness_status(
                    run_dir, metadata, source_fingerprint
                )["fresh"]
            )

    def test_accepted_prior_item_requires_resolution_before_next_round(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            MM.safe_write_json(
                run_dir / "triage.json",
                {
                    "findings": [
                        {
                            "id": "claude-001",
                            "kind": "finding",
                            "severity": "high",
                            "decision": "accepted",
                        }
                    ],
                    "test_gaps": [],
                },
            )
            metadata = {
                "run_id": "run-1",
                "repository": {"id": "repo-1"},
                "status": "completed",
                "round": 1,
                "source_fingerprint": "same",
            }
            with (
                mock.patch.object(
                    MM, "workflow_runs", return_value=[(run_dir, metadata)]
                ),
                self.assertRaisesRegex(MM.ReviewError, "fixed, rejected, or deferred"),
            ):
                MM.ensure_prior_rounds_triaged(
                    "wf-test", "repo-1", 2, "same"
                )
            triage = MM.read_json(run_dir / "triage.json")
            triage["findings"][0]["decision"] = "fixed"
            triage["findings"][0]["verification"] = "focused test passed"
            MM.safe_write_json(run_dir / "triage.json", triage)
            with (
                mock.patch.object(
                    MM, "workflow_runs", return_value=[(run_dir, metadata)]
                ),
                self.assertRaisesRegex(MM.ReviewError, "fingerprint is unchanged"),
            ):
                MM.ensure_prior_rounds_triaged(
                    "wf-test", "repo-1", 2, "same"
                )
            with mock.patch.object(
                MM, "workflow_runs", return_value=[(run_dir, metadata)]
            ):
                MM.ensure_prior_rounds_triaged(
                    "wf-test", "repo-1", 2, "changed"
                )
            MM.safe_write_json(
                run_dir / "triage.json",
                {
                    "findings": [],
                    "test_gaps": [
                        {
                            "id": "claude-test-001",
                            "kind": "test_gap",
                            "severity": "medium",
                            "decision": "covered",
                            "verification": "claimed test",
                            "decision_history": [
                                {"decision": "accepted"},
                                {"decision": "covered"},
                            ],
                        }
                    ],
                },
            )
            with (
                mock.patch.object(
                    MM, "workflow_runs", return_value=[(run_dir, metadata)]
                ),
                self.assertRaisesRegex(
                    MM.ReviewError, "accepted then marked covered"
                ),
            ):
                MM.ensure_prior_rounds_triaged(
                    "wf-test", "repo-1", 2, "same"
                )

    def test_review_contract_cannot_shrink_or_drop_risk(self) -> None:
        baseline = (
            Path("/private/tmp/repair"),
            {
                "repository": {"id": "repo-1"},
                "status": "completed",
                "round": 1,
                "created_at": MM.utc_now(),
                "scope": {
                    "kind": "uncommitted",
                    "value": None,
                    "label": "test",
                },
                "path_filters": ["src/feature.py"],
                "risks": ["db-write"],
                "review_profile": "data-change",
                "task": "Change the feature value.",
            },
        )
        with mock.patch.object(MM, "workflow_runs", return_value=[baseline]):
            MM.validate_review_contract(
                "wf-test",
                "repo-1",
                scope=MM.Scope("uncommitted", None, "test"),
                path_filters=("src/feature.py",),
                risks=("db-write",),
                review_profile="data-change",
                task="Change the feature value.",
            )
            with self.assertRaisesRegex(MM.ReviewError, "path_filters, risks"):
                MM.validate_review_contract(
                    "wf-test",
                    "repo-1",
                    scope=MM.Scope("uncommitted", None, "test"),
                    path_filters=("unrelated.txt",),
                    risks=(),
                    review_profile="data-change",
                    task="Change the feature value.",
                )
            with self.assertRaisesRegex(MM.ReviewError, "--reuse-contract"):
                MM.validate_review_contract(
                    "wf-test",
                    "repo-1",
                    phase="confirmation",
                    scope=MM.Scope("uncommitted", None, "test"),
                    path_filters=("src/feature.py",),
                    risks=("db-write",),
                    review_profile="data-change",
                    task="Describe the repair instead.",
                )

    def test_reused_base_and_commit_scopes_preserve_the_pinned_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            initialize_repo(repo)
            head = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
            for kind, label in (
                ("base", f"branch changes since main (merge-base {head})"),
                ("commit", "commit HEAD"),
            ):
                args = MM.build_parser().parse_args(
                    ["run", "--workflow-id", "wf-test", "--reuse-contract"]
                )
                scope = MM.resolve_pinned_scope(
                    args,
                    repo,
                    {"kind": kind, "value": head, "label": label},
                )
                self.assertEqual(
                    {
                        "kind": scope.kind,
                        "value": scope.value,
                        "label": scope.label,
                    },
                    {"kind": kind, "value": head, "label": label},
                )

    def test_commit_scope_ignores_changes_outside_path_filters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            initialize_repo(repo)
            head = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
            (repo / "REVIEW.md").write_text("local notes\n", encoding="utf-8")
            (repo / "unrelated.txt").write_text("dirty\n", encoding="utf-8")
            args = MM.build_parser().parse_args(
                ["run", "--commit", "HEAD", "--path", "src"]
            )

            scope = MM.resolve_scope(args, repo, ("src",))

            self.assertEqual(scope.kind, "commit")
            self.assertEqual(scope.value, head)
            in_scope_untracked = repo / "src" / "new_file.py"
            in_scope_untracked.write_text("VALUE = 2\n", encoding="utf-8")
            with self.assertRaisesRegex(MM.ReviewError, "task-scoped"):
                MM.resolve_scope(args, repo, ("src",))
            in_scope_untracked.unlink()
            (repo / "src" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
            with self.assertRaisesRegex(MM.ReviewError, "task-scoped"):
                MM.resolve_scope(args, repo, ("src",))

    def test_workflow_status_exposes_active_run(self) -> None:
        active = (
            Path("/private/tmp/running-review"),
            {
                "run_id": "run-active",
                "workflow_id": "wf-active",
                "repository": {"id": "repo-1", "name": "repo"},
                "status": "running",
                "round": 2,
                "phase": "confirmation",
                "started_at": MM.utc_now(),
                "reviewers": {"claude": {"model": "sonnet"}},
            },
        )
        with (
            mock.patch.object(MM, "workflow_runs", return_value=[active]),
            mock.patch.object(MM, "workflow_requires_confirmation", return_value=True),
        ):
            status, ready = MM.workflow_status("wf-active")
        self.assertFalse(ready)
        self.assertEqual(status["active_runs"][0]["phase"], "confirmation")
        self.assertEqual(status["active_runs"][0]["reviewers"], ["claude"])
        self.assertEqual(status["metrics"]["running_runs"], 1)

    def test_workflow_status_reports_persisted_workflow_before_first_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workflows_dir = Path(temporary)
            with mock.patch.object(MM, "WORKFLOWS_DIR", workflows_dir):
                MM.create_workflow(
                    "wf-new",
                    name="new workflow",
                    max_budget_usd=3.0,
                )
                with (
                    mock.patch.object(MM, "workflow_runs", return_value=[]),
                    mock.patch.object(MM, "latest_workflow_runs", return_value=[]),
                ):
                    status, ready = MM.workflow_status("wf-new")

        self.assertFalse(ready)
        self.assertEqual(status["state"], "active")
        self.assertEqual(status["workflow"]["name"], "new workflow")
        self.assertEqual(status["metrics"]["run_count"], 0)
        self.assertEqual(status["repositories"], [])

    def test_workflow_status_rejects_unknown_workflow_without_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with (
                mock.patch.object(MM, "WORKFLOWS_DIR", Path(temporary)),
                mock.patch.object(MM, "workflow_runs", return_value=[]),
                mock.patch.object(MM, "latest_workflow_runs", return_value=[]),
                self.assertRaisesRegex(MM.ReviewError, "Unknown workflow"),
            ):
                MM.workflow_status("wf-missing")

    def test_failed_only_workflow_cannot_be_ready_or_finalized(self) -> None:
        failed = (
            Path("/private/tmp/failed-review"),
            {
                "run_id": "run-failed",
                "workflow_id": "wf-failed",
                "repository": {"id": "repo-1", "name": "repo"},
                "status": "failed",
                "round": 1,
                "phase": "repair",
                "reviewers": {
                    "claude": {
                        "model": "sonnet",
                        "exit_code": 1,
                        "duration_seconds": 1,
                    }
                },
            },
        )
        with (
            mock.patch.object(MM, "workflow_runs", return_value=[failed]),
            mock.patch.object(MM, "workflow_requires_confirmation", return_value=True),
        ):
            status, ready = MM.workflow_status("wf-failed")
        self.assertFalse(ready)
        self.assertFalse(status["ready"])
        self.assertEqual(status["metrics"]["failed_runs"], 1)
        self.assertEqual(
            status["metrics"]["successful_reviewer_invocations"],
            0,
        )
        invalid_metadata = {
            **failed[1],
            "run_id": "run-invalid",
            "reviewers": {
                "claude": {
                    "model": "sonnet",
                    "exit_code": 0,
                    "duration_seconds": 1,
                    "verdict": "UNKNOWN",
                }
            },
        }
        metrics = MM.workflow_metrics(
            [(Path("/private/tmp/invalid-review"), invalid_metadata)]
        )
        self.assertEqual(metrics["successful_reviewer_invocations"], 0)
        self.assertEqual(metrics["failed_reviewer_invocations"], 1)
        self.assertEqual(metrics["successful_models"], [])

    def test_structured_medium_gap_is_low_without_risk_profile(self) -> None:
        parsed = MM.parse_review_report(
            "claude",
            """# Verdict
PASS_WITH_FINDINGS

# Findings
None.

# Test gaps
## [medium] Add a bounded regression test
- Needed test: cover the helper
- Risk: bounded coverage debt
""",
        )
        gap = parsed["test_gaps"][0]
        self.assertEqual(gap["reported_severity"], "medium")
        self.assertEqual(gap["severity"], "low")
        self.assertIn("no changed risk profile", gap["severity_adjustment"])

    def test_blocker_or_high_test_gap_violates_report_contract(self) -> None:
        parsed = MM.parse_review_report(
            "claude",
            """# Verdict
PASS_WITH_FINDINGS

# Findings
None.

# Test gaps
## [high] Out-of-contract gap
- Needed test: verify a critical behavior
- Risk: provider emitted an invalid severity
""",
            risk_profiled=True,
        )
        self.assertEqual(
            parsed["invalid_test_gap_severities"],
            ["claude-test-001"],
        )
        triage = {
            "findings": [],
            "test_gaps": [
                {
                    **parsed["test_gaps"][0],
                    "decision": "pending",
                }
            ],
        }
        with self.assertRaisesRegex(MM.ReviewError, "cannot be deferred"):
            MM.apply_triage_decision(
                triage,
                identifier="claude-test-001",
                decision="deferred",
                evidence="Keep visible.",
                action="Address later.",
                verification=None,
            )

    def test_final_gate_blocks_accepted_and_high_test_gaps(self) -> None:
        self.assertEqual(
            MM.final_gate_status(
                [],
                [
                    {
                        "severity": "medium",
                        "decision": "accepted",
                    }
                ],
            ),
            "BLOCK",
        )
        self.assertEqual(
            MM.final_gate_status(
                [],
                [
                    {
                        "severity": "high",
                        "decision": "deferred",
                    }
                ],
            ),
            "BLOCK",
        )
        self.assertEqual(
            MM.final_gate_status(
                [],
                [
                    {
                        "severity": "medium",
                        "decision": "deferred",
                    }
                ],
            ),
            "PASS_WITH_FINDINGS",
        )
        self.assertEqual(
            MM.final_gate_status(
                [],
                [
                    {
                        "severity": "high",
                        "decision": "covered",
                    }
                ],
            ),
            "PASS_CLEAN",
        )

    def test_antigravity_reviewer_is_headless_read_only_and_model_optional(
        self,
    ) -> None:
        parser = MM.build_parser()
        args = parser.parse_args(
            [
                "run",
                "--without-claude",
                "--with-antigravity",
                "--without-kimi",
            ]
        )
        config = json.loads(json.dumps(MM.DEFAULT_CONFIG))

        with (
            mock.patch.object(MM, "version_of", return_value="1.1.8"),
            mock.patch.object(MM.shutil, "which", return_value="/fake/provider"),
            mock.patch.object(
                MM,
                "provider_readiness",
                return_value=MM.ProviderReadiness(
                    True,
                    "authenticated; 1 models available",
                    ("gemini-3.6-flash-high",),
                ),
            ),
        ):
            reviewers = MM.reviewer_definitions(args, config)

        self.assertEqual(len(reviewers), 1)
        reviewer = reviewers[0]
        self.assertEqual(reviewer.name, "antigravity")
        self.assertEqual(reviewer.model, "auto")
        self.assertEqual(reviewer.command[0], "agy")
        self.assertIn("--agent", reviewer.command)
        self.assertIn(MM.ANTIGRAVITY_AGENT_NAME, reviewer.command)
        self.assertIn("--mode", reviewer.command)
        self.assertIn("plan", reviewer.command)
        self.assertIn("--sandbox", reviewer.command)
        self.assertIn("--output-format", reviewer.command)
        self.assertIn("json", reviewer.command)
        self.assertNotIn("--model", reviewer.command)

    def test_antigravity_json_response_and_usage_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            run_dir = root / "run"
            repo.mkdir()
            run_dir.mkdir()
            argument_log = root / "antigravity-args.json"
            fake_antigravity = root / "agy"
            fake_antigravity.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    import os
                    import sys

                    with open(os.environ["ARGUMENT_LOG"], "w", encoding="utf-8") as output:
                        json.dump(sys.argv[1:], output)
                    print(json.dumps({
                        "status": "SUCCESS",
                        "response": "# Verdict\\nPASS_CLEAN\\n\\n# Findings\\nNone.\\n\\n# Test gaps\\nNone.\\n",
                        "duration_seconds": 1.25,
                        "num_turns": 1,
                        "usage": {"total_tokens": 12}
                    }))
                    """
                ),
                encoding="utf-8",
            )
            fake_antigravity.chmod(0o755)
            reviewer = MM.Reviewer(
                "antigravity",
                (
                    str(fake_antigravity),
                    "--mode",
                    "plan",
                    "--sandbox",
                    "--output-format",
                    "json",
                ),
                {"ARGUMENT_LOG": str(argument_log)},
                "auto",
                "fake-antigravity 1.0",
            )

            result = MM.invoke_reviewer(
                reviewer,
                repo=repo,
                prompt="Review this change.",
                run_dir=run_dir,
                timeout_seconds=10,
            )

            self.assertEqual(result.returncode, 0)
            self.assertEqual(
                result.usage,
                {
                    "duration_seconds": 1.25,
                    "num_turns": 1,
                    "usage": {"total_tokens": 12},
                },
            )
            self.assertIn(
                "PASS_CLEAN",
                (run_dir / "antigravity.md").read_text(encoding="utf-8"),
            )
            self.assertTrue((run_dir / "antigravity.raw.json").exists())
            arguments = json.loads(argument_log.read_text(encoding="utf-8"))
            self.assertIn("--add-dir", arguments)
            self.assertIn(str(run_dir), arguments)
            self.assertEqual(arguments[-2:], ["--print", "Review this change."])

    def test_antigravity_empty_success_response_is_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            run_dir = root / "run"
            repo.mkdir()
            run_dir.mkdir()
            fake_antigravity = root / "agy"
            fake_antigravity.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    print(json.dumps({
                        "status": "SUCCESS",
                        "response": "",
                        "usage": {"total_tokens": 1}
                    }))
                    """
                ),
                encoding="utf-8",
            )
            fake_antigravity.chmod(0o755)
            reviewer = MM.Reviewer(
                "antigravity",
                (str(fake_antigravity), "--mode", "plan"),
                {},
                "auto",
                "fake-antigravity 1.0",
            )

            result = MM.invoke_reviewer(
                reviewer,
                repo=repo,
                prompt="Review this change.",
                run_dir=run_dir,
                timeout_seconds=10,
            )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.failure_category, "empty_response")

    def test_keyboard_interrupt_terminates_reviewer_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_process = mock.Mock()
            fake_process.communicate.side_effect = KeyboardInterrupt
            reviewer = MM.Reviewer(
                "kimi",
                ("kimi",),
                {},
                "test-model",
                "test-version",
            )
            with (
                mock.patch.object(
                    MM.subprocess, "Popen", return_value=fake_process
                ),
                mock.patch.object(
                    MM,
                    "terminate_process_group",
                    return_value=("", ""),
                ) as terminate,
                self.assertRaises(KeyboardInterrupt),
            ):
                MM.invoke_reviewer(
                    reviewer,
                    repo=root,
                    prompt="test",
                    run_dir=root,
                    timeout_seconds=10,
                )
        terminate.assert_called_once_with(fake_process)

    def test_parallel_process_registry_signals_every_reviewer(self) -> None:
        registry = MM.ReviewerProcessRegistry()
        first = mock.Mock(pid=101)
        second = mock.Mock(pid=202)
        registry.add(first)
        registry.add(second)
        with mock.patch.object(MM.os, "killpg") as killpg:
            registry.signal_all(MM.signal.SIGTERM)
        killpg.assert_has_calls(
            [
                mock.call(101, MM.signal.SIGTERM),
                mock.call(202, MM.signal.SIGTERM),
            ],
            any_order=True,
        )
        self.assertEqual(killpg.call_count, 2)

    def test_recover_marks_orphaned_running_review_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            MM.safe_write_json(
                run_dir / "metadata.json",
                {
                    "status": "running",
                    "created_at": MM.utc_now(),
                    "started_at": MM.utc_now(),
                    "runner_pid": 999999,
                },
            )
            args = MM.build_parser().parse_args(
                ["recover", "--run", str(run_dir)]
            )
            with mock.patch.object(MM, "process_is_alive", return_value=False):
                self.assertEqual(MM.recover_command(args), 0)
            metadata = MM.read_json(run_dir / "metadata.json")
            self.assertEqual(metadata["status"], "failed")
            self.assertEqual(
                metadata["failure"]["type"], "stale_runner_recovered"
            )

    def test_antigravity_readiness_requires_authenticated_models(self) -> None:
        with (
            mock.patch.object(MM.shutil, "which", return_value="/fake/agy"),
            mock.patch.object(
                MM,
                "antigravity_agent_readiness",
                return_value=MM.ProviderReadiness(True, "verified"),
            ),
            mock.patch.object(
                MM.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    ["agy", "models"],
                    1,
                    stdout="",
                    stderr="",
                ),
            ),
        ):
            unavailable = MM.provider_readiness("antigravity")
        self.assertFalse(unavailable.ready)
        self.assertIn("authentication or network", unavailable.detail)

        with (
            mock.patch.object(MM.shutil, "which", return_value="/fake/agy"),
            mock.patch.object(
                MM,
                "antigravity_agent_readiness",
                return_value=MM.ProviderReadiness(True, "verified"),
            ),
            mock.patch.object(
                MM.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    ["agy", "models"],
                    0,
                    stdout="gemini-3.6-flash-high\n",
                    stderr="",
                ),
            ),
        ):
            available = MM.provider_readiness("antigravity")
        self.assertTrue(available.ready)
        self.assertEqual(available.models, ("gemini-3.6-flash-high",))

    def test_antigravity_reviewer_rejects_unready_and_unknown_model(
        self,
    ) -> None:
        parser = MM.build_parser()
        automatic_args = parser.parse_args(
            [
                "run",
                "--without-claude",
                "--with-antigravity",
                "--without-kimi",
            ]
        )
        explicit_args = parser.parse_args(
            [
                "run",
                "--without-claude",
                "--with-antigravity",
                "--without-kimi",
                "--antigravity-model",
                "missing-model",
            ]
        )
        config = json.loads(json.dumps(MM.DEFAULT_CONFIG))

        with (
            mock.patch.object(MM, "version_of", return_value="1.1.8"),
            mock.patch.object(MM.shutil, "which", return_value="/fake/agy"),
            mock.patch.object(
                MM,
                "provider_readiness",
                return_value=MM.ProviderReadiness(
                    False,
                    "read-only agent is missing; run install command",
                ),
            ),
            self.assertRaisesRegex(MM.ReviewError, "not ready") as raised,
        ):
            MM.reviewer_definitions(automatic_args, config)
        self.assertNotIn("Run `agy`", str(raised.exception))
        self.assertIn("run install command", str(raised.exception))

        with (
            mock.patch.object(MM, "version_of", return_value="1.1.8"),
            mock.patch.object(MM.shutil, "which", return_value="/fake/agy"),
            mock.patch.object(
                MM,
                "provider_readiness",
                return_value=MM.ProviderReadiness(
                    True,
                    "authenticated",
                    ("gemini-3.6-flash-high",),
                ),
            ),
            self.assertRaisesRegex(MM.ReviewError, "is unavailable"),
        ):
            MM.reviewer_definitions(explicit_args, config)

    def test_locked_provider_rejects_one_run_override(self) -> None:
        args = MM.build_parser().parse_args(
            [
                "run",
                "--without-claude",
                "--with-antigravity",
                "--without-kimi",
            ]
        )
        config = json.loads(json.dumps(MM.DEFAULT_CONFIG))
        config["antigravity"]["allow_run_override"] = False
        with self.assertRaisesRegex(MM.ReviewError, "locked off"):
            MM.reviewer_definitions(args, config)

    def test_legacy_gemini_config_migrates_to_antigravity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.json"
            config_path.write_text(
                json.dumps({"gemini": {"enabled": False, "model": "legacy-model"}}),
                encoding="utf-8",
            )
            with mock.patch.object(MM, "CONFIG_PATH", config_path):
                config = MM.load_config()

        self.assertFalse(config["antigravity"]["enabled"])
        self.assertEqual(config["antigravity"]["model"], "legacy-model")
        self.assertNotIn("gemini", config)

    def test_legacy_gemini_commands_persist_only_antigravity(self) -> None:
        parser = MM.build_parser()
        with tempfile.TemporaryDirectory() as temporary:
            config_dir = Path(temporary)
            config_path = config_dir / "config.json"
            with (
                mock.patch.object(MM, "CONFIG_DIR", config_dir),
                mock.patch.object(MM, "CONFIG_PATH", config_path),
                mock.patch.object(MM, "install_antigravity_agent"),
                mock.patch.object(
                    MM,
                    "provider_readiness",
                    return_value=MM.ProviderReadiness(True, "authenticated"),
                ),
            ):
                MM.toggle_command(parser.parse_args(["disable", "gemini"]))
                MM.toggle_command(parser.parse_args(["enable", "gemini"]))
                MM.set_model_command(
                    parser.parse_args(["set-model", "gemini", "legacy-alias-model"])
                )
                persisted = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertTrue(persisted["antigravity"]["enabled"])
        self.assertEqual(
            persisted["antigravity"]["model"],
            "legacy-alias-model",
        )
        self.assertNotIn("gemini", persisted)

    def test_disable_lock_persists_until_provider_is_enabled(self) -> None:
        parser = MM.build_parser()
        with tempfile.TemporaryDirectory() as temporary:
            config_dir = Path(temporary)
            config_path = config_dir / "config.json"
            with (
                mock.patch.object(MM, "CONFIG_DIR", config_dir),
                mock.patch.object(MM, "CONFIG_PATH", config_path),
                mock.patch.object(MM, "install_antigravity_agent"),
                mock.patch.object(
                    MM,
                    "provider_readiness",
                    return_value=MM.ProviderReadiness(True, "authenticated"),
                ),
            ):
                MM.toggle_command(
                    parser.parse_args(["disable", "antigravity", "--lock"])
                )
                locked = json.loads(config_path.read_text(encoding="utf-8"))
                MM.toggle_command(parser.parse_args(["enable", "antigravity"]))
                enabled = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertFalse(locked["antigravity"]["enabled"])
        self.assertFalse(locked["antigravity"]["allow_run_override"])
        self.assertTrue(enabled["antigravity"]["enabled"])
        self.assertTrue(enabled["antigravity"]["allow_run_override"])

    def test_status_exit_code_tracks_enabled_reviewer_readiness(self) -> None:
        config = json.loads(json.dumps(MM.DEFAULT_CONFIG))
        config["antigravity"]["enabled"] = True
        config["kimi"]["enabled"] = False

        def readiness(
            provider: str, _model: str | None = None
        ) -> MM.ProviderReadiness:
            if provider == "antigravity":
                return MM.ProviderReadiness(False, "not authenticated")
            return MM.ProviderReadiness(True, "available")

        with (
            mock.patch.object(MM, "load_config", return_value=config),
            mock.patch.object(MM, "provider_readiness", side_effect=readiness),
            mock.patch.object(MM, "version_of", return_value="test-version"),
        ):
            self.assertEqual(MM.status_command(mock.Mock()), 3)

        config["antigravity"]["enabled"] = False
        with (
            mock.patch.object(MM, "load_config", return_value=config),
            mock.patch.object(MM, "provider_readiness", side_effect=readiness),
            mock.patch.object(MM, "version_of", return_value="test-version"),
        ):
            self.assertEqual(MM.status_command(mock.Mock()), 0)

        config["antigravity"]["enabled"] = True
        with (
            mock.patch.object(MM, "load_config", return_value=config),
            mock.patch.object(
                MM,
                "provider_readiness",
                return_value=MM.ProviderReadiness(True, "available"),
            ),
            mock.patch.object(MM, "version_of", return_value="test-version"),
        ):
            self.assertEqual(MM.status_command(mock.Mock()), 0)

    def test_status_does_not_probe_disabled_providers(self) -> None:
        config = json.loads(json.dumps(MM.DEFAULT_CONFIG))
        config["claude"]["enabled"] = False
        with (
            mock.patch.object(MM, "load_config", return_value=config),
            mock.patch.object(MM, "provider_readiness") as readiness,
            mock.patch.object(MM, "version_of") as version,
        ):
            self.assertEqual(MM.status_command(mock.Mock()), 0)
        readiness.assert_not_called()
        version.assert_not_called()

    def test_antigravity_agent_is_installed_with_hard_tool_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "agents" / "reviewer" / "agent.md"
            with mock.patch.object(
                MM,
                "ANTIGRAVITY_AGENT_INSTALL_PATH",
                target,
            ):
                installed = MM.install_antigravity_agent()
                content = installed.read_text(encoding="utf-8")
                readiness = MM.antigravity_agent_readiness()
                installed.write_text("outdated\n", encoding="utf-8")
                outdated = MM.antigravity_agent_readiness()

            self.assertIn("name: codex-multi-model-review-read-only-v1", content)
            self.assertIn("  - view_file", content)
            self.assertIn("  - grep_search", content)
            self.assertIn("commandExecutionPolicy: off", content)
            self.assertNotIn("run_command", content)
            self.assertTrue(readiness.ready)
            self.assertFalse(outdated.ready)
            self.assertIn("outdated", outdated.detail)

    def test_antigravity_agent_symlink_is_rejected_without_writing_target(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "sensitive.txt"
            target.write_text("unchanged\n", encoding="utf-8")
            agent_path = root / "agents" / "reviewer" / "agent.md"
            agent_path.parent.mkdir(parents=True)
            agent_path.symlink_to(target)

            with mock.patch.object(
                MM,
                "ANTIGRAVITY_AGENT_INSTALL_PATH",
                agent_path,
            ):
                readiness = MM.antigravity_agent_readiness()
                with self.assertRaisesRegex(MM.ReviewError, "symlink"):
                    MM.install_antigravity_agent()

            self.assertFalse(readiness.ready)
            self.assertIn("must not be a symlink", readiness.detail)
            self.assertEqual(target.read_text(encoding="utf-8"), "unchanged\n")

    def test_external_snapshot_symlink_cannot_be_broadly_overridden(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            runs_dir = root / "runs"
            repo.mkdir()
            initialize_repo(repo)
            outside = root / "outside.txt"
            outside.write_text("must remain outside the snapshot\n", encoding="utf-8")
            (repo / "src" / "outside-link").symlink_to(outside)
            args = MM.build_parser().parse_args(
                [
                    "run",
                    "--repo",
                    str(repo),
                    "--path",
                    "src",
                    "--allow-sensitive-paths",
                ]
            )

            with (
                mock.patch.object(MM, "RUNS_DIR", runs_dir),
                mock.patch.object(MM, "WORKFLOWS_DIR", runs_dir / "workflows"),
                mock.patch.object(
                    MM,
                    "load_config",
                    return_value=json.loads(json.dumps(MM.DEFAULT_CONFIG)),
                ),
                mock.patch.object(MM, "reviewer_definitions", return_value=[]),
                self.assertRaisesRegex(
                    MM.ReviewError,
                    "External symlinks cannot be overridden",
                ),
            ):
                MM.run_review_command(args)

    def test_secret_assignment_is_blocked_in_cli_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            runs_dir = root / "runs"
            repo.mkdir()
            initialize_repo(repo)
            (repo / "src" / "config.py").write_text(
                'password = "integration-only-value-73918426"\n',
                encoding="utf-8",
            )
            args = MM.build_parser().parse_args(
                [
                    "run",
                    "--repo",
                    str(repo),
                    "--path",
                    "src",
                ]
            )

            with (
                mock.patch.object(MM, "RUNS_DIR", runs_dir),
                mock.patch.object(MM, "WORKFLOWS_DIR", runs_dir / "workflows"),
                mock.patch.object(
                    MM,
                    "load_config",
                    return_value=json.loads(json.dumps(MM.DEFAULT_CONFIG)),
                ),
                mock.patch.object(MM, "reviewer_definitions", return_value=[]),
                self.assertRaisesRegex(
                    MM.ReviewError,
                    "likely sensitive material is in the private snapshot",
                ),
            ):
                MM.run_review_command(args)

    def test_task_paths_isolate_patch_and_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            initialize_repo(repo)
            (repo / "src" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
            (repo / "unrelated.txt").write_text("dirty\n", encoding="utf-8")
            scope = MM.Scope("uncommitted", None, "test")
            filters = MM.normalize_path_filters(repo, ["src"])

            paths = MM.changed_paths(repo, scope, filters)
            patch = MM.render_patch(repo, scope, filters)
            before = MM.fingerprint(repo, scope, paths, filters)

            self.assertEqual(paths, ["src/feature.py"])
            self.assertIn("VALUE = 2", patch)
            self.assertNotIn("unrelated.txt", patch)
            (repo / "unrelated.txt").write_text("different dirty value\n", encoding="utf-8")
            after = MM.fingerprint(
                repo, scope, MM.changed_paths(repo, scope, filters), filters
            )
            self.assertEqual(before, after)

    def test_repository_metadata_redacts_remote_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            initialize_repo(repo)
            run(
                [
                    "git",
                    "remote",
                    "add",
                    "origin",
                    "https://review-user:secret-token@example.com/org/repo.git?token=hidden",
                ],
                cwd=repo,
            )
            metadata = MM.repository_metadata(repo.resolve())
            self.assertEqual(
                metadata["origin"], "https://example.com/org/repo.git"
            )
            self.assertNotIn("secret-token", json.dumps(metadata))
            self.assertNotIn("hidden", json.dumps(metadata))

    def test_secret_diagnostics_are_redacted_and_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            secret = "super-secret-value-123456789"
            (repo / "config.ts").write_text(
                f'const password = "{secret}";\n', encoding="utf-8"
            )
            findings = MM.sensitive_content_findings(
                repo, ["config.ts"], ""
            )

            self.assertTrue(findings)
            rendered = "\n".join(item.display() for item in findings)
            self.assertIn("config.ts:1", rendered)
            self.assertIn("key=password", rendered)
            self.assertNotIn(secret, rendered)
            self.assertEqual(len(findings[0].identifier), 12)

    def test_report_parser_distinguishes_pass_with_findings(self) -> None:
        report = """# Verdict
PASS_WITH_FINDINGS

# Findings
## [medium] Verify the write postcondition
- Location: src/write.ts:42
- Trigger: malformed options
- Evidence: update is a no-op
- Impact: data is not written
- Smallest fix: nest the option correctly
- Confidence: high

# Test gaps
- Exercise a real adapter.

# Notes
None.
"""
        parsed = MM.parse_review_report("claude", report)
        self.assertEqual(parsed["verdict"], "PASS_WITH_FINDINGS")
        self.assertEqual(parsed["finding_counts"]["medium"], 1)
        self.assertEqual(parsed["findings"][0]["id"], "claude-001")
        self.assertEqual(parsed["findings"][0]["location"], "src/write.ts:42")
        self.assertEqual(len(parsed["test_gaps"]), 1)
        self.assertEqual(parsed["test_gaps"][0]["id"], "claude-test-001")
        self.assertEqual(parsed["test_gaps"][0]["severity"], "low")

    def test_review_prompt_disallows_empty_pass_with_findings(self) -> None:
        prompt = MM.build_prompt(
            repo=Path("/private/review-snapshot"),
            scope=MM.Scope("uncommitted", None, "uncommitted changes"),
            patch_path=Path("/private/change.patch"),
            manifest_path=Path("/private/manifest.md"),
            task="Review the public release.",
            risks=["security"],
            review_profile="security",
            phase="confirmation",
        )
        collapsed_prompt = " ".join(prompt.split())

        self.assertIn(
            "PASS_WITH_FINDINGS is invalid unless at least one structured",
            collapsed_prompt,
        )
        self.assertIn(
            "Limited review time, context, budget, or tool access belongs in Notes",
            collapsed_prompt,
        )
        self.assertIn(
            "use PASS_CLEAN and state the limitation under Notes",
            collapsed_prompt,
        )

        structured_gap = MM.parse_review_report(
            "kimi",
            """# Verdict
PASS_WITH_FINDINGS

# Findings
None.

# Test gaps
## [medium] Exercise the real adapter
- Needed test: assert the persisted postcondition
- Risk: malformed options can silently no-op

# Notes
None.
""",
            risk_profiled=True,
        )
        self.assertEqual(
            structured_gap["test_gaps"][0]["id"], "kimi-test-001"
        )
        self.assertEqual(
            structured_gap["test_gaps"][0]["severity"], "medium"
        )

    def test_batch_triage_is_atomic_for_findings_and_test_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            triage_path = run_dir / "triage.json"
            MM.safe_write_json(
                triage_path,
                {
                    "findings": [
                        {
                            "id": "claude-001",
                            "kind": "finding",
                            "decision": "pending",
                        }
                    ],
                    "test_gaps": [
                        {
                            "id": "claude-test-001",
                            "kind": "test_gap",
                            "decision": "pending",
                        }
                    ],
                },
            )
            with self.assertRaises(MM.ReviewError):
                MM.write_triage_decisions(
                    run_dir,
                    [
                        {
                            "finding": "claude-001",
                            "decision": "rejected",
                            "evidence": "Not reachable.",
                        },
                        {
                            "finding": "missing",
                            "decision": "rejected",
                            "evidence": "Invalid item.",
                        },
                    ],
                )
            unchanged = json.loads(triage_path.read_text(encoding="utf-8"))
            self.assertEqual(unchanged["findings"][0]["decision"], "pending")

            MM.write_triage_decisions(
                run_dir,
                [
                    {
                        "finding": "claude-001",
                        "decision": "rejected",
                        "evidence": "Not reachable.",
                    },
                    {
                        "finding": "claude-test-001",
                        "decision": "covered",
                        "evidence": "Focused regression test passes.",
                        "verification": "python test: passed",
                    },
                ],
            )
            updated = json.loads(triage_path.read_text(encoding="utf-8"))
            self.assertEqual(updated["findings"][0]["decision"], "rejected")
            self.assertEqual(updated["test_gaps"][0]["decision"], "covered")


    def test_kimi_readiness_requires_the_configured_model_alias(self) -> None:
        payload = json.dumps(
            {"providers": {"moonshot": {}}, "models": {"k3-256k": {}}}
        )
        with (
            mock.patch.object(MM.shutil, "which", return_value="/fake/kimi"),
            mock.patch.object(
                MM.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    ["kimi", "provider", "list", "--json"],
                    0,
                    stdout=payload,
                    stderr="",
                ),
            ),
        ):
            ready = MM.provider_readiness("kimi", "k3-256k")
            missing = MM.provider_readiness("kimi", "k3")
        self.assertTrue(ready.ready)
        self.assertFalse(missing.ready)
        self.assertIn("not configured", missing.detail)
        self.assertEqual(missing.models, ("k3-256k",))

    def test_kimi_model_failure_is_typed_and_records_meaningful_line(self) -> None:
        detail = (
            "kimi version 0.30.0\n"
            'error: failed to run prompt: Model "k3" is not configured in config.toml.\n'
        )
        category = MM.classify_provider_failure(
            returncode=1,
            timed_out=False,
            stdout="",
            stderr=detail,
        )
        self.assertEqual(category, "model_not_configured")
        MM.record_provider_failure("kimi", category, detail)
        recorded = MM.read_json(MM.PROVIDER_HEALTH_PATH)["kimi"]
        self.assertEqual(recorded["category"], "model_not_configured")
        self.assertIn("not configured", recorded["detail"])
        self.assertNotEqual(recorded["detail"], "kimi version 0.30.0")
        unrelated = MM.classify_provider_failure(
            returncode=1,
            timed_out=False,
            stdout='selected model "k3"\nerror: invalid file path',
            stderr="",
        )
        self.assertEqual(unrelated, "provider_error")

    def test_claude_structured_output_is_rendered_and_audited(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_claude = root / "claude"
            fake_claude.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    print(json.dumps({
                        "result": "fallback",
                        "structured_output": {
                            "verdict": "PASS_WITH_FINDINGS",
                            "findings": [{
                                "severity": "medium",
                                "title": "Reachable defect",
                                "location": "src/a.py:1",
                                "trigger": "input",
                                "evidence": "trace",
                                "impact": "wrong result",
                                "smallest_fix": "guard",
                                "confidence": "high"
                            }],
                            "test_gaps": [],
                            "notes": ["Static review"]
                        },
                        "total_cost_usd": 0.01
                    }))
                    """
                ),
                encoding="utf-8",
            )
            fake_claude.chmod(0o755)
            reviewer = MM.Reviewer(
                "claude", (str(fake_claude),), {}, "sonnet", "fake"
            )
            result = MM.invoke_reviewer(
                reviewer,
                repo=root,
                prompt="Review.",
                run_dir=root,
                timeout_seconds=10,
            )
            report = result.report_path.read_text(encoding="utf-8")
            structured = MM.read_json(root / "claude.structured.json")
        self.assertEqual(result.returncode, 0)
        self.assertIn("## [medium] Reachable defect", report)
        self.assertEqual(structured["verdict"], "PASS_WITH_FINDINGS")

    def test_claude_provider_error_records_the_provider_message(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_claude = root / "claude"
            fake_claude.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    print(json.dumps({
                        "is_error": True,
                        "result": "Review budget was exhausted before completion.",
                        "total_cost_usd": 0.06
                    }))
                    """
                ),
                encoding="utf-8",
            )
            fake_claude.chmod(0o755)
            reviewer = MM.Reviewer(
                "claude", (str(fake_claude),), {}, "sonnet", "fake"
            )
            with mock.patch.object(MM, "record_provider_failure") as record:
                result = MM.invoke_reviewer(
                    reviewer,
                    repo=root,
                    prompt="Review.",
                    run_dir=root,
                    timeout_seconds=10,
                )
        self.assertEqual(result.returncode, 1)
        record.assert_called_once_with(
            "claude",
            "budget_exhausted",
            "Review budget was exhausted before completion.",
        )

    def test_resume_attempt_history_preserves_artifacts_cost_and_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            report = run_dir / "claude.md"
            error = run_dir / "claude.stderr.log"
            raw = run_dir / "claude.raw.json"
            report.write_text("", encoding="utf-8")
            error.write_text("Reached maximum budget ($1.25)\n", encoding="utf-8")
            raw.write_text('{"is_error":true}\n', encoding="utf-8")
            now = MM.utc_now()
            metadata = {
                "run_id": "run-resumed",
                "workflow_id": "wf-resumed",
                "repository": {"id": "repo-1"},
                "created_at": now,
                "started_at": now,
                "risks": [],
            }
            reviewer = MM.Reviewer(
                "claude", ("claude",), {}, "sonnet", "fake"
            )
            first = MM.ReviewResult(
                "claude",
                1,
                report,
                error,
                now,
                now,
                1.0,
                False,
                {"total_cost_usd": 1.25, "num_turns": 10},
                "budget_exhausted",
            )
            with mock.patch.object(MM, "workflow_runs", return_value=[]):
                MM.persist_review_results(
                    run_dir=run_dir,
                    metadata=metadata,
                    reviewers=[reviewer],
                    results=[first],
                )
            MM.archive_reviewer_artifacts(
                run_dir, "claude", metadata["reviewers"]["claude"]
            )
            report.write_text(
                "# Verdict\nPASS_CLEAN\n\n# Findings\nNone.\n\n"
                "# Test gaps\nNone.\n",
                encoding="utf-8",
            )
            error.write_text("", encoding="utf-8")
            second = MM.ReviewResult(
                "claude",
                0,
                report,
                error,
                now,
                now,
                2.0,
                False,
                {"total_cost_usd": 0.75, "num_turns": 5},
                None,
            )
            with mock.patch.object(MM, "workflow_runs", return_value=[]):
                MM.persist_review_results(
                    run_dir=run_dir,
                    metadata=metadata,
                    reviewers=[reviewer],
                    results=[second],
                )
            persisted = MM.read_json(run_dir / "metadata.json")
            metrics = MM.workflow_metrics([(run_dir, persisted)])
            archived_raw_exists = (run_dir / "claude.attempt-1.raw.json").exists()
            legacy = json.loads(json.dumps(persisted))
            legacy["resumed_at"] = now
            legacy["reviewers"]["claude"].pop("attempts")
            legacy_metrics = MM.workflow_metrics([(run_dir, legacy)])
            healthy_partial = json.loads(json.dumps(persisted))
            healthy_partial["resumed_at"] = now
            healthy_partial["resumed_reviewers"] = ["claude"]
            healthy_partial["reviewers"]["kimi"] = {
                "model": "k3-256k",
                "exit_code": 0,
                "report_contract_valid": True,
                "verdict": "PASS_CLEAN",
            }
            healthy_metrics = MM.workflow_metrics(
                [(run_dir, healthy_partial)]
            )

        claude = persisted["reviewers"]["claude"]
        self.assertEqual(len(claude["attempts"]), 1)
        self.assertEqual(
            claude["attempts"][0]["failure_category"], "budget_exhausted"
        )
        self.assertEqual(
            claude["attempts"][0]["report"], "claude.attempt-1.md"
        )
        self.assertTrue(archived_raw_exists)
        self.assertEqual(metrics["reviewer_invocations"], 2)
        self.assertEqual(metrics["failed_reviewer_invocations"], 1)
        self.assertEqual(metrics["successful_reviewer_invocations"], 1)
        self.assertEqual(metrics["reported_cost_usd"], 2.0)
        self.assertEqual(
            legacy_metrics[
                "legacy_resume_runs_with_incomplete_attempt_history"
            ],
            1,
        )
        self.assertEqual(
            healthy_metrics[
                "legacy_resume_runs_with_incomplete_attempt_history"
            ],
            0,
        )

    def test_batch_archive_collision_does_not_move_any_reviewer_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            (run_dir / "claude.md").write_text("claude", encoding="utf-8")
            (run_dir / "kimi.md").write_text("kimi", encoding="utf-8")
            (run_dir / "kimi.attempt-1.md").write_text(
                "collision", encoding="utf-8"
            )
            reviewers = {
                "claude": {"exit_code": 1, "report": "claude.md"},
                "kimi": {"exit_code": 1, "report": "kimi.md"},
            }
            with self.assertRaisesRegex(MM.ReviewError, "already exists"):
                MM.archive_reviewer_artifacts_batch(
                    run_dir, ["claude", "kimi"], reviewers
                )

            self.assertTrue((run_dir / "claude.md").exists())
            self.assertFalse((run_dir / "claude.attempt-1.md").exists())
            self.assertTrue((run_dir / "kimi.md").exists())
            self.assertEqual(reviewers["claude"]["report"], "claude.md")
            self.assertEqual(reviewers["kimi"]["report"], "kimi.md")

    def test_pass_clean_with_items_is_safely_downgraded(self) -> None:
        parsed = MM.parse_review_report(
            "claude",
            """# Verdict
PASS_CLEAN

# Findings
None.

# Test gaps
## [low] Missing assertion
- Needed test: assert the branch
- Risk: regression
""",
        )
        self.assertEqual(parsed["declared_verdict"], "PASS_CLEAN")
        self.assertEqual(parsed["verdict"], "PASS_WITH_FINDINGS")
        self.assertTrue(parsed["normalizations"])

    def test_terminal_error_preserves_typed_root_cause(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            MM.safe_write_json(
                run_dir / "metadata.json",
                {
                    "failure": {
                        "type": "reviewer_failure",
                        "reviewers": ["kimi"],
                    }
                },
            )
            MM.update_terminal_error(
                run_dir,
                error_type="ReviewError",
                message="reviewer failed",
                status="partial",
            )
            metadata = MM.read_json(run_dir / "metadata.json")
        self.assertEqual(metadata["failure"]["type"], "reviewer_failure")
        self.assertEqual(metadata["terminal_error"]["type"], "ReviewError")
        self.assertEqual(metadata["status"], "partial")

    def test_workflow_budget_caps_claude_and_blocks_when_exhausted(self) -> None:
        reviewer = MM.Reviewer(
            "claude",
            ("claude", "--max-budget-usd", "1.25"),
            {},
            "sonnet",
            "fake",
        )
        with (
            mock.patch.object(MM, "workflow_budget_limit", return_value=1.0),
            mock.patch.object(MM, "workflow_spend", return_value=0.7),
        ):
            adjusted, status = MM.apply_workflow_budget([reviewer], "wf-test")
        self.assertEqual(adjusted[0].command[-1], "0.3")
        self.assertEqual(status["remaining_before_run_usd"], 0.3)
        with (
            mock.patch.object(MM, "workflow_budget_limit", return_value=1.0),
            mock.patch.object(MM, "workflow_spend", return_value=0.99),
            self.assertRaisesRegex(MM.ReviewError, "exhausted"),
        ):
            MM.apply_workflow_budget([reviewer], "wf-test")

    def test_workflow_budget_reservations_are_atomic(self) -> None:
        reviewer = MM.Reviewer(
            "claude",
            ("claude", "--max-budget-usd", "1.25"),
            {},
            "sonnet",
            "fake",
        )
        with tempfile.TemporaryDirectory() as temporary:
            workflows = Path(temporary)
            with (
                mock.patch.object(MM, "WORKFLOWS_DIR", workflows),
                mock.patch.object(MM, "workflow_spend", return_value=0.0),
            ):
                MM.create_workflow("wf-budget", max_budget_usd=2.0)
                first, first_status = MM.apply_workflow_budget(
                    [reviewer], "wf-budget", reservation_id="run-1"
                )
                second, second_status = MM.apply_workflow_budget(
                    [reviewer], "wf-budget", reservation_id="run-2"
                )
                document = MM.read_json(workflows / "wf-budget.json")
                first_budget = float(first[0].command[-1])
                second_budget = float(second[0].command[-1])
                self.assertEqual(first_budget + second_budget, 2.0)
                self.assertEqual(first_status["reserved_before_run_usd"], 0.0)
                self.assertEqual(second_status["reserved_before_run_usd"], 1.25)
                self.assertEqual(len(document["budget_reservations"]), 2)
                MM.release_workflow_budget_reservation("wf-budget", "run-1")
                MM.release_workflow_budget_reservation("wf-budget", "run-2")
                released = MM.read_json(workflows / "wf-budget.json")
        self.assertEqual(released["budget_reservations"], {})

    def test_mixed_reviewer_and_contract_failures_are_both_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            failed_report = run_dir / "kimi.md"
            invalid_report = run_dir / "claude.md"
            failed_error = run_dir / "kimi.stderr.log"
            invalid_error = run_dir / "claude.stderr.log"
            failed_report.write_text("", encoding="utf-8")
            failed_error.write_text("provider failed", encoding="utf-8")
            invalid_report.write_text(
                "# Verdict\nBLOCK\n\n# Findings\nNone.\n\n"
                "# Test gaps\nNone.\n",
                encoding="utf-8",
            )
            invalid_error.write_text("", encoding="utf-8")
            now = MM.utc_now()
            metadata = {
                "run_id": "run-mixed",
                "workflow_id": "wf-mixed",
                "repository": {"id": "repo-1"},
                "created_at": now,
                "started_at": now,
                "risks": ["security"],
            }
            reviewers = [
                MM.Reviewer("kimi", ("kimi",), {}, "k3", "fake"),
                MM.Reviewer("claude", ("claude",), {}, "sonnet", "fake"),
            ]
            results = [
                MM.ReviewResult(
                    "kimi",
                    1,
                    failed_report,
                    failed_error,
                    now,
                    now,
                    0.1,
                    False,
                    None,
                    "provider_error",
                ),
                MM.ReviewResult(
                    "claude",
                    0,
                    invalid_report,
                    invalid_error,
                    now,
                    now,
                    0.1,
                    False,
                    None,
                    None,
                ),
            ]
            with mock.patch.object(MM, "workflow_runs", return_value=[]):
                failures, invalid = MM.persist_review_results(
                    run_dir=run_dir,
                    metadata=metadata,
                    reviewers=reviewers,
                    results=results,
                )
            persisted = MM.read_json(run_dir / "metadata.json")
        self.assertEqual(failures, ["kimi"])
        self.assertEqual(invalid, ["claude"])
        self.assertEqual(persisted["failure"]["type"], "reviewer_failure")
        self.assertEqual(persisted["failure"]["invalid_reports"], ["claude"])

    def test_workflow_supersede_links_both_documents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workflows = Path(temporary)
            with mock.patch.object(MM, "WORKFLOWS_DIR", workflows):
                MM.create_workflow(
                    "wf-old",
                    name="Old",
                    max_budget_usd=7.0,
                    review_mode="fast",
                )
                args = MM.build_parser().parse_args(
                    [
                        "workflow",
                        "supersede",
                        "wf-old",
                        "--reason",
                        "source changed after confirmation",
                    ]
                )
                output = io.StringIO()
                with redirect_stdout(output):
                    MM.workflow_supersede_command(args)
                replacement = output.getvalue().strip()
                old = MM.read_json(workflows / "wf-old.json")
                new = MM.read_json(workflows / f"{replacement}.json")
        self.assertEqual(old["superseded_by"], replacement)
        self.assertEqual(old["status"], "superseded")
        self.assertEqual(new["supersedes"], ["wf-old"])
        self.assertEqual(
            new["policy"],
            {
                "review_mode": "fast",
                "max_repair_rounds": 1,
                "confirmation_required": True,
                "repair_effort": "low",
                "confirmation_effort": "medium",
                "max_budget_usd": 7.0,
            },
        )

    def test_workflow_supersede_preserves_legacy_repair_depth(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workflows = Path(temporary)
            with mock.patch.object(MM, "WORKFLOWS_DIR", workflows):
                MM.safe_write_json(
                    workflows / "wf-legacy.json",
                    {
                        "workflow_id": "wf-legacy",
                        "name": "Legacy",
                        "policy": {
                            "max_repair_rounds": 3,
                            "confirmation_required": True,
                            "max_budget_usd": 5.0,
                        },
                    },
                )
                args = MM.build_parser().parse_args(
                    [
                        "workflow",
                        "supersede",
                        "wf-legacy",
                        "--reason",
                        "source changed",
                    ]
                )
                output = io.StringIO()
                with redirect_stdout(output):
                    MM.workflow_supersede_command(args)
                replacement = output.getvalue().strip()
                successor = MM.read_json(workflows / f"{replacement}.json")

        self.assertEqual(successor["policy"]["review_mode"], "deep")
        self.assertEqual(successor["policy"]["max_repair_rounds"], 3)
        self.assertEqual(successor["policy"]["repair_effort"], "medium")
        self.assertEqual(
            successor["policy"]["confirmation_effort"], "medium"
        )

    def test_workflow_supersede_waits_for_budget_writer_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workflows = Path(temporary)
            errors: list[Exception] = []
            started = threading.Event()
            finished = threading.Event()
            with mock.patch.object(MM, "WORKFLOWS_DIR", workflows):
                MM.create_workflow("wf-old")
                MM.create_workflow("wf-new")
                args = MM.build_parser().parse_args(
                    [
                        "workflow",
                        "supersede",
                        "wf-old",
                        "--by",
                        "wf-new",
                        "--reason",
                        "contract changed",
                    ]
                )

                def supersede() -> None:
                    started.set()
                    try:
                        MM.workflow_supersede_command(args)
                    except Exception as exc:  # pragma: no cover - asserted below
                        errors.append(exc)
                    finally:
                        finished.set()

                old_path = workflows / "wf-old.json"
                with MM.exclusive_file_lock(old_path):
                    worker = threading.Thread(target=supersede)
                    worker.start()
                    self.assertTrue(started.wait(timeout=1))
                    document = MM.read_json(old_path)
                    document["budget_reservations"] = {
                        "run-1": {"max_budget_usd": 1.25}
                    }
                    MM.safe_write_json(old_path, document)
                    self.assertFalse(finished.wait(timeout=0.05))
                worker.join(timeout=2)
                self.assertFalse(worker.is_alive())
                superseded = MM.read_json(old_path)
        self.assertEqual(errors, [])
        self.assertEqual(
            superseded["budget_reservations"],
            {"run-1": {"max_budget_usd": 1.25}},
        )

    def test_run_rejects_a_superseded_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            workflows = root / "workflows"
            runs = root / "runs"
            repo.mkdir()
            initialize_repo(repo)
            (repo / "src" / "feature.py").write_text(
                "VALUE = 2\n", encoding="utf-8"
            )
            with mock.patch.object(MM, "WORKFLOWS_DIR", workflows):
                MM.create_workflow("wf-old")
                document = MM.read_json(workflows / "wf-old.json")
                document.update(
                    {"status": "superseded", "superseded_by": "wf-new"}
                )
                MM.safe_write_json(workflows / "wf-old.json", document)
                args = MM.build_parser().parse_args(
                    [
                        "run",
                        "--repo",
                        str(repo),
                        "--workflow-id",
                        "wf-old",
                        "--path",
                        "src",
                    ]
                )
                with (
                    mock.patch.object(MM, "RUNS_DIR", runs),
                    mock.patch.object(
                        MM,
                        "load_config",
                        return_value=json.loads(json.dumps(MM.DEFAULT_CONFIG)),
                    ),
                    self.assertRaisesRegex(
                        MM.ReviewError, "successor workflow wf-new"
                    ),
                ):
                    MM.run_review_command(args)

    def test_unexpected_resume_error_preserves_partial_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "run"
            snapshot = root / "snapshot"
            repo = root / "repo"
            run_dir.mkdir()
            snapshot.mkdir()
            repo.mkdir()
            now = MM.utc_now()
            MM.safe_write(run_dir / "change.patch", "")
            MM.safe_write(run_dir / "prompt.md", "review")
            MM.safe_write_json(
                run_dir / "metadata.json",
                {
                    "run_id": "run-resume",
                    "status": "partial",
                    "failure": {
                        "type": "reviewer_failure",
                        "reviewers": ["kimi"],
                        "successful_reviewers": ["claude"],
                    },
                    "reviewers": {
                        "claude": {"exit_code": 0},
                        "kimi": {"exit_code": 1, "model": "k3-256k"},
                    },
                    "repository": {"root": str(repo), "id": "repo-1"},
                    "scope": {
                        "kind": "uncommitted",
                        "value": None,
                        "label": "changes",
                    },
                    "path_filters": [],
                    "paths": ["src/a.py"],
                    "source_fingerprint": "fingerprint",
                    "workflow_id": "wf-active",
                    "review_policy": {"timeout_minutes": 1},
                    "created_at": now,
                    "started_at": now,
                },
            )
            reviewer = MM.Reviewer(
                "kimi", ("kimi",), {}, "k3-256k", "fake"
            )
            args = MM.build_parser().parse_args(
                ["resume", "--run", str(run_dir)]
            )
            with (
                mock.patch.object(MM, "require_active_workflow"),
                mock.patch.object(MM, "resolve_repo", return_value=repo),
                mock.patch.object(
                    MM, "changed_paths", return_value=["src/a.py"]
                ),
                mock.patch.object(MM, "fingerprint", return_value="fingerprint"),
                mock.patch.object(MM, "workflow_runs", return_value=[]),
                mock.patch.object(
                    MM, "reviewer_definitions", return_value=[reviewer]
                ),
                mock.patch.object(
                    MM, "load_config", return_value=MM.DEFAULT_CONFIG
                ),
                mock.patch.object(
                    MM,
                    "apply_workflow_budget",
                    return_value=([reviewer], None),
                ),
                mock.patch.object(MM, "create_snapshot", return_value=snapshot),
                mock.patch.object(
                    MM, "external_snapshot_symlinks", return_value=[]
                ),
                mock.patch.object(
                    MM, "sensitive_content_findings", return_value=[]
                ),
                mock.patch.object(
                    MM,
                    "invoke_reviewer",
                    side_effect=RuntimeError("unexpected resume failure"),
                ),
                self.assertRaisesRegex(RuntimeError, "unexpected resume failure"),
            ):
                MM.resume_review_command(args)
            metadata = MM.read_json(run_dir / "metadata.json")
        self.assertEqual(metadata["status"], "partial")
        self.assertEqual(metadata["failure"]["type"], "reviewer_failure")
        self.assertEqual(metadata["terminal_error"]["type"], "RuntimeError")

    def test_budget_exhaustion_requires_an_explicit_resume_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "run"
            repo = root / "repo"
            run_dir.mkdir()
            repo.mkdir()
            MM.safe_write_json(
                run_dir / "metadata.json",
                {
                    "run_id": "run-budget",
                    "status": "failed",
                    "failure": {
                        "type": "reviewer_failure",
                        "reviewers": ["claude"],
                    },
                    "reviewers": {
                        "claude": {
                            "exit_code": 1,
                            "failure_category": "budget_exhausted",
                            "model": "sonnet",
                        }
                    },
                    "repository": {"root": str(repo), "id": "repo-1"},
                    "scope": {
                        "kind": "uncommitted",
                        "value": None,
                        "label": "changes",
                    },
                    "path_filters": [],
                    "paths": ["src/a.py"],
                    "source_fingerprint": "fingerprint",
                    "workflow_id": "wf-active",
                    "review_policy": {
                        "claude_effort": "medium",
                        "claude_max_budget_usd": 1.25,
                    },
                },
            )
            with (
                mock.patch.object(MM, "require_active_workflow"),
                mock.patch.object(MM, "resolve_repo", return_value=repo),
                mock.patch.object(
                    MM, "changed_paths", return_value=["src/a.py"]
                ),
                mock.patch.object(MM, "fingerprint", return_value="fingerprint"),
                mock.patch.object(MM, "workflow_runs", return_value=[]),
                self.assertRaisesRegex(
                    MM.ReviewError, "explicit --claude-max-budget-usd"
                ),
            ):
                MM.resume_review_locked(run_dir)
            with (
                mock.patch.object(MM, "require_active_workflow"),
                mock.patch.object(MM, "resolve_repo", return_value=repo),
                mock.patch.object(
                    MM, "changed_paths", return_value=["src/a.py"]
                ),
                mock.patch.object(MM, "fingerprint", return_value="fingerprint"),
                mock.patch.object(MM, "workflow_runs", return_value=[]),
                self.assertRaisesRegex(
                    MM.ReviewError, "greater than 1.25"
                ),
            ):
                MM.resume_review_locked(
                    run_dir, claude_max_budget_usd=1.25
                )

    def test_concurrent_resume_commands_do_not_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            MM.safe_write_json(run_dir / "metadata.json", {"status": "partial"})
            args = MM.build_parser().parse_args(
                ["resume", "--run", str(run_dir)]
            )
            state_lock = threading.Lock()
            first_started = threading.Event()
            release_first = threading.Event()
            active = 0
            maximum_active = 0
            calls = 0

            def locked(_run_dir: Path) -> int:
                nonlocal active, maximum_active, calls
                with state_lock:
                    calls += 1
                    active += 1
                    maximum_active = max(maximum_active, active)
                    call_number = calls
                if call_number == 1:
                    first_started.set()
                    self.assertTrue(release_first.wait(timeout=2))
                with state_lock:
                    active -= 1
                return 0

            with mock.patch.object(MM, "resume_review_locked", side_effect=locked):
                first = threading.Thread(target=MM.resume_review_command, args=(args,))
                second = threading.Thread(target=MM.resume_review_command, args=(args,))
                first.start()
                self.assertTrue(first_started.wait(timeout=1))
                second.start()
                second.join(timeout=0.05)
                self.assertTrue(second.is_alive())
                release_first.set()
                first.join(timeout=2)
                second.join(timeout=2)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(calls, 2)
        self.assertEqual(maximum_active, 1)

    def test_sensitive_scan_deduplicates_added_patch_content_and_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scans = root / "scans"
            source = root / "fixture.py"
            source.write_text(
                'apiKey = "credential-value-123456"\n', encoding="utf-8"
            )
            patch = '+apiKey = "credential-value-123456"\n'
            findings = MM.sensitive_content_findings(
                root, ["fixture.py"], patch
            )
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].path, "fixture.py")
            repository = {"id": "repo-1", "root": str(root)}
            scope = MM.Scope("uncommitted", None, "changes")
            with mock.patch.object(MM, "SENSITIVE_SCANS_DIR", scans):
                token = MM.create_sensitive_scan_token(
                    repository=repository,
                    scope=scope,
                    path_filters=(),
                    paths=["fixture.py"],
                    source_fingerprint="abc",
                    findings=findings,
                )
                path, value = MM.validate_sensitive_scan_token(
                    token,
                    repository=repository,
                    scope=scope,
                    path_filters=(),
                    paths=["fixture.py"],
                    source_fingerprint="abc",
                )
                MM.consume_sensitive_scan_token(path, value)
                with self.assertRaisesRegex(MM.ReviewError, "already consumed"):
                    MM.validate_sensitive_scan_token(
                        token,
                        repository=repository,
                        scope=scope,
                        path_filters=(),
                        paths=["fixture.py"],
                        source_fingerprint="abc",
                    )

    def test_analytics_separates_partial_runs_and_typed_failures(self) -> None:
        now = MM.utc_now()
        runs = [
            (
                Path("/tmp/run"),
                {
                    "created_at": now,
                    "status": "partial",
                    "workflow_id": "wf-one",
                    "failure": {
                        "type": "reviewer_failure",
                        "invalid_reports": ["antigravity"],
                    },
                    "reviewers": {
                        "claude": {
                            "exit_code": 0,
                            "verdict": "PASS_CLEAN",
                            "report_contract_valid": True,
                            "duration_seconds": 1,
                            "usage": {"total_cost_usd": 0.2},
                        },
                        "kimi": {"exit_code": 1, "duration_seconds": 0.1},
                    },
                },
            )
        ]
        with (
            mock.patch.object(MM, "all_run_metadata", return_value=runs),
            mock.patch.object(MM, "workflow_path", return_value=Path("/missing")),
            mock.patch.object(MM, "WORKFLOWS_DIR", Path("/missing")),
        ):
            report = MM.analytics_report(7)
        self.assertEqual(report["partial_runs"], 1)
        self.assertEqual(
            report["failure_types"],
            {"invalid_report": 1, "reviewer_failure": 1},
        )
        self.assertEqual(report["providers"]["claude"]["cost_usd"], 0.2)
        self.assertEqual(
            report["review_modes"]["legacy"],
            {
                "runs": 1,
                "completed_runs": 0,
                "failed_runs": 1,
                "reviewer_invocations": 2,
                "successful_invocations": 1,
                "reviewer_duration_seconds": 1.1,
                "reported_cost_usd": 0.2,
                "findings": 0,
                "test_gaps": 0,
            },
        )


class RunnerEndToEndTests(unittest.TestCase):
    def test_commit_run_ignores_unrelated_dirty_paths_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            repo = root / "repo"
            bin_dir = root / "bin"
            home.mkdir()
            repo.mkdir()
            bin_dir.mkdir()
            initialize_repo(repo)
            (repo / "src" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
            run(["git", "add", "src/feature.py"], cwd=repo)
            run(["git", "commit", "-qm", "change feature"], cwd=repo)
            (repo / "unrelated.txt").write_text("dirty\n", encoding="utf-8")
            (repo / "REVIEW.md").write_text("local notes\n", encoding="utf-8")

            fake_claude = bin_dir / "claude"
            fake_claude.write_text(
                textwrap.dedent(
                    """\
                    #!/bin/sh
                    if [ "$1" = "--version" ]; then
                      echo "fake-claude 1.0"
                      exit 0
                    fi
                    test "$(cat unrelated.txt)" = "clean" || exit 3
                    test ! -e REVIEW.md || exit 4
                    cat >/dev/null
                    python3 -c 'import json; print(json.dumps({
                      "result": "# Verdict\\nPASS_CLEAN\\n\\n# Findings\\nNone.\\n\\n# Test gaps\\nNone.\\n",
                      "total_cost_usd": 0.01
                    }))'
                    """
                ),
                encoding="utf-8",
            )
            fake_claude.chmod(0o755)
            environment = os.environ.copy()
            environment["HOME"] = str(home)
            environment["PATH"] = f"{bin_dir}:{environment['PATH']}"
            completed = run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    "run",
                    "--repo",
                    str(repo),
                    "--commit",
                    "HEAD",
                    "--path",
                    "src",
                    "--task",
                    "Review the committed feature change.",
                    "--without-antigravity",
                    "--without-kimi",
                ],
                cwd=repo,
                env=environment,
            )

            self.assertIn("PASS_CLEAN", completed.stdout)
            metadata_path = next(
                (home / ".codex" / "review-runs").glob("*/*/metadata.json")
            )
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            patch = (metadata_path.parent / "change.patch").read_text(encoding="utf-8")
            self.assertEqual(metadata["status"], "completed")
            self.assertEqual(metadata["scope"]["kind"], "commit")
            self.assertEqual(metadata["paths"], ["src/feature.py"])
            self.assertEqual(metadata["excluded_changed_paths"], [])
            self.assertNotIn("dirty", patch)
            self.assertNotIn("local notes", patch)

    def test_scan_token_flows_through_cli_and_is_consumed_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            repo = root / "repo"
            bin_dir = root / "bin"
            invocation_count = root / "claude-count"
            home.mkdir()
            repo.mkdir()
            bin_dir.mkdir()
            initialize_repo(repo)
            (repo / "src" / "secret-fixture.py").write_text(
                'apiKey = "credential-value-123456"\n', encoding="utf-8"
            )
            fake_claude = bin_dir / "claude"
            fake_claude.write_text(
                textwrap.dedent(
                    f"""\
                    #!/usr/bin/env python3
                    import json
                    import pathlib
                    import sys
                    if "--version" in sys.argv:
                        print("fake-claude 1.0")
                        raise SystemExit(0)
                    count_path = pathlib.Path({str(invocation_count)!r})
                    count = int(count_path.read_text()) if count_path.exists() else 0
                    count_path.write_text(str(count + 1))
                    sys.stdin.read()
                    print(json.dumps({{
                        "result": "# Verdict\\nPASS_CLEAN\\n\\n# Findings\\nNone.\\n\\n# Test gaps\\nNone.\\n",
                        "total_cost_usd": 0.01
                    }}))
                    """
                ),
                encoding="utf-8",
            )
            fake_claude.chmod(0o755)
            environment = os.environ.copy()
            environment["HOME"] = str(home)
            environment["PATH"] = f"{bin_dir}:{environment['PATH']}"
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            base = [sys.executable, str(SCRIPT_PATH)]
            scan = run(
                [
                    *base,
                    "scan",
                    "--repo",
                    str(repo),
                    "--path",
                    "src",
                    "--approve-findings",
                ],
                cwd=repo,
                env=environment,
            )
            token = json.loads(scan.stdout)["approved_token"]
            self.assertIsInstance(token, str)
            reviewed = run(
                [
                    *base,
                    "run",
                    "--repo",
                    str(repo),
                    "--path",
                    "src",
                    "--task",
                    "Review the safe credential fixture.",
                    "--without-antigravity",
                    "--without-kimi",
                    "--sensitive-scan-token",
                    token,
                ],
                cwd=repo,
                env=environment,
            )
            self.assertIn("PASS_CLEAN", reviewed.stdout)
            reused = run(
                [
                    *base,
                    "run",
                    "--repo",
                    str(repo),
                    "--path",
                    "src",
                    "--task",
                    "Review the safe credential fixture.",
                    "--without-antigravity",
                    "--without-kimi",
                    "--sensitive-scan-token",
                    token,
                ],
                cwd=repo,
                env=environment,
                check=False,
            )
            count = invocation_count.read_text(encoding="utf-8")
        self.assertEqual(reused.returncode, 2)
        self.assertIn("already consumed", reused.stderr)
        self.assertEqual(count, "1")

    def test_fast_mode_wires_phase_effort_into_run_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            repo = root / "repo"
            bin_dir = root / "bin"
            home.mkdir()
            repo.mkdir()
            bin_dir.mkdir()
            initialize_repo(repo)
            (repo / "src" / "feature.py").write_text(
                "VALUE = 2\n", encoding="utf-8"
            )

            fake_claude = bin_dir / "claude"
            fake_claude.write_text(
                textwrap.dedent(
                    """\
                    #!/bin/sh
                    if [ "$1" = "--version" ]; then
                      echo "fake-claude 1.0"
                      exit 0
                    fi
                    cat >/dev/null
                    python3 -c 'import json; print(json.dumps({
                      "result": "# Verdict\\nPASS_CLEAN\\n\\n# Findings\\nNone.\\n\\n# Test gaps\\nNone.\\n",
                      "total_cost_usd": 0.01
                    }))'
                    """
                ),
                encoding="utf-8",
            )
            fake_claude.chmod(0o755)
            environment = os.environ.copy()
            environment["HOME"] = str(home)
            environment["PATH"] = f"{bin_dir}:{environment['PATH']}"
            base = [sys.executable, str(SCRIPT_PATH)]
            started = run(
                [
                    *base,
                    "workflow",
                    "start",
                    "--review-mode",
                    "fast",
                    "--max-budget-usd",
                    "3",
                ],
                cwd=repo,
                env=environment,
            )
            workflow_identifier = started.stdout.strip()
            run(
                [
                    *base,
                    "run",
                    "--repo",
                    str(repo),
                    "--workflow-id",
                    workflow_identifier,
                    "--path",
                    "src",
                    "--task",
                    "Change the feature value.",
                    "--without-antigravity",
                    "--without-kimi",
                ],
                cwd=repo,
                env=environment,
            )
            run(
                [
                    *base,
                    "run",
                    "--repo",
                    str(repo),
                    "--workflow-id",
                    workflow_identifier,
                    "--phase",
                    "confirmation",
                    "--reuse-contract",
                    "--without-antigravity",
                    "--without-kimi",
                ],
                cwd=repo,
                env=environment,
            )
            metadata = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in (home / ".codex" / "review-runs").glob(
                    "*/*/metadata.json"
                )
            ]

        by_phase = {item["phase"]: item for item in metadata}
        self.assertEqual(set(by_phase), {"repair", "confirmation"})
        self.assertEqual(by_phase["repair"]["review_mode"], "fast")
        self.assertEqual(
            by_phase["repair"]["review_policy"]["claude_effort"], "low"
        )
        self.assertEqual(
            by_phase["confirmation"]["review_policy"]["claude_effort"],
            "medium",
        )

    def test_run_triage_finalize_and_freshness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            repo = root / "repo"
            bin_dir = root / "bin"
            home.mkdir()
            repo.mkdir()
            bin_dir.mkdir()
            initialize_repo(repo)
            (repo / "src" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
            (repo / "unrelated.txt").write_text("dirty\n", encoding="utf-8")

            fake_claude = bin_dir / "claude"
            fake_claude.write_text(
                textwrap.dedent(
                    """\
                    #!/bin/sh
                    if [ "$1" = "--version" ]; then
                      echo "fake-claude 1.0"
                      exit 0
                    fi
                    cat >/dev/null
                    python3 -c 'import json; print(json.dumps({
                      "result": "# Verdict\\nPASS_WITH_FINDINGS\\n\\n# Findings\\n## [medium] Add a behavior assertion\\n- Location: src/feature.py:1\\n- Trigger: value changes\\n- Evidence: adapter behavior is not asserted\\n- Impact: bounded regression risk\\n- Smallest fix: add an assertion\\n- Confidence: medium\\n\\n# Test gaps\\n- Add a behavior assertion.\\n\\n# Notes\\nNone.\\n",
                      "duration_ms": 25,
                      "total_cost_usd": 0.01,
                      "usage": {"input_tokens": 10, "output_tokens": 20}
                    }))'
                    """
                ),
                encoding="utf-8",
            )
            fake_claude.chmod(0o755)
            config_dir = home / ".config" / "multi-model-review"
            config_dir.mkdir(parents=True)
            (config_dir / "config.json").write_text(
                json.dumps({"antigravity": {"enabled": False}}),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["HOME"] = str(home)
            environment["PATH"] = f"{bin_dir}:{environment['PATH']}"
            base = [sys.executable, str(SCRIPT_PATH)]

            completed = run(
                [
                    *base,
                    "run",
                    "--repo",
                    str(repo),
                    "--path",
                    "src",
                    "--risk",
                    "db-write",
                    "--task",
                    "Change the feature value.",
                ],
                cwd=repo,
                env=environment,
            )
            self.assertIn("PASS_WITH_FINDINGS", completed.stdout)

            metadata_paths = list(
                (home / ".codex" / "review-runs").glob("*/*/metadata.json")
            )
            self.assertEqual(len(metadata_paths), 1)
            run_dir = metadata_paths[0].parent
            metadata = json.loads(metadata_paths[0].read_text(encoding="utf-8"))
            self.assertEqual(metadata["schema_version"], MM.SCHEMA_VERSION)
            self.assertEqual(metadata["status"], "completed")
            self.assertEqual(metadata["phase"], "repair")
            self.assertEqual(metadata["paths"], ["src/feature.py"])
            self.assertEqual(
                metadata["excluded_changed_paths"], ["unrelated.txt"]
            )
            self.assertEqual(metadata["reviewers"]["claude"]["cli_version"], "fake-claude 1.0")
            self.assertEqual(metadata["reviewers"]["claude"]["usage"]["total_cost_usd"], 0.01)
            self.assertEqual(
                metadata["review_policy"]["claude_max_budget_usd"], 1.25
            )

            blocked_round = run(
                [
                    *base,
                    "run",
                    "--repo",
                    str(repo),
                    "--path",
                    "src",
                    "--workflow-id",
                    metadata["workflow_id"],
                    "--round",
                    "2",
                    "--risk",
                    "db-write",
                    "--task",
                    "Change the feature value.",
                ],
                cwd=repo,
                env=environment,
                check=False,
            )
            self.assertEqual(blocked_round.returncode, 2)
            self.assertIn("fully triaged", blocked_round.stderr)

            commands = [
                [
                    *base,
                    "decide",
                    "--run",
                    str(run_dir),
                    "--finding",
                    "claude-001",
                    "--decision",
                    "rejected",
                    "--evidence",
                    "The focused behavior assertion already passes.",
                    "--verification",
                    "unit test passed",
                ],
                [
                    *base,
                    "decide",
                    "--run",
                    str(run_dir),
                    "--finding",
                    "claude-test-001",
                    "--decision",
                    "covered",
                    "--evidence",
                    "The same focused behavior assertion covers this gap.",
                    "--verification",
                    "unit test passed",
                ],
            ]
            processes = [
                subprocess.Popen(
                    command,
                    cwd=repo,
                    env=environment,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for command in commands
            ]
            for process in processes:
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, stderr or stdout)
            triage = json.loads(
                (run_dir / "triage.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                triage["findings"][0]["decision_history"][0]["decision"],
                "rejected",
            )
            self.assertEqual(
                triage["test_gaps"][0]["decision_history"][0]["decision"],
                "covered",
            )
            repair_finalize = run(
                [
                    *base,
                    "finalize",
                    "--run",
                    str(run_dir),
                    "--codex-review",
                    "Final diff review found no remaining defects.",
                    "--verification",
                    "python tests: passed",
                ],
                cwd=repo,
                env=environment,
                check=False,
            )
            self.assertEqual(repair_finalize.returncode, 2)
            self.assertIn("Repair rounds cannot be finalized", repair_finalize.stderr)

            mismatched_confirmation = run(
                [
                    *base,
                    "run",
                    "--repo",
                    str(repo),
                    "--workflow-id",
                    metadata["workflow_id"],
                    "--phase",
                    "confirmation",
                    "--base",
                    "HEAD",
                    "--reuse-contract",
                ],
                cwd=repo,
                env=environment,
                check=False,
            )
            self.assertEqual(mismatched_confirmation.returncode, 2)
            self.assertIn(
                "scope selector does not match",
                mismatched_confirmation.stderr,
            )

            confirmation = run(
                [
                    *base,
                    "run",
                    "--repo",
                    str(repo),
                    "--workflow-id",
                    metadata["workflow_id"],
                    "--phase",
                    "confirmation",
                    "--uncommitted",
                    "--reuse-contract",
                ],
                cwd=repo,
                env=environment,
            )
            self.assertIn("phase=confirmation", confirmation.stdout)
            confirmation_metadata_paths = [
                path
                for path in (home / ".codex" / "review-runs").glob(
                    "*/*/metadata.json"
                )
                if json.loads(path.read_text(encoding="utf-8")).get("phase")
                == "confirmation"
            ]
            self.assertEqual(len(confirmation_metadata_paths), 1)
            run_dir = confirmation_metadata_paths[0].parent
            confirmation_metadata = json.loads(
                confirmation_metadata_paths[0].read_text(encoding="utf-8")
            )
            self.assertEqual(confirmation_metadata["round"], 2)
            confirmation_triage = json.loads(
                (run_dir / "triage.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                confirmation_triage["findings"][0]["prior_matches"][0]["round"],
                1,
            )
            for item_id, decision in (
                ("claude-001", "rejected"),
                ("claude-test-001", "covered"),
            ):
                command = [
                    *base,
                    "decide",
                    "--run",
                    str(run_dir),
                    "--finding",
                    item_id,
                    "--decision",
                    decision,
                    "--evidence",
                    "The focused behavior assertion passes.",
                    "--verification",
                    "unit test passed",
                ]
                run(command, cwd=repo, env=environment)

            finalized = run(
                [
                    *base,
                    "finalize",
                    "--run",
                    str(run_dir),
                    "--codex-review",
                    "Confirmation review found no remaining defects.",
                    "--verification",
                    "python tests: passed",
                ],
                cwd=repo,
                env=environment,
            )
            self.assertIn("PASS_CLEAN", finalized.stdout)
            run(
                [*base, "verify", "--run", str(run_dir)],
                cwd=repo,
                env=environment,
            )
            workflow = metadata["workflow_id"]
            workflow_final = run(
                [*base, "workflow", "finalize", workflow],
                cwd=repo,
                env=environment,
            )
            self.assertIn("Workflow PASS", workflow_final.stdout)

            (repo / "unrelated.txt").write_text(
                "another unrelated change\n", encoding="utf-8"
            )
            still_fresh = run(
                [*base, "verify", "--run", str(run_dir)],
                cwd=repo,
                env=environment,
            )
            self.assertIn('"fresh": true', still_fresh.stdout)

            run(["git", "add", "src/feature.py"], cwd=repo)
            run(["git", "commit", "-qm", "reviewed change"], cwd=repo)
            committed_fresh = run(
                [*base, "verify", "--run", str(run_dir)],
                cwd=repo,
                env=environment,
            )
            self.assertIn('"fresh": true', committed_fresh.stdout)
            self.assertIn(
                '"freshness_mode": "committed-equivalent"',
                committed_fresh.stdout,
            )
            committed_workflow = run(
                [*base, "workflow", "status", workflow],
                cwd=repo,
                env=environment,
            )
            self.assertIn('"ready": true', committed_workflow.stdout)
            self.assertIn('"failed_runs": 1', committed_workflow.stdout)
            attested = run(
                [
                    *base,
                    "attest-commit",
                    "--run",
                    str(run_dir),
                    "--commit",
                    "HEAD",
                ],
                cwd=repo,
                env=environment,
            )
            self.assertIn("Commit attested", attested.stdout)
            final = json.loads(
                (run_dir / "final.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(final["commit_attestations"]), 1)

            (repo / "src" / "feature.py").write_text("VALUE = 3\n", encoding="utf-8")
            stale = run(
                [*base, "verify", "--run", str(run_dir)],
                cwd=repo,
                env=environment,
                check=False,
            )
            self.assertEqual(stale.returncode, 3)
            self.assertIn('"fresh": false', stale.stdout)
            workflow_stale = run(
                [*base, "workflow", "status", workflow],
                cwd=repo,
                env=environment,
                check=False,
            )
            self.assertEqual(workflow_stale.returncode, 3)

            secret = "do-not-print-this-secret-123456789"
            (repo / ".env").write_text(
                f"API_KEY={secret}\n", encoding="utf-8"
            )
            blocked = run(
                [
                    *base,
                    "run",
                    "--repo",
                    str(repo),
                    "--path",
                    ".env",
                ],
                cwd=repo,
                env=environment,
                check=False,
            )
            self.assertEqual(blocked.returncode, 2)
            self.assertNotIn(secret, blocked.stderr)
            failed_metadata = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in (home / ".codex" / "review-runs").glob(
                    "*/*/metadata.json"
                )
                if json.loads(path.read_text(encoding="utf-8")).get("status")
                == "failed"
            ]
            self.assertEqual(len(failed_metadata), 2)
            self.assertTrue(
                all(item["status"] == "failed" for item in failed_metadata)
            )

    def test_partial_run_resumes_only_failed_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            repo = root / "repo"
            bin_dir = root / "bin"
            home.mkdir()
            repo.mkdir()
            bin_dir.mkdir()
            initialize_repo(repo)
            (repo / "src" / "feature.py").write_text(
                "VALUE = 2\n", encoding="utf-8"
            )
            claude_count = root / "claude-count"
            kimi_marker = root / "kimi-marker"
            fake_claude = bin_dir / "claude"
            fake_claude.write_text(
                textwrap.dedent(
                    f"""\
                    #!/usr/bin/env python3
                    import json
                    import pathlib
                    import sys
                    if "--version" in sys.argv:
                        print("fake-claude 1.0")
                        raise SystemExit(0)
                    path = pathlib.Path({str(claude_count)!r})
                    count = int(path.read_text() or "0") if path.exists() else 0
                    path.write_text(str(count + 1))
                    print(json.dumps({{
                        "result": "# Verdict\\nPASS_CLEAN\\n\\n# Findings\\nNone.\\n\\n# Test gaps\\nNone.\\n",
                        "total_cost_usd": 0.01
                    }}))
                    """
                ),
                encoding="utf-8",
            )
            fake_claude.chmod(0o755)
            fake_kimi = bin_dir / "kimi"
            fake_kimi.write_text(
                textwrap.dedent(
                    f"""\
                    #!/usr/bin/env python3
                    import json
                    import pathlib
                    import sys
                    if "--version" in sys.argv:
                        print("fake-kimi 1.0")
                        raise SystemExit(0)
                    if sys.argv[1:4] == ["provider", "list", "--json"]:
                        print(json.dumps({{"providers": {{"fake": {{}}}}, "models": {{"k3-256k": {{}}}}}}))
                        raise SystemExit(0)
                    marker = pathlib.Path({str(kimi_marker)!r})
                    if not marker.exists():
                        marker.write_text("failed")
                        print('error: Model "k3-256k" temporarily unavailable', file=sys.stderr)
                        raise SystemExit(1)
                    print("# Verdict\\nPASS_CLEAN\\n\\n# Findings\\nNone.\\n\\n# Test gaps\\nNone.\\n")
                    """
                ),
                encoding="utf-8",
            )
            fake_kimi.chmod(0o755)
            config_dir = home / ".config" / "multi-model-review"
            config_dir.mkdir(parents=True)
            (config_dir / "config.json").write_text(
                json.dumps(
                    {
                        "antigravity": {"enabled": False},
                        "kimi": {"enabled": True, "model": "k3-256k"},
                    }
                ),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["HOME"] = str(home)
            environment["PATH"] = f"{bin_dir}:{environment['PATH']}"
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            base = [sys.executable, str(SCRIPT_PATH)]
            initial = run(
                [
                    *base,
                    "run",
                    "--repo",
                    str(repo),
                    "--path",
                    "src",
                    "--task",
                    "Review resume behavior.",
                ],
                cwd=repo,
                env=environment,
                check=False,
            )
            self.assertEqual(initial.returncode, 2, initial.stderr)
            metadata_path = next(
                (home / ".codex" / "review-runs").glob("*/*/metadata.json")
            )
            run_dir = metadata_path.parent
            partial = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(
                partial["status"],
                "partial",
                (
                    f"stderr={initial.stderr}; failure_type="
                    f"{partial.get('failure', {}).get('type')}; reviewer_exits="
                    f"{[(name, item.get('exit_code')) for name, item in partial.get('reviewers', {}).items()]}"
                ),
            )
            self.assertEqual(partial["failure"]["type"], "reviewer_failure")
            stale_snapshot = run_dir / "snapshot"
            stale_snapshot.mkdir()
            (stale_snapshot / "left-behind.txt").write_text(
                "ephemeral", encoding="utf-8"
            )
            resumed = run(
                [*base, "resume", "--run", str(run_dir)],
                cwd=repo,
                env=environment,
            )
            self.assertIn("resumed and completed", resumed.stdout)
            completed = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["reviewers"]["kimi"]["exit_code"], 0)
            self.assertEqual(claude_count.read_text(encoding="utf-8"), "1")


if __name__ == "__main__":
    unittest.main()
