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
                return_value=[reviewer],
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

    def test_claude_cli_contract_checks_required_flags(self) -> None:
        help_text = " ".join(
            (
                "--effort",
                "--max-budget-usd",
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

    def test_status_exit_code_tracks_enabled_reviewer_readiness(self) -> None:
        config = json.loads(json.dumps(MM.DEFAULT_CONFIG))
        config["antigravity"]["enabled"] = True
        config["kimi"]["enabled"] = False

        def readiness(provider: str) -> MM.ProviderReadiness:
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


class RunnerEndToEndTests(unittest.TestCase):
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
            self.assertEqual(metadata["schema_version"], 4)
            self.assertEqual(metadata["status"], "completed")
            self.assertEqual(metadata["phase"], "repair")
            self.assertEqual(metadata["paths"], ["src/feature.py"])
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

            confirmation = run(
                [
                    *base,
                    "run",
                    "--repo",
                    str(repo),
                    "--path",
                    "src",
                    "--risk",
                    "db-write",
                    "--workflow-id",
                    metadata["workflow_id"],
                    "--phase",
                    "confirmation",
                    "--task",
                    "Change the feature value.",
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


if __name__ == "__main__":
    unittest.main()
