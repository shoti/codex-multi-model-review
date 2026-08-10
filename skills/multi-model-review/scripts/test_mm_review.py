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
                "# Test gaps\nNone.\n\n# Coverage\n- Complete: yes\n"
                "- Unreviewed changed paths: []\n- Limitations: []\n\n"
                "# Notes\nNone.\n",
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

    def test_base_snapshot_overlays_in_scope_revert_to_base_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            run_dir = root / "run"
            repo.mkdir()
            run_dir.mkdir()
            initialize_repo(repo)
            (repo / "src" / "secrets.env").write_text(
                'password = "integration-only-value-73918426"\n',
                encoding="utf-8",
            )
            run(["git", "add", "src/secrets.env"], cwd=repo)
            run(["git", "commit", "-qm", "add base fixture"], cwd=repo)
            base = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
            (repo / "src" / "feature.py").write_text(
                "VALUE = 2\n", encoding="utf-8"
            )
            (repo / "src" / "secrets.env").write_text(
                "REDACTED = True\n", encoding="utf-8"
            )
            (repo / "unrelated.txt").write_text(
                "branch\n", encoding="utf-8"
            )
            run(
                [
                    "git",
                    "add",
                    "src/feature.py",
                    "src/secrets.env",
                    "unrelated.txt",
                ],
                cwd=repo,
            )
            run(["git", "commit", "-qm", "branch changes"], cwd=repo)

            (repo / "src" / "feature.py").write_text(
                "VALUE = 1\n", encoding="utf-8"
            )
            (repo / "src" / "secrets.env").write_text(
                'password = "integration-only-value-73918426"\n',
                encoding="utf-8",
            )
            (repo / "unrelated.txt").write_text(
                "local unrelated\n", encoding="utf-8"
            )
            scope = MM.Scope("base", base, "working tree against base")
            paths = MM.changed_paths(repo, scope, ("src",))
            self.assertEqual(paths, [])
            overlay_paths = MM.snapshot_overlay_paths(
                repo, scope, paths, ("src",)
            )
            self.assertEqual(
                overlay_paths,
                ["src/feature.py", "src/secrets.env"],
            )

            snapshot = MM.create_snapshot(
                repo,
                scope,
                overlay_paths,
                run_dir,
            )

            self.assertEqual(
                (snapshot / "src" / "feature.py").read_text(encoding="utf-8"),
                "VALUE = 1\n",
            )
            self.assertEqual(
                (snapshot / "unrelated.txt").read_text(encoding="utf-8"),
                "branch\n",
            )
            self.assertTrue(MM.is_sensitive_path("src/secrets.env"))
            findings = MM.sensitive_content_findings(
                snapshot, overlay_paths, ""
            )
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].path, "src/secrets.env")

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
                        "response": "# Verdict\\nPASS_CLEAN\\n\\n# Findings\\nNone.\\n\\n# Test gaps\\nNone.\\n\\n# Coverage\\n- Complete: yes\\n- Unreviewed changed paths: []\\n- Limitations: []\\n\\n# Notes\\nNone.\\n",
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
            "Limited review time, context, budget, or tool access is not itself",
            collapsed_prompt,
        )
        self.assertIn(
            "marking coverage incomplete",
            collapsed_prompt,
        )
        self.assertIn("# Coverage", prompt)
        self.assertIn("Do not hide incomplete coverage in Notes", prompt)

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

    def test_incomplete_coverage_is_structured_and_requires_compensation(
        self,
    ) -> None:
        report = textwrap.dedent(
            """\
            # Verdict
            PASS_CLEAN

            # Findings
            None.

            # Test gaps
            None.

            # Coverage
            - Complete: no
            - Unreviewed changed paths: ["src/large.ts"]
            - Limitations: ["Budget ended before the full file was traced."]

            # Notes
            - No defect was found in inspected code.
            """
        )
        parsed = MM.parse_review_report("claude", report)
        self.assertFalse(parsed["coverage"]["complete"])
        self.assertEqual(
            parsed["coverage"]["unreviewed_changed_paths"],
            ["src/large.ts"],
        )
        self.assertTrue(parsed["coverage"]["contract_valid"])
        self.assertFalse(
            MM.parsed_report_is_invalid(parsed, require_coverage=True)
        )

        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            MM.safe_write_json(
                run_dir / "metadata.json",
                {
                    "status": "completed",
                    "phase": "confirmation",
                    "source_fingerprint": "fingerprint",
                    "coverage_contract_required": True,
                    "risks": [],
                },
            )
            MM.safe_write_json(
                run_dir / "triage.json",
                {"findings": [], "test_gaps": []},
            )
            MM.safe_write_json(
                run_dir / "review-summary.json",
                {"reviews": {"claude": parsed}},
            )
            without_compensation = MM.build_parser().parse_args(
                [
                    "finalize",
                    "--run",
                    str(run_dir),
                    "--codex-review",
                    "No reachable defect found.",
                ]
            )
            with (
                mock.patch.object(
                    MM,
                    "freshness_status",
                    return_value={"fresh": True, "mode": "working-tree"},
                ),
                self.assertRaisesRegex(
                    MM.ReviewError, "Confirmation coverage is incomplete"
                ),
            ):
                MM.finalize_command(without_compensation)

            compensated = MM.build_parser().parse_args(
                [
                    "finalize",
                    "--run",
                    str(run_dir),
                    "--codex-review",
                    "No reachable defect found.",
                    "--coverage-verification",
                    "Read src/large.ts in full and traced both callers.",
                ]
            )
            with mock.patch.object(
                MM,
                "freshness_status",
                return_value={"fresh": True, "mode": "working-tree"},
            ):
                self.assertEqual(MM.finalize_command(compensated), 0)
            final = MM.read_json(run_dir / "final.json")
            self.assertTrue(final["review_coverage"]["gate_compensated"])
            self.assertEqual(
                final["review_coverage"]["incomplete_reviewers"][0]["reviewer"],
                "claude",
            )

    def test_missing_coverage_is_invalid_when_contract_requires_it(self) -> None:
        parsed = MM.parse_review_report(
            "claude",
            "# Verdict\nPASS_CLEAN\n\n# Findings\nNone.\n\n"
            "# Test gaps\nNone.\n",
        )
        self.assertFalse(parsed["coverage"]["contract_valid"])
        self.assertTrue(
            MM.parsed_report_is_invalid(parsed, require_coverage=True)
        )

    def test_coverage_accepts_multiline_json_arrays(self) -> None:
        parsed = MM.parse_review_report(
            "antigravity",
            """# Verdict
PASS_CLEAN

# Findings
None.

# Test gaps
None.

# Coverage
- Complete: no
- Unreviewed changed paths: [
  "src/large.ts"
]
- Limitations: [
  "Context ended before tracing the caller."
]

# Notes
None.
""",
        )
        self.assertTrue(parsed["coverage"]["contract_valid"])
        self.assertEqual(
            parsed["coverage"]["unreviewed_changed_paths"],
            ["src/large.ts"],
        )
        self.assertEqual(
            parsed["coverage"]["limitations"],
            ["Context ended before tracing the caller."],
        )

    def test_base_scope_review_attests_clean_branch_head(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            run_dir = root / "run"
            repo.mkdir()
            run_dir.mkdir()
            initialize_repo(repo)
            base = run(
                ["git", "rev-parse", "HEAD"], cwd=repo
            ).stdout.strip()
            (repo / "src" / "feature.py").write_text(
                "VALUE = 2\n", encoding="utf-8"
            )
            run(["git", "add", "src/feature.py"], cwd=repo)
            run(["git", "commit", "-qm", "feature"], cwd=repo)
            head = run(
                ["git", "rev-parse", "HEAD"], cwd=repo
            ).stdout.strip()
            scope = MM.Scope("base", base, "working tree against base")
            filters = ("src",)
            paths = MM.changed_paths(repo, scope, filters)
            fingerprint = MM.fingerprint(repo, scope, paths, filters)
            MM.safe_write_json(
                run_dir / "metadata.json",
                {
                    "status": "completed",
                    "repository": {"root": str(repo), "head": head},
                    "scope": MM.dataclasses.asdict(scope),
                    "path_filters": list(filters),
                    "paths": paths,
                    "source_fingerprint": fingerprint,
                    "result_content_fingerprint": MM.content_fingerprint(
                        repo, paths
                    ),
                },
            )
            MM.safe_write_json(
                run_dir / "final.json",
                {"source_fingerprint": fingerprint, "status": "PASS_CLEAN"},
            )

            freshness = MM.freshness_status(
                run_dir,
                MM.read_json(run_dir / "metadata.json"),
                fingerprint,
            )
            self.assertTrue(freshness["fresh"])
            self.assertEqual(freshness["mode"], "committed-equivalent")
            self.assertEqual(freshness["commit"], head)

            args = MM.build_parser().parse_args(
                [
                    "attest-commit",
                    "--run",
                    str(run_dir),
                    "--commit",
                    "HEAD",
                ]
            )
            self.assertEqual(MM.attest_commit_command(args), 0)
            final = MM.read_json(run_dir / "final.json")
            self.assertEqual(final["commit_attestations"][0]["commit"], head)

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
                                "actionable": True,
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
                            "coverage": {
                                "complete": True,
                                "unreviewed_changed_paths": [],
                                "limitations": []
                            },
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
                "# Test gaps\nNone.\n\n# Coverage\n- Complete: yes\n"
                "- Unreviewed changed paths: []\n- Limitations: []\n\n"
                "# Notes\nNone.\n",
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
            triage = MM.read_json(run_dir / "triage.json")
            self.assertTrue(triage["review_coverage"]["claude"]["complete"])
            self.assertEqual(triage["review_notes"]["claude"], [])
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
            mock.patch.object(MM, "workflow_spend", return_value=0.8),
            self.assertRaisesRegex(
                MM.ReviewError, r"below the \$0.25 minimum"
            ),
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

    def test_supplemental_siblings_share_parent_lineage_reservations(self) -> None:
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
                MM.create_workflow("wf-parent", max_budget_usd=2.0)
                MM.create_workflow(
                    "wf-supplemental-one",
                    max_budget_usd=2.0,
                    workflow_kind="supplemental",
                    supplemental_parent_workflow_id="wf-parent",
                )
                MM.create_workflow(
                    "wf-supplemental-two",
                    max_budget_usd=2.0,
                    workflow_kind="supplemental",
                    supplemental_parent_workflow_id="wf-parent",
                )
                first, first_status = MM.apply_workflow_budget(
                    [reviewer], "wf-supplemental-one", reservation_id="run-one"
                )
                second, second_status = MM.apply_workflow_budget(
                    [reviewer], "wf-supplemental-two", reservation_id="run-two"
                )
                lineage = MM.workflow_lineage_ids("wf-supplemental-two")
        self.assertEqual(float(first[0].command[-1]), 1.25)
        self.assertEqual(float(second[0].command[-1]), 0.75)
        self.assertEqual(first_status["reserved_before_run_usd"], 0.0)
        self.assertEqual(second_status["reserved_before_run_usd"], 1.25)
        self.assertEqual(
            lineage,
            ["wf-parent", "wf-supplemental-one", "wf-supplemental-two"],
        )

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

    def test_workflow_supersede_by_rejects_a_different_budget_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workflows = Path(temporary)
            with mock.patch.object(MM, "WORKFLOWS_DIR", workflows):
                MM.create_workflow("wf-old", max_budget_usd=5.0)
                MM.create_workflow("wf-large", max_budget_usd=1000.0)
                args = MM.build_parser().parse_args(
                    [
                        "workflow",
                        "supersede",
                        "wf-old",
                        "--by",
                        "wf-large",
                        "--reason",
                        "contract changed",
                    ]
                )
                with self.assertRaisesRegex(
                    MM.ReviewError, "must exactly match"
                ):
                    MM.workflow_supersede_command(args)
                old = MM.read_json(workflows / "wf-old.json")
                replacement = MM.read_json(workflows / "wf-large.json")
        self.assertNotIn("superseded_by", old)
        self.assertEqual(replacement["supersedes"], [])

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
                "preflight_blocked_runs": 0,
                "reviewer_invocations": 2,
                "successful_invocations": 1,
                "reviewer_duration_seconds": 1.1,
                "reported_cost_usd": 0.2,
                "attempts_with_token_usage": 0,
                "token_usage": MM.empty_token_usage(),
                "artifact_bytes": MM.empty_artifact_bytes(),
                "findings": 0,
                "test_gaps": 0,
                "coverage_complete_reviews": 0,
                "incomplete_coverage_reviews": 0,
                "unknown_coverage_reviews": 1,
                "unreviewed_changed_paths": 0,
                "decisions": {},
            },
        )

    def test_preflight_blocks_are_not_counted_as_failed_reviews(self) -> None:
        run_dir = Path("/tmp/preflight-block")
        metadata = {
            "created_at": MM.utc_now(),
            "status": "preflight_blocked",
            "workflow_id": "wf-preflight",
            "review_mode": "deep",
            "phase": "confirmation",
            "failure": {
                "type": "ReviewError",
                "message": "Sensitive material requires inspection.",
            },
        }
        metrics = MM.workflow_metrics(
            [(run_dir, metadata)], include_artifact_bytes=False
        )
        self.assertEqual(metrics["preflight_blocked_runs"], 1)
        self.assertEqual(metrics["failed_runs"], 0)
        self.assertEqual(metrics["failed_reviewer_invocations"], 0)
        with (
            mock.patch.object(
                MM, "all_run_metadata", return_value=[(run_dir, metadata)]
            ),
            mock.patch.object(MM, "workflow_path", return_value=Path("/missing")),
            mock.patch.object(MM, "WORKFLOWS_DIR", Path("/missing")),
        ):
            report = MM.analytics_report(7)
        self.assertEqual(report["run_statuses"]["preflight_blocked"], 1)
        self.assertEqual(report["run_statuses"]["failed"], 0)
        self.assertEqual(
            report["review_modes"]["deep"]["preflight_blocked_runs"], 1
        )

    def test_analytics_aggregates_provider_reported_tokens_and_artifact_bytes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            run_dir.mkdir()
            (run_dir / "prompt.md").write_text("abc", encoding="utf-8")
            (run_dir / "manifest.md").write_text("m", encoding="utf-8")
            (run_dir / "change.patch").write_text("patch", encoding="utf-8")
            (run_dir / "claude.md").write_text("report", encoding="utf-8")
            (run_dir / "claude.raw.json").write_text("{}", encoding="utf-8")
            metadata = {
                "created_at": MM.utc_now(),
                "status": "completed",
                "workflow_id": "wf-token-test",
                "review_mode": "balanced",
                "phase": "confirmation",
                "reviewers": {
                    "claude": {
                        "exit_code": 0,
                        "verdict": "PASS_CLEAN",
                        "report_contract_valid": True,
                        "duration_seconds": 2,
                        "model": "sonnet",
                        "usage": {
                            "total_cost_usd": 0.51,
                            "num_turns": 3,
                            "modelUsage": {
                                "claude-sonnet": {
                                    "inputTokens": 10,
                                    "cacheCreationInputTokens": 20,
                                    "cacheReadInputTokens": 30,
                                    "outputTokens": 40,
                                    "costUSD": 0.5,
                                },
                                "claude-haiku": {
                                    "inputTokens": 1,
                                    "cacheCreationInputTokens": 2,
                                    "cacheReadInputTokens": 3,
                                    "outputTokens": 4,
                                    "costUSD": 0.01,
                                },
                            },
                            "usage": {
                                "input_tokens": 999,
                                "output_tokens": 999,
                            },
                        },
                    }
                },
            }
            with (
                mock.patch.object(MM, "all_run_metadata", return_value=[(run_dir, metadata)]),
                mock.patch.object(MM, "workflow_path", return_value=Path("/missing")),
                mock.patch.object(MM, "WORKFLOWS_DIR", Path("/missing")),
                mock.patch.object(
                    MM, "run_artifact_bytes", wraps=MM.run_artifact_bytes
                ) as artifact_bytes,
            ):
                report = MM.analytics_report(7)
        self.assertEqual(artifact_bytes.call_count, 1)
        expected_tokens = {
            "input_tokens": 11,
            "cache_creation_input_tokens": 22,
            "cache_read_input_tokens": 33,
            "output_tokens": 44,
            "total_input_tokens": 66,
            "total_tokens": 110,
        }
        self.assertEqual(report["metrics"]["token_usage"], expected_tokens)
        self.assertEqual(
            report["providers"]["claude"]["token_usage"], expected_tokens
        )
        self.assertEqual(
            report["review_phases"]["confirmation"]["token_usage"],
            expected_tokens,
        )
        self.assertEqual(
            report["metrics"]["artifact_bytes"],
            {
                "prompt_bytes": 3,
                "manifest_bytes": 1,
                "patch_bytes": 5,
                "reviewer_report_bytes": 6,
                "raw_response_bytes": 2,
            },
        )
        self.assertEqual(
            report["model_usage"]["claude-sonnet"]["token_usage"]
            ["total_tokens"],
            100,
        )
        self.assertEqual(
            report["model_usage"]["claude-haiku"]["cost_usd"], 0.01
        )

    def test_compact_rendering_never_emits_more_than_json(self) -> None:
        full = '{"ok": true}'
        self.assertEqual(MM.smaller_output(full, "ok"), "ok")
        self.assertEqual(MM.smaller_output(full, "longer compact output"), full)
        args = MM.build_parser().parse_args(
            ["analytics", "--format", "compact"]
        )
        self.assertEqual(args.output_format, "compact")
        default_args = MM.build_parser().parse_args(["analytics"])
        self.assertEqual(default_args.output_format, "json")

    def test_memory_search_compact_renders_empty_and_matched_results(self) -> None:
        empty = MM.render_memory_search_compact(
            {"query": "nothing", "results": []}
        )
        self.assertIn("0 matches", empty)
        rendered = MM.render_memory_search_compact(
            {
                "query": "budget cap",
                "results": [
                    {
                        "kind": "finding",
                        "severity": "high",
                        "decision": "fixed",
                        "title": "Budget cap bypass",
                        "similarity": 0.75,
                        "matched_fields": ["title", "evidence"],
                        "location": "scripts/mm_review.py:1",
                        "workflow_id": "wf-one",
                        "run_id": "run-one",
                    }
                ],
            }
        )
        self.assertIn("Budget cap bypass", rendered)
        self.assertIn("matched=title,evidence", rendered)
        self.assertIn("workflow=wf-one run=run-one", rendered)

    def test_workflow_status_compact_renders_empty_and_detailed_status(self) -> None:
        empty = MM.render_workflow_status_compact({})
        self.assertIn("reviewer calls=0", empty)
        rendered = MM.render_workflow_status_compact(
            {
                "workflow_id": "wf-one",
                "state": "active",
                "ready": False,
                "lineage_root": "wf-root",
                "metrics": {
                    "run_count": 2,
                    "reviewer_invocations": 3,
                    "reported_cost_usd": 1.25,
                    "reviewer_duration_seconds": 4.5,
                },
                "active_runs": [
                    {
                        "round": 2,
                        "phase": "repair",
                        "process_alive": True,
                        "elapsed_seconds": 1.5,
                        "run_dir": "/tmp/run",
                    }
                ],
                "repositories": [
                    {
                        "repository": {"name": "plugin"},
                        "round": 1,
                        "phase": "repair",
                        "state": "triage_required",
                        "final_status": None,
                        "fresh": True,
                    }
                ],
                "history_issues": ["missing confirmation"],
            }
        )
        self.assertIn("Workflow wf-one: state=active", rendered)
        self.assertIn("Active runs:", rendered)
        self.assertIn("- plugin: round=1 phase=repair", rendered)
        self.assertIn("- missing confirmation", rendered)

    def test_real_compact_renderers_never_emit_more_bytes_than_json(self) -> None:
        tokens = MM.empty_token_usage()
        analytics = {
            "since_days": 30,
            "run_attempts": 100,
            "workflow_count": 20,
            "lineage_count": 10,
            "finalized_workflows": 8,
            "workflows_with_run_finals": 9,
            "metrics": {
                "reviewer_invocations": 90,
                "failed_reviewer_invocations": 2,
                "reported_cost_usd": 12.5,
                "reviewer_duration_seconds": 3600,
                "attempts_with_token_usage": 88,
                "token_usage": tokens,
            },
            "providers": {
                f"provider-{index}": {
                    "successful": index,
                    "invocations": index + 1,
                    "cost_usd": index / 10,
                    "token_usage": tokens,
                }
                for index in range(20)
            },
            "review_phases": {
                f"phase-{index}": {
                    "runs": index,
                    "reviewer_invocations": index,
                    "reported_cost_usd": index / 10,
                    "token_usage": tokens,
                }
                for index in range(20)
            },
            "failure_types": {},
        }
        memory = {
            "query": "budget cap",
            "results": [
                {
                    "kind": "finding",
                    "severity": "low",
                    "decision": "fixed",
                    "title": f"Finding {index}",
                    "similarity": 0.75,
                    "matched_fields": ["title", "evidence"],
                    "location": f"file.py:{index}",
                    "workflow_id": "wf-one",
                    "run_id": f"run-{index}",
                }
                for index in range(20)
            ],
        }
        workflow = {
            "workflow_id": "wf-one",
            "state": "active",
            "ready": False,
            "lineage_root": "wf-root",
            "metrics": {
                "run_count": 20,
                "reviewer_invocations": 20,
                "reported_cost_usd": 2.5,
                "reviewer_duration_seconds": 100,
            },
            "active_runs": [],
            "repositories": [
                {
                    "repository": {"name": f"repo-{index}"},
                    "round": 1,
                    "phase": "repair",
                    "state": "triage_required",
                    "final_status": None,
                    "fresh": True,
                }
                for index in range(20)
            ],
            "history_issues": [],
        }
        cases = (
            (analytics, MM.render_analytics_compact(analytics)),
            (memory, MM.render_memory_search_compact(memory)),
            (workflow, MM.render_workflow_status_compact(workflow)),
        )
        for payload, compact in cases:
            full = json.dumps(payload, indent=2)
            output = io.StringIO()
            with redirect_stdout(output):
                MM.print_structured_output(payload, "compact", compact)
            emitted = output.getvalue().rstrip("\n")
            self.assertEqual(emitted, MM.smaller_output(full, compact))
            self.assertLessEqual(len(emitted.encode()), len(full.encode()))


    def test_lineage_spend_and_budget_include_superseded_ancestors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            workflows = runs / "workflows"
            run_dir = runs / "repo-one" / "run-one"
            run_dir.mkdir(parents=True)
            workflows.mkdir()
            MM.safe_write_json(
                workflows / "wf-old.json",
                {
                    "workflow_id": "wf-old",
                    "status": "superseded",
                    "superseded_by": "wf-new",
                    "supersedes": [],
                    "policy": MM.workflow_policy(1.5, "balanced"),
                    "budget_reservations": {
                        "ancestor-run": {
                            "max_budget_usd": 0.25,
                            "reserved_at": MM.utc_now(),
                        }
                    },
                },
            )
            MM.safe_write_json(
                workflows / "wf-new.json",
                {
                    "workflow_id": "wf-new",
                    "supersedes": ["wf-old"],
                    "policy": MM.workflow_policy(1.5, "balanced"),
                },
            )
            MM.safe_write_json(
                run_dir / "metadata.json",
                {
                    "workflow_id": "wf-old",
                    "created_at": MM.utc_now(),
                    "status": "completed",
                    "reviewers": {
                        "claude": {
                            "exit_code": 0,
                            "duration_seconds": 1,
                            "usage": {"total_cost_usd": 1.0},
                        }
                    },
                },
            )
            reviewer = MM.Reviewer(
                "claude",
                ("claude", "--max-budget-usd", "1.25"),
                {},
                "sonnet",
                "test",
            )
            with (
                mock.patch.object(MM, "RUNS_DIR", runs),
                mock.patch.object(MM, "WORKFLOWS_DIR", workflows),
                mock.patch.object(MM, "run_artifact_bytes") as artifact_bytes,
            ):
                self.assertEqual(MM.workflow_spend("wf-new"), 1.0)
                adjusted, budget = MM.apply_workflow_budget(
                    [reviewer], "wf-new", reservation_id="run-new"
                )
            self.assertEqual(adjusted[0].command[-1], "0.25")
            self.assertEqual(budget["spent_before_run_usd"], 1.0)
            self.assertEqual(budget["reserved_before_run_usd"], 0.25)
            artifact_bytes.assert_not_called()

    def test_evidence_memory_rebuilds_and_searches_across_workflows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "run"
            run_dir.mkdir()
            database = root / "evidence.sqlite3"
            MM.safe_write_json(
                run_dir / "metadata.json",
                {
                    "run_id": "run-one",
                    "workflow_id": "wf-successor",
                    "repository": {"id": "repo-one", "name": "repo"},
                    "source_fingerprint": "abc",
                    "phase": "confirmation",
                    "round": 2,
                    "created_at": MM.utc_now(),
                },
            )
            MM.safe_write_json(
                run_dir / "triage.json",
                {
                    "findings": [
                        {
                            "id": "claude-001",
                            "kind": "finding",
                            "reviewer": "claude",
                            "severity": "low",
                            "title": "Duration alert rounds below threshold",
                            "location": "services/alerts.py:42",
                            "decision": "fixed",
                            "evidence": "Message now retains precision.",
                            "action": "Preserve millisecond precision.",
                            "verification": "Focused test passes.",
                        }
                    ],
                    "test_gaps": [],
                },
            )
            rebuilt = MM.rebuild_evidence_memory(
                database, [(run_dir, "wf-root")]
            )
            results = MM.search_evidence_memory(
                database,
                "duration threshold",
                repository_id="repo-one",
            )
            evidence_results = MM.search_evidence_memory(
                database,
                "message retains precision",
                repository_id="repo-one",
            )
            path_results = MM.search_evidence_memory(
                database,
                "alerts py",
                repository_id="repo-one",
            )
            broad_results = MM.search_evidence_memory(
                database,
                "duration",
                repository_id="repo-one",
            )
            null_results = MM.search_evidence_memory(
                database,
                "none",
                repository_id="repo-one",
            )
            compacted = MM.compact_evidence_memory(database)
            self.assertEqual(rebuilt["evidence_items"], 1)
            self.assertEqual(results[0]["lineage_root"], "wf-root")
            self.assertEqual(results[0]["decision"], "fixed")
            self.assertIn("title", results[0]["matched_fields"])
            self.assertIn("evidence", evidence_results[0]["matched_fields"])
            self.assertIn("location", path_results[0]["matched_fields"])
            self.assertEqual(broad_results, [])
            self.assertEqual(null_results, [])
            self.assertEqual(database.stat().st_mode & 0o777, 0o600)
            self.assertEqual(compacted["evidence_items"], 1)

    def test_analytics_reports_lineage_outcomes_and_run_finals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflows = root / "workflows"
            run_dir = root / "repo" / "run"
            workflows.mkdir()
            run_dir.mkdir(parents=True)
            metadata = {
                "run_id": "run-one",
                "workflow_id": "wf-one",
                "created_at": MM.utc_now(),
                "status": "completed",
                "review_mode": "balanced",
                "repository": {"id": "repo-one"},
                "reviewers": {},
            }
            MM.safe_write_json(run_dir / "metadata.json", metadata)
            MM.safe_write_json(run_dir / "triage.json", {"findings": [], "test_gaps": []})
            MM.safe_write_json(run_dir / "final.json", {"status": "PASS_CLEAN"})
            MM.safe_write_json(
                workflows / "wf-one.json",
                {
                    "workflow_id": "wf-one",
                    "supersedes": [],
                    "policy": MM.workflow_policy(5.0, "balanced"),
                },
            )
            with (
                mock.patch.object(MM, "RUNS_DIR", root),
                mock.patch.object(MM, "WORKFLOWS_DIR", workflows),
                mock.patch.object(MM, "all_run_metadata", return_value=[(run_dir, metadata)]),
            ):
                report = MM.analytics_report(7)
            self.assertEqual(report["finalized_workflows"], 0)
            self.assertEqual(report["workflows_with_run_finals"], 1)
            self.assertEqual(report["lineage_count"], 1)
            self.assertEqual(report["lineage_outcomes"], {"PASS_CLEAN": 1})
            self.assertEqual(
                report["review_modes"]["balanced"]["lineage_outcomes"],
                {"PASS_CLEAN": 1},
            )

    def test_analytics_separates_explicit_and_inferred_legacy_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflows = root / "workflows"
            workflows.mkdir()
            runs = []
            for identifier, mode in (
                ("wf-explicit", "balanced"),
                ("wf-legacy", None),
            ):
                run_dir = root / identifier / "run"
                run_dir.mkdir(parents=True)
                metadata = {
                    "run_id": f"run-{identifier}",
                    "workflow_id": identifier,
                    "created_at": MM.utc_now(),
                    "status": "completed",
                    "review_mode": mode,
                    "repository": {"id": identifier},
                    "reviewers": {},
                }
                MM.safe_write_json(
                    run_dir / "triage.json", {"findings": [], "test_gaps": []}
                )
                MM.safe_write_json(run_dir / "final.json", {"status": "PASS_CLEAN"})
                policy = (
                    MM.workflow_policy(5.0, "balanced")
                    if mode
                    else {
                        "max_repair_rounds": 3,
                        "confirmation_required": True,
                        "max_budget_usd": 5.0,
                    }
                )
                MM.safe_write_json(
                    workflows / f"{identifier}.json",
                    {"workflow_id": identifier, "supersedes": [], "policy": policy},
                )
                runs.append((run_dir, metadata))
            with (
                mock.patch.object(MM, "RUNS_DIR", root),
                mock.patch.object(MM, "WORKFLOWS_DIR", workflows),
                mock.patch.object(MM, "all_run_metadata", return_value=runs),
            ):
                report = MM.analytics_report(7)
        self.assertEqual(
            report["lineage_mode_cohorts"]["explicit"]["balanced"]["lineages"],
            1,
        )
        self.assertEqual(
            report["lineage_mode_cohorts"]["inferred_legacy"]["deep"]["lineages"],
            1,
        )
        self.assertNotIn("lineages", report["review_modes"].get("deep", {}))
        report["run_statuses"]["unclassified"] = 2
        report["metrics"].update(
            {
                "memory_candidate_items": 3,
                "memory_candidate_matches": 4,
                "memory_structural_matches": 1,
                "memory_assessments": {"mixed": 1},
            }
        )
        compact = MM.render_analytics_compact(report)
        self.assertIn(
            "Evidence memory: candidate-items=3; matches=4; structural=1; assessed=1",
            compact,
        )
        self.assertIn("Run status warning: 2 legacy/unclassified records", compact)
        self.assertIn("Mode cohorts: explicit=1; inferred-legacy=1", compact)

    def test_budget_estimate_uses_comparable_history_without_changing_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = []
            for index, cost in enumerate((0.40, 0.50, 0.60, 0.70, 0.80, 0.90)):
                run_dir = root / f"run-{index}"
                run_dir.mkdir()
                (run_dir / "change.patch").write_text("x" * 100, encoding="utf-8")
                runs.append(
                    (
                        run_dir,
                        {
                            "created_at": MM.utc_now(),
                            "review_mode": "balanced",
                            "review_policy": {"claude_effort": "medium"},
                            "reviewers": {
                                "claude": {
                                    "model": "sonnet",
                                    "exit_code": 0,
                                    "verdict": "PASS_CLEAN",
                                    "usage": {"total_cost_usd": cost},
                                    "failure_category": (
                                        "budget_exhausted" if index == 5 else None
                                    ),
                                }
                            },
                        },
                    )
                )
            missing_usage_dir = root / "run-missing-usage"
            missing_usage_dir.mkdir()
            (missing_usage_dir / "change.patch").write_text(
                "x" * 100, encoding="utf-8"
            )
            runs.append(
                (
                    missing_usage_dir,
                    {
                        "created_at": MM.utc_now(),
                        "review_mode": "balanced",
                        "review_policy": {"claude_effort": "medium"},
                        "reviewers": {
                            "claude": {
                                "model": "sonnet",
                                "exit_code": 1,
                                "failure_category": "budget_exhausted",
                            }
                        },
                    },
                )
            )
            with mock.patch.object(MM, "all_run_metadata", return_value=runs):
                estimate = MM.historical_budget_estimate(
                    provider="claude",
                    model="sonnet",
                    effort="medium",
                    review_mode="balanced",
                    patch_bytes=100,
                    configured_budget_usd=0.50,
                )
        self.assertEqual(estimate["cohort"], "same_mode_model_effort_and_size")
        self.assertEqual(estimate["evidence_attempt_count"], 7)
        self.assertEqual(estimate["sample_count"], 6)
        self.assertEqual(estimate["confidence"], "medium")
        self.assertEqual(estimate["budget_exhausted_attempts"], 2)
        self.assertTrue(estimate["configured_below_recommendation"])
        self.assertFalse(estimate["automatic_policy_change"])
        args = MM.build_parser().parse_args(["budget-estimate", "--uncommitted"])
        self.assertEqual(args.review_mode, "balanced")

    def test_workflow_audit_classifies_actionable_history_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflows = root / "workflows"
            workflows.mkdir()
            pending_dir = root / "repo" / "pending"
            final_dir = root / "repo" / "final"
            stale_dir = root / "repo" / "stale"
            pending_dir.mkdir(parents=True)
            final_dir.mkdir()
            stale_dir.mkdir()
            documents = {
                "wf-20260806T000000Z-pending": {},
                "wf-20260806T000001Z-final": {},
                "wf-20200101T000000Z-stale": {},
                "wf-20260806T000002Z-old": {"status": "superseded"},
            }
            for identifier, extra in documents.items():
                MM.safe_write_json(
                    workflows / f"{identifier}.json",
                    {
                        "workflow_id": identifier,
                        "supersedes": [],
                        "policy": MM.workflow_policy(),
                        **extra,
                    },
                )
            pending_metadata = {
                "run_id": "run-pending",
                "workflow_id": "wf-20260806T000000Z-pending",
                "status": "completed",
                "created_at": MM.utc_now(),
            }
            final_metadata = {
                "run_id": "run-final",
                "workflow_id": "wf-20260806T000001Z-final",
                "status": "completed",
                "created_at": MM.utc_now(),
            }
            stale_metadata = {
                "run_id": None,
                "workflow_id": "wf-20200101T000000Z-stale",
                "created_at": "2020-01-01T00:00:00+00:00",
            }
            MM.safe_write_json(
                pending_dir / "triage.json",
                {
                    "findings": [
                        {
                            "id": "claude-001",
                            "kind": "finding",
                            "decision": "pending",
                        }
                    ],
                    "test_gaps": [],
                },
            )
            MM.safe_write_json(final_dir / "final.json", {"status": "PASS_CLEAN"})
            records = [
                (pending_dir, pending_metadata),
                (final_dir, final_metadata),
                (stale_dir, stale_metadata),
            ]
            with (
                mock.patch.object(MM, "WORKFLOWS_DIR", workflows),
                mock.patch.object(MM, "all_run_metadata", return_value=records),
            ):
                report = MM.workflow_audit_report(7)
        states = {
            entry["workflow_id"]: entry["state"] for entry in report["workflows"]
        }
        self.assertEqual(states["wf-20260806T000000Z-pending"], "needs_triage")
        self.assertEqual(states["wf-20260806T000001Z-final"], "unclosed_run_final")
        self.assertEqual(states["wf-20200101T000000Z-stale"], "stale_incomplete")
        self.assertEqual(states["wf-20260806T000002Z-old"], "superseded")
        self.assertEqual(report["unclassified_run_records"], 1)
        self.assertFalse(report["mutated"])
        compact = MM.render_workflow_audit_compact(report)
        self.assertIn("Workflow audit: 4 workflows; stale threshold=7d", compact)
        self.assertIn(
            "wf-20260806T000000Z-pending: needs_triage; "
            "decide pending or unresolved items",
            compact,
        )
        self.assertIn(
            "wf-20260806T000001Z-final: unclosed_run_final; "
            "verify freshness and run workflow finalize",
            compact,
        )
        args = MM.build_parser().parse_args(["workflow", "audit", "--format", "compact"])
        self.assertEqual(args.output_format, "compact")

    def test_memory_assessment_is_persisted_and_reported_separately(self) -> None:
        triage = {
            "findings": [
                {
                    "id": "claude-001",
                    "kind": "finding",
                    "severity": "low",
                    "memory_matches": [
                        {
                            "similarity": 0.75,
                            "matched_fields": ["title", "location"],
                        }
                    ],
                }
            ],
            "test_gaps": [],
        }
        MM.apply_triage_decision(
            triage,
            identifier="claude-001",
            decision="rejected",
            evidence="Repository evidence disproves the finding.",
            action=None,
            verification=None,
            memory_assessment="useful",
        )
        self.assertEqual(triage["findings"][0]["memory_assessment"], "useful")
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            MM.safe_write_json(run_dir / "triage.json", triage)
            metrics = MM.workflow_metrics(
                [(run_dir, {"status": "completed", "reviewers": {}})],
                include_artifact_bytes=False,
            )
        self.assertEqual(metrics["memory_candidate_items"], 1)
        self.assertEqual(metrics["memory_candidate_matches"], 1)
        self.assertEqual(metrics["memory_structural_matches"], 1)
        self.assertEqual(metrics["memory_assessments"], {"useful": 1})
        self.assertEqual(metrics["memory_similarity_distribution"]["p50"], 0.75)

    def test_non_actionable_finding_is_preserved_as_observation(self) -> None:
        parsed = MM.parse_review_report(
            "claude",
            textwrap.dedent(
                """\
                # Verdict
                PASS_CLEAN

                # Findings
                ## [low] Deterministic timer setup
                - Location: test_feature.py:10
                - Trigger: Test runs
                - Evidence: Fake time advances only in the stub
                - Impact: None - assertions remain deterministic
                - Smallest fix: No action needed
                - Confidence: high

                # Test gaps
                None.

                # Notes
                None.
                """
            ),
        )
        self.assertEqual(parsed["verdict"], "PASS_CLEAN")
        self.assertEqual(parsed["findings"], [])
        self.assertEqual(len(parsed["observations"]), 1)
        self.assertIn("non-actionable", parsed["normalizations"][0])

    def test_observation_only_pass_with_findings_normalizes_to_clean(self) -> None:
        parsed = MM.parse_review_report(
            "claude",
            textwrap.dedent(
                """\
                # Verdict
                PASS_WITH_FINDINGS

                # Findings
                ## [low] Deterministic timer setup
                - Location: test_feature.py:10
                - Trigger: Test runs
                - Evidence: Fake time advances only in the stub
                - Impact: None
                - Smallest fix: No action needed
                - Confidence: high

                # Test gaps
                None.

                # Notes
                None.
                """
            ),
        )
        self.assertEqual(parsed["declared_verdict"], "PASS_WITH_FINDINGS")
        self.assertEqual(parsed["verdict"], "PASS_CLEAN")
        self.assertEqual(parsed["findings"], [])
        self.assertEqual(len(parsed["observations"]), 1)
        self.assertFalse(MM.parsed_report_is_invalid(parsed))

    def test_high_severity_no_action_text_remains_a_finding(self) -> None:
        parsed = MM.parse_review_report(
            "claude",
            textwrap.dedent(
                """\
                # Verdict
                BLOCK

                # Findings
                ## [high] Security invariant is unclear
                - Location: src/security.py:10
                - Trigger: Untrusted input reaches the parser
                - Evidence: The reviewer could not prove the guard
                - Impact: No impact was reproduced
                - Smallest fix: No action needed
                - Confidence: low

                # Test gaps
                None.

                # Notes
                None.
                """
            ),
            risk_profiled=True,
        )
        self.assertEqual(parsed["verdict"], "BLOCK")
        self.assertEqual(len(parsed["findings"]), 1)
        self.assertEqual(parsed["observations"], [])

    def test_non_actionable_observation_is_persisted_in_triage_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            report = run_dir / "claude.md"
            error = run_dir / "claude.stderr.log"
            report.write_text(
                textwrap.dedent(
                    """\
                    # Verdict
                    PASS_CLEAN

                    # Findings
                    ## [low] Harmless test observation
                    - Location: test_feature.py:10
                    - Trigger: Fake clock advances
                    - Evidence: Only the isolated stub changes
                    - Impact: None
                    - Smallest fix: No action needed
                    - Confidence: high

                    # Test gaps
                    None.

                    # Notes
                    None.
                    """
                ),
                encoding="utf-8",
            )
            error.write_text("", encoding="utf-8")
            now = MM.utc_now()
            metadata = {
                "run_id": "run-observation",
                "workflow_id": "wf-observation",
                "repository": {"id": "repo-observation"},
                "created_at": now,
                "started_at": now,
                "risks": [],
            }
            reviewer = MM.Reviewer(
                "claude", ("claude",), {}, "sonnet", "fake"
            )
            result = MM.ReviewResult(
                "claude",
                0,
                report,
                error,
                now,
                now,
                0.1,
                False,
                {"total_cost_usd": 0.01},
                None,
            )
            with mock.patch.object(MM, "workflow_lineage_runs", return_value=[]):
                MM.persist_review_results(
                    run_dir=run_dir,
                    metadata=metadata,
                    reviewers=[reviewer],
                    results=[result],
                )
            triage = MM.read_json(run_dir / "triage.json")
        self.assertEqual(triage["findings"], [])
        self.assertEqual(len(triage["observations"]), 1)

    def test_mode_recommendation_fails_closed_for_explicit_risk(self) -> None:
        args = MM.build_parser().parse_args(
            ["recommend", "--risk", "security"]
        )
        output = io.StringIO()
        with (
            mock.patch.object(MM, "resolve_repo", return_value=Path("/repo")),
            mock.patch.object(MM, "normalize_path_filters", return_value=()),
            mock.patch.object(
                MM,
                "resolve_scope",
                return_value=MM.Scope("uncommitted", None, "changes"),
            ),
            mock.patch.object(MM, "changed_paths", return_value=["src/auth.py"]),
            redirect_stdout(output),
        ):
            MM.recommend_mode_command(args)
        result = json.loads(output.getvalue())
        self.assertEqual(result["recommended_mode"], "deep")
        self.assertTrue(result["advisory_only"])

    def test_workflow_status_distinguishes_ready_to_finalize_and_completed(self) -> None:
        run_dir = Path("/private/tmp/finalized-review")
        metadata = {
            "workflow_id": "wf-done",
            "repository": {"id": "repo-one"},
            "status": "completed",
            "round": 2,
            "phase": "confirmation",
        }
        with tempfile.TemporaryDirectory() as temporary:
            workflows = Path(temporary)
            MM.safe_write_json(
                workflows / "wf-done.json",
                {
                    "workflow_id": "wf-done",
                    "supersedes": [],
                    "policy": MM.workflow_policy(),
                },
            )
            with (
                mock.patch.object(MM, "WORKFLOWS_DIR", workflows),
                mock.patch.object(MM, "workflow_runs", return_value=[(run_dir, metadata)]),
                mock.patch.object(
                    MM, "workflow_lineage_runs", return_value=[(run_dir, metadata)]
                ),
                mock.patch.object(
                    MM, "workflow_lineage_ids", return_value=["wf-done"]
                ),
                mock.patch.object(MM, "latest_workflow_runs", return_value=[(run_dir, metadata)]),
                mock.patch.object(Path, "exists", return_value=True),
                mock.patch.object(MM, "read_json") as read_json,
                mock.patch.object(
                    MM,
                    "freshness_status",
                    return_value={"fresh": True, "mode": "working-tree"},
                ),
                mock.patch.object(
                    MM,
                    "run_artifact_bytes",
                    return_value=MM.empty_artifact_bytes(),
                ) as artifact_bytes,
            ):
                workflow_document = {
                    "workflow_id": "wf-done",
                    "supersedes": [],
                    "policy": MM.workflow_policy(),
                }
                final_document = {"status": "PASS_CLEAN", "source_fingerprint": "x"}
                read_json.side_effect = lambda path: (
                    final_document if path.name == "final.json" else workflow_document
                )
                status, ready = MM.workflow_status("wf-done")
            self.assertTrue(ready)
            self.assertEqual(status["state"], "ready_to_finalize")
            self.assertFalse(status["repositories"][0]["accepts_reviews"])
            self.assertEqual(artifact_bytes.call_count, 1)

    def test_supplemental_final_is_explicitly_non_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            MM.safe_write_json(
                run_dir / "metadata.json",
                {
                    "run_id": "run-supplemental",
                    "workflow_id": "wf-supplemental",
                    "status": "completed",
                    "phase": "supplemental",
                    "round": 1,
                    "source_fingerprint": "same",
                    "supplemental_of": "/private/tmp/parent",
                    "risks": [],
                },
            )
            MM.safe_write_json(
                run_dir / "triage.json",
                {"findings": [], "test_gaps": []},
            )
            args = MM.build_parser().parse_args(
                [
                    "finalize",
                    "--run",
                    str(run_dir),
                    "--codex-review",
                    "Focused recheck passed.",
                ]
            )
            with (
                mock.patch.object(MM, "resolve_run_dir", return_value=run_dir),
                mock.patch.object(
                    MM,
                    "freshness_status",
                    return_value={"fresh": True, "mode": "working-tree"},
                ),
                mock.patch.object(
                    MM, "workflow_requires_confirmation", return_value=False
                ),
            ):
                result = MM.finalize_command(args)
            supplemental = MM.read_json(run_dir / "supplemental.json")
            self.assertEqual(result, 0)
            self.assertEqual(supplemental["status"], "SUPPLEMENTAL_CLEAN")
            self.assertFalse(supplemental["authoritative_gate"])
            self.assertFalse((run_dir / "final.json").exists())

    def test_workflow_finalize_persists_completed_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workflows = Path(temporary)
            MM.safe_write_json(
                workflows / "wf-ready.json",
                {
                    "workflow_id": "wf-ready",
                    "supersedes": [],
                    "policy": MM.workflow_policy(),
                },
            )
            args = MM.build_parser().parse_args(
                ["workflow", "finalize", "wf-ready"]
            )
            with (
                mock.patch.object(MM, "WORKFLOWS_DIR", workflows),
                mock.patch.object(
                    MM,
                    "workflow_status",
                    return_value=(
                        {
                            "workflow_id": "wf-ready",
                            "state": "ready_to_finalize",
                            "repositories": [],
                        },
                        True,
                    ),
                ),
            ):
                MM.workflow_finalize_command(args)
            workflow = MM.read_json(workflows / "wf-ready.json")
            final = MM.read_json(workflows / "wf-ready.final.json")
            self.assertEqual(workflow["status"], "completed")
            self.assertEqual(final["state"], "completed")


class RunnerEndToEndTests(unittest.TestCase):
    def test_supplemental_recheck_uses_one_review_and_preserves_parent_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            repo = root / "repo"
            bin_dir = root / "bin"
            count_path = root / "invocations"
            home.mkdir()
            repo.mkdir()
            bin_dir.mkdir()
            initialize_repo(repo)
            (repo / "src" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
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
                    path = pathlib.Path({str(count_path)!r})
                    path.write_text(str(int(path.read_text()) + 1 if path.exists() else 1))
                    sys.stdin.read()
                    print(json.dumps({{
                        "result": "# Verdict\\nPASS_CLEAN\\n\\n# Findings\\nNone.\\n\\n# Test gaps\\nNone.\\n\\n# Coverage\\n- Complete: yes\\n- Unreviewed changed paths: []\\n- Limitations: []\\n\\n# Notes\\nNone.\\n",
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
            base = [sys.executable, str(SCRIPT_PATH)]
            started = run(
                [
                    *base,
                    "workflow",
                    "start",
                    "--review-mode",
                    "balanced",
                    "--max-budget-usd",
                    "0.80",
                ],
                cwd=repo,
                env=environment,
            )
            workflow_identifier = started.stdout.strip()
            run(
                [
                    *base,
                    "run",
                    "--workflow-id",
                    workflow_identifier,
                    "--path",
                    "src",
                    "--task",
                    "Review the feature change.",
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
            run_dirs = [
                path.parent
                for path in (home / ".codex" / "review-runs").glob(
                    "*/*/metadata.json"
                )
            ]
            confirmation_dir = next(
                path
                for path in run_dirs
                if json.loads((path / "metadata.json").read_text())["phase"]
                == "confirmation"
            )
            run(
                [
                    *base,
                    "finalize",
                    "--run",
                    str(confirmation_dir),
                    "--codex-review",
                    "Parent gate passed.",
                ],
                cwd=repo,
                env=environment,
            )
            supplemental = run(
                [
                    *base,
                    "run",
                    "--supplemental-of",
                    str(confirmation_dir),
                    "--task",
                    "Check the unchanged value path once more.",
                    "--without-antigravity",
                    "--without-kimi",
                ],
                cwd=repo,
                env=environment,
            )
            supplemental_dir = Path(
                next(
                    line.split(": ", 1)[1]
                    for line in supplemental.stdout.splitlines()
                    if line.startswith("Review artifacts: ")
                )
            )
            run(
                [
                    *base,
                    "finalize",
                    "--run",
                    str(supplemental_dir),
                    "--codex-review",
                    "Supplemental question passed.",
                ],
                cwd=repo,
                env=environment,
            )
            supplemental_metadata = json.loads(
                (supplemental_dir / "metadata.json").read_text()
            )
            supplemental_workflow = json.loads(
                (
                    home
                    / ".codex"
                    / "review-runs"
                    / "workflows"
                    / f"{supplemental_metadata['workflow_id']}.json"
                ).read_text()
            )
            invocation_count = count_path.read_text()
            parent_final_exists = (confirmation_dir / "final.json").exists()
            supplemental_final_exists = (
                supplemental_dir / "supplemental.json"
            ).exists()

        self.assertEqual(invocation_count, "3")
        self.assertTrue(parent_final_exists)
        self.assertTrue(supplemental_final_exists)
        self.assertEqual(supplemental_metadata["phase"], "supplemental")
        self.assertEqual(
            supplemental_workflow["supplemental_parent_workflow_id"],
            workflow_identifier,
        )
        self.assertEqual(
            supplemental_workflow["policy"]["max_budget_usd"], 0.8
        )

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
                      "result": "# Verdict\\nPASS_CLEAN\\n\\n# Findings\\nNone.\\n\\n# Test gaps\\nNone.\\n\\n# Coverage\\n- Complete: yes\\n- Unreviewed changed paths: []\\n- Limitations: []\\n\\n# Notes\\nNone.\\n",
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

    def test_base_run_overlays_revert_and_excludes_unrelated_dirty_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            repo = root / "repo"
            bin_dir = root / "bin"
            home.mkdir()
            repo.mkdir()
            bin_dir.mkdir()
            initialize_repo(repo)
            base = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
            (repo / "src" / "feature.py").write_text(
                "VALUE = 2\n", encoding="utf-8"
            )
            (repo / "src" / "task.py").write_text(
                "TASK = True\n", encoding="utf-8"
            )
            (repo / "unrelated.txt").write_text(
                "branch\n", encoding="utf-8"
            )
            run(["git", "add", "src", "unrelated.txt"], cwd=repo)
            run(["git", "commit", "-qm", "branch changes"], cwd=repo)
            (repo / "src" / "feature.py").write_text(
                "VALUE = 1\n", encoding="utf-8"
            )
            (repo / "unrelated.txt").write_text(
                "local unrelated\n", encoding="utf-8"
            )
            (repo / "REVIEW.md").write_text(
                "local notes\n", encoding="utf-8"
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
                    test "$(cat src/feature.py)" = "VALUE = 1" || exit 3
                    test "$(cat src/task.py)" = "TASK = True" || exit 4
                    test "$(cat unrelated.txt)" = "branch" || exit 5
                    test ! -e REVIEW.md || exit 6
                    cat >/dev/null
                    python3 -c 'import json; print(json.dumps({
                      "result": "# Verdict\\nPASS_CLEAN\\n\\n# Findings\\nNone.\\n\\n# Test gaps\\nNone.\\n\\n# Coverage\\n- Complete: yes\\n- Unreviewed changed paths: []\\n- Limitations: []\\n\\n# Notes\\nNone.\\n",
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
                    "--base",
                    base,
                    "--path",
                    "src",
                    "--task",
                    "Review the base-scoped feature change.",
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
            patch = (metadata_path.parent / "change.patch").read_text(
                encoding="utf-8"
            )
            manifest = (metadata_path.parent / "manifest.md").read_text(
                encoding="utf-8"
            )
            self.assertEqual(metadata["status"], "completed")
            self.assertEqual(metadata["scope"]["kind"], "base")
            self.assertEqual(metadata["paths"], ["src/task.py"])
            self.assertEqual(
                metadata["snapshot_overlay_paths"],
                ["src/feature.py", "src/task.py"],
            )
            self.assertNotIn("VALUE = 2", patch)
            self.assertNotIn("local unrelated", patch)
            self.assertIn("- src/feature.py", manifest)
            self.assertIn("- src/task.py", manifest)

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
                        "result": "# Verdict\\nPASS_CLEAN\\n\\n# Findings\\nNone.\\n\\n# Test gaps\\nNone.\\n\\n# Coverage\\n- Complete: yes\\n- Unreviewed changed paths: []\\n- Limitations: []\\n\\n# Notes\\nNone.\\n",
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
                      "result": "# Verdict\\nPASS_CLEAN\\n\\n# Findings\\nNone.\\n\\n# Test gaps\\nNone.\\n\\n# Coverage\\n- Complete: yes\\n- Unreviewed changed paths: []\\n- Limitations: []\\n\\n# Notes\\nNone.\\n",
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
                      "result": "# Verdict\\nPASS_WITH_FINDINGS\\n\\n# Findings\\n## [medium] Add a behavior assertion\\n- Location: src/feature.py:1\\n- Trigger: value changes\\n- Evidence: adapter behavior is not asserted\\n- Impact: bounded regression risk\\n- Smallest fix: add an assertion\\n- Confidence: medium\\n\\n# Test gaps\\n- Add a behavior assertion.\\n\\n# Coverage\\n- Complete: yes\\n- Unreviewed changed paths: []\\n- Limitations: []\\n\\n# Notes\\nNone.\\n",
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
            self.assertIn(
                '"preflight_blocked_runs": 1', committed_workflow.stdout
            )
            self.assertIn('"failed_runs": 0', committed_workflow.stdout)
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
            preflight_metadata = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in (home / ".codex" / "review-runs").glob(
                    "*/*/metadata.json"
                )
                if json.loads(path.read_text(encoding="utf-8")).get("status")
                == "preflight_blocked"
            ]
            self.assertEqual(len(preflight_metadata), 2)
            self.assertTrue(
                all(
                    item["status"] == "preflight_blocked"
                    for item in preflight_metadata
                )
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
                        "result": "# Verdict\\nPASS_CLEAN\\n\\n# Findings\\nNone.\\n\\n# Test gaps\\nNone.\\n\\n# Coverage\\n- Complete: yes\\n- Unreviewed changed paths: []\\n- Limitations: []\\n\\n# Notes\\nNone.\\n",
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
                    print("# Verdict\\nPASS_CLEAN\\n\\n# Findings\\nNone.\\n\\n# Test gaps\\nNone.\\n\\n# Coverage\\n- Complete: yes\\n- Unreviewed changed paths: []\\n- Limitations: []\\n\\n# Notes\\nNone.\\n")
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
