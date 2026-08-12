#!/usr/bin/env python3
"""Focused regression tests for the multi-model review runner."""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock


SCRIPT_PATH = Path(__file__).with_name("mm_review.py")
SPEC = importlib.util.spec_from_file_location("mm_review", SCRIPT_PATH)
assert SPEC and SPEC.loader
MM = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MM
SPEC.loader.exec_module(MM)
import evidence_memory as EM


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


def structured_report(
    *,
    verdict: str = "PASS_CLEAN",
    findings: str = "None.",
    test_gaps: str = "None.",
    observations: str = "None.",
    coverage_complete: bool = True,
    unreviewed_paths: list[str] | None = None,
    limitations: list[str] | None = None,
) -> str:
    return (
        f"# Verdict\n{verdict}\n\n"
        f"# Findings\n{findings}\n\n"
        f"# Test gaps\n{test_gaps}\n\n"
        f"# Observations\n{observations}\n\n"
        "# Coverage\n"
        f"- Complete: {'yes' if coverage_complete else 'no'}\n"
        "- Unreviewed changed paths: "
        f"{json.dumps(unreviewed_paths or [])}\n"
        f"- Limitations: {json.dumps(limitations or [])}\n\n"
        "# Notes\nNone.\n"
    )


class FakeProviderHarness:
    """Real subprocess harness whose only provider binaries are local shims."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "home"
        self.bin_dir = root / "bin"
        self.state_path = root / "provider-state.json"
        self.log_path = root / "provider-invocations.jsonl"
        self.home.mkdir()
        self.bin_dir.mkdir()
        self.state_path.write_text(
            json.dumps(
                {
                    "_sequence": 0,
                    "claude": [],
                    "agy": [],
                    "kimi": [],
                }
            ),
            encoding="utf-8",
        )
        shim = self.bin_dir / "provider-shim"
        shim.write_text(
            textwrap.dedent(
                r"""
                #!/usr/bin/env python3
                import fcntl
                import hashlib
                import json
                import os
                import pathlib
                import sys
                import time

                provider = pathlib.Path(sys.argv[0]).name
                args = sys.argv[1:]
                if args == ["--version"]:
                    print(f"fake-{provider} campaign-1.0")
                    raise SystemExit(0)
                if provider == "claude" and args == ["--help"]:
                    print("--effort --max-budget-usd --json-schema --permission-mode --tools --safe-mode --no-session-persistence")
                    raise SystemExit(0)
                if provider == "claude" and args == ["auth", "status"]:
                    print(json.dumps({"loggedIn": True, "authMethod": "oauth", "subscription": "synthetic"}))
                    raise SystemExit(0)
                if provider == "agy" and args == ["models"]:
                    print("fake-model")
                    raise SystemExit(0)
                if provider == "kimi" and args == ["provider", "list", "--json"]:
                    print(json.dumps({"models": {"k3-256k": {}}}))
                    raise SystemExit(0)

                prompt = sys.stdin.read() if provider == "claude" else ""
                state_path = pathlib.Path(os.environ["MM_FAKE_PROVIDER_STATE"])
                with state_path.open("r+", encoding="utf-8") as state_file:
                    fcntl.flock(state_file, fcntl.LOCK_EX)
                    state = json.load(state_file)
                    queue = state.get(provider, [])
                    if not queue:
                        print(f"no queued outcome for {provider}", file=sys.stderr)
                        raise SystemExit(96)
                    outcome = queue.pop(0)
                    state[provider] = queue
                    state["_sequence"] = int(state.get("_sequence", 0)) + 1
                    sequence = state["_sequence"]
                    state_file.seek(0)
                    json.dump(state, state_file)
                    state_file.truncate()

                cwd = pathlib.Path.cwd()
                snapshot_files = sorted(
                    str(path.relative_to(cwd))
                    for path in cwd.rglob("*")
                    if path.is_file() or path.is_symlink()
                )
                add_dir = None
                if "--add-dir" in args:
                    add_dir = pathlib.Path(args[args.index("--add-dir") + 1])
                granted_files = sorted(
                    str(path.relative_to(add_dir))
                    for path in add_dir.rglob("*")
                    if path.is_file() or path.is_symlink()
                ) if add_dir else []
                workspace_files = sorted(
                    str(path.relative_to(cwd.parent))
                    for path in cwd.parent.rglob("*")
                    if path.is_file() or path.is_symlink()
                )
                staged_prompt = (
                    (add_dir / "prompt.md").read_bytes()
                    if add_dir and (add_dir / "prompt.md").is_file()
                    else b""
                )
                mutation = outcome.get("mutate_relative")
                if mutation:
                    target = cwd / mutation
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("provider mutation\n", encoding="utf-8")
                record = {
                    "sequence": sequence,
                    "provider": provider,
                    "cwd": str(cwd),
                    "args": args,
                    "stdin_bytes": len(prompt.encode()),
                    "staged_prompt_sha256": hashlib.sha256(
                        staged_prompt
                    ).hexdigest(),
                    "snapshot_files": snapshot_files,
                    "granted_dir": str(add_dir) if add_dir else None,
                    "granted_files": granted_files,
                    "workspace_files": workspace_files,
                    "mutate_relative": mutation,
                }
                log_path = pathlib.Path(os.environ["MM_FAKE_PROVIDER_LOG"])
                with log_path.open("a", encoding="utf-8") as log_file:
                    fcntl.flock(log_file, fcntl.LOCK_EX)
                    log_file.write(json.dumps(record, sort_keys=True) + "\n")

                kind = outcome.get("kind", "report")
                if kind == "malformed_wrapper":
                    print("{not-json")
                    raise SystemExit(int(outcome.get("exit_code", 0)))
                if kind == "failure":
                    print(outcome.get("stdout", ""))
                    print(outcome.get("stderr", "provider failed"), file=sys.stderr)
                    raise SystemExit(int(outcome.get("exit_code", 1)))
                if kind == "budget_exhausted":
                    message = "budget_exhausted: synthetic provider stop"
                    if provider == "claude":
                        print(json.dumps({"is_error": True, "result": message}))
                    else:
                        print(message, file=sys.stderr)
                    raise SystemExit(int(outcome.get("exit_code", 1)))
                if kind == "timeout":
                    time.sleep(float(outcome.get("seconds", 120)))
                    raise SystemExit(0)

                report = outcome.get("report", "")
                if provider == "claude":
                    print(json.dumps({
                        "result": report,
                        "total_cost_usd": float(outcome.get("cost", 0.01)),
                        "num_turns": 1,
                    }))
                elif provider == "agy":
                    print(json.dumps({
                        "status": "SUCCESS",
                        "response": report,
                        "duration_seconds": 0.01,
                        "num_turns": 1,
                    }))
                else:
                    print(report)
                raise SystemExit(int(outcome.get("exit_code", 0)))
                """
            ).lstrip(),
            encoding="utf-8",
        )
        shim.chmod(0o755)
        for provider in ("claude", "agy", "kimi"):
            shutil.copy2(shim, self.bin_dir / provider)
            (self.bin_dir / provider).chmod(0o755)
        agent_path = (
            self.home
            / ".gemini"
            / "config"
            / "agents"
            / MM.ANTIGRAVITY_AGENT_NAME
            / "agent.md"
        )
        agent_path.parent.mkdir(parents=True)
        shutil.copy2(MM.ANTIGRAVITY_AGENT_PATH, agent_path)
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "HOME": str(self.home),
                "PATH": f"{self.bin_dir}:{self.environment['PATH']}",
                "PYTHONDONTWRITEBYTECODE": "1",
                "MM_FAKE_PROVIDER_STATE": str(self.state_path),
                "MM_FAKE_PROVIDER_LOG": str(self.log_path),
            }
        )
        self.base = [sys.executable, str(SCRIPT_PATH)]

    def assert_fake_resolution(self) -> None:
        for provider in ("claude", "agy", "kimi"):
            resolved = shutil.which(provider, path=self.environment["PATH"])
            if Path(resolved or "").resolve() != (self.bin_dir / provider).resolve():
                raise AssertionError(
                    f"unsafe provider resolution for {provider}: {resolved}"
                )

    def queue(self, provider: str, *outcomes: dict[str, object]) -> None:
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        state.setdefault(provider, []).extend(outcomes)
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def cli(
        self,
        repo: Path,
        *arguments: str,
        check: bool = True,
        provider_backed: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        if provider_backed:
            self.assert_fake_resolution()
        completed = run(
            [*self.base, *arguments],
            cwd=repo,
            env=self.environment,
            check=False,
        )
        if check and completed.returncode != 0:
            raise AssertionError(
                f"runner exited {completed.returncode}; stdout={completed.stdout!r}; "
                f"stderr={completed.stderr!r}"
            )
        return completed

    def invocations(self) -> list[dict[str, object]]:
        if not self.log_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def run_directories(self) -> list[Path]:
        return sorted(
            path.parent
            for path in (self.home / ".codex" / "review-runs").glob(
                "*/*/metadata.json"
            )
        )


class RunnerUnitTests(unittest.TestCase):
    def test_runtime_identity_records_exact_bundle_and_runner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "plugin"
            manifest = root / ".codex-plugin" / "plugin.json"
            runner = root / "skills" / "review" / "scripts" / "runner.py"
            manifest.parent.mkdir(parents=True)
            runner.parent.mkdir(parents=True)
            manifest.write_text(
                json.dumps({"name": "review-test", "version": "1.2.3"}),
                encoding="utf-8",
            )
            runner.write_text("print('runner')\n", encoding="utf-8")

            identity = MM.runtime_identity(
                plugin_root=root,
                runner_path=runner,
            )
            expected_runner_sha256 = MM.sha256_file(runner)

        self.assertEqual(identity["plugin_name"], "review-test")
        self.assertEqual(identity["plugin_version"], "1.2.3")
        self.assertEqual(identity["plugin_root"], str(root.resolve()))
        self.assertEqual(identity["runner_path"], str(runner.resolve()))
        self.assertEqual(identity["runner_sha256"], expected_runner_sha256)

    def test_main_rejects_unsupported_python_before_argument_parsing(self) -> None:
        error = io.StringIO()
        with (
            mock.patch.object(MM.sys, "version_info", (3, 9, 6)),
            mock.patch.object(MM, "build_parser") as parser,
            redirect_stderr(error),
        ):
            self.assertEqual(MM.main(), 2)
        parser.assert_not_called()
        self.assertIn("Python 3.12 or newer is required", error.getvalue())
        self.assertIn("running 3.9.6", error.getvalue())

    def test_internal_error_diagnostic_omits_exception_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            try:
                raise TypeError("synthetic-sensitive-value-must-not-persist")
            except TypeError as exc:
                path = MM.persist_internal_error(run_dir, exc)
            diagnostic = MM.read_json(path)
            raw = path.read_text(encoding="utf-8")
        self.assertEqual(diagnostic["error_type"], "TypeError")
        self.assertTrue(diagnostic["frames"])
        self.assertNotIn("synthetic-sensitive-value-must-not-persist", raw)
        self.assertEqual(
            diagnostic["privacy"], "exception message and local values omitted"
        )

    def test_evidence_memory_context_closes_sqlite_connection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "evidence.sqlite3"
            with EM._open_database(database) as connection:
                connection.execute("SELECT 1").fetchone()
            with self.assertRaisesRegex(sqlite3.ProgrammingError, "closed"):
                connection.execute("SELECT 1")

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

    def test_claude_resume_policy_updates_canonical_api_equivalent_stop(self) -> None:
        updated = MM.updated_claude_resume_policy(
            {
                "claude_effort": "medium",
                "claude_max_budget_usd": 1.25,
                "claude_api_equivalent_limit_usd": 1.25,
            },
            effort="low",
            api_equivalent_limit_usd=1.75,
        )
        self.assertEqual(updated["claude_effort"], "low")
        self.assertEqual(updated["claude_max_budget_usd"], 1.75)
        self.assertEqual(updated["claude_api_equivalent_limit_usd"], 1.75)
        self.assertFalse(updated["api_equivalent_usd_is_billing"])

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
        seen_repositories: list[tuple[bool, bool, list[Path], list[Path]]] = []
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
            input_dir: Path,
            timeout_seconds: int,
        ) -> MM.ReviewResult:
            del prompt, timeout_seconds
            seen_repositories.append(
                (
                    repo == run_dir,
                    repo == input_dir,
                    list(repo.iterdir()),
                    list(input_dir.iterdir()),
                )
            )
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
        self.assertEqual(seen_repositories[0], (False, False, [], []))
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

    def test_new_workflow_uses_provider_allowance_not_lineage_dollars(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workflows = Path(temporary)
            config = json.loads(json.dumps(MM.DEFAULT_CONFIG))
            args = MM.build_parser().parse_args(
                [
                    "workflow",
                    "start",
                    "--name",
                    "usage-aware",
                    "--max-provider-attempts",
                    "4",
                ]
            )
            output = io.StringIO()
            with (
                mock.patch.object(MM, "WORKFLOWS_DIR", workflows),
                mock.patch.object(MM, "load_config", return_value=config),
                redirect_stdout(output),
            ):
                MM.workflow_start_command(args)
                identifier = output.getvalue().strip()
                document = MM.read_json(workflows / f"{identifier}.json")
                self.assertIsNone(MM.workflow_budget_limit(identifier))
        usage_policy = document["policy"]["usage_policy"]
        self.assertEqual(usage_policy["mode"], "provider_allowance")
        self.assertEqual(usage_policy["max_attempts_per_provider"], 4)
        self.assertFalse(document["policy"]["enforce_lineage_api_equivalent_cap"])
        self.assertNotIn("max_budget_usd", document["policy"])

    def test_claude_authentication_mode_distinguishes_subscription_and_api(self) -> None:
        subscription = subprocess.CompletedProcess(
            ["claude", "auth", "status"],
            0,
            stdout='{"loggedIn":true,"authMethod":"oauth","subscriptionType":"max"}',
            stderr="",
        )
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(MM.subprocess, "run", return_value=subscription),
        ):
            self.assertEqual(MM.claude_authentication_mode()[0], "subscription")
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "redacted"}, clear=True):
            self.assertEqual(MM.claude_authentication_mode()[0], "api_billed")

    def test_claude_authentication_mode_fails_closed_to_unknown(self) -> None:
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(MM.subprocess, "run", side_effect=OSError("missing")),
        ):
            self.assertEqual(MM.claude_authentication_mode()[0], "unknown")
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(
                MM.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(["claude"], 10),
            ),
        ):
            self.assertEqual(MM.claude_authentication_mode()[0], "unknown")
        invalid = subprocess.CompletedProcess(
            ["claude", "auth", "status"],
            0,
            stdout="authenticated but format changed",
            stderr="",
        )
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(MM.subprocess, "run", return_value=invalid),
        ):
            self.assertEqual(MM.claude_authentication_mode()[0], "unknown")

    def test_non_probed_claude_usage_does_not_assume_subscription(self) -> None:
        config = json.loads(json.dumps(MM.DEFAULT_CONFIG))
        config["claude"]["enabled"] = True
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(MM, "load_config", return_value=config),
            mock.patch.object(
                MM,
                "workflow_provider_attempts",
                return_value={provider: 0 for provider in MM.PROVIDERS},
            ),
            mock.patch.object(MM, "workflow_usage_policy", return_value={}),
            mock.patch.object(MM, "active_provider_cooldown", return_value=None),
        ):
            usage = MM.provider_usage_snapshot(
                "wf-test", probe_readiness=False
            )
        claude = usage["providers"]["claude"]
        self.assertEqual(claude["authentication_mode"], "unknown")
        self.assertEqual(claude["usage_resource"], "unknown")

    def test_provider_usage_policy_skips_exhausted_provider(self) -> None:
        claude = MM.Reviewer(
            "claude", ("claude", "--max-budget-usd", "1.25"), {}, "sonnet", "fake"
        )
        kimi = MM.Reviewer("kimi", ("kimi",), {}, "k3", "fake")
        with tempfile.TemporaryDirectory() as temporary:
            workflows = Path(temporary)
            with (
                mock.patch.object(MM, "WORKFLOWS_DIR", workflows),
                mock.patch.object(
                    MM,
                    "workflow_provider_attempts",
                    return_value={"claude": 2, "antigravity": 0, "kimi": 0},
                ),
            ):
                MM.create_workflow(
                    "wf-usage",
                    usage_based=True,
                    max_provider_attempts=2,
                )
                selected, usage = MM.apply_workflow_budget(
                    [claude, kimi], "wf-usage", reservation_id="run-1"
                )
                document = MM.read_json(workflows / "wf-usage.json")
        self.assertEqual([reviewer.name for reviewer in selected], ["kimi"])
        self.assertIn("claude", usage["skipped_providers"])
        self.assertEqual(
            document["usage_reservations"]["run-1"]["providers"], ["kimi"]
        )

    def test_dead_provider_reservation_is_ignored_and_reclaimed(self) -> None:
        reviewer = MM.Reviewer("claude", ("claude",), {}, "sonnet", "fake")
        with tempfile.TemporaryDirectory() as temporary:
            workflows = Path(temporary) / "workflows"
            with (
                mock.patch.object(MM, "WORKFLOWS_DIR", workflows),
                mock.patch.object(MM, "all_run_metadata", return_value=[]),
                mock.patch.object(
                    MM,
                    "workflow_provider_attempts",
                    return_value={provider: 0 for provider in MM.PROVIDERS},
                ),
                mock.patch.object(MM, "process_is_alive", return_value=False),
                mock.patch.object(MM, "active_provider_cooldown", return_value=None),
                mock.patch.object(
                    MM,
                    "load_config",
                    return_value=json.loads(json.dumps(MM.DEFAULT_CONFIG)),
                ),
            ):
                MM.create_workflow(
                    "wf-dead-reservation",
                    usage_based=True,
                    max_provider_attempts=1,
                )
                document = MM.read_json(workflows / "wf-dead-reservation.json")
                document["usage_reservations"] = {
                    "orphan": {
                        "providers": ["claude"],
                        "runner_pid": 99999999,
                        "reserved_at": MM.utc_now(),
                    }
                }
                MM.safe_write_json(workflows / "wf-dead-reservation.json", document)
                usage = MM.provider_usage_snapshot("wf-dead-reservation")
                selected, _ = MM.apply_workflow_budget(
                    [reviewer],
                    "wf-dead-reservation",
                    reservation_id="replacement",
                )
                updated = MM.read_json(workflows / "wf-dead-reservation.json")
        self.assertEqual(usage["providers"]["claude"]["active_reservations"], 0)
        self.assertEqual(usage["providers"]["claude"]["attempts_remaining"], 1)
        self.assertEqual([item.name for item in selected], ["claude"])
        self.assertNotIn("orphan", updated["usage_reservations"])
        self.assertIn("replacement", updated["usage_reservations"])

    def test_live_provider_reservation_reduces_reported_headroom(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workflows = Path(temporary) / "workflows"
            with (
                mock.patch.object(MM, "WORKFLOWS_DIR", workflows),
                mock.patch.object(MM, "all_run_metadata", return_value=[]),
                mock.patch.object(
                    MM,
                    "workflow_provider_attempts",
                    return_value={provider: 0 for provider in MM.PROVIDERS},
                ),
                mock.patch.object(MM, "process_is_alive", return_value=True),
                mock.patch.object(MM, "active_provider_cooldown", return_value=None),
                mock.patch.object(
                    MM,
                    "load_config",
                    return_value=json.loads(json.dumps(MM.DEFAULT_CONFIG)),
                ),
            ):
                MM.create_workflow(
                    "wf-live-reservation",
                    usage_based=True,
                    max_provider_attempts=1,
                )
                document = MM.read_json(workflows / "wf-live-reservation.json")
                document["usage_reservations"] = {
                    "active": {
                        "providers": ["claude"],
                        "runner_pid": 12345,
                        "reserved_at": MM.utc_now(),
                    }
                }
                MM.safe_write_json(workflows / "wf-live-reservation.json", document)
                usage = MM.provider_usage_snapshot("wf-live-reservation")
        claude = usage["providers"]["claude"]
        self.assertEqual(claude["active_reservations"], 1)
        self.assertEqual(claude["attempts_including_reservations"], 1)
        self.assertEqual(claude["attempts_remaining"], 0)

    def test_provider_usage_policy_fails_when_every_provider_is_exhausted(self) -> None:
        reviewers = [
            MM.Reviewer("claude", ("claude",), {}, "sonnet", "fake"),
            MM.Reviewer("kimi", ("kimi",), {}, "k3", "fake"),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            workflows = Path(temporary)
            with (
                mock.patch.object(MM, "WORKFLOWS_DIR", workflows),
                mock.patch.object(
                    MM,
                    "workflow_provider_attempts",
                    return_value={provider: 2 for provider in MM.PROVIDERS},
                ),
                mock.patch.object(MM, "active_provider_cooldown", return_value=None),
            ):
                MM.create_workflow(
                    "wf-exhausted",
                    usage_based=True,
                    max_provider_attempts=2,
                )
                with self.assertRaisesRegex(
                    MM.ReviewError, "provider-attempt allowance exhausted"
                ):
                    MM.apply_workflow_budget(
                        reviewers, "wf-exhausted", reservation_id="run-1"
                    )
                document = MM.read_json(workflows / "wf-exhausted.json")
        self.assertNotIn("usage_reservations", document)

    def test_concurrent_provider_attempt_reservations_cannot_exceed_limit(
        self,
    ) -> None:
        reviewer = MM.Reviewer("claude", ("claude",), {}, "sonnet", "fake")
        with tempfile.TemporaryDirectory() as temporary:
            workflows = Path(temporary)
            barrier = threading.Barrier(3)
            results: list[str] = []
            results_lock = threading.Lock()

            def reserve(reservation_id: str) -> None:
                barrier.wait()
                try:
                    MM.apply_workflow_budget(
                        [reviewer],
                        "wf-concurrent-usage",
                        reservation_id=reservation_id,
                    )
                except MM.ReviewError:
                    outcome = "blocked"
                else:
                    outcome = "reserved"
                with results_lock:
                    results.append(outcome)

            with (
                mock.patch.object(MM, "WORKFLOWS_DIR", workflows),
                mock.patch.object(
                    MM,
                    "workflow_provider_attempts",
                    return_value={provider: 0 for provider in MM.PROVIDERS},
                ),
                mock.patch.object(MM, "active_provider_cooldown", return_value=None),
            ):
                MM.create_workflow(
                    "wf-concurrent-usage",
                    usage_based=True,
                    max_provider_attempts=1,
                )
                threads = [
                    threading.Thread(target=reserve, args=(f"run-{index}",))
                    for index in range(2)
                ]
                for thread in threads:
                    thread.start()
                barrier.wait()
                for thread in threads:
                    thread.join(timeout=2)
                document = MM.read_json(
                    workflows / "wf-concurrent-usage.json"
                )
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertCountEqual(results, ["reserved", "blocked"])
        self.assertEqual(len(document["usage_reservations"]), 1)

    def test_continue_zero_run_is_read_only_and_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workflows = Path(temporary) / "workflows"
            runs = Path(temporary) / "runs"
            config = json.loads(json.dumps(MM.DEFAULT_CONFIG))
            config["claude"]["enabled"] = False
            with (
                mock.patch.object(MM, "WORKFLOWS_DIR", workflows),
                mock.patch.object(MM, "RUNS_DIR", runs),
                mock.patch.object(MM, "load_config", return_value=config),
            ):
                MM.create_workflow("wf-empty", usage_based=True)
                plan = MM.workflow_continue_plan("wf-empty")
        self.assertEqual(plan["next"], "NEEDS_INITIAL_REVIEW")
        self.assertFalse(plan["actions"][0]["automatable"])
        self.assertIn("mm-review run", plan["actions"][0]["command"])

    def test_continue_and_gate_surface_real_partial_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflows = root / "workflows"
            runs = root / "runs"
            run_dir = runs / "repo-1" / "run-1"
            run_dir.mkdir(parents=True)
            with (
                mock.patch.object(MM, "WORKFLOWS_DIR", workflows),
                mock.patch.object(MM, "RUNS_DIR", runs),
            ):
                MM.create_workflow("wf-partial", usage_based=True)
                MM.safe_write_json(
                    run_dir / "metadata.json",
                    {
                        "schema_version": 10,
                        "workflow_id": "wf-partial",
                        "run_id": "run-partial",
                        "status": "partial",
                        "round": 1,
                        "phase": "repair",
                        "created_at": MM.utc_now(),
                        "repository": {
                            "id": "repo-1",
                            "name": "partial-repo",
                            "root": str(root / "repo"),
                        },
                        "reviewers": {"claude": {"exit_code": 1}},
                        "failure": {
                            "type": "reviewer_failure",
                            "reviewers": ["claude"],
                        },
                    },
                )
                usage = {
                    "providers": {
                        "claude": {
                            "enabled": True,
                            "ready": True,
                            "attempts_remaining": 2,
                        }
                    }
                }
                with mock.patch.object(
                    MM, "provider_usage_snapshot", return_value=usage
                ):
                    plan = MM.workflow_continue_plan(
                        "wf-partial", probe_usage=True
                    )
                    args = MM.build_parser().parse_args(
                        ["gate", "wf-partial"]
                    )
                    output = io.StringIO()
                    with redirect_stdout(output):
                        result = MM.gate_command(args)
        self.assertEqual(plan["next"], "NEEDS_REVIEW")
        self.assertEqual(plan["actions"][0]["type"], "resume")
        self.assertEqual(plan["actions"][0]["run_dir"], str(run_dir))
        self.assertEqual(result, 3)
        gate = json.loads(output.getvalue())
        self.assertEqual(gate["gate"], "INCOMPLETE")
        self.assertEqual(gate["next"], "NEEDS_REVIEW")

    def test_continue_requires_execution_authority_by_default(self) -> None:
        plan = {
            "workflow_id": "wf-test",
            "state": "active",
            "next": "NEEDS_REVIEW",
            "actions": [
                {
                    "type": "review",
                    "automatable": True,
                    "command": "mm-review run --workflow-id wf-test",
                }
            ],
        }
        args = MM.build_parser().parse_args(["continue", "wf-test"])
        output = io.StringIO()
        with (
            mock.patch.object(
                MM, "workflow_continue_plan", return_value=plan
            ) as continue_plan,
            mock.patch.object(
                MM,
                "workflow_usage_policy",
                return_value={"provider_use": "explicit"},
            ),
            mock.patch.object(MM, "_execute_continue_action") as execute,
            redirect_stdout(output),
        ):
            self.assertEqual(MM.continue_command(args), 3)
        execute.assert_not_called()
        continue_plan.assert_called_once_with("wf-test", probe_usage=True)
        self.assertEqual(json.loads(output.getvalue())["next"], "NEEDS_REVIEW")

    def test_continue_auto_policy_executes_one_review_step(self) -> None:
        before = {
            "workflow_id": "wf-test",
            "state": "active",
            "next": "NEEDS_REVIEW",
            "actions": [
                {
                    "type": "review",
                    "automatable": True,
                    "command": "mm-review run --workflow-id wf-test",
                }
            ],
        }
        after = {
            "workflow_id": "wf-test",
            "state": "active",
            "next": "NEEDS_TRIAGE",
            "actions": [],
        }
        args = MM.build_parser().parse_args(["continue", "wf-test"])
        output = io.StringIO()
        with (
            mock.patch.object(
                MM, "workflow_continue_plan", side_effect=[before, after]
            ) as continue_plan,
            mock.patch.object(
                MM,
                "workflow_usage_policy",
                return_value={"provider_use": "auto"},
            ),
            mock.patch.object(
                MM, "_execute_continue_action", return_value="review ran"
            ) as execute,
            redirect_stdout(output),
        ):
            self.assertEqual(MM.continue_command(args), 3)
        execute.assert_called_once()
        self.assertEqual(
            continue_plan.call_args_list,
            [
                mock.call("wf-test", probe_usage=True),
                mock.call("wf-test", probe_usage=True),
            ],
        )
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["next"], "NEEDS_TRIAGE")
        self.assertEqual(payload["execution_log"], ["review ran"])

    def test_execute_continue_action_selects_only_ready_providers(self) -> None:
        action = {
            "type": "review",
            "command": (
                "mm-review run --repo /tmp/repo --workflow-id wf-test "
                "--phase confirmation --reuse-contract"
            ),
        }
        usage = {
            "providers": {
                "claude": {
                    "enabled": True,
                    "ready": True,
                    "attempts_remaining": 2,
                },
                "antigravity": {
                    "enabled": False,
                    "ready": False,
                    "attempts_remaining": 2,
                },
                "kimi": {
                    "enabled": True,
                    "ready": True,
                    "attempts_remaining": 0,
                },
            }
        }
        captured: list[argparse.Namespace] = []

        def run_review(args: argparse.Namespace) -> int:
            captured.append(args)
            print("review executed")
            return 0

        with (
            mock.patch.object(
                MM, "provider_usage_snapshot", return_value=usage
            ) as snapshot,
            mock.patch.object(MM, "run_review_command", side_effect=run_review),
        ):
            output = MM._execute_continue_action("wf-test", action)
        snapshot.assert_called_once_with("wf-test", probe_readiness=True)
        self.assertEqual(output, "review executed")
        self.assertEqual(len(captured), 1)
        args = captured[0]
        self.assertTrue(args.with_claude)
        self.assertFalse(args.with_antigravity)
        self.assertFalse(args.with_kimi)
        self.assertTrue(args.without_antigravity)
        self.assertTrue(args.without_kimi)
        self.assertTrue(args.reuse_contract)
        self.assertEqual(args.phase, "confirmation")

    def test_gate_reports_manual_blocker_without_mutating(self) -> None:
        plan = {
            "workflow_id": "wf-test",
            "state": "active",
            "next": "NEEDS_TRIAGE",
            "actions": [{"type": "triage", "automatable": False}],
        }
        args = MM.build_parser().parse_args(["gate", "wf-test"])
        output = io.StringIO()
        with (
            mock.patch.object(MM, "workflow_path", return_value=Path(__file__)),
            mock.patch.object(MM, "workflow_continue_plan", return_value=plan),
            mock.patch.object(MM, "latest_workflow_runs", return_value=[]),
            mock.patch.object(MM, "workflow_finalize_command") as finalize,
            redirect_stdout(output),
        ):
            self.assertEqual(MM.gate_command(args), 3)
        finalize.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["gate"], "INCOMPLETE")
        self.assertEqual(payload["next"], "NEEDS_TRIAGE")

    def test_gate_rejects_one_verdict_for_multiple_pending_finals(self) -> None:
        actions = [
            {
                "type": "codex_final",
                "run_dir": f"/tmp/repository-{index}",
                "automatable": False,
            }
            for index in (1, 2)
        ]
        plan = {
            "workflow_id": "wf-test",
            "state": "active",
            "next": "NEEDS_CODEX_FINAL",
            "actions": actions,
        }
        args = MM.build_parser().parse_args(
            [
                "gate",
                "wf-test",
                "--codex-verdict",
                "PASS_CLEAN",
                "--codex-review",
                "reviewed",
            ]
        )
        with (
            mock.patch.object(MM, "workflow_path", return_value=Path(__file__)),
            mock.patch.object(MM, "workflow_continue_plan", return_value=plan),
            mock.patch.object(MM, "finalize_command") as finalize,
        ):
            with self.assertRaisesRegex(
                MM.ReviewError, "Multiple repositories are awaiting"
            ):
                MM.gate_command(args)
        finalize.assert_not_called()

    def test_gate_skips_commit_attestation_for_supplemental_evidence(self) -> None:
        ready = {
            "workflow_id": "wf-test",
            "state": "ready_to_finalize",
            "next": "READY_TO_GATE",
            "actions": [],
        }
        complete = {
            "workflow_id": "wf-test",
            "state": "completed",
            "next": "COMPLETE",
            "actions": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflows = root / "workflows"
            run_dir = root / "run"
            run_dir.mkdir()
            MM.safe_write_json(
                run_dir / "supplemental.json",
                {"status": "SUPPLEMENTAL_CLEAN"},
            )
            MM.safe_write_json(
                workflows / "wf-test.json",
                {"workflow_id": "wf-test", "status": "completed"},
            )
            args = MM.build_parser().parse_args(
                ["gate", "wf-test", "--attest-commit"]
            )
            output = io.StringIO()
            with (
                mock.patch.object(MM, "WORKFLOWS_DIR", workflows),
                mock.patch.object(
                    MM, "workflow_continue_plan", side_effect=[ready, complete]
                ),
                mock.patch.object(
                    MM,
                    "latest_workflow_runs",
                    return_value=[(run_dir, {"phase": "supplemental"})],
                ),
                mock.patch.object(MM, "attest_commit_command") as attest,
                mock.patch.object(MM, "verify_command", return_value=0),
                redirect_stdout(output),
            ):
                self.assertEqual(MM.gate_command(args), 0)
        attest.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertIn(
            "attestation skipped",
            " ".join(payload["execution_log"]).lower(),
        )

    def test_gate_success_finalizes_verifies_attests_and_closes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflows = root / "workflows"
            run_dir = root / "confirmation"
            run_dir.mkdir()
            MM.safe_write_json(
                workflows / "wf-test.json",
                {"workflow_id": "wf-test", "status": "active"},
            )
            initial = {
                "workflow_id": "wf-test",
                "state": "confirmation_required",
                "next": "NEEDS_CODEX_FINAL",
                "actions": [
                    {
                        "type": "codex_final",
                        "run_dir": str(run_dir),
                        "automatable": False,
                    }
                ],
            }
            ready = {
                "workflow_id": "wf-test",
                "state": "ready_to_finalize",
                "next": "READY_TO_GATE",
                "actions": [],
            }
            complete = {
                "workflow_id": "wf-test",
                "state": "completed",
                "next": "COMPLETE",
                "actions": [],
            }
            events: list[str] = []

            def finalize(args: argparse.Namespace) -> int:
                events.append("finalize")
                self.assertEqual(args.codex_verdict, "PASS_CLEAN")
                self.assertEqual(args.verification, ["131 tests passed"])
                MM.safe_write_json(
                    run_dir / "final.json", {"status": "PASS_CLEAN"}
                )
                return 0

            def attest(_args: argparse.Namespace) -> int:
                events.append("attest")
                return 0

            def verify(_args: argparse.Namespace) -> int:
                events.append("verify")
                return 0

            def close(_args: argparse.Namespace) -> int:
                events.append("close")
                return 0

            args = MM.build_parser().parse_args(
                [
                    "gate",
                    "wf-test",
                    "--codex-verdict",
                    "PASS_CLEAN",
                    "--codex-review",
                    "All evidence checked.",
                    "--verification",
                    "131 tests passed",
                    "--attest-commit",
                ]
            )
            output = io.StringIO()
            with (
                mock.patch.object(MM, "WORKFLOWS_DIR", workflows),
                mock.patch.object(
                    MM,
                    "workflow_continue_plan",
                    side_effect=[initial, ready, complete],
                ),
                mock.patch.object(
                    MM,
                    "latest_workflow_runs",
                    return_value=[(run_dir, {"phase": "confirmation"})],
                ),
                mock.patch.object(MM, "finalize_command", side_effect=finalize),
                mock.patch.object(
                    MM, "attest_commit_command", side_effect=attest
                ),
                mock.patch.object(MM, "verify_command", side_effect=verify),
                mock.patch.object(
                    MM, "workflow_finalize_command", side_effect=close
                ),
                redirect_stdout(output),
            ):
                self.assertEqual(MM.gate_command(args), 0)
        self.assertEqual(events, ["finalize", "attest", "verify", "close"])
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["gate"], "PASS")
        self.assertEqual(payload["next"], "COMPLETE")

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

    def test_snapshot_includes_tracked_export_ignored_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            workspace = root / "workspace"
            repo.mkdir()
            workspace.mkdir()
            initialize_repo(repo)
            smudge_marker = root / "smudge-ran"
            run(
                [
                    "git",
                    "config",
                    "filter.audit.smudge",
                    f"touch {smudge_marker}; cat",
                ],
                cwd=repo,
            )
            run(["git", "config", "filter.audit.clean", "cat"], cwd=repo)
            (repo / ".gitattributes").write_text(
                "src/helper.py export-ignore filter=audit\n"
                "src/helper_two.py export-ignore filter=audit\n",
                encoding="utf-8",
            )
            (repo / "src" / "helper.py").write_text(
                "HELPER = 1\n", encoding="utf-8"
            )
            (repo / "src" / "helper_two.py").write_text(
                "HELPER_TWO = 2\n", encoding="utf-8"
            )
            run(
                [
                    "git",
                    "add",
                    ".gitattributes",
                    "src/helper.py",
                    "src/helper_two.py",
                ],
                cwd=repo,
            )
            run(["git", "commit", "-qm", "add export ignored helper"], cwd=repo)
            (repo / "src" / "feature.py").write_text(
                "VALUE = 2\n", encoding="utf-8"
            )
            scope = MM.Scope("uncommitted", None, "working tree changes")
            paths = MM.changed_paths(repo, scope, ())
            overlay_paths = MM.snapshot_overlay_paths(repo, scope, paths, ())

            with mock.patch.object(
                MM, "run_bytes_command", wraps=MM.run_bytes_command
            ) as bytes_command:
                snapshot = MM.create_snapshot(
                    repo, scope, overlay_paths, workspace
                )

            self.assertTrue((snapshot / "src" / "helper.py").is_file())
            self.assertEqual(
                (snapshot / "src" / "helper.py").read_text(encoding="utf-8"),
                "HELPER = 1\n",
            )
            self.assertEqual(
                (snapshot / "src" / "helper_two.py").read_text(
                    encoding="utf-8"
                ),
                "HELPER_TWO = 2\n",
            )
            batch_calls = [
                call
                for call in bytes_command.call_args_list
                if call.args and call.args[0] == ["git", "cat-file", "--batch"]
            ]
            self.assertEqual(len(batch_calls), 1)
            self.assertFalse(smudge_marker.exists())

    def test_provider_prompt_preserves_task_text_that_matches_artifact_labels(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "run"
            workspace = root / "workspace"
            run_dir.mkdir()
            workspace.mkdir()
            MM.safe_write(run_dir / "change.patch", "patch")
            MM.safe_write(run_dir / "manifest.md", "manifest")
            reviewer = MM.Reviewer(
                "claude", ("claude",), {}, "test", "fake-claude"
            )
            task = (
                "Preserve these exact task words: Patch artifact: change.patch; "
                "Manifest artifact: manifest.md"
            )

            inputs = MM.stage_reviewer_inputs(
                workspace,
                run_dir,
                [reviewer],
                repo=workspace / "snapshot",
                scope=MM.Scope("uncommitted", None, "changes"),
                task=task,
                risks=[],
                review_profile="normal",
                phase="repair",
            )

            input_dir, prompt = inputs["claude"]
            self.assertIn(task, prompt)
            self.assertIn(
                f"Patch artifact: {input_dir / 'change.patch'}", prompt
            )
            self.assertIn(
                f"Manifest artifact: {input_dir / 'manifest.md'}", prompt
            )
            self.assertEqual(prompt.count("Patch artifact:"), 2)
            self.assertEqual(prompt.count("Manifest artifact:"), 2)

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

    def test_workflow_coverage_excludes_contract_invalid_exit_zero_report(
        self,
    ) -> None:
        invalid = (
            Path("/private/tmp/invalid-review"),
            {
                "run_id": "run-invalid",
                "workflow_id": "wf-invalid",
                "repository": {"id": "repo-1", "name": "repo"},
                "status": "failed",
                "round": 1,
                "phase": "repair",
                "reviewers": {
                    "claude": {
                        "exit_code": 0,
                        "report_contract_valid": False,
                        "verdict": "PASS_CLEAN",
                    }
                },
            },
        )
        with (
            mock.patch.object(MM, "workflow_runs", return_value=[invalid]),
            mock.patch.object(MM, "latest_workflow_runs", return_value=[invalid]),
            mock.patch.object(MM, "workflow_lineage_runs", return_value=[invalid]),
            mock.patch.object(
                MM, "workflow_lineage_ids", return_value=["wf-invalid"]
            ),
            mock.patch.object(MM, "workflow_requires_confirmation", return_value=True),
        ):
            status, ready = MM.workflow_status("wf-invalid")

        self.assertFalse(ready)
        self.assertEqual(
            status["external_review_coverage"]["attempted_providers"],
            ["claude"],
        )
        self.assertEqual(
            status["external_review_coverage"]["successful_providers"], []
        )
        self.assertEqual(
            status["external_review_coverage"]["headline"],
            "no successful external provider",
        )

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

    def test_lineage_deferral_is_carried_until_matching_resolution(self) -> None:
        ancestor_metadata = {
            "run_id": "run-ancestor",
            "workflow_id": "wf-ancestor",
        }
        current_metadata = {
            "run_id": "run-current",
            "workflow_id": "wf-current",
        }
        effective = MM.effective_finalization_items(
            [
                (
                    {
                        "id": "claude-001",
                        "kind": "finding",
                        "title": "Resolved ancestor issue",
                        "decision": "deferred",
                    },
                    Path("/ancestor"),
                    ancestor_metadata,
                ),
                (
                    {
                        "id": "claude-002",
                        "kind": "finding",
                        "title": "Still deferred ancestor issue",
                        "decision": "deferred",
                    },
                    Path("/ancestor"),
                    ancestor_metadata,
                ),
                (
                    {
                        "id": "claude-001",
                        "kind": "finding",
                        "title": "Resolved ancestor issue",
                        "decision": "rejected",
                    },
                    Path("/current"),
                    current_metadata,
                ),
            ],
            "wf-current",
        )
        decisions_by_title = {
            str(item[0]["title"]): item[0]["decision"] for item in effective
        }
        self.assertEqual(
            decisions_by_title,
            {
                "Resolved ancestor issue": "rejected",
                "Still deferred ancestor issue": "deferred",
            },
        )

    def test_later_resolution_clears_all_matching_lineage_deferrals(self) -> None:
        effective = MM.effective_finalization_items(
            [
                (
                    {
                        "id": "claude-001",
                        "kind": "finding",
                        "title": "Shared ancestor issue",
                        "decision": "deferred",
                    },
                    Path("/ancestor-one"),
                    {"run_id": "run-ancestor-one", "workflow_id": "wf-one"},
                ),
                (
                    {
                        "id": "claude-002",
                        "kind": "finding",
                        "title": "Shared ancestor issue",
                        "decision": "deferred",
                    },
                    Path("/ancestor-two"),
                    {"run_id": "run-ancestor-two", "workflow_id": "wf-two"},
                ),
                (
                    {
                        "id": "claude-003",
                        "kind": "finding",
                        "title": "Shared ancestor issue",
                        "decision": "rejected",
                    },
                    Path("/current"),
                    {"run_id": "run-current", "workflow_id": "wf-current"},
                ),
            ],
            "wf-current",
        )

        self.assertEqual(
            [(item[2]["run_id"], item[0]["decision"]) for item in effective],
            [("run-current", "rejected")],
        )

    def test_legacy_final_triage_freshness_uses_singular_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            triage_path = run_dir / "triage.json"
            metadata = {"run_id": "run-legacy"}
            MM.safe_write_json(triage_path, {"findings": [], "test_gaps": []})
            final = {
                "triage_sha256": MM.sha256_text(
                    triage_path.read_text(encoding="utf-8")
                )
            }

            self.assertTrue(MM.final_triage_is_fresh(run_dir, metadata, final))

            MM.safe_write_json(
                triage_path,
                {"findings": [{"id": "claude-001"}], "test_gaps": []},
            )
            self.assertFalse(MM.final_triage_is_fresh(run_dir, metadata, final))

            triage_path.unlink()
            self.assertTrue(MM.final_triage_is_fresh(run_dir, metadata, final))

    def test_conservative_gate_preserves_pass_with_findings_both_ways(self) -> None:
        self.assertEqual(
            MM.conservative_gate_status("PASS_CLEAN", "PASS_WITH_FINDINGS"),
            "PASS_WITH_FINDINGS",
        )
        self.assertEqual(
            MM.conservative_gate_status("PASS_WITH_FINDINGS", "PASS_CLEAN"),
            "PASS_WITH_FINDINGS",
        )
        for other_status in ("PASS_CLEAN", "PASS_WITH_FINDINGS"):
            self.assertEqual(
                MM.conservative_gate_status("BLOCK", other_status),
                "BLOCK",
            )
            self.assertEqual(
                MM.conservative_gate_status(other_status, "BLOCK"),
                "BLOCK",
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
                input_dir=run_dir,
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
                input_dir=run_dir,
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
                    input_dir=root,
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

    def test_recover_releases_orphaned_provider_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflows = root / "workflows"
            run_dir = root / "runs" / "run-stale"
            workflows.mkdir()
            run_dir.mkdir(parents=True)
            MM.safe_write_json(
                run_dir / "metadata.json",
                {
                    "run_id": "run-stale",
                    "workflow_id": "wf-stale",
                    "status": "preflight",
                    "created_at": MM.utc_now(),
                    "runner_pid": 999999,
                },
            )
            MM.safe_write_json(
                workflows / "wf-stale.json",
                {
                    "workflow_id": "wf-stale",
                    "usage_reservations": {
                        "run-stale": {
                            "providers": ["claude"],
                            "runner_pid": 999999,
                        }
                    },
                },
            )
            args = MM.build_parser().parse_args(
                ["recover", "--run", str(run_dir)]
            )
            with (
                mock.patch.object(MM, "WORKFLOWS_DIR", workflows),
                mock.patch.object(MM, "process_is_alive", return_value=False),
            ):
                self.assertEqual(MM.recover_command(args), 0)
            workflow = MM.read_json(workflows / "wf-stale.json")
            self.assertEqual(workflow["usage_reservations"], {})

    def test_concurrent_review_start_rejects_existing_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            workflows = root / "workflows"
            runs = root / "runs"
            repo.mkdir()
            workflows.mkdir()
            runs.mkdir()
            initialize_repo(repo)
            (repo / "src" / "feature.py").write_text(
                "VALUE = 2\n", encoding="utf-8"
            )
            with mock.patch.object(MM, "WORKFLOWS_DIR", workflows):
                MM.create_workflow("wf-concurrent", usage_based=True)
            args = MM.build_parser().parse_args(
                [
                    "run",
                    "--repo",
                    str(repo),
                    "--workflow-id",
                    "wf-concurrent",
                    "--uncommitted",
                    "--task",
                    "review concurrency",
                    "--with-claude",
                    "--without-antigravity",
                    "--without-kimi",
                    "--sequential",
                ]
            )
            snapshot_entered = threading.Event()
            allow_snapshot_failure = threading.Event()
            first_errors: list[BaseException] = []

            def block_snapshot(*_args: object, **_kwargs: object) -> Path:
                snapshot_entered.set()
                self.assertTrue(allow_snapshot_failure.wait(timeout=10))
                raise MM.ReviewError("synthetic snapshot stop")

            def first_run() -> None:
                try:
                    MM.run_review_command(args)
                except BaseException as exc:  # captured for the test thread
                    first_errors.append(exc)

            reviewer = MM.Reviewer(
                "claude", ("claude",), {}, "test", "fake-claude"
            )
            with (
                mock.patch.object(MM, "WORKFLOWS_DIR", workflows),
                mock.patch.object(MM, "RUNS_DIR", runs),
                mock.patch.object(
                    MM,
                    "reviewer_definitions",
                    return_value=[reviewer],
                ),
                mock.patch.object(MM, "reviewer_budget_estimates", return_value={}),
                mock.patch.object(
                    MM,
                    "apply_workflow_budget",
                    return_value=([reviewer], None),
                ),
                mock.patch.object(MM, "create_snapshot", side_effect=block_snapshot),
            ):
                thread = threading.Thread(target=first_run)
                thread.start()
                self.assertTrue(snapshot_entered.wait(timeout=10))
                with self.assertRaisesRegex(
                    MM.ReviewError, "already in preflight or running"
                ):
                    MM.run_review_command(args)
                allow_snapshot_failure.set()
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())

            self.assertEqual(len(first_errors), 1)
            self.assertIsInstance(first_errors[0], MM.ReviewError)
            metadata_paths = list(runs.rglob("metadata.json"))
            self.assertEqual(len(metadata_paths), 1)
            self.assertEqual(
                MM.read_json(metadata_paths[0])["status"], "preflight_blocked"
            )

    def test_gate_reports_unknown_workflow_actionably(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workflows = Path(temporary)
            args = MM.build_parser().parse_args(["gate", "wf-missing"])
            with (
                mock.patch.object(MM, "WORKFLOWS_DIR", workflows),
                self.assertRaisesRegex(
                    MM.ReviewError,
                    "Unknown workflow wf-missing.*workflow start",
                ),
            ):
                MM.gate_command(args)

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

    def test_external_snapshot_symlink_is_blocked(self) -> None:
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

    def test_provider_failure_redaction_covers_all_scanner_secret_shapes(self) -> None:
        raw_values = [
            "AKIAIOSFODNN7EXAMPLE",
            "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
            "xoxb-" + "123456789012-" + "abcdefghijklmnop",
            "sk-abcdefghijklmnopqrstuvwxyz123456",
            "-----BEGIN RSA PRIVATE KEY-----",
            'password = "synthetic-sensitive-value-123456"',
        ]
        redacted = MM.sanitized_failure_text(*raw_values)
        for value in raw_values:
            self.assertNotIn(value, redacted)
        self.assertIn("[REDACTED:", redacted)

    def test_secret_scan_covers_large_files_and_deleted_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            large = repo / "large.txt"
            large.write_text(
                "a" * (2 * 1024 * 1024 + 32)
                + "\nAKIAIOSFODNN7EXAMPLE\n",
                encoding="utf-8",
            )
            findings = MM.sensitive_content_findings(
                repo,
                ["large.txt"],
                '---- apiKey = "deleted-sensitive-value-123456"\n',
            )
        by_path: dict[str, list[MM.SensitiveFinding]] = {}
        for finding in findings:
            by_path.setdefault(finding.path, []).append(finding)
        self.assertEqual(len(by_path["large.txt"]), 1)
        self.assertEqual(len(by_path["change.patch"]), 1)

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

    def test_legacy_observation_records_vacated_finding_identifier(self) -> None:
        report = structured_report(
            verdict="PASS_WITH_FINDINGS",
            findings=(
                "## [low] Formatting preference\n"
                "- Location: src/feature.py:1\n"
                "- Trigger: reading the file\n"
                "- Evidence: naming is subjective\n"
                "- Impact: none\n"
                "- Smallest fix: none\n"
                "- Confidence: medium\n\n"
                "## [medium] Retry can duplicate work\n"
                "- Location: src/feature.py:2\n"
                "- Trigger: retry after persistence\n"
                "- Evidence: no idempotency key\n"
                "- Impact: duplicate job\n"
                "- Smallest fix: persist a stable key\n"
                "- Confidence: high"
            ),
        )
        parsed = MM.parse_review_report("claude", report)
        self.assertEqual([item["id"] for item in parsed["findings"]], ["claude-002"])
        self.assertEqual(
            parsed["observations"][0]["promoted_from"], "claude-001"
        )

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
        self.assertIn("trace every helper called from that loop", collapsed_prompt)
        self.assertIn(
            "N-times or N-by-M external-I/O amplification", collapsed_prompt
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
                    "--codex-verdict",
                    "PASS_CLEAN",
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
                    "--codex-verdict",
                    "PASS_CLEAN",
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
            workflows_dir = root / "workflows"
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
                    "workflow_id": "wf-attest",
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
                workflows_dir / "wf-attest.final.json",
                {"state": "completed", "finalized_at": "original-time"},
            )
            MM.safe_write_json(
                run_dir / "final.json",
                {
                    "schema_version": 8,
                    "source_fingerprint": fingerprint,
                    "status": "PASS_CLEAN",
                    "codex_verdict": "PASS_CLEAN",
                    "triage_status": "PASS_CLEAN",
                    "triage_sha256s": {"run-attest": "a" * 64},
                },
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
            recomputed_status = {
                "state": "completed_stale",
                "ready": False,
                "deployment_ready": False,
            }
            with (
                mock.patch.object(MM, "WORKFLOWS_DIR", workflows_dir),
                mock.patch.object(
                    MM,
                    "workflow_status",
                    return_value=(recomputed_status, False),
                ),
            ):
                self.assertEqual(MM.attest_commit_command(args), 0)
            final = MM.read_json(run_dir / "final.json")
            self.assertEqual(final["commit_attestations"][0]["commit"], head)
            workflow_final = MM.read_json(
                workflows_dir / "wf-attest.final.json"
            )
            self.assertEqual(workflow_final["state"], "completed_stale")
            self.assertFalse(workflow_final["deployment_ready"])
            self.assertEqual(workflow_final["finalized_at"], "original-time")

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

    def test_fixed_and_covered_decisions_require_changed_scoped_source(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            run_dir = root / "run"
            repo.mkdir()
            run_dir.mkdir()
            initialize_repo(repo)
            (repo / "src" / "feature.py").write_text(
                "VALUE = 2\n", encoding="utf-8"
            )
            scope = MM.Scope(
                "uncommitted",
                None,
                "staged, unstaged, and untracked changes",
            )
            paths = ["src/feature.py"]
            reviewed_fingerprint = MM.fingerprint(
                repo, scope, paths, ("src",)
            )
            MM.safe_write_json(
                run_dir / "metadata.json",
                {
                    "repository": {"root": str(repo)},
                    "scope": {
                        "kind": scope.kind,
                        "value": scope.value,
                        "label": scope.label,
                    },
                    "path_filters": ["src"],
                    "paths": paths,
                    "source_fingerprint": reviewed_fingerprint,
                },
            )
            MM.safe_write_json(
                run_dir / "triage.json",
                {
                    "findings": [
                        {
                            "id": "claude-001",
                            "kind": "finding",
                            "decision": "accepted",
                        }
                    ],
                    "test_gaps": [
                        {
                            "id": "claude-test-001",
                            "kind": "test_gap",
                            "decision": "accepted",
                        }
                    ],
                    "observations": [],
                },
            )
            decisions = [
                {
                    "finding": "claude-001",
                    "decision": "fixed",
                    "evidence": "The enqueue now follows persistence.",
                    "verification": "Focused regression passed.",
                },
                {
                    "finding": "claude-test-001",
                    "decision": "covered",
                    "evidence": "A regression test now asserts ordering.",
                    "verification": "Focused regression passed.",
                },
            ]

            with self.assertRaisesRegex(
                MM.ReviewError, "task-scoped source is unchanged"
            ):
                MM.write_triage_decisions(run_dir, decisions)
            unchanged = MM.read_json(run_dir / "triage.json")
            self.assertEqual(unchanged["findings"][0]["decision"], "accepted")
            self.assertEqual(
                unchanged["test_gaps"][0]["decision"], "accepted"
            )

            (repo / "src" / "feature.py").write_text(
                "VALUE = 3\n", encoding="utf-8"
            )
            MM.write_triage_decisions(run_dir, decisions)
            resolved = MM.read_json(run_dir / "triage.json")
            self.assertEqual(resolved["findings"][0]["decision"], "fixed")
            self.assertEqual(
                resolved["test_gaps"][0]["decision"], "covered"
            )


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
                            "observations": [],
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
                input_dir=root,
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
                    input_dir=root,
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
        self.assertEqual(adjusted[0].command[-1], "0.272727")
        self.assertEqual(status["remaining_before_run_usd"], 0.3)
        self.assertEqual(status["reserved_for_run_usd"], 0.3)
        self.assertEqual(status["provider_overrun_safety_reserve_usd"], 0.027273)
        with (
            mock.patch.object(MM, "workflow_budget_limit", return_value=1.0),
            mock.patch.object(MM, "workflow_spend", return_value=0.8),
            self.assertRaisesRegex(
                MM.ReviewError, r"below the \$0.25 minimum viable provider budget"
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
                self.assertAlmostEqual(first_budget + second_budget, 1.818182)
                self.assertEqual(first_status["reserved_before_run_usd"], 0.0)
                self.assertEqual(second_status["reserved_before_run_usd"], 1.375)
                self.assertEqual(first_status["reserved_for_run_usd"], 1.375)
                self.assertEqual(second_status["reserved_for_run_usd"], 0.625)
                self.assertEqual(len(document["budget_reservations"]), 2)
                MM.release_workflow_budget_reservation("wf-budget", "run-1")
                MM.release_workflow_budget_reservation("wf-budget", "run-2")
                released = MM.read_json(workflows / "wf-budget.json")
        self.assertEqual(released["budget_reservations"], {})

    def test_workflow_budget_blocks_historically_underfunded_attempt(self) -> None:
        reviewer = MM.Reviewer(
            "claude",
            ("claude", "--max-budget-usd", "1.25"),
            {},
            "sonnet",
            "fake",
        )
        with self.assertRaisesRegex(
            MM.ReviewError,
            r"at most \$0.91.*below the \$1.10 minimum viable provider budget",
        ):
            MM._adjust_workflow_budget(
                [reviewer],
                identifier="wf-underfunded",
                limit=5.0,
                spent=4.0,
                reserved=0.0,
                minimum_provider_budget_usd=1.10,
            )

    def test_repair_admission_blocks_underfunded_last_chance(self) -> None:
        reviewer = MM.Reviewer(
            "claude",
            ("claude", "--max-budget-usd", "1.25"),
            {},
            "sonnet",
            "fake",
        )
        assessment = MM.review_admission_assessment(
            [reviewer],
            phase="repair",
            workflow_budget={
                "mode": "provider_allowance",
                "max_attempts_per_provider": 6,
                "attempts_before_run": {"claude": 4},
                "reserved_before_run": {"claude": 0},
            },
            budget_estimates={
                "claude": {
                    "configured_budget_usd": 1.25,
                    "recommended_budget_usd": 1.82,
                    "sample_count": 261,
                    "confidence": "high",
                }
            },
        )

        provider = assessment["providers"][0]
        self.assertTrue(assessment["blocked"])
        self.assertEqual(
            provider["block_reason"], "underfunded_without_recovery_headroom"
        )
        self.assertEqual(provider["required_successful_rounds"], 2)
        self.assertEqual(provider["recovery_attempts_after_required_rounds"], 0)
        with self.assertRaisesRegex(
            MM.ReviewError,
            r"No provider was started.*--claude-max-budget-usd 1\.82.*--to 7",
        ):
            MM.enforce_review_admission(assessment, workflow_id="wf-test")

    def test_repair_admission_blocks_when_confirmation_cannot_be_reached(self) -> None:
        reviewer = MM.Reviewer(
            "claude",
            ("claude", "--max-budget-usd", "2.00"),
            {},
            "sonnet",
            "fake",
        )
        assessment = MM.review_admission_assessment(
            [reviewer],
            phase="repair",
            workflow_budget={
                "mode": "provider_allowance",
                "max_attempts_per_provider": 6,
                "attempts_before_run": {"claude": 5},
                "reserved_before_run": {"claude": 0},
            },
            budget_estimates={},
        )

        self.assertTrue(assessment["blocked"])
        self.assertEqual(
            assessment["providers"][0]["block_reason"],
            "insufficient_attempts",
        )
        with self.assertRaisesRegex(
            MM.ReviewError,
            r"1 attempt\(s\) remaining but 2 successful round\(s\).*--to 8",
        ):
            MM.enforce_review_admission(assessment, workflow_id="wf-test")

    def test_repair_admission_allows_recommended_budget_at_exact_headroom(self) -> None:
        reviewer = MM.Reviewer(
            "claude",
            ("claude", "--max-budget-usd", "1.82"),
            {},
            "sonnet",
            "fake",
        )
        assessment = MM.review_admission_assessment(
            [reviewer],
            phase="repair",
            workflow_budget={
                "mode": "provider_allowance",
                "max_attempts_per_provider": 6,
                "attempts_before_run": {"claude": 4},
                "reserved_before_run": {"claude": 0},
            },
            budget_estimates={
                "claude": {
                    "configured_budget_usd": 1.82,
                    "recommended_budget_usd": 1.82,
                    "sample_count": 261,
                    "confidence": "high",
                }
            },
        )

        self.assertFalse(assessment["blocked"])
        MM.enforce_review_admission(assessment, workflow_id="wf-test")

    def test_confirmation_admission_blocks_underfunded_last_attempt(self) -> None:
        reviewer = MM.Reviewer(
            "claude",
            ("claude", "--max-budget-usd", "1.25"),
            {},
            "sonnet",
            "fake",
        )
        assessment = MM.review_admission_assessment(
            [reviewer],
            phase="confirmation",
            workflow_budget={
                "mode": "provider_allowance",
                "max_attempts_per_provider": 6,
                "attempts_before_run": {"claude": 5},
                "reserved_before_run": {"claude": 0},
            },
            budget_estimates={
                "claude": {
                    "configured_budget_usd": 1.25,
                    "recommended_budget_usd": 1.82,
                    "sample_count": 261,
                    "confidence": "high",
                }
            },
        )

        provider = assessment["providers"][0]
        self.assertTrue(assessment["blocked"])
        self.assertEqual(assessment["phase"], "confirmation")
        self.assertEqual(provider["required_successful_rounds"], 1)
        self.assertEqual(provider["attempts_remaining_before_run"], 1)
        self.assertEqual(
            provider["block_reason"], "underfunded_without_recovery_headroom"
        )
        with self.assertRaisesRegex(
            MM.ReviewError,
            r"1 required round\(s\).*--claude-max-budget-usd 1\.82.*--to 7",
        ):
            MM.enforce_review_admission(assessment, workflow_id="wf-test")

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
        self.assertEqual(float(second[0].command[-1]), 0.568182)
        self.assertEqual(first_status["reserved_before_run_usd"], 0.0)
        self.assertEqual(second_status["reserved_before_run_usd"], 1.375)
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

    def test_provider_cost_beyond_safety_reservation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            report = run_dir / "claude.md"
            error = run_dir / "claude.stderr.log"
            kimi_report = run_dir / "kimi.md"
            kimi_error = run_dir / "kimi.stderr.log"
            report.write_text(
                "# Verdict\nPASS_CLEAN\n\n# Findings\nNone.\n\n"
                "# Test gaps\nNone.\n\n# Coverage\n- Complete: yes\n"
                "- Unreviewed changed paths: []\n- Limitations: []\n\n"
                "# Notes\nNone.\n",
                encoding="utf-8",
            )
            error.write_text("", encoding="utf-8")
            kimi_report.write_text(
                report.read_text(encoding="utf-8"), encoding="utf-8"
            )
            kimi_error.write_text("", encoding="utf-8")
            now = MM.utc_now()
            metadata = {
                "run_id": "run-budget-overrun",
                "workflow_id": "wf-budget-overrun",
                "repository": {"id": "repo-1"},
                "created_at": now,
                "started_at": now,
                "risks": [],
                "workflow_budget": {"reserved_for_run_usd": 1.375},
            }
            reviewer = MM.Reviewer(
                "claude", ("claude",), {}, "sonnet", "fake"
            )
            kimi = MM.Reviewer("kimi", ("kimi",), {}, "k3", "fake")
            results = [
                MM.ReviewResult(
                    "claude",
                    0,
                    report,
                    error,
                    now,
                    now,
                    0.1,
                    False,
                    {"total_cost_usd": 1.40},
                    None,
                ),
                MM.ReviewResult(
                    "kimi",
                    0,
                    kimi_report,
                    kimi_error,
                    now,
                    now,
                    0.1,
                    False,
                    None,
                    None,
                ),
            ]
            with mock.patch.object(MM, "workflow_lineage_runs", return_value=[]):
                failures, invalid = MM.persist_review_results(
                    run_dir=run_dir,
                    metadata=metadata,
                    reviewers=[reviewer, kimi],
                    results=results,
                )
            persisted = MM.read_json(run_dir / "metadata.json")
            summary = MM.read_json(run_dir / "review-summary.json")
            guidance = MM.reviewer_failure_guidance(run_dir, persisted)
        self.assertEqual(failures, ["claude"])
        self.assertEqual(invalid, [])
        self.assertEqual(persisted["status"], "failed")
        self.assertEqual(persisted["failure"]["type"], "lineage_budget_exceeded")
        self.assertEqual(summary["reviews"]["kimi"]["verdict"], "PASS_CLEAN")
        self.assertNotIn("mm-review resume", guidance)
        self.assertIn("cannot be resumed", guidance)
        self.assertIn("fresh review round", guidance)

    def test_provider_cost_beyond_usage_stop_reserve_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            report = run_dir / "claude.md"
            error = run_dir / "claude.stderr.log"
            report.write_text(
                "# Verdict\nPASS_CLEAN\n\n# Findings\nNone.\n\n"
                "# Test gaps\nNone.\n\n# Coverage\n- Complete: yes\n"
                "- Unreviewed changed paths: []\n- Limitations: []\n\n"
                "# Notes\nNone.\n",
                encoding="utf-8",
            )
            error.write_text("", encoding="utf-8")
            now = MM.utc_now()
            metadata = {
                "run_id": "run-usage-overrun",
                "workflow_id": "wf-usage-overrun",
                "repository": {"id": "repo-1"},
                "created_at": now,
                "started_at": now,
                "risks": [],
                "workflow_usage": {"mode": "provider_allowance"},
                "workflow_budget": None,
                "review_policy": {
                    "claude_api_equivalent_limit_usd": 1.25,
                    "api_equivalent_usd_is_billing": False,
                },
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
                {"total_cost_usd": 1.40},
                None,
            )
            with mock.patch.object(MM, "workflow_lineage_runs", return_value=[]):
                failures, invalid = MM.persist_review_results(
                    run_dir=run_dir,
                    metadata=metadata,
                    reviewers=[reviewer],
                    results=[result],
                )
            persisted = MM.read_json(run_dir / "metadata.json")
            guidance = MM.reviewer_failure_guidance(run_dir, persisted)
        self.assertEqual(failures, ["claude"])
        self.assertEqual(invalid, [])
        self.assertEqual(persisted["status"], "failed")
        self.assertEqual(
            persisted["failure"]["type"],
            "provider_api_equivalent_stop_exceeded",
        )
        self.assertAlmostEqual(
            persisted["failure"]["protected_reservation_usd"], 1.375
        )
        self.assertIn("per-call emergency stop", guidance)
        self.assertNotIn("mm-review resume", guidance)

    def test_workflow_supersede_links_both_documents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workflows = Path(temporary)
            prior_runs = [
                (
                    Path(temporary) / "repo-one" / "run-one",
                    {
                        "phase": "confirmation",
                        "repository": {
                            "id": "repo-one",
                            "name": "one",
                            "root": "/private/tmp/one",
                        },
                    },
                ),
                (
                    Path(temporary) / "repo-two" / "run-two",
                    {
                        "phase": "confirmation",
                        "repository": {
                            "id": "repo-two",
                            "name": "two",
                            "root": "/private/tmp/two",
                        },
                    },
                ),
            ]
            with (
                mock.patch.object(MM, "WORKFLOWS_DIR", workflows),
                mock.patch.object(
                    MM, "workflow_lineage_runs", return_value=prior_runs
                ),
            ):
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
            [item["id"] for item in new["required_repositories"]],
            ["repo-one", "repo-two"],
        )
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

    def test_successor_missing_inherited_repository_cannot_finalize(self) -> None:
        metadata = {
            "workflow_id": "wf-successor",
            "run_id": "run-one",
            "repository": {
                "id": "repo-one",
                "name": "one",
                "root": "/private/tmp/one",
            },
            "status": "completed",
            "round": 1,
            "phase": "confirmation",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflows = root / "workflows"
            workflows.mkdir()
            run_dir = root / "run-one"
            run_dir.mkdir()
            MM.safe_write_json(
                workflows / "wf-successor.json",
                {
                    "workflow_id": "wf-successor",
                    "supersedes": ["wf-old"],
                    "policy": MM.workflow_policy(),
                    "required_repositories": [
                        metadata["repository"],
                        {
                            "id": "repo-two",
                            "name": "two",
                            "root": "/private/tmp/two",
                        },
                    ],
                },
            )
            MM.safe_write_json(
                run_dir / "final.json",
                {
                    "schema_version": MM.SCHEMA_VERSION,
                    "status": "PASS_CLEAN",
                    "source_fingerprint": "same",
                    "codex_verdict": "PASS_CLEAN",
                    "triage_status": "PASS_CLEAN",
                    "triage_sha256s": {"run-one": "a" * 64},
                },
            )
            MM.safe_write_json(
                run_dir / "triage.json", {"findings": [], "test_gaps": []}
            )
            records = [(run_dir, metadata)]
            with (
                mock.patch.object(MM, "WORKFLOWS_DIR", workflows),
                mock.patch.object(MM, "CONFIG_PATH", root / "missing.json"),
                mock.patch.object(MM, "workflow_runs", return_value=records),
                mock.patch.object(
                    MM, "workflow_lineage_runs", return_value=records
                ),
                mock.patch.object(
                    MM, "workflow_lineage_ids", return_value=["wf-successor"]
                ),
                mock.patch.object(
                    MM, "latest_workflow_runs", return_value=records
                ),
                mock.patch.object(
                    MM, "latest_workflow_attempts", return_value=records
                ),
                mock.patch.object(
                    MM,
                    "freshness_status",
                    return_value={"fresh": True, "mode": "working-tree"},
                ),
                mock.patch.object(MM, "final_triage_is_fresh", return_value=True),
                mock.patch.object(
                    MM, "run_artifact_bytes", return_value=MM.empty_artifact_bytes()
                ),
            ):
                status, ready = MM.workflow_status("wf-successor")
                plan = MM.workflow_continue_plan("wf-successor")
                args = MM.build_parser().parse_args(
                    ["workflow", "finalize", "wf-successor"]
                )
                with self.assertRaisesRegex(
                    MM.ReviewError, "Workflow is not ready"
                ):
                    MM.workflow_finalize_command(args)
        self.assertFalse(ready)
        missing = [
            item
            for item in status["repositories"]
            if item["state"] == "not-reviewed"
        ]
        self.assertEqual([item["repository"]["id"] for item in missing], ["repo-two"])
        self.assertEqual(plan["next"], "NEEDS_REVIEW")
        self.assertEqual(plan["actions"][0]["repository"], "two")
        self.assertIn("--reuse-contract", plan["actions"][0]["command"])

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

    def test_workflow_supersede_by_rejects_different_provider_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workflows = Path(temporary)
            with mock.patch.object(MM, "WORKFLOWS_DIR", workflows):
                MM.create_workflow(
                    "wf-old",
                    usage_based=True,
                    max_provider_attempts=4,
                    provider_use_policy="explicit",
                )
                MM.create_workflow(
                    "wf-replacement",
                    usage_based=True,
                    max_provider_attempts=6,
                    provider_use_policy="explicit",
                )
                args = MM.build_parser().parse_args(
                    [
                        "workflow",
                        "supersede",
                        "wf-old",
                        "--by",
                        "wf-replacement",
                        "--reason",
                        "contract changed",
                    ]
                )
                with self.assertRaisesRegex(
                    MM.ReviewError, "provider-usage policy must exactly match"
                ):
                    MM.workflow_supersede_command(args)
                old = MM.read_json(workflows / "wf-old.json")
                replacement = MM.read_json(workflows / "wf-replacement.json")
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
            MM.safe_write(run_dir / "manifest.md", "manifest")
            MM.safe_write(run_dir / "prompt.md", "review")
            MM.safe_write_json(
                run_dir / "metadata.json",
                {
                    "schema_version": 9,
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
                    "review_snapshot_fingerprint": "snapshot-fingerprint",
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
                mock.patch.object(MM, "snapshot_review_paths", return_value=[]),
                mock.patch.object(
                    MM, "content_fingerprint", return_value="snapshot-fingerprint"
                ),
                mock.patch.object(
                    MM,
                    "invoke_reviewer",
                    side_effect=RuntimeError(
                        "synthetic-sensitive-value-must-not-persist"
                    ),
                ),
                self.assertRaisesRegex(
                    MM.ReviewError, "redacted private diagnostics"
                ),
            ):
                MM.resume_review_command(args)
            metadata = MM.read_json(run_dir / "metadata.json")
            diagnostic = MM.read_json(run_dir / "internal-error.json")
            raw_artifacts = "".join(
                path.read_text(encoding="utf-8")
                for path in run_dir.glob("*.json")
            )
        self.assertEqual(metadata["status"], "partial")
        self.assertEqual(metadata["failure"]["type"], "reviewer_failure")
        self.assertEqual(metadata["terminal_error"]["type"], "RuntimeError")
        self.assertEqual(
            metadata["terminal_error"]["message"],
            "Unexpected internal runner failure.",
        )
        self.assertEqual(diagnostic["error_type"], "RuntimeError")
        self.assertNotIn("synthetic-sensitive-value-must-not-persist", raw_artifacts)

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
                    "schema_version": 9,
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

    def test_resume_rejects_mixed_exhausted_and_available_reviewers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "run"
            repo = root / "repo"
            run_dir.mkdir()
            repo.mkdir()
            (run_dir / "change.patch").write_text("patch", encoding="utf-8")
            reviewers = {
                name: {
                    "exit_code": 1,
                    "failure_category": "provider_failure",
                    "model": model,
                }
                for name, model in (("claude", "sonnet"), ("kimi", "k3"))
            }
            MM.safe_write_json(
                run_dir / "metadata.json",
                {
                    "schema_version": 10,
                    "run_id": "run-mixed",
                    "status": "partial",
                    "failure": {
                        "type": "reviewer_failure",
                        "reviewers": ["claude", "kimi"],
                    },
                    "reviewers": reviewers,
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
                    "review_policy": {},
                },
            )
            selected = MM.Reviewer("kimi", ("kimi",), {}, "k3", "fake")
            with (
                mock.patch.object(MM, "require_active_workflow"),
                mock.patch.object(MM, "resolve_repo", return_value=repo),
                mock.patch.object(MM, "changed_paths", return_value=["src/a.py"]),
                mock.patch.object(
                    MM, "snapshot_overlay_paths", return_value=["src/a.py"]
                ),
                mock.patch.object(MM, "fingerprint", return_value="fingerprint"),
                mock.patch.object(MM, "workflow_runs", return_value=[]),
                mock.patch.object(
                    MM,
                    "reviewer_definitions",
                    return_value=[
                        MM.Reviewer(
                            "claude", ("claude",), {}, "sonnet", "fake"
                        ),
                        selected,
                    ],
                ),
                mock.patch.object(MM, "reviewer_budget_estimates", return_value={}),
                mock.patch.object(
                    MM, "minimum_viable_reviewer_budget", return_value=0.25
                ),
                mock.patch.object(
                    MM,
                    "apply_workflow_budget",
                    return_value=(
                        [selected],
                        {
                            "mode": "provider_allowance",
                            "skipped_providers": {
                                "claude": (
                                    "provider-attempt allowance exhausted (2/2)"
                                )
                            },
                        },
                    ),
                ),
                mock.patch.object(
                    MM, "release_workflow_budget_reservation"
                ) as release,
                mock.patch.object(MM, "invoke_reviewer") as invoke,
                self.assertRaisesRegex(
                    MM.ReviewError,
                    "not eligible for another provider attempt: claude: "
                    "provider-attempt allowance exhausted",
                ),
            ):
                MM.resume_review_locked(run_dir)
        release.assert_called_once_with("wf-active", "run-mixed")
        invoke.assert_not_called()

    def test_resume_failure_releases_provider_usage_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflows = root / "workflows"
            run_dir = root / "run"
            repo = root / "repo"
            run_dir.mkdir()
            repo.mkdir()
            (run_dir / "change.patch").write_text("patch", encoding="utf-8")
            MM.safe_write_json(
                run_dir / "metadata.json",
                {
                    "schema_version": 10,
                    "run_id": "run-reserved",
                    "status": "failed",
                    "failure": {
                        "type": "reviewer_failure",
                        "reviewers": ["kimi"],
                    },
                    "reviewers": {
                        "kimi": {
                            "exit_code": 1,
                            "failure_category": "provider_failure",
                            "model": "k3",
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
                    "review_policy": {},
                },
            )
            kimi = MM.Reviewer("kimi", ("kimi",), {}, "k3", "fake")
            with (
                mock.patch.object(MM, "WORKFLOWS_DIR", workflows),
                mock.patch.object(MM, "resolve_repo", return_value=repo),
                mock.patch.object(MM, "changed_paths", return_value=["src/a.py"]),
                mock.patch.object(
                    MM, "snapshot_overlay_paths", return_value=["src/a.py"]
                ),
                mock.patch.object(MM, "fingerprint", return_value="fingerprint"),
                mock.patch.object(MM, "workflow_runs", return_value=[]),
                mock.patch.object(
                    MM,
                    "workflow_provider_attempts",
                    return_value={provider: 0 for provider in MM.PROVIDERS},
                ),
                mock.patch.object(MM, "active_provider_cooldown", return_value=None),
                mock.patch.object(MM, "reviewer_definitions", return_value=[kimi]),
                mock.patch.object(MM, "reviewer_budget_estimates", return_value={}),
                mock.patch.object(
                    MM, "minimum_viable_reviewer_budget", return_value=0.25
                ),
                mock.patch.object(
                    MM,
                    "create_snapshot",
                    side_effect=MM.ReviewError("snapshot fingerprint mismatch"),
                ),
            ):
                MM.create_workflow(
                    "wf-active", usage_based=True, max_provider_attempts=2
                )
                with self.assertRaisesRegex(
                    MM.ReviewError, "snapshot fingerprint mismatch"
                ):
                    MM.resume_review_locked(run_dir)
                workflow = MM.read_json(workflows / "wf-active.json")
        self.assertEqual(workflow.get("usage_reservations"), {})

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
                    review_snapshot_fingerprint="snapshot-abc",
                    findings=findings,
                )
                path, value = MM.validate_sensitive_scan_token(
                    token,
                    repository=repository,
                    scope=scope,
                    path_filters=(),
                    paths=["fixture.py"],
                    source_fingerprint="abc",
                    review_snapshot_fingerprint="snapshot-abc",
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
                        review_snapshot_fingerprint="snapshot-abc",
                    )

    def test_sensitive_scan_covers_unchanged_files_outside_task_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            scans = root / "scans"
            repo.mkdir()
            initialize_repo(repo)
            (repo / "credentials.py").write_text(
                'apiKey = "credential-value-123456"\n', encoding="utf-8"
            )
            run(["git", "add", "credentials.py"], cwd=repo)
            run(["git", "commit", "-qm", "add review fixture"], cwd=repo)
            (repo / "src" / "feature.py").write_text(
                "VALUE = 2\n", encoding="utf-8"
            )
            args = MM.build_parser().parse_args(
                [
                    "scan",
                    "--repo",
                    str(repo),
                    "--path",
                    "src",
                    "--approve-findings",
                ]
            )
            output = io.StringIO()
            with (
                mock.patch.object(MM, "SENSITIVE_SCANS_DIR", scans),
                redirect_stdout(output),
            ):
                result = MM.sensitive_scan_command(args)
                report = json.loads(output.getvalue())
                token_path = MM.sensitive_scan_path(report["approved_token"])
                token = MM.read_json(token_path)
        self.assertEqual(result, 0)
        self.assertEqual(report["paths"], ["src/feature.py"])
        self.assertIn(
            "credentials.py",
            [item["path"] for item in report["sensitive_findings"]],
        )
        self.assertGreater(report["review_snapshot_file_count"], 1)
        self.assertEqual(
            token["review_snapshot_fingerprint"],
            report["review_snapshot_fingerprint"],
        )

    def test_run_blocks_unchanged_secret_outside_task_scope_before_provider(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            runs = root / "runs"
            repo.mkdir()
            initialize_repo(repo)
            (repo / "credentials.py").write_text(
                'apiKey = "credential-value-123456"\n', encoding="utf-8"
            )
            run(["git", "add", "credentials.py"], cwd=repo)
            run(["git", "commit", "-qm", "add review fixture"], cwd=repo)
            (repo / "src" / "feature.py").write_text(
                "VALUE = 2\n", encoding="utf-8"
            )
            args = MM.build_parser().parse_args(
                [
                    "run",
                    "--repo",
                    str(repo),
                    "--path",
                    "src",
                    "--task",
                    "Review the scoped feature change.",
                ]
            )
            with (
                mock.patch.object(MM, "RUNS_DIR", runs),
                mock.patch.object(MM, "WORKFLOWS_DIR", runs / "workflows"),
                mock.patch.object(
                    MM,
                    "load_config",
                    return_value=json.loads(json.dumps(MM.DEFAULT_CONFIG)),
                ),
                mock.patch.object(MM, "reviewer_definitions", return_value=[]),
                mock.patch.object(MM, "invoke_reviewer") as invoke,
                self.assertRaisesRegex(
                    MM.ReviewError,
                    "likely sensitive material is in the private snapshot",
                ),
            ):
                MM.run_review_command(args)
        invoke.assert_not_called()

    def test_direct_sensitive_finding_override_is_rejected(self) -> None:
        args = MM.build_parser().parse_args(
            ["run", "--allow-sensitive-finding", "deadbeef1234"]
        )
        with self.assertRaisesRegex(
            MM.ReviewError, "Direct sensitive-content overrides"
        ):
            MM.run_review_command(args)

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

    def test_analytics_accepts_timezone_naive_legacy_timestamp(self) -> None:
        runs = [
            (
                Path("/tmp/legacy-naive"),
                {
                    "created_at": MM.utc_now().replace("+00:00", ""),
                    "status": "preflight_blocked",
                    "workflow_id": "wf-legacy-naive",
                },
            )
        ]
        with (
            mock.patch.object(MM, "all_run_metadata", return_value=runs),
            mock.patch.object(MM, "workflow_path", return_value=Path("/missing")),
            mock.patch.object(MM, "WORKFLOWS_DIR", Path("/missing")),
        ):
            report = MM.analytics_report(7)
        self.assertEqual(report["run_statuses"]["preflight_blocked"], 1)

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
                with self.assertRaisesRegex(
                    MM.ReviewError, "provider-overrun safety reserve"
                ):
                    MM.apply_workflow_budget(
                        [reviewer], "wf-new", reservation_id="run-new"
                    )
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

    def test_evidence_memory_rebuild_reports_skipped_malformed_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            valid = root / "valid"
            malformed = root / "malformed"
            valid.mkdir()
            malformed.mkdir()
            database = root / "evidence.sqlite3"
            MM.safe_write_json(
                valid / "metadata.json",
                {
                    "run_id": "run-valid",
                    "workflow_id": "wf-valid",
                    "round": 1,
                },
            )
            MM.safe_write_json(valid / "triage.json", {"findings": []})
            MM.safe_write_json(
                malformed / "metadata.json",
                {
                    "run_id": "run-malformed",
                    "workflow_id": "wf-malformed",
                    "round": "not-a-number",
                },
            )
            MM.safe_write_json(malformed / "triage.json", {"findings": []})

            rebuilt = EM.rebuild(
                database,
                [(valid, "wf-valid"), (malformed, "wf-malformed")],
            )

            self.assertEqual(rebuilt["runs"], 1)
            self.assertEqual(rebuilt["skipped_runs"], 1)
            self.assertIsNone(
                EM.upsert_run(database, malformed, lineage_root="wf-malformed")
            )

    def test_evidence_memory_search_many_uses_one_database_connection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "run"
            run_dir.mkdir()
            database = root / "evidence.sqlite3"
            MM.safe_write_json(
                run_dir / "metadata.json",
                {
                    "run_id": "run-one",
                    "workflow_id": "wf-one",
                    "round": 1,
                    "repository": {"id": "repo-one"},
                },
            )
            MM.safe_write_json(
                run_dir / "triage.json",
                {
                    "findings": [
                        {
                            "id": "claude-001",
                            "title": "Retry duplicates a queued job",
                            "decision": "fixed",
                        }
                    ]
                },
            )
            self.assertEqual(
                EM.upsert_run(database, run_dir, lineage_root="wf-one"), 1
            )
            original_open = EM._open_database
            with mock.patch.object(
                EM, "_open_database", wraps=original_open
            ) as open_database:
                results = EM.search_many(
                    database,
                    ["retry duplicate", "queued job"],
                    repository_id="repo-one",
                    minimum_similarity=0.0,
                )
            self.assertEqual(open_database.call_count, 1)
            self.assertEqual(len(results), 2)
            self.assertTrue(all(result for result in results))

    def test_workflow_query_cache_reads_run_metadata_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runs = Path(temporary) / "runs"
            run_dir = runs / "repo" / "run-one"
            run_dir.mkdir(parents=True)
            MM.safe_write_json(
                run_dir / "metadata.json",
                {"run_id": "run-one", "workflow_id": "wf-one"},
            )
            original_read_json = MM.read_json
            with (
                mock.patch.object(MM, "RUNS_DIR", runs),
                mock.patch.object(
                    MM, "read_json", wraps=original_read_json
                ) as read_json,
                MM.workflow_query_cache(),
            ):
                self.assertEqual(MM.all_run_metadata(), MM.all_run_metadata())
            metadata_reads = [
                call
                for call in read_json.call_args_list
                if call.args and Path(call.args[0]).name == "metadata.json"
            ]
            self.assertEqual(len(metadata_reads), 1)

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
        self.assertEqual(estimate["minimum_viable_budget_usd"], 0.6)
        self.assertEqual(
            estimate["successful_cost_distribution_usd"]["count"], 5
        )
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
            legacy_dir = root / "repo" / "legacy"
            stale_dir = root / "repo" / "stale"
            pending_dir.mkdir(parents=True)
            final_dir.mkdir()
            legacy_dir.mkdir()
            stale_dir.mkdir()
            documents = {
                "wf-20260806T000000Z-pending": {},
                "wf-20260806T000001Z-final": {},
                "wf-20260806T000003Z-legacy": {"status": "completed"},
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
            legacy_metadata = {
                "run_id": "run-legacy",
                "workflow_id": "wf-20260806T000003Z-legacy",
                "status": "completed",
                "created_at": MM.utc_now(),
            }
            MM.safe_write_json(
                final_dir / "final.json",
                {
                    "schema_version": 8,
                    "status": "PASS_CLEAN",
                    "codex_verdict": "PASS_CLEAN",
                    "triage_status": "PASS_CLEAN",
                    "triage_sha256s": {"run-final": "a" * 64},
                },
            )
            MM.safe_write_json(
                legacy_dir / "final.json", {"schema_version": 7, "status": "PASS_CLEAN"}
            )
            records = [
                (pending_dir, pending_metadata),
                (final_dir, final_metadata),
                (legacy_dir, legacy_metadata),
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
        self.assertEqual(
            states["wf-20260806T000003Z-legacy"], "legacy_untrusted_final"
        )
        self.assertEqual(states["wf-20200101T000000Z-stale"], "stale_incomplete")
        self.assertEqual(states["wf-20260806T000002Z-old"], "superseded")
        self.assertEqual(report["unclassified_run_records"], 1)
        self.assertFalse(report["mutated"])
        compact = MM.render_workflow_audit_compact(report)
        self.assertIn("Workflow audit: 5 workflows; stale threshold=7d", compact)
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
        self.assertIn(
            "wf-20260806T000003Z-legacy: legacy_untrusted_final; "
            "start a fresh structured review; legacy finals cannot gate",
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
        self.assertEqual(parsed["observations"][0]["kind"], "observation")
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
        self.assertEqual(triage["observations"][0]["decision"], "pending")

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
        metadata = {
            "workflow_id": "wf-done",
            "run_id": "run-done",
            "repository": {"id": "repo-one"},
            "status": "completed",
            "round": 2,
            "phase": "confirmation",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflows = root / "workflows"
            run_dir = root / "finalized-review"
            run_dir.mkdir()
            workflow_document = {
                "workflow_id": "wf-done",
                "supersedes": [],
                "policy": MM.workflow_policy(),
            }
            final_document = {
                "schema_version": 8,
                "status": "PASS_CLEAN",
                "source_fingerprint": "x",
                "codex_verdict": "PASS_CLEAN",
                "triage_status": "PASS_CLEAN",
                "triage_sha256s": {"run-done": "a" * 64},
            }
            MM.safe_write_json(
                workflows / "wf-done.json",
                workflow_document,
            )
            MM.safe_write_json(run_dir / "final.json", final_document)
            MM.safe_write_json(
                run_dir / "triage.json", {"findings": [], "test_gaps": []}
            )
            with (
                mock.patch.object(MM, "WORKFLOWS_DIR", workflows),
                mock.patch.object(MM, "CONFIG_PATH", root / "missing-config.json"),
                mock.patch.object(MM, "workflow_runs", return_value=[(run_dir, metadata)]),
                mock.patch.object(
                    MM, "workflow_lineage_runs", return_value=[(run_dir, metadata)]
                ),
                mock.patch.object(
                    MM, "workflow_lineage_ids", return_value=["wf-done"]
                ),
                mock.patch.object(MM, "latest_workflow_runs", return_value=[(run_dir, metadata)]),
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
                with mock.patch.object(MM, "final_triage_is_fresh", return_value=True):
                    status, ready = MM.workflow_status("wf-done")
            self.assertTrue(ready)
            self.assertEqual(status["state"], "ready_to_finalize")
            self.assertFalse(status["repositories"][0]["accepts_reviews"])
            self.assertTrue(status["repositories"][0]["final_contract_trusted"])
            self.assertEqual(artifact_bytes.call_count, 1)

    def test_codex_block_overrides_clean_reviewer_triage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            MM.safe_write_json(
                run_dir / "metadata.json",
                {
                    "run_id": "run-codex-block",
                    "status": "completed",
                    "phase": "confirmation",
                    "source_fingerprint": "same",
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
                    "--codex-verdict",
                    "BLOCK",
                    "--codex-review",
                    "Codex reproduced a high-severity defect.",
                ]
            )
            with (
                mock.patch.object(MM, "resolve_run_dir", return_value=run_dir),
                mock.patch.object(
                    MM,
                    "freshness_status",
                    return_value={"fresh": True, "mode": "working-tree"},
                ),
            ):
                result = MM.finalize_command(args)
            final = MM.read_json(run_dir / "final.json")
        self.assertEqual(result, 3)
        self.assertEqual(final["triage_status"], "PASS_CLEAN")
        self.assertEqual(final["codex_verdict"], "BLOCK")
        self.assertEqual(final["status"], "BLOCK")
        self.assertEqual(final["convergence"], "failed")

    def test_same_run_title_collision_preserves_triage_block(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            MM.safe_write_json(
                run_dir / "metadata.json",
                {
                    "run_id": "run-title-collision",
                    "status": "completed",
                    "phase": "confirmation",
                    "source_fingerprint": "same",
                    "risks": [],
                },
            )
            MM.safe_write_json(
                run_dir / "triage.json",
                {
                    "findings": [
                        {
                            "id": "claude-001",
                            "kind": "finding",
                            "title": "Missing auth check",
                            "severity": "high",
                            "decision": "accepted",
                        },
                        {
                            "id": "claude-002",
                            "kind": "finding",
                            "title": "missing auth-check!",
                            "severity": "low",
                            "decision": "rejected",
                        },
                    ],
                    "test_gaps": [],
                },
            )
            args = MM.build_parser().parse_args(
                [
                    "finalize",
                    "--run",
                    str(run_dir),
                    "--codex-verdict",
                    "PASS_CLEAN",
                    "--codex-review",
                    "Codex found no additional issue.",
                ]
            )
            with (
                mock.patch.object(MM, "resolve_run_dir", return_value=run_dir),
                mock.patch.object(
                    MM,
                    "freshness_status",
                    return_value={"fresh": True, "mode": "working-tree"},
                ),
            ):
                result = MM.finalize_command(args)
            final = MM.read_json(run_dir / "final.json")
        self.assertEqual(result, 3)
        self.assertEqual(final["triage_status"], "BLOCK")
        self.assertEqual(final["codex_verdict"], "PASS_CLEAN")
        self.assertEqual(final["status"], "BLOCK")
        self.assertEqual(
            final["remaining_finding_ids"],
            ["run-title-collision:claude-001"],
        )

    def test_finalize_deduplicates_symlink_equivalent_run_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "physical-run"
            alias_dir = root / "run-alias"
            run_dir.mkdir()
            alias_dir.symlink_to(run_dir, target_is_directory=True)
            metadata = {
                "run_id": "run-symlink",
                "workflow_id": "wf-symlink",
                "repository": {"id": "repo-symlink", "root": "/repo"},
                "status": "completed",
                "phase": "confirmation",
                "round": 2,
                "source_fingerprint": "same",
                "risks": [],
            }
            MM.safe_write_json(run_dir / "metadata.json", metadata)
            MM.safe_write_json(
                run_dir / "triage.json",
                {"findings": [], "test_gaps": []},
            )
            args = MM.build_parser().parse_args(
                [
                    "finalize",
                    "--run",
                    str(alias_dir),
                    "--codex-verdict",
                    "PASS_CLEAN",
                    "--codex-review",
                    "No defect found.",
                ]
            )
            with (
                mock.patch.object(
                    MM, "resolve_run_dir", return_value=alias_dir.resolve()
                ),
                mock.patch.object(
                    MM,
                    "freshness_status",
                    return_value={"fresh": True, "mode": "working-tree"},
                ),
                mock.patch.object(
                    MM, "workflow_requires_confirmation", return_value=True
                ),
                mock.patch.object(
                    MM, "workflow_ancestry_ids", return_value=["wf-symlink"]
                ),
                mock.patch.object(
                    MM,
                    "workflow_runs",
                    return_value=[(alias_dir, metadata), (run_dir, metadata)],
                ),
            ):
                result = MM.finalize_command(args)
            final = MM.read_json(run_dir / "final.json")
        self.assertEqual(result, 0)
        self.assertEqual(final["status"], "PASS_CLEAN")
        self.assertEqual(list(final["triage_sha256s"]), ["run-symlink"])

    def test_final_gate_retains_deferred_items_from_prior_workflow_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repair_dir = root / "repair"
            confirmation_dir = root / "confirmation"
            repair_dir.mkdir()
            confirmation_dir.mkdir()
            repository = {"id": "repo-one", "root": "/repo"}
            repair_metadata = {
                "run_id": "run-repair",
                "workflow_id": "wf-history",
                "repository": repository,
                "status": "completed",
                "phase": "repair",
                "round": 1,
            }
            confirmation_metadata = {
                "run_id": "run-confirmation",
                "workflow_id": "wf-history",
                "repository": repository,
                "status": "completed",
                "phase": "confirmation",
                "round": 2,
                "source_fingerprint": "same",
                "risks": [],
            }
            MM.safe_write_json(repair_dir / "metadata.json", repair_metadata)
            MM.safe_write_json(
                repair_dir / "triage.json",
                {
                    "findings": [
                        {
                            "id": "claude-001",
                            "kind": "finding",
                            "title": "Deferred audit issue",
                            "severity": "low",
                            "decision": "deferred",
                        }
                    ],
                    "test_gaps": [
                        {
                            "id": "claude-test-001",
                            "kind": "test_gap",
                            "title": "Deferred integration coverage",
                            "severity": "medium",
                            "decision": "deferred",
                        }
                    ],
                },
            )
            MM.safe_write_json(
                confirmation_dir / "metadata.json", confirmation_metadata
            )
            MM.safe_write_json(
                confirmation_dir / "triage.json",
                {"findings": [], "test_gaps": []},
            )
            args = MM.build_parser().parse_args(
                [
                    "finalize",
                    "--run",
                    str(confirmation_dir),
                    "--codex-verdict",
                    "PASS_CLEAN",
                    "--codex-review",
                    "No additional defect found in confirmation.",
                ]
            )
            workflow_runs = [
                (repair_dir, repair_metadata),
                (confirmation_dir, confirmation_metadata),
            ]
            with (
                mock.patch.object(
                    MM, "resolve_run_dir", return_value=confirmation_dir
                ),
                mock.patch.object(
                    MM,
                    "freshness_status",
                    return_value={"fresh": True, "mode": "working-tree"},
                ),
                mock.patch.object(
                    MM, "workflow_requires_confirmation", return_value=True
                ),
                mock.patch.object(
                    MM, "workflow_runs", return_value=workflow_runs
                ),
            ):
                result = MM.finalize_command(args)
                final = MM.read_json(confirmation_dir / "final.json")
                triage_fresh_before = MM.final_triage_is_fresh(
                    confirmation_dir, confirmation_metadata, final
                )
                repair_triage = MM.read_json(repair_dir / "triage.json")
                repair_triage["findings"][0]["decision"] = "rejected"
                MM.safe_write_json(repair_dir / "triage.json", repair_triage)
                triage_fresh_after = MM.final_triage_is_fresh(
                    confirmation_dir, confirmation_metadata, final
                )
        self.assertEqual(result, 0)
        self.assertTrue(triage_fresh_before)
        self.assertFalse(triage_fresh_after)
        self.assertEqual(final["triage_status"], "PASS_WITH_FINDINGS")
        self.assertEqual(final["codex_verdict"], "PASS_CLEAN")
        self.assertEqual(final["status"], "PASS_WITH_FINDINGS")
        self.assertEqual(
            final["remaining_finding_ids"], ["run-repair:claude-001"]
        )
        self.assertEqual(
            final["remaining_test_gap_ids"],
            ["run-repair:claude-test-001"],
        )
        self.assertEqual(final["remaining_findings"][0]["round"], 1)
        self.assertEqual(final["remaining_test_gaps"][0]["severity"], "medium")
        self.assertEqual(
            set(final["triage_sha256s"]), {"run-repair", "run-confirmation"}
        )

    def test_legacy_final_contract_is_untrusted(self) -> None:
        trusted, issues = MM.final_contract_trust(
            {
                "schema_version": 7,
                "status": "PASS_CLEAN",
                "codex_review": "Codex says this should block.",
                "triage_sha256": "a" * 64,
            }
        )
        self.assertFalse(trusted)
        self.assertIn("schema_version must be 8 or newer", issues)
        self.assertIn("codex_verdict is missing or invalid", issues)
        self.assertIn("triage_status is missing or invalid", issues)
        self.assertIn("triage_sha256s is missing or invalid", issues)

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
                    "--codex-verdict",
                    "PASS_CLEAN",
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

    def test_structured_observations_are_explicit_and_require_acknowledgment(
        self,
    ) -> None:
        report = MM.render_structured_review(
            {
                "verdict": "PASS_CLEAN",
                "findings": [],
                "test_gaps": [],
                "observations": [
                    {
                        "actionable": False,
                        "severity": "low",
                        "title": "Deterministic fixture behavior",
                        "location": "tests/test_fixture.py:10",
                        "evidence": "The fake clock is isolated to this test.",
                        "why_non_actionable": "Production state is unreachable.",
                    }
                ],
                "coverage": {
                    "complete": True,
                    "unreviewed_changed_paths": [],
                    "limitations": [],
                },
                "notes": [],
            }
        )
        parsed = MM.parse_review_report("claude", report)
        observation = parsed["observations"][0]
        self.assertEqual(observation["id"], "claude-observation-001")
        triage = {
            "findings": [],
            "test_gaps": [],
            "observations": [{**observation, "decision": "pending"}],
        }
        self.assertEqual(
            MM.pending_triage_ids(triage), ["claude-observation-001"]
        )
        with self.assertRaisesRegex(MM.ReviewError, "invalid for observation"):
            MM.apply_triage_decision(
                triage,
                identifier="claude-observation-001",
                decision="rejected",
                evidence="Incorrect disposition.",
                action=None,
                verification=None,
            )
        MM.apply_triage_decision(
            triage,
            identifier="claude-observation-001",
            decision="acknowledged",
            evidence="Codex verified the stated non-production boundary.",
            action=None,
            verification=None,
        )
        self.assertEqual(MM.pending_triage_ids(triage), [])

    def test_provider_usage_does_not_claim_disabled_or_unprobed_readiness(
        self,
    ) -> None:
        config = json.loads(json.dumps(MM.DEFAULT_CONFIG))
        config["claude"]["enabled"] = True
        config["antigravity"]["enabled"] = False
        with (
            mock.patch.object(MM, "load_config", return_value=config),
            mock.patch.object(
                MM, "workflow_provider_attempts", return_value={name: 0 for name in MM.PROVIDERS}
            ),
            mock.patch.object(
                MM,
                "workflow_usage_policy",
                return_value={"mode": "provider_allowance", "max_attempts_per_provider": 6},
            ),
            mock.patch.object(MM, "active_provider_cooldown", return_value=None),
            mock.patch.object(
                MM,
                "latest_provider_resource_metadata",
                return_value=("subscription", "included_plan_allowance"),
            ),
        ):
            usage = MM.provider_usage_snapshot("wf-provider")
        self.assertIsNone(usage["providers"]["claude"]["ready"])
        self.assertEqual(
            usage["providers"]["claude"]["readiness_status"], "not_probed"
        )
        self.assertIsNone(usage["providers"]["antigravity"]["ready"])
        self.assertEqual(
            usage["providers"]["antigravity"]["readiness_status"], "disabled"
        )
        self.assertEqual(
            usage["providers"]["claude"]["authentication_mode"], "subscription"
        )

    def test_claude_resource_metadata_records_subscription_allowance(self) -> None:
        reviewer = MM.Reviewer("claude", ("claude",), {}, "sonnet", "test")
        with mock.patch.object(
            MM,
            "claude_authentication_mode",
            return_value=("subscription", "Claude subscription authentication"),
        ):
            resource = MM.reviewer_resource_metadata(reviewer)
        self.assertEqual(resource["authentication_mode"], "subscription")
        self.assertEqual(resource["usage_resource"], "included_plan_allowance")

    def test_snapshot_exclusion_is_exact_unchanged_sensitive_file_with_provenance(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            repo.mkdir()
            initialize_repo(repo)
            npmrc = repo / ".npmrc"
            npmrc.write_text("registry=https://registry.npmjs.org/\n", encoding="utf-8")
            run(["git", "add", ".npmrc"], cwd=repo)
            run(["git", "commit", "-qm", "add npm config"], cwd=repo)
            (repo / "src" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
            paths = MM.changed_paths(
                repo, MM.Scope("uncommitted", None, "changes"), ("src",)
            )
            exclusions = MM.resolve_snapshot_exclusions(
                repo,
                [".npmrc"],
                task_paths=paths,
                path_filters=("src",),
            )
            self.assertEqual(exclusions[0]["path"], ".npmrc")
            self.assertEqual(len(exclusions[0]["sha256"]), 64)
            run_dir = root / "run"
            run_dir.mkdir()
            snapshot = MM.create_snapshot(
                repo,
                MM.Scope("uncommitted", None, "changes"),
                paths,
                run_dir,
            )
            MM.apply_snapshot_exclusions(snapshot, exclusions)
            self.assertFalse((snapshot / ".npmrc").exists())
            self.assertTrue((snapshot / "src" / "feature.py").exists())
            npmrc.write_text("registry=https://example.invalid/\n", encoding="utf-8")
            with self.assertRaisesRegex(MM.ReviewError, "unchanged from HEAD"):
                MM.validate_snapshot_exclusion_provenance(
                    repo,
                    exclusions,
                    task_paths=paths,
                    path_filters=("src",),
                )

    def test_snapshot_exclusion_rejects_non_sensitive_or_task_scoped_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            initialize_repo(repo)
            with self.assertRaisesRegex(MM.ReviewError, "recognized sensitive"):
                MM.resolve_snapshot_exclusions(
                    repo,
                    ["unrelated.txt"],
                    task_paths=["src/feature.py"],
                    path_filters=("src",),
                )
            npmrc = repo / ".npmrc"
            npmrc.write_text("registry=https://registry.npmjs.org/\n", encoding="utf-8")
            run(["git", "add", ".npmrc"], cwd=repo)
            run(["git", "commit", "-qm", "add npm config"], cwd=repo)
            with self.assertRaisesRegex(MM.ReviewError, "task-scoped"):
                MM.resolve_snapshot_exclusions(
                    repo,
                    [".npmrc"],
                    task_paths=[".npmrc"],
                    path_filters=(),
                )

    def test_snapshot_exclusion_fails_closed_for_historical_commit_mismatch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            repo.mkdir()
            initialize_repo(repo)
            npmrc = repo / ".npmrc"
            npmrc.write_text(
                "registry=https://old.example.invalid/\n", encoding="utf-8"
            )
            run(["git", "add", ".npmrc"], cwd=repo)
            run(["git", "commit", "-qm", "add old npm config"], cwd=repo)
            (repo / "src" / "feature.py").write_text(
                "VALUE = 2\n", encoding="utf-8"
            )
            run(["git", "add", "src/feature.py"], cwd=repo)
            run(["git", "commit", "-qm", "add historical feature"], cwd=repo)
            historical_commit = run(
                ["git", "rev-parse", "HEAD"], cwd=repo
            ).stdout.strip()
            npmrc.write_text(
                "registry=https://current.example.invalid/\n", encoding="utf-8"
            )
            run(["git", "add", ".npmrc"], cwd=repo)
            run(["git", "commit", "-qm", "update npm config"], cwd=repo)

            scope = MM.Scope("commit", historical_commit, "historical commit")
            paths = MM.changed_paths(repo, scope)
            self.assertEqual(paths, ["src/feature.py"])
            exclusions = MM.resolve_snapshot_exclusions(
                repo,
                [".npmrc"],
                task_paths=paths,
                path_filters=(),
            )
            run_dir = root / "run"
            run_dir.mkdir()
            snapshot = MM.create_snapshot(
                repo,
                scope,
                MM.snapshot_overlay_paths(repo, scope, paths, ()),
                run_dir,
            )

            with self.assertRaisesRegex(
                MM.ReviewError,
                "does not match its pinned provenance.*historical --commit",
            ):
                MM.apply_snapshot_exclusions(snapshot, exclusions)

    def test_lineage_sensitive_approval_reuse_requires_every_identity_field(
        self,
    ) -> None:
        finding = MM.SensitiveFinding(
            identifier="abc123def456",
            path="tests/fixture.txt",
            line=2,
            rule="literal secret-like assignment",
            key="password",
            content_sha256="f" * 64,
        )
        metadata = {
            "schema_version": 11,
            "run_id": "run-approved",
            "repository": {"id": "repo-one"},
            "allowed_sensitive_findings": [finding.identifier],
            "sensitive_findings": [MM.dataclasses.asdict(finding)],
        }
        with mock.patch.object(
            MM, "workflow_lineage_runs", return_value=[(Path("/run"), metadata)]
        ):
            approved, sources = MM.reusable_lineage_sensitive_approvals(
                "wf-one", "repo-one", [finding]
            )
            mismatches = [
                MM.dataclasses.replace(finding, path="tests/other.txt"),
                MM.dataclasses.replace(finding, line=3),
                MM.dataclasses.replace(finding, rule="OpenAI-style secret key"),
                MM.dataclasses.replace(finding, key="api_key"),
                MM.dataclasses.replace(finding, content_sha256="e" * 64),
            ]
            changed_approvals = [
                MM.reusable_lineage_sensitive_approvals(
                    "wf-one", "repo-one", [changed]
                )[0]
                for changed in mismatches
            ]
        self.assertEqual(approved, {finding.identifier})
        self.assertEqual(sources, ["run-approved"])
        self.assertEqual(changed_approvals, [set()] * len(mismatches))

    def test_provider_attempt_limit_raise_is_increase_only_and_audited(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workflows = Path(temporary)
            with mock.patch.object(MM, "WORKFLOWS_DIR", workflows):
                MM.create_workflow(
                    "wf-raise", usage_based=True, max_provider_attempts=2
                )
                args = MM.build_parser().parse_args(
                    [
                        "workflow",
                        "raise-provider-attempt-limit",
                        "wf-raise",
                        "--to",
                        "5",
                        "--reason",
                        "reserve confirmation recovery headroom",
                    ]
                )
                MM.workflow_raise_provider_attempt_limit_command(args)
                document = MM.read_json(workflows / "wf-raise.json")
                self.assertEqual(
                    document["policy"]["usage_policy"]["max_attempts_per_provider"],
                    5,
                )
                self.assertEqual(
                    document["provider_attempt_limit_history"][0]["previous"], 2
                )
                lower = MM.build_parser().parse_args(
                    [
                        "workflow",
                        "raise-provider-attempt-limit",
                        "wf-raise",
                        "--to",
                        "4",
                        "--reason",
                        "would weaken audit semantics",
                    ]
                )
                with self.assertRaisesRegex(MM.ReviewError, "must increase"):
                    MM.workflow_raise_provider_attempt_limit_command(lower)

    def test_confirmation_plan_warns_when_recovery_headroom_is_low(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            MM.safe_write_json(
                run_dir / "triage.json", {"findings": [], "test_gaps": []}
            )
            metadata = {
                "status": "completed",
                "phase": "repair",
                "round": 1,
                "repository": {"id": "repo-one", "root": "/repo"},
            }
            usage = {
                "providers": {
                    "claude": {
                        "enabled": True,
                        "ready": True,
                        "attempts": 5,
                        "attempts_remaining": 1,
                    }
                }
            }
            with (
                mock.patch.object(
                    MM,
                    "workflow_status",
                    return_value=({"state": "active", "active_runs": []}, False),
                ),
                mock.patch.object(MM, "provider_usage_snapshot", return_value=usage),
                mock.patch.object(
                    MM, "latest_workflow_attempts", return_value=[(run_dir, metadata)]
                ),
                mock.patch.object(MM, "workflow_runs", return_value=[(run_dir, metadata)]),
                mock.patch.object(MM, "workflow_max_repair_rounds", return_value=2),
                mock.patch.object(MM, "_current_run_source_changed", return_value=False),
            ):
                plan = MM.workflow_continue_plan("wf-headroom", probe_usage=True)
        warning = plan["actions"][0]["attempt_headroom_warning"]
        self.assertEqual(warning["providers"], ["claude"])
        self.assertIn("raise-provider-attempt-limit", warning["raise_command"])

    def test_review_binding_distinguishes_working_tree_commit_and_attestation(
        self,
    ) -> None:
        commit = "a" * 40
        working_metadata = {
            "scope": {"kind": "uncommitted", "value": None, "label": "changes"}
        }
        self.assertEqual(
            MM.review_binding(working_metadata, {}, commit),
            ("working_tree_only", False),
        )
        self.assertEqual(
            MM.review_binding(
                working_metadata,
                {"commit_attestations": [{"commit": commit}]},
                commit,
            ),
            ("attested_commit", True),
        )
        commit_metadata = {
            "scope": {"kind": "commit", "value": commit, "label": "commit"}
        }
        self.assertEqual(
            MM.review_binding(commit_metadata, {}, commit),
            ("immutable_commit", True),
        )

    def test_fixed_lineage_requires_local_verification_before_another_provider(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "fixed"
            verified_run_dir = root / "verified"
            run_dir.mkdir()
            verified_run_dir.mkdir()
            MM.safe_write_json(
                run_dir / "triage.json",
                {
                    "findings": [
                        {"id": "claude-001", "kind": "finding", "decision": "fixed"}
                    ],
                    "test_gaps": [],
                },
            )
            metadata = {
                "status": "completed",
                "completed_at": "2026-08-11T10:00:00+00:00",
                "run_id": "run-fixed",
                "source_fingerprint": "before",
                "repository": {"id": "repo-one"},
            }
            MM.safe_write_json(
                verified_run_dir / "triage.json",
                {"findings": [], "test_gaps": [], "observations": []},
            )
            verified_metadata = {
                "status": "completed",
                "completed_at": "2026-08-11T10:01:00+00:00",
                "run_id": "run-verified",
                "source_fingerprint": "after",
                "local_verification_before_provider": ["full suite passed"],
                "repository": {"id": "repo-one"},
            }
            with mock.patch.object(
                MM, "workflow_lineage_runs", return_value=[(run_dir, metadata)]
            ):
                self.assertTrue(
                    MM.local_verification_required("wf-one", "repo-one", "after")
                )
                self.assertFalse(
                    MM.local_verification_required("wf-one", "repo-one", "before")
                )
            with mock.patch.object(
                MM,
                "workflow_lineage_runs",
                return_value=[
                    (verified_run_dir, verified_metadata),
                    (run_dir, metadata),
                ],
            ):
                self.assertFalse(
                    MM.local_verification_required("wf-one", "repo-one", "after")
                )
                self.assertTrue(
                    MM.local_verification_required("wf-one", "repo-one", "later")
                )

    def test_snapshot_exclusion_requires_codex_coverage_compensation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            MM.safe_write_json(
                run_dir / "metadata.json",
                {
                    "run_id": "run-excluded",
                    "status": "completed",
                    "phase": "confirmation",
                    "source_fingerprint": "same",
                    "risks": [],
                    "snapshot_exclusions": [{"path": ".npmrc", "sha256": "a" * 64}],
                },
            )
            MM.safe_write_json(
                run_dir / "triage.json",
                {"findings": [], "test_gaps": [], "observations": []},
            )
            MM.safe_write_json(
                run_dir / "review-summary.json",
                {
                    "reviews": {
                        "claude": {
                            "coverage": {
                                "complete": True,
                                "unreviewed_changed_paths": [],
                                "limitations": [],
                            }
                        }
                    }
                },
            )
            args = MM.build_parser().parse_args(
                [
                    "finalize",
                    "--run",
                    str(run_dir),
                    "--codex-verdict",
                    "PASS_CLEAN",
                    "--codex-review",
                    "Provider-reviewed source is clean.",
                ]
            )
            with (
                mock.patch.object(MM, "resolve_run_dir", return_value=run_dir),
                mock.patch.object(
                    MM,
                    "freshness_status",
                    return_value={"fresh": True, "mode": "working-tree"},
                ),
            ):
                with self.assertRaisesRegex(MM.ReviewError, "snapshot exclusions"):
                    MM.finalize_command(args)


class RunnerEndToEndTests(unittest.TestCase):
    def test_last_chance_budget_guard_blocks_without_consuming_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "admission-repo"
            repo.mkdir()
            initialize_repo(repo)
            (repo / "src" / "feature.py").write_text(
                "VALUE = 2\n", encoding="utf-8"
            )
            harness = FakeProviderHarness(root)
            history_root = (
                harness.home / ".codex" / "review-runs" / "historical-repo"
            )
            for index in range(5):
                run_dir = history_root / f"history-{index}"
                run_dir.mkdir(parents=True)
                (run_dir / "change.patch").write_text(
                    "x" * 100, encoding="utf-8"
                )
                MM.safe_write_json(
                    run_dir / "metadata.json",
                    {
                        "created_at": MM.utc_now(),
                        "review_mode": "deep",
                        "review_policy": {"claude_effort": "medium"},
                        "reviewers": {
                            "claude": {
                                "model": "sonnet",
                                "exit_code": 0,
                                "verdict": "PASS_CLEAN",
                                "report_contract_valid": True,
                                "failure_category": None,
                                "usage": {"total_cost_usd": 2.0},
                            }
                        },
                    },
                )

            workflow = harness.cli(
                repo,
                "workflow",
                "start",
                "--review-mode",
                "deep",
                "--max-provider-attempts",
                "2",
            ).stdout.strip()
            blocked = harness.cli(
                repo,
                "run",
                "--repo",
                str(repo),
                "--uncommitted",
                "--workflow-id",
                workflow,
                "--phase",
                "repair",
                "--path",
                "src/feature.py",
                "--task",
                "Exercise the final-attempt admission guard.",
                "--without-antigravity",
                "--without-kimi",
                check=False,
                provider_backed=True,
            )

            self.assertEqual(blocked.returncode, 2)
            self.assertIn("review admission blocked", blocked.stderr)
            self.assertIn("No provider was started", blocked.stderr)
            self.assertIn("--claude-max-budget-usd 2.50", blocked.stderr)
            self.assertEqual(harness.invocations(), [])
            blocked_runs = [
                path
                for path in harness.run_directories()
                if MM.read_json(path / "metadata.json").get("status")
                == "preflight_blocked"
            ]
            self.assertEqual(len(blocked_runs), 1)
            blocked_metadata = MM.read_json(blocked_runs[0] / "metadata.json")
            self.assertTrue(blocked_metadata["review_admission"]["blocked"])
            self.assertEqual(
                blocked_metadata["review_admission"]["providers"][0][
                    "block_reason"
                ],
                "underfunded_without_recovery_headroom",
            )
            self.assertIn("runner_sha256", blocked_metadata["runtime_identity"])

            harness.queue("claude", {"kind": "report", "report": structured_report()})
            completed = harness.cli(
                repo,
                "run",
                "--repo",
                str(repo),
                "--uncommitted",
                "--workflow-id",
                workflow,
                "--phase",
                "repair",
                "--path",
                "src/feature.py",
                "--task",
                "Exercise the final-attempt admission guard.",
                "--claude-max-budget-usd",
                "2.50",
                "--without-antigravity",
                "--without-kimi",
                provider_backed=True,
            )

            completed_dir = next(
                path
                for path in harness.run_directories()
                if MM.read_json(path / "metadata.json").get("workflow_id")
                == workflow
                and MM.read_json(path / "metadata.json").get("status")
                == "completed"
            )
            completed_metadata = MM.read_json(completed_dir / "metadata.json")
            self.assertEqual(completed_metadata["status"], "completed")
            self.assertFalse(completed_metadata["review_admission"]["blocked"])
            self.assertEqual(
                [item["provider"] for item in harness.invocations()], ["claude"]
            )

    def test_failure_resume_and_attempt_accounting_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "failure-repo"
            repo.mkdir()
            initialize_repo(repo)
            (repo / "src" / "feature.py").write_text(
                "VALUE = 2\n", encoding="utf-8"
            )
            harness = FakeProviderHarness(root)
            contradictory = structured_report(
                verdict="PASS_CLEAN",
                findings=textwrap.dedent(
                    """\
                    ## [low] Contradictory declared verdict is normalized
                    - Location: src/feature.py:1
                    - Trigger: A finding accompanies PASS_CLEAN
                    - Evidence: The structured finding is present
                    - Impact: The report must not be called clean
                    - Smallest fix: Preserve the finding in triage
                    - Confidence: high
                    """
                ).strip(),
            )
            clean = structured_report()
            harness.queue(
                "claude", {"kind": "report", "report": contradictory}
            )
            harness.queue(
                "kimi",
                {
                    "kind": "failure",
                    "stderr": "synthetic provider process failed",
                    "exit_code": 7,
                },
                {"kind": "report", "report": clean},
            )
            partial_workflow = harness.cli(
                repo,
                "workflow",
                "start",
                "--review-mode",
                "balanced",
                "--max-provider-attempts",
                "4",
            ).stdout.strip()
            partial = harness.cli(
                repo,
                "run",
                "--repo",
                str(repo),
                "--uncommitted",
                "--workflow-id",
                partial_workflow,
                "--path",
                "src/feature.py",
                "--task",
                "Exercise partial provider recovery.",
                "--with-kimi",
                "--without-antigravity",
                check=False,
                provider_backed=True,
            )
            self.assertEqual(partial.returncode, 2)
            partial_dir = next(
                path
                for path in harness.run_directories()
                if MM.read_json(path / "metadata.json").get("workflow_id")
                == partial_workflow
            )
            partial_metadata = MM.read_json(partial_dir / "metadata.json")
            self.assertEqual(partial_metadata["status"], "partial")
            self.assertEqual(
                partial_metadata["failure"]["successful_reviewers"], ["claude"]
            )
            self.assertEqual(
                partial_metadata["failure"]["categories"]["kimi"],
                "provider_error",
            )
            harness.cli(
                repo,
                "resume",
                "--run",
                str(partial_dir),
                provider_backed=True,
            )
            resumed = MM.read_json(partial_dir / "metadata.json")
            self.assertEqual(resumed["status"], "completed")
            self.assertEqual(resumed["resumed_reviewers"], ["kimi"])
            self.assertEqual(len(MM.reviewer_attempts(resumed["reviewers"]["claude"])), 1)
            self.assertEqual(len(MM.reviewer_attempts(resumed["reviewers"]["kimi"])), 2)
            self.assertTrue((partial_dir / "kimi.attempt-1.md").exists())
            self.assertTrue((partial_dir / "kimi.attempt-1.stderr.log").exists())
            summary = MM.read_json(partial_dir / "review-summary.json")
            self.assertEqual(
                summary["reviews"]["claude"]["declared_verdict"], "PASS_CLEAN"
            )
            self.assertEqual(
                summary["reviews"]["claude"]["verdict"], "PASS_WITH_FINDINGS"
            )
            provider_calls = harness.invocations()
            self.assertEqual(
                [item["provider"] for item in provider_calls].count("claude"), 1
            )
            self.assertEqual(
                [item["provider"] for item in provider_calls].count("kimi"), 2
            )
            resumed_kimi_call = [
                item for item in provider_calls if item["provider"] == "kimi"
            ][-1]
            initial_claude_call = next(
                item for item in provider_calls if item["provider"] == "claude"
            )
            initial_kimi_call = [
                item for item in provider_calls if item["provider"] == "kimi"
            ][0]
            self.assertEqual(
                resumed["reviewers"]["claude"]["prompt_sha256"],
                initial_claude_call["staged_prompt_sha256"],
            )
            self.assertEqual(
                resumed["reviewers"]["kimi"]["prompt_sha256"],
                resumed_kimi_call["staged_prompt_sha256"],
            )
            self.assertEqual(
                resumed["reviewers"]["kimi"]["attempts"][0]["prompt_sha256"],
                initial_kimi_call["staged_prompt_sha256"],
            )
            self.assertEqual(
                resumed["prompt_template_sha256"], resumed["prompt_sha256"]
            )
            self.assertEqual(
                resumed_kimi_call["granted_files"],
                ["change.patch", "manifest.md", "prompt.md"],
            )
            self.assertFalse(
                {
                    "claude.md",
                    "review-summary.json",
                    "triage.json",
                    "metadata.json",
                }
                & {Path(path).name for path in resumed_kimi_call["workspace_files"]}
            )
            self.assertNotEqual(
                Path(str(resumed_kimi_call["granted_dir"])).parent,
                partial_dir,
            )
            partial_status = json.loads(
                harness.cli(
                    repo, "workflow", "status", partial_workflow, check=False
                ).stdout
            )
            self.assertEqual(
                partial_status["external_review_coverage"]["headline"],
                "claude + kimi",
            )
            partial_continue = json.loads(
                harness.cli(
                    repo, "continue", partial_workflow, check=False
                ).stdout
            )
            self.assertEqual(partial_continue["next"], "NEEDS_TRIAGE")

            malformed_workflow = harness.cli(
                repo,
                "workflow",
                "start",
                "--review-mode",
                "balanced",
                "--max-provider-attempts",
                "2",
            ).stdout.strip()
            harness.queue("claude", {"kind": "malformed_wrapper", "exit_code": 0})
            malformed = harness.cli(
                repo,
                "run",
                "--repo",
                str(repo),
                "--workflow-id",
                malformed_workflow,
                "--path",
                "src/feature.py",
                "--task",
                "Reject malformed provider output.",
                "--without-antigravity",
                "--without-kimi",
                check=False,
                provider_backed=True,
            )
            self.assertEqual(malformed.returncode, 2)
            malformed_dir = next(
                path
                for path in harness.run_directories()
                if MM.read_json(path / "metadata.json").get("workflow_id")
                == malformed_workflow
            )
            malformed_metadata = MM.read_json(malformed_dir / "metadata.json")
            self.assertEqual(
                malformed_metadata["reviewers"]["claude"]["failure_category"],
                "malformed_response",
            )

            invalid_workflow = harness.cli(
                repo,
                "workflow",
                "start",
                "--review-mode",
                "balanced",
                "--max-provider-attempts",
                "2",
            ).stdout.strip()
            harness.queue(
                "claude",
                {
                    "kind": "report",
                    "report": structured_report(verdict="PASS_WITH_FINDINGS"),
                },
            )
            invalid = harness.cli(
                repo,
                "run",
                "--repo",
                str(repo),
                "--workflow-id",
                invalid_workflow,
                "--path",
                "src/feature.py",
                "--task",
                "Reject exit-zero contract-invalid output.",
                "--without-antigravity",
                "--without-kimi",
                check=False,
                provider_backed=True,
            )
            self.assertEqual(invalid.returncode, 2)
            invalid_dir = next(
                path
                for path in harness.run_directories()
                if MM.read_json(path / "metadata.json").get("workflow_id")
                == invalid_workflow
            )
            invalid_metadata = MM.read_json(invalid_dir / "metadata.json")
            self.assertEqual(invalid_metadata["failure"]["type"], "invalid_report")
            self.assertEqual(invalid_metadata["reviewers"]["claude"]["exit_code"], 0)
            self.assertFalse(
                invalid_metadata["reviewers"]["claude"]["report_contract_valid"]
            )
            invalid_status = json.loads(
                harness.cli(
                    repo, "workflow", "status", invalid_workflow, check=False
                ).stdout
            )
            self.assertEqual(
                invalid_status["external_review_coverage"]["headline"],
                "no successful external provider",
            )
            self.assertEqual(
                invalid_status["provider_usage"]["providers"]["claude"][
                    "readiness_status"
                ],
                "not_probed",
            )
            self.assertEqual(
                invalid_status["provider_usage"]["providers"]["kimi"][
                    "readiness_status"
                ],
                "disabled",
            )
            invalid_continue = json.loads(
                harness.cli(
                    repo, "continue", invalid_workflow, check=False
                ).stdout
            )
            self.assertEqual(invalid_continue["next"], "NEEDS_RECOVERY")

            budget_workflow = harness.cli(
                repo,
                "workflow",
                "start",
                "--review-mode",
                "balanced",
                "--max-provider-attempts",
                "2",
            ).stdout.strip()
            harness.queue("claude", {"kind": "budget_exhausted"})
            exhausted = harness.cli(
                repo,
                "run",
                "--repo",
                str(repo),
                "--workflow-id",
                budget_workflow,
                "--path",
                "src/feature.py",
                "--task",
                "Recover explicitly from a synthetic provider stop.",
                "--without-antigravity",
                "--without-kimi",
                check=False,
                provider_backed=True,
            )
            self.assertEqual(exhausted.returncode, 2)
            budget_dir = next(
                path
                for path in harness.run_directories()
                if MM.read_json(path / "metadata.json").get("workflow_id")
                == budget_workflow
            )
            budget_metadata = MM.read_json(budget_dir / "metadata.json")
            self.assertEqual(
                budget_metadata["reviewers"]["claude"]["failure_category"],
                "budget_exhausted",
            )
            calls_before_blind_retry = len(harness.invocations())
            blind = harness.cli(
                repo,
                "resume",
                "--run",
                str(budget_dir),
                check=False,
                provider_backed=True,
            )
            self.assertEqual(blind.returncode, 2)
            self.assertIn("blind resume", blind.stderr)
            self.assertEqual(len(harness.invocations()), calls_before_blind_retry)
            harness.queue("claude", {"kind": "report", "report": clean})
            harness.cli(
                repo,
                "resume",
                "--run",
                str(budget_dir),
                "--claude-max-budget-usd",
                "2",
                provider_backed=True,
            )
            recovered = MM.read_json(budget_dir / "metadata.json")
            self.assertEqual(recovered["status"], "completed")
            self.assertEqual(len(MM.reviewer_attempts(recovered["reviewers"]["claude"])), 2)
            self.assertTrue((budget_dir / "claude.attempt-1.md").exists())
            headroom = json.loads(
                harness.cli(
                    repo, "continue", budget_workflow, check=False
                ).stdout
            )
            self.assertEqual(headroom["next"], "WAIT_FOR_PROVIDER")
            warning = headroom["actions"][0]["attempt_headroom_warning"]
            self.assertEqual(warning["providers"], ["claude"])
            harness.cli(
                repo,
                "workflow",
                "raise-provider-attempt-limit",
                budget_workflow,
                "--to",
                "6",
                "--reason",
                "reserve successor repair and confirmation recovery headroom",
            )
            cannot_lower = harness.cli(
                repo,
                "workflow",
                "raise-provider-attempt-limit",
                budget_workflow,
                "--to",
                "5",
                "--reason",
                "invalid lowering attempt",
                check=False,
            )
            self.assertEqual(cannot_lower.returncode, 2)
            budget_document = MM.read_json(
                harness.home
                / ".codex"
                / "review-runs"
                / "workflows"
                / f"{budget_workflow}.json"
            )
            self.assertEqual(
                budget_document["provider_attempt_limit_history"][0]["new"], 6
            )
            successor = harness.cli(
                repo,
                "workflow",
                "supersede",
                budget_workflow,
                "--reason",
                "Exercise successor usage inheritance.",
            ).stdout.strip()
            successor_before = json.loads(
                harness.cli(
                    repo, "workflow", "status", successor, check=False
                ).stdout
            )
            self.assertEqual(
                successor_before["provider_usage"]["providers"]["claude"][
                    "attempts"
                ],
                2,
            )
            harness.queue(
                "claude",
                {"kind": "report", "report": clean},
                {"kind": "report", "report": clean},
            )
            harness.cli(
                repo,
                "run",
                "--repo",
                str(repo),
                "--workflow-id",
                successor,
                "--phase",
                "repair",
                "--reuse-contract",
                "--without-antigravity",
                "--without-kimi",
                provider_backed=True,
            )
            successor_plan = json.loads(
                harness.cli(repo, "continue", successor, check=False).stdout
            )
            self.assertEqual(successor_plan["actions"][0]["phase"], "confirmation")
            self.assertNotIn(
                "attempt_headroom_warning", successor_plan["actions"][0]
            )
            confirmation = harness.cli(
                repo,
                "run",
                "--repo",
                str(repo),
                "--workflow-id",
                successor,
                "--phase",
                "confirmation",
                "--reuse-contract",
                "--without-antigravity",
                "--without-kimi",
                provider_backed=True,
            )
            confirmation_dir = Path(
                next(
                    line.split(": ", 1)[1]
                    for line in confirmation.stdout.splitlines()
                    if line.startswith("Review artifacts: ")
                )
            )
            harness.cli(
                repo,
                "finalize",
                "--run",
                str(confirmation_dir),
                "--codex-verdict",
                "PASS_CLEAN",
                "--codex-review",
                "Attempt lineage and recovery behavior are consistent.",
            )
            successor_status = json.loads(
                harness.cli(
                    repo, "workflow", "status", successor, check=False
                ).stdout
            )
            self.assertEqual(
                successor_status["provider_usage"]["providers"]["claude"][
                    "attempts"
                ],
                4,
            )
            self.assertEqual(
                successor_status["provider_usage"]["providers"]["claude"][
                    "attempts_remaining"
                ],
                2,
            )
            original_status = json.loads(
                harness.cli(
                    repo, "workflow", "status", budget_workflow, check=False
                ).stdout
            )
            self.assertEqual(original_status["state"], "superseded")

            harness.queue(
                "claude", {"kind": "timeout", "seconds": 10}
            )
            harness.assert_fake_resolution()
            timeout_dir = root / "timeout-run"
            timeout_dir.mkdir()
            timeout_reviewer = MM.Reviewer(
                "claude",
                (str(harness.bin_dir / "claude"),),
                {
                    "MM_FAKE_PROVIDER_STATE": str(harness.state_path),
                    "MM_FAKE_PROVIDER_LOG": str(harness.log_path),
                },
                "sonnet",
                "fake",
            )
            with mock.patch.object(
                MM, "PROVIDER_HEALTH_PATH", root / "provider-health.json"
            ):
                timeout_result = MM.invoke_reviewer(
                    timeout_reviewer,
                    repo=repo,
                    prompt="synthetic timeout",
                    run_dir=timeout_dir,
                    input_dir=timeout_dir,
                    timeout_seconds=1,
                )
            self.assertTrue(timeout_result.timed_out)
            self.assertEqual(timeout_result.returncode, 124)
            self.assertEqual(timeout_result.failure_category, "timeout")

    def test_security_boundary_campaign_is_fail_closed_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "security-repo"
            repo.mkdir()
            initialize_repo(repo)
            fixtures = repo / "fixtures"
            fixtures.mkdir()
            task_path = repo / "src" / "task.py"
            task_path.write_text("VALUE = 1\n", encoding="utf-8")
            npmrc = repo / ".npmrc"
            npmrc.write_text(
                "registry=https://registry.npmjs.org/\n", encoding="utf-8"
            )
            unchanged_secret = fixtures / "unchanged_secret.py"
            synthetic_value = "credential-value-123456"
            unchanged_secret.write_text(
                f'apiKey = "{synthetic_value}"\n', encoding="utf-8"
            )
            deleted_secret = fixtures / "deleted_secret.py"
            deleted_secret.write_text(
                f'password = "{synthetic_value}"\n', encoding="utf-8"
            )
            run(
                [
                    "git",
                    "add",
                    ".npmrc",
                    "src/task.py",
                    "fixtures/unchanged_secret.py",
                    "fixtures/deleted_secret.py",
                ],
                cwd=repo,
            )
            run(["git", "commit", "-qm", "add security fixtures"], cwd=repo)
            task_path.write_text("VALUE = 2\n", encoding="utf-8")
            deleted_secret.unlink()
            (repo / "unrelated.txt").write_text("dirty unrelated\n", encoding="utf-8")
            (repo / "src" / "internal-link").symlink_to("task.py")
            outside = root / "outside.txt"
            outside.write_text("outside\n", encoding="utf-8")
            (repo / "src" / "external-link").symlink_to(outside)
            harness = FakeProviderHarness(root)
            task = "Review the scoped task while preserving snapshot security boundaries."
            workflow_identifier = harness.cli(
                repo,
                "workflow",
                "start",
                "--name",
                "snapshot security campaign",
                "--review-mode",
                "deep",
                "--max-provider-attempts",
                "6",
            ).stdout.strip()
            common = [
                "--repo",
                str(repo),
                "--uncommitted",
                "--path",
                "src/task.py",
                "--path",
                "src/internal-link",
                "--path",
                "fixtures/deleted_secret.py",
                "--exclude-snapshot-path",
                ".npmrc",
            ]
            blocked = harness.cli(
                repo,
                "run",
                *common,
                "--workflow-id",
                workflow_identifier,
                "--phase",
                "repair",
                "--risk",
                "security",
                "--review-profile",
                "security",
                "--task",
                task,
                "--without-antigravity",
                "--without-kimi",
                check=False,
                provider_backed=True,
            )
            self.assertEqual(blocked.returncode, 2)
            self.assertIn("likely sensitive material", blocked.stderr)
            self.assertNotIn(synthetic_value, blocked.stderr)
            self.assertEqual(harness.invocations(), [])
            preflight = [
                MM.read_json(path / "metadata.json")
                for path in harness.run_directories()
            ]
            self.assertEqual(preflight[-1]["status"], "preflight_blocked")
            self.assertNotIn("reviewers", preflight[-1])
            self.assertIn("unrelated.txt", preflight[-1]["excluded_changed_paths"])
            self.assertIn(
                "src/external-link", preflight[-1]["excluded_changed_paths"]
            )

            scan = harness.cli(
                repo,
                "scan",
                *common,
                "--approve-findings",
            )
            scan_result = json.loads(scan.stdout)
            finding_paths = {
                item["path"] for item in scan_result["sensitive_findings"]
            }
            self.assertIn("fixtures/unchanged_secret.py", finding_paths)
            self.assertIn("change.patch", finding_paths)
            self.assertEqual(scan_result["blocked_paths"], [])
            self.assertEqual(scan_result["external_symlinks"], [])
            token = scan_result["approved_token"]
            harness.queue("claude", {"kind": "report", "report": structured_report()})
            reviewed = harness.cli(
                repo,
                "run",
                *common,
                "--workflow-id",
                workflow_identifier,
                "--phase",
                "repair",
                "--risk",
                "security",
                "--review-profile",
                "security",
                "--task",
                task,
                "--sensitive-scan-token",
                token,
                "--without-antigravity",
                "--without-kimi",
                provider_backed=True,
            )
            repair_dir = Path(
                next(
                    line.split(": ", 1)[1]
                    for line in reviewed.stdout.splitlines()
                    if line.startswith("Review artifacts: ")
                )
            )
            repair_metadata = MM.read_json(repair_dir / "metadata.json")
            self.assertEqual(repair_metadata["status"], "completed")
            exclusion = repair_metadata["snapshot_exclusions"][0]
            self.assertEqual(exclusion["path"], ".npmrc")
            self.assertTrue(exclusion["git_blob"])
            provider_view = harness.invocations()[0]
            self.assertNotIn(".npmrc", provider_view["snapshot_files"])
            self.assertIn("src/internal-link", provider_view["snapshot_files"])
            self.assertNotIn(
                "fixtures/deleted_secret.py", provider_view["snapshot_files"]
            )

            reused_token = harness.cli(
                repo,
                "run",
                "--repo",
                str(repo),
                "--workflow-id",
                workflow_identifier,
                "--phase",
                "confirmation",
                "--reuse-contract",
                "--sensitive-scan-token",
                token,
                "--without-antigravity",
                "--without-kimi",
                check=False,
                provider_backed=True,
            )
            self.assertEqual(reused_token.returncode, 2)
            self.assertIn("already consumed", reused_token.stderr)
            self.assertEqual(len(harness.invocations()), 1)

            second_scan = json.loads(
                harness.cli(
                    repo, "scan", *common, "--approve-findings"
                ).stdout
            )
            second_token = second_scan["approved_token"]
            original_task = task_path.read_text(encoding="utf-8")
            task_path.write_text(original_task + "# modified snapshot\n", encoding="utf-8")
            mismatched_token = harness.cli(
                repo,
                "run",
                "--repo",
                str(repo),
                "--workflow-id",
                workflow_identifier,
                "--phase",
                "confirmation",
                "--reuse-contract",
                "--sensitive-scan-token",
                second_token,
                "--without-antigravity",
                "--without-kimi",
                check=False,
                provider_backed=True,
            )
            self.assertEqual(mismatched_token.returncode, 2)
            self.assertIn("does not match the current snapshot", mismatched_token.stderr)
            task_path.write_text(original_task, encoding="utf-8")
            self.assertEqual(len(harness.invocations()), 1)

            unchanged_secret.write_text(
                'apiKey = "different-credential-value-654321"\n',
                encoding="utf-8",
            )
            run(["git", "add", "fixtures/unchanged_secret.py"], cwd=repo)
            run(["git", "commit", "-qm", "change synthetic fixture"], cwd=repo)
            stale_approval = harness.cli(
                repo,
                "run",
                "--repo",
                str(repo),
                "--workflow-id",
                workflow_identifier,
                "--phase",
                "confirmation",
                "--reuse-contract",
                "--reuse-lineage-sensitive-approvals",
                "--without-antigravity",
                "--without-kimi",
                check=False,
                provider_backed=True,
            )
            self.assertEqual(stale_approval.returncode, 2)
            self.assertIn("New or changed findings require", stale_approval.stderr)
            self.assertNotIn("different-credential-value", stale_approval.stderr)
            unchanged_secret.write_text(
                f'apiKey = "{synthetic_value}"\n', encoding="utf-8"
            )
            run(["git", "add", "fixtures/unchanged_secret.py"], cwd=repo)
            run(["git", "commit", "-qm", "restore synthetic fixture"], cwd=repo)

            incomplete = structured_report(
                coverage_complete=False,
                unreviewed_paths=["src/internal-link"],
                limitations=["The symlink target was not followed independently."],
            )
            harness.queue("claude", {"kind": "report", "report": incomplete})
            confirmation = harness.cli(
                repo,
                "run",
                "--repo",
                str(repo),
                "--workflow-id",
                workflow_identifier,
                "--phase",
                "confirmation",
                "--reuse-contract",
                "--reuse-lineage-sensitive-approvals",
                "--without-antigravity",
                "--without-kimi",
                provider_backed=True,
            )
            confirmation_dir = Path(
                next(
                    line.split(": ", 1)[1]
                    for line in confirmation.stdout.splitlines()
                    if line.startswith("Review artifacts: ")
                )
            )
            no_compensation = harness.cli(
                repo,
                "finalize",
                "--run",
                str(confirmation_dir),
                "--codex-verdict",
                "PASS_CLEAN",
                "--codex-review",
                "The provider report alone is insufficient.",
                "--verification",
                "security fixture checks passed",
                check=False,
            )
            self.assertEqual(no_compensation.returncode, 2)
            self.assertIn("explicit Codex compensation", no_compensation.stderr)
            harness.cli(
                repo,
                "finalize",
                "--run",
                str(confirmation_dir),
                "--codex-verdict",
                "PASS_CLEAN",
                "--codex-review",
                "Inspected the omitted config and internal symlink target.",
                "--verification",
                "security fixture checks passed",
                "--coverage-verification",
                "Read .npmrc and src/task.py through src/internal-link; both are safe and task behavior is covered.",
            )
            verification = json.loads(
                harness.cli(
                    repo, "verify", "--run", str(confirmation_dir)
                ).stdout
            )
            self.assertTrue(verification["fresh"])
            self.assertFalse(verification["deployment_ready"])
            status = json.loads(
                harness.cli(
                    repo, "workflow", "status", workflow_identifier
                ).stdout
            )
            self.assertEqual(status["state"], "ready_to_finalize")
            self.assertEqual(
                status["external_review_coverage"]["headline"], "claude only"
            )
            continuation = json.loads(
                harness.cli(
                    repo, "continue", workflow_identifier, check=False
                ).stdout
            )
            self.assertEqual(continuation["next"], "READY_TO_GATE")
            audit = json.loads(
                harness.cli(repo, "workflow", "audit", "--stale-days", "7").stdout
            )
            self.assertFalse(audit["mutated"])
            harness.cli(repo, "workflow", "finalize", workflow_identifier)
            completed_status = json.loads(
                harness.cli(
                    repo, "workflow", "status", workflow_identifier, check=False
                ).stdout
            )
            self.assertEqual(completed_status["state"], "completed")
            self.assertFalse(completed_status["deployment_ready"])

            external_workflow = harness.cli(
                repo,
                "workflow",
                "start",
                "--review-mode",
                "deep",
            ).stdout.strip()
            external_block = harness.cli(
                repo,
                "run",
                "--repo",
                str(repo),
                "--uncommitted",
                "--workflow-id",
                external_workflow,
                "--exclude-snapshot-path",
                ".npmrc",
                "--risk",
                "security",
                "--task",
                "Reject an external symlink before provider invocation.",
                "--without-antigravity",
                "--without-kimi",
                check=False,
                provider_backed=True,
            )
            self.assertEqual(external_block.returncode, 2)
            self.assertIn("external symlink escapes", external_block.stderr)
            self.assertEqual(len(harness.invocations()), 2)

            task_scoped_exclusion = harness.cli(
                repo,
                "scan",
                "--repo",
                str(repo),
                "--uncommitted",
                "--path",
                ".npmrc",
                "--exclude-snapshot-path",
                ".npmrc",
                check=False,
            )
            self.assertEqual(task_scoped_exclusion.returncode, 2)
            self.assertIn("task-scoped or changed", task_scoped_exclusion.stderr)
            original_npmrc = npmrc.read_text(encoding="utf-8")
            npmrc.write_text(original_npmrc + "audit=true\n", encoding="utf-8")
            changed_exclusion = harness.cli(
                repo,
                "scan",
                *common,
                check=False,
            )
            self.assertEqual(changed_exclusion.returncode, 2)
            self.assertIn("must be unchanged", changed_exclusion.stderr)
            npmrc.write_text(original_npmrc, encoding="utf-8")

            authoritative_json = [
                path
                for run_dir in harness.run_directories()
                for path in run_dir.glob("*.json")
            ]
            for path in authoritative_json:
                self.assertNotIn(
                    synthetic_value, path.read_text(encoding="utf-8"), str(path)
                )

    def test_repair_to_confirmation_campaign_enforces_authoritative_gate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "campaign-repo"
            repo.mkdir()
            initialize_repo(repo)
            tests = repo / "tests"
            tests.mkdir()
            scheduler = repo / "src" / "scheduler.py"
            scheduler.write_text(
                "def schedule(store, queue, job):\n"
                "    raise NotImplementedError\n",
                encoding="utf-8",
            )
            scheduler_test = tests / "test_scheduler.py"
            scheduler_test.write_text(
                "import unittest\n\n"
                "class SchedulerTest(unittest.TestCase):\n"
                "    def test_placeholder(self):\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8",
            )
            run(["git", "add", "src/scheduler.py", "tests/test_scheduler.py"], cwd=repo)
            run(["git", "commit", "-qm", "add scheduler skeleton"], cwd=repo)
            scheduler.write_text(
                "def schedule(store, queue, job):\n"
                "    queue.enqueue(job['id'])\n"
                "    store.save(job)\n"
                "    store.commit()\n",
                encoding="utf-8",
            )
            scheduler_test.write_text(
                "import pathlib\n"
                "import sys\n"
                "import unittest\n\n"
                "sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / 'src'))\n"
                "from scheduler import schedule\n\n"
                "class SchedulerTest(unittest.TestCase):\n"
                "    def test_schedules_job(self):\n"
                "        events = []\n"
                "        store = type('Store', (), {\n"
                "            'save': lambda self, job: events.append('save'),\n"
                "            'commit': lambda self: events.append('commit'),\n"
                "        })()\n"
                "        queue = type('Queue', (), {\n"
                "            'enqueue': lambda self, job_id: events.append('enqueue'),\n"
                "        })()\n"
                "        schedule(store, queue, {'id': 'job-1'})\n"
                "        self.assertCountEqual(events, ['save', 'commit', 'enqueue'])\n",
                encoding="utf-8",
            )
            harness = FakeProviderHarness(root)
            finding_report = structured_report(
                verdict="PASS_WITH_FINDINGS",
                findings=textwrap.dedent(
                    """\
                    ## [medium] Queue publication precedes durable commit
                    - Location: src/scheduler.py:2
                    - Trigger: The queue worker starts immediately after enqueue
                    - Evidence: enqueue is called before save and commit
                    - Impact: A worker can observe a job that is not durable
                    - Smallest fix: Commit before enqueueing the protected task
                    - Confidence: high
                    """
                ).strip(),
                test_gaps=textwrap.dedent(
                    """\
                    ## [medium] Ordering is not asserted
                    - Needed test: Assert save and commit happen before enqueue
                    - Risk: A future refactor can reintroduce publication before durability
                    """
                ).strip(),
                observations=textwrap.dedent(
                    """\
                    ## [low] Job identifiers are already stable
                    - Location: src/scheduler.py:2
                    - Evidence: enqueue receives the persisted job id
                    - Why non-actionable: The identifier needs no code or test change
                    """
                ).strip(),
            )
            clean_report = structured_report()
            harness.queue(
                "claude",
                {
                    "kind": "report",
                    "report": finding_report,
                    "mutate_relative": "provider-marker.txt",
                },
                {"kind": "report", "report": clean_report},
                {"kind": "report", "report": clean_report},
            )
            workflow_identifier = harness.cli(
                repo,
                "workflow",
                "start",
                "--name",
                "scheduler commit-before-publish repair",
                "--review-mode",
                "balanced",
                "--max-provider-attempts",
                "6",
            ).stdout.strip()
            task = (
                "Persist a scheduled job before publishing its background task, "
                "with a regression test for call ordering."
            )
            initial = harness.cli(
                repo,
                "run",
                "--repo",
                str(repo),
                "--uncommitted",
                "--workflow-id",
                workflow_identifier,
                "--phase",
                "repair",
                "--path",
                "src/scheduler.py",
                "--path",
                "tests/test_scheduler.py",
                "--risk",
                "db-write",
                "--review-profile",
                "data-change",
                "--task",
                task,
                "--without-antigravity",
                "--without-kimi",
                provider_backed=True,
            )
            initial_dir = Path(
                next(
                    line.split(": ", 1)[1]
                    for line in initial.stdout.splitlines()
                    if line.startswith("Review artifacts: ")
                )
            )
            for artifact in (
                "metadata.json",
                "review-summary.json",
                "triage.json",
                "claude.md",
            ):
                self.assertTrue((initial_dir / artifact).is_file(), artifact)
            self.assertFalse((initial_dir / "final.json").exists())
            initial_triage = MM.read_json(initial_dir / "triage.json")
            self.assertEqual(len(initial_triage["findings"]), 1)
            self.assertEqual(len(initial_triage["test_gaps"]), 1)
            self.assertEqual(len(initial_triage["observations"]), 1)
            invocation = harness.invocations()[0]
            self.assertNotEqual(Path(str(invocation["cwd"])), repo)
            self.assertIn("/snapshot", str(invocation["cwd"]))
            self.assertIn("src/scheduler.py", invocation["snapshot_files"])
            self.assertFalse((repo / "provider-marker.txt").exists())

            unresolved = harness.cli(
                repo,
                "run",
                "--repo",
                str(repo),
                "--workflow-id",
                workflow_identifier,
                "--phase",
                "repair",
                "--reuse-contract",
                "--without-antigravity",
                "--without-kimi",
                check=False,
                provider_backed=True,
            )
            self.assertEqual(unresolved.returncode, 2)
            self.assertIn("fully triaged", unresolved.stderr)
            self.assertEqual(len(harness.invocations()), 1)
            decisions = [
                {
                    "finding": "claude-001",
                    "decision": "accepted",
                    "evidence": "The source calls enqueue before store.commit.",
                    "action": "Move enqueue after the commit.",
                },
                {
                    "finding": "claude-test-001",
                    "decision": "accepted",
                    "evidence": "The test ignores event order via assertCountEqual.",
                    "action": "Assert the exact event sequence.",
                },
                {
                    "finding": "claude-observation-001",
                    "decision": "acknowledged",
                    "evidence": "The stable job id is passed through unchanged.",
                },
            ]
            harness.cli(
                repo,
                "decide-batch",
                "--run",
                str(initial_dir),
                *sum(
                    (["--item", json.dumps(item)] for item in decisions),
                    [],
                ),
            )
            accepted_block = harness.cli(
                repo,
                "run",
                "--repo",
                str(repo),
                "--workflow-id",
                workflow_identifier,
                "--phase",
                "repair",
                "--reuse-contract",
                "--without-antigravity",
                "--without-kimi",
                check=False,
                provider_backed=True,
            )
            self.assertEqual(accepted_block.returncode, 2)
            self.assertIn("is accepted", accepted_block.stderr)
            premature = harness.cli(
                repo,
                "decide-batch",
                "--run",
                str(initial_dir),
                "--item",
                json.dumps(
                    {
                        "finding": "claude-001",
                        "decision": "fixed",
                        "evidence": "Claimed fixed before source edit.",
                        "verification": "Not yet valid.",
                    }
                ),
                "--item",
                json.dumps(
                    {
                        "finding": "claude-test-001",
                        "decision": "covered",
                        "evidence": "Claimed covered before source edit.",
                        "verification": "Not yet valid.",
                    }
                ),
                check=False,
            )
            self.assertEqual(premature.returncode, 2)
            self.assertIn("task-scoped source is unchanged", premature.stderr)

            scheduler.write_text(
                "def schedule(store, queue, job):\n"
                "    store.save(job)\n"
                "    store.commit()\n"
                "    queue.enqueue(job['id'])\n",
                encoding="utf-8",
            )
            scheduler_test.write_text(
                scheduler_test.read_text(encoding="utf-8").replace(
                    "self.assertCountEqual(events, ['save', 'commit', 'enqueue'])",
                    "self.assertEqual(events, ['save', 'commit', 'enqueue'])",
                ),
                encoding="utf-8",
            )
            local_test = run(
                [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                cwd=repo,
            )
            self.assertEqual(local_test.returncode, 0)
            harness.cli(
                repo,
                "decide-batch",
                "--run",
                str(initial_dir),
                "--item",
                json.dumps(
                    {
                        "finding": "claude-001",
                        "decision": "fixed",
                        "evidence": "Commit now precedes enqueue in scheduler.py.",
                        "verification": "Ordering regression test passed.",
                    }
                ),
                "--item",
                json.dumps(
                    {
                        "finding": "claude-test-001",
                        "decision": "covered",
                        "evidence": "The test now asserts the exact event order.",
                        "verification": "Ordering regression test passed.",
                    }
                ),
            )
            repaired = harness.cli(
                repo,
                "run",
                "--repo",
                str(repo),
                "--workflow-id",
                workflow_identifier,
                "--phase",
                "repair",
                "--reuse-contract",
                "--local-verification",
                "python -m unittest discover -s tests: passed",
                "--without-antigravity",
                "--without-kimi",
                provider_backed=True,
            )
            self.assertIn("PASS_CLEAN", repaired.stdout)

            drift_commands = [
                ["--uncommitted", "--path", "src/scheduler.py", "--risk", "db-write", "--review-profile", "data-change", "--task", task],
                ["--uncommitted", "--path", "src/scheduler.py", "--path", "tests/test_scheduler.py", "--risk", "db-write", "--review-profile", "data-change", "--task", task + " drift"],
                ["--uncommitted", "--path", "src/scheduler.py", "--path", "tests/test_scheduler.py", "--review-profile", "data-change", "--task", task],
                ["--uncommitted", "--path", "src/scheduler.py", "--path", "tests/test_scheduler.py", "--risk", "db-write", "--task", task],
                ["--base", "HEAD", "--path", "src/scheduler.py", "--path", "tests/test_scheduler.py", "--risk", "db-write", "--review-profile", "data-change", "--task", task],
            ]
            for drift in drift_commands:
                blocked = harness.cli(
                    repo,
                    "run",
                    "--repo",
                    str(repo),
                    "--workflow-id",
                    workflow_identifier,
                    "--phase",
                    "confirmation",
                    *drift,
                    "--without-antigravity",
                    "--without-kimi",
                    check=False,
                    provider_backed=True,
                )
                self.assertEqual(blocked.returncode, 2, blocked.stderr)
                self.assertIn("Confirmation must reuse", blocked.stderr)
            self.assertEqual(len(harness.invocations()), 2)
            confirmation = harness.cli(
                repo,
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
                provider_backed=True,
            )
            confirmation_dir = Path(
                next(
                    line.split(": ", 1)[1]
                    for line in confirmation.stdout.splitlines()
                    if line.startswith("Review artifacts: ")
                )
            )
            self.assertFalse((confirmation_dir / "final.json").exists())
            before_codex = harness.cli(
                repo,
                "verify",
                "--run",
                str(confirmation_dir),
                check=False,
            )
            self.assertEqual(before_codex.returncode, 2)
            self.assertIn("has not been finalized", before_codex.stderr)
            harness.cli(
                repo,
                "finalize",
                "--run",
                str(confirmation_dir),
                "--codex-verdict",
                "PASS_CLEAN",
                "--codex-review",
                "Traced persistence through enqueue and reviewed the exact diff.",
                "--verification",
                "python -m unittest discover -s tests: passed",
            )
            local_verify = json.loads(
                harness.cli(
                    repo, "verify", "--run", str(confirmation_dir)
                ).stdout
            )
            self.assertTrue(local_verify["fresh"])
            self.assertFalse(local_verify["deployment_ready"])
            run(["git", "add", "src/scheduler.py", "tests/test_scheduler.py"], cwd=repo)
            run(["git", "commit", "-qm", "fix scheduler publish ordering"], cwd=repo)
            harness.cli(
                repo,
                "attest-commit",
                "--run",
                str(confirmation_dir),
                "--commit",
                "HEAD",
            )
            committed_verify = json.loads(
                harness.cli(
                    repo, "verify", "--run", str(confirmation_dir)
                ).stdout
            )
            self.assertTrue(committed_verify["deployment_ready"])
            harness.cli(repo, "workflow", "finalize", workflow_identifier)
            final_status = json.loads(
                harness.cli(
                    repo, "workflow", "status", workflow_identifier
                ).stdout
            )
            self.assertEqual(final_status["state"], "completed")
            self.assertTrue(final_status["deployment_ready"])
            final = MM.read_json(confirmation_dir / "final.json")
            self.assertEqual(final["status"], "PASS_CLEAN")
            self.assertEqual(final["codex_verdict"], "PASS_CLEAN")
            self.assertEqual(len(final["acknowledged_observations"]), 1)
            self.assertEqual(len(harness.invocations()), 3)

    def test_multi_repository_freshness_and_successor_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            harness = FakeProviderHarness(root)
            repos: list[Path] = []
            remotes: list[Path] = []
            for name in ("scheduler-api", "scheduler-worker"):
                repo = root / name
                remote = root / f"{name}.git"
                repo.mkdir()
                initialize_repo(repo)
                run(["git", "branch", "-M", "main"], cwd=repo)
                remote.mkdir()
                run(["git", "init", "--bare", "-q"], cwd=remote)
                run(["git", "remote", "add", "origin", str(remote)], cwd=repo)
                run(["git", "push", "-qu", "origin", "main"], cwd=repo)
                (repo / "src" / "feature.py").write_text(
                    "VALUE = 2\n", encoding="utf-8"
                )
                repos.append(repo)
                remotes.append(remote)

            def artifact_dir(completed: subprocess.CompletedProcess[str]) -> Path:
                return Path(
                    next(
                        line.split(": ", 1)[1]
                        for line in completed.stdout.splitlines()
                        if line.startswith("Review artifacts: ")
                    )
                )

            def review(
                repo: Path,
                workflow: str,
                phase: str,
                *,
                reuse_lineage: bool = False,
                task: str | None = None,
            ) -> Path:
                arguments = [
                    "run",
                    "--repo",
                    str(repo),
                    "--workflow-id",
                    workflow,
                    "--phase",
                    phase,
                ]
                if phase == "repair" and not reuse_lineage:
                    arguments.extend(
                        [
                            "--uncommitted",
                            "--path",
                            "src/feature.py",
                            "--task",
                            task or "Review the repository state transition.",
                        ]
                    )
                else:
                    arguments.append("--reuse-contract")
                    if reuse_lineage:
                        arguments.append("--reuse-lineage-sensitive-approvals")
                arguments.extend(["--without-antigravity", "--without-kimi"])
                return artifact_dir(
                    harness.cli(
                        repo,
                        *arguments,
                        provider_backed=True,
                    )
                )

            deferred_report = structured_report(
                verdict="PASS_WITH_FINDINGS",
                findings=textwrap.dedent(
                    """\
                    ## [low] Compatibility constant remains provisional
                    - Location: src/feature.py:1
                    - Trigger: A legacy consumer still reads the old value
                    - Evidence: The compatibility contract is intentionally external
                    - Impact: Removal should wait for the consumer migration
                    - Smallest fix: Track removal in the consumer migration
                    - Confidence: medium
                    """
                ).strip(),
            )
            clean = structured_report()
            harness.queue(
                "claude",
                {"kind": "report", "report": deferred_report},
                {"kind": "report", "report": clean},
                {"kind": "report", "report": clean},
                {"kind": "report", "report": clean},
            )
            workflow = harness.cli(
                repos[0],
                "workflow",
                "start",
                "--name",
                "two repository release gate",
                "--review-mode",
                "balanced",
                "--max-provider-attempts",
                "12",
            ).stdout.strip()
            repo_one_repair = review(
                repos[0], workflow, "repair", task="Review API scheduling state."
            )
            harness.cli(
                repos[0],
                "decide",
                "--run",
                str(repo_one_repair),
                "--finding",
                "claude-001",
                "--decision",
                "deferred",
                "--evidence",
                "The compatibility consumer migration is outside this task.",
                "--action",
                "Remove the constant after the consumer migration lands.",
            )
            repo_one_final = review(repos[0], workflow, "confirmation")
            harness.cli(
                repos[0],
                "finalize",
                "--run",
                str(repo_one_final),
                "--codex-verdict",
                "PASS_WITH_FINDINGS",
                "--codex-review",
                "The low-risk compatibility item is explicitly deferred.",
            )
            review(repos[1], workflow, "repair", task="Review worker scheduling state.")
            repo_two_final = review(repos[1], workflow, "confirmation")
            harness.cli(
                repos[1],
                "finalize",
                "--run",
                str(repo_two_final),
                "--codex-verdict",
                "PASS_CLEAN",
                "--codex-review",
                "The worker transition is clean and fully covered.",
            )
            local_status = json.loads(
                harness.cli(
                    repos[0], "workflow", "status", workflow, check=False
                ).stdout
            )
            self.assertEqual(local_status["state"], "ready_to_finalize")
            self.assertTrue(local_status["ready"])
            self.assertFalse(local_status["deployment_ready"])
            self.assertEqual(len(local_status["repositories"]), 2)

            for repo, final_dir, remote in zip(
                repos, (repo_one_final, repo_two_final), remotes
            ):
                run(["git", "add", "src/feature.py"], cwd=repo)
                run(["git", "commit", "-qm", "update scheduling state"], cwd=repo)
                local_head = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
                remote_head = run(
                    ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/main"],
                    cwd=repo,
                ).stdout.strip()
                self.assertNotEqual(local_head, remote_head)
                harness.cli(
                    repo,
                    "attest-commit",
                    "--run",
                    str(final_dir),
                    "--commit",
                    "HEAD",
                )
            attested_status = json.loads(
                harness.cli(repos[0], "workflow", "status", workflow).stdout
            )
            self.assertTrue(attested_status["deployment_ready"])
            for item in attested_status["repositories"]:
                self.assertNotIn("deployed", item)
                self.assertNotIn("remote_branch_equal", item)
            harness.cli(repos[0], "workflow", "finalize", workflow)
            completed = json.loads(
                harness.cli(repos[0], "workflow", "status", workflow).stdout
            )
            self.assertEqual(completed["state"], "completed")
            completed_audit = json.loads(
                harness.cli(repos[0], "workflow", "audit", "--format", "json").stdout
            )
            completed_states = {
                item["workflow_id"]: item["state"]
                for item in completed_audit["workflows"]
            }
            self.assertEqual(completed_states[workflow], "completed")
            for repo in repos:
                run(["git", "push", "-q", "origin", "main"], cwd=repo)
                local_head = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
                remote_head = run(
                    ["git", "rev-parse", "refs/remotes/origin/main"], cwd=repo
                ).stdout.strip()
                self.assertEqual(local_head, remote_head)

            (repos[0] / "src" / "feature.py").write_text(
                "VALUE = 3\n", encoding="utf-8"
            )
            stale_verify = harness.cli(
                repos[0], "verify", "--run", str(repo_one_final), check=False
            )
            self.assertEqual(stale_verify.returncode, 3)
            stale_status = json.loads(
                harness.cli(
                    repos[0], "workflow", "status", workflow, check=False
                ).stdout
            )
            self.assertEqual(stale_status["state"], "completed_stale")
            stale_compact = harness.cli(
                repos[0],
                "workflow",
                "status",
                workflow,
                "--format",
                "compact",
                check=False,
            ).stdout
            self.assertIn("state=completed_stale", stale_compact)
            stale_audit = json.loads(
                harness.cli(repos[0], "workflow", "audit", "--format", "json").stdout
            )
            stale_states = {
                item["workflow_id"]: item["state"]
                for item in stale_audit["workflows"]
            }
            self.assertEqual(stale_states[workflow], "completed_stale")

            successor = harness.cli(
                repos[0],
                "workflow",
                "supersede",
                workflow,
                "--reason",
                "repository source changed after the authoritative finals",
            ).stdout.strip()
            (repos[1] / "src" / "feature.py").write_text(
                "VALUE = 3\n", encoding="utf-8"
            )
            harness.queue(
                "claude",
                *({"kind": "report", "report": clean} for _ in range(6)),
            )
            review(repos[0], successor, "repair", reuse_lineage=True)
            successor_one_final = review(
                repos[0], successor, "confirmation", reuse_lineage=True
            )
            harness.cli(
                repos[0],
                "finalize",
                "--run",
                str(successor_one_final),
                "--codex-verdict",
                "PASS_WITH_FINDINGS",
                "--codex-review",
                "The inherited low-risk deferred item remains explicit.",
            )
            incomplete_successor = json.loads(
                harness.cli(
                    repos[0], "workflow", "status", successor, check=False
                ).stdout
            )
            self.assertFalse(incomplete_successor["ready"])
            missing = [
                item
                for item in incomplete_successor["repositories"]
                if item["state"] == "not-reviewed"
            ]
            self.assertEqual(len(missing), 1)
            self.assertEqual(missing[0]["repository"]["name"], repos[1].name)
            successor_plan = json.loads(
                harness.cli(repos[0], "continue", successor, check=False).stdout
            )
            self.assertEqual(successor_plan["next"], "NEEDS_REVIEW")
            self.assertIn("--reuse-contract", successor_plan["actions"][0]["command"])
            blocked_finalize = harness.cli(
                repos[0], "workflow", "finalize", successor, check=False
            )
            self.assertEqual(blocked_finalize.returncode, 2)

            review(repos[1], successor, "repair", reuse_lineage=True)
            successor_two_final = review(
                repos[1], successor, "confirmation", reuse_lineage=True
            )
            harness.cli(
                repos[1],
                "finalize",
                "--run",
                str(successor_two_final),
                "--codex-verdict",
                "PASS_CLEAN",
                "--codex-review",
                "The second repository is freshly confirmed.",
            )
            carried_final = MM.read_json(successor_one_final / "final.json")
            self.assertEqual(carried_final["status"], "PASS_WITH_FINDINGS")
            self.assertTrue(
                any(
                    item.get("title") == "Compatibility constant remains provisional"
                    for item in carried_final["remaining_findings"]
                )
            )
            ready_successor = json.loads(
                harness.cli(repos[0], "workflow", "status", successor).stdout
            )
            self.assertEqual(ready_successor["state"], "ready_to_finalize")
            ready_audit = json.loads(
                harness.cli(repos[0], "workflow", "audit", "--format", "json").stdout
            )
            ready_states = {
                item["workflow_id"]: item["state"]
                for item in ready_audit["workflows"]
            }
            self.assertEqual(ready_states[successor], "ready_to_finalize")
            self.assertEqual(ready_states[workflow], "superseded")

            for repo, final_dir in zip(
                repos, (successor_one_final, successor_two_final)
            ):
                run(["git", "add", "src/feature.py"], cwd=repo)
                run(["git", "commit", "-qm", "advance successor state"], cwd=repo)
                harness.cli(
                    repo,
                    "attest-commit",
                    "--run",
                    str(final_dir),
                    "--commit",
                    "HEAD",
                )
            harness.cli(repos[0], "workflow", "finalize", successor)

            blocked_path = repos[0] / "src" / "blocked.py"
            blocked_path.write_text("BLOCKED = True\n", encoding="utf-8")
            active_workflow = harness.cli(
                repos[0], "workflow", "start", "--name", "intentionally active"
            ).stdout.strip()
            blocked_workflow = harness.cli(
                repos[0],
                "workflow",
                "start",
                "--name",
                "Codex blocked gate",
                "--max-provider-attempts",
                "4",
            ).stdout.strip()
            blocked_repair = artifact_dir(
                harness.cli(
                    repos[0],
                    "run",
                    "--repo",
                    str(repos[0]),
                    "--uncommitted",
                    "--workflow-id",
                    blocked_workflow,
                    "--phase",
                    "repair",
                    "--path",
                    "src/blocked.py",
                    "--task",
                    "Demonstrate an explicit Codex block.",
                    "--without-antigravity",
                    "--without-kimi",
                    provider_backed=True,
                )
            )
            self.assertTrue((blocked_repair / "review-summary.json").exists())
            blocked_confirmation = review(
                repos[0], blocked_workflow, "confirmation"
            )
            blocked_finalization = harness.cli(
                repos[0],
                "finalize",
                "--run",
                str(blocked_confirmation),
                "--codex-verdict",
                "BLOCK",
                "--codex-review",
                "Codex found the synthetic state unacceptable despite provider PASS.",
                check=False,
            )
            self.assertEqual(blocked_finalization.returncode, 3)
            blocked_status = json.loads(
                harness.cli(
                    repos[0], "workflow", "status", blocked_workflow, check=False
                ).stdout
            )
            self.assertEqual(blocked_status["state"], "blocked")
            final_audit = json.loads(
                harness.cli(repos[0], "workflow", "audit", "--format", "json").stdout
            )
            final_states = {
                item["workflow_id"]: item["state"]
                for item in final_audit["workflows"]
            }
            self.assertEqual(final_states[active_workflow], "active")
            self.assertEqual(final_states[blocked_workflow], "blocked")
            self.assertEqual(final_states[successor], "completed")
            self.assertEqual(final_states[workflow], "superseded")
            self.assertEqual(len(harness.invocations()), 10)

    def test_supplemental_recheck_preserves_legacy_parent_policy(self) -> None:
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
                    if sys.argv[1:] == ["auth", "status"]:
                        print(json.dumps({{"loggedIn": True, "authMethod": "oauth"}}))
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
                    "--codex-verdict",
                    "PASS_CLEAN",
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
                    "--codex-verdict",
                    "PASS_CLEAN",
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
        self.assertEqual(supplemental_workflow["policy"]["max_budget_usd"], 0.8)
        self.assertNotIn("usage_policy", supplemental_workflow["policy"])

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
                    if sys.argv[1:] == ["auth", "status"]:
                        print(json.dumps({{"loggedIn": True, "authMethod": "oauth"}}))
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
                    "rejected",
                    "--evidence",
                    "The same focused behavior assertion already covers this gap.",
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
                "rejected",
            )
            repair_finalize = run(
                [
                    *base,
                    "finalize",
                    "--run",
                    str(run_dir),
                    "--codex-verdict",
                    "PASS_CLEAN",
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
                ("claude-test-001", "rejected"),
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
                    "--codex-verdict",
                    "PASS_CLEAN",
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
                    if sys.argv[1:] == ["auth", "status"]:
                        print(json.dumps({{"loggedIn": True, "authMethod": "oauth"}}))
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

    def test_run_enforces_post_fix_local_verification_across_supersede(
        self,
    ) -> None:
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
            (repo / "src" / "feature.py").write_text(
                "VALUE = 2\n", encoding="utf-8"
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
                    if sys.argv[1:] == ["auth", "status"]:
                        print(json.dumps({{"loggedIn": True, "authMethod": "oauth"}}))
                        raise SystemExit(0)
                    path = pathlib.Path({str(invocation_count)!r})
                    count = int(path.read_text()) if path.exists() else 0
                    path.write_text(str(count + 1))
                    sys.stdin.read()
                    if count == 0:
                        report = "# Verdict\\nPASS_WITH_FINDINGS\\n\\n# Findings\\n## [medium] Exercise the post-fix gate\\n- Location: src/feature.py:1\\n- Trigger: A repaired source changes\\n- Evidence: The first review supplies a triage item\\n- Impact: Another provider call needs local evidence\\n- Smallest fix: Record local checks\\n- Confidence: high\\n\\n# Test gaps\\nNone.\\n\\n# Observations\\nNone.\\n\\n# Coverage\\n- Complete: yes\\n- Unreviewed changed paths: []\\n- Limitations: []\\n\\n# Notes\\nNone.\\n"
                    else:
                        report = "# Verdict\\nPASS_CLEAN\\n\\n# Findings\\nNone.\\n\\n# Test gaps\\nNone.\\n\\n# Observations\\nNone.\\n\\n# Coverage\\n- Complete: yes\\n- Unreviewed changed paths: []\\n- Limitations: []\\n\\n# Notes\\nNone.\\n"
                    print(json.dumps({{"result": report, "total_cost_usd": 0.01}}))
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
            started = run(
                [*base, "workflow", "start", "--review-mode", "deep"],
                cwd=repo,
                env=environment,
            )
            workflow_identifier = started.stdout.strip()
            initial = run(
                [
                    *base,
                    "run",
                    "--repo",
                    str(repo),
                    "--workflow-id",
                    workflow_identifier,
                    "--phase",
                    "repair",
                    "--task",
                    "Verify the post-fix local gate.",
                    "--without-antigravity",
                    "--without-kimi",
                ],
                cwd=repo,
                env=environment,
            )
            self.assertIn("PASS_WITH_FINDINGS", initial.stdout)
            metadata_paths = list(
                (home / ".codex" / "review-runs").glob("*/*/metadata.json")
            )
            initial_metadata_path = next(
                path
                for path in metadata_paths
                if json.loads(path.read_text(encoding="utf-8")).get("status")
                == "completed"
            )
            initial_run_dir = initial_metadata_path.parent
            (repo / "src" / "feature.py").write_text(
                "VALUE = 3\n", encoding="utf-8"
            )
            run(
                [
                    *base,
                    "decide",
                    "--run",
                    str(initial_run_dir),
                    "--finding",
                    "claude-001",
                    "--decision",
                    "fixed",
                    "--evidence",
                    "The implementation was updated after review.",
                    "--verification",
                    "Focused test passed.",
                ],
                cwd=repo,
                env=environment,
            )
            successor_identifier = run(
                [
                    *base,
                    "workflow",
                    "supersede",
                    workflow_identifier,
                    "--reason",
                    "The repaired source needs a fresh workflow lineage gate.",
                ],
                cwd=repo,
                env=environment,
            ).stdout.strip()
            blocked = run(
                [
                    *base,
                    "run",
                    "--repo",
                    str(repo),
                    "--workflow-id",
                    successor_identifier,
                    "--phase",
                    "repair",
                    "--reuse-contract",
                    "--without-antigravity",
                    "--without-kimi",
                ],
                cwd=repo,
                env=environment,
                check=False,
            )
            self.assertEqual(blocked.returncode, 2)
            self.assertIn("--local-verification", blocked.stderr)
            self.assertEqual(invocation_count.read_text(encoding="utf-8"), "1")
            completed = run(
                [
                    *base,
                    "run",
                    "--repo",
                    str(repo),
                    "--workflow-id",
                    successor_identifier,
                    "--phase",
                    "repair",
                    "--reuse-contract",
                    "--local-verification",
                    "formatter, static checks, and full tests passed",
                    "--without-antigravity",
                    "--without-kimi",
                ],
                cwd=repo,
                env=environment,
            )
            self.assertIn("PASS_CLEAN", completed.stdout)
            completed_metadata = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in (home / ".codex" / "review-runs").glob(
                    "*/*/metadata.json"
                )
                if json.loads(path.read_text(encoding="utf-8")).get("status")
                == "completed"
            ]
            final_invocation_count = invocation_count.read_text(encoding="utf-8")
        latest = next(
            item
            for item in completed_metadata
            if item["workflow_id"] == successor_identifier
        )
        self.assertEqual(latest["round"], 1)
        self.assertEqual(
            latest["local_verification_before_provider"],
            ["formatter, static checks, and full tests passed"],
        )
        self.assertEqual(final_invocation_count, "2")

    def test_run_reuses_only_identical_lineage_sensitive_approvals(self) -> None:
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
            fixture = repo / "credential-fixture.py"
            approved_content = "api" + 'Key = "credential-value-123456"\n'
            fixture.write_text(approved_content, encoding="utf-8")
            run(["git", "add", "credential-fixture.py"], cwd=repo)
            run(["git", "commit", "-qm", "add credential fixture"], cwd=repo)
            (repo / "src" / "feature.py").write_text(
                "VALUE = 2\n", encoding="utf-8"
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
                    if sys.argv[1:] == ["auth", "status"]:
                        print(json.dumps({{"loggedIn": True, "authMethod": "oauth"}}))
                        raise SystemExit(0)
                    path = pathlib.Path({str(invocation_count)!r})
                    count = int(path.read_text()) if path.exists() else 0
                    path.write_text(str(count + 1))
                    sys.stdin.read()
                    report = "# Verdict\\nPASS_CLEAN\\n\\n# Findings\\nNone.\\n\\n# Test gaps\\nNone.\\n\\n# Observations\\nNone.\\n\\n# Coverage\\n- Complete: yes\\n- Unreviewed changed paths: []\\n- Limitations: []\\n\\n# Notes\\nNone.\\n"
                    print(json.dumps({{"result": report, "total_cost_usd": 0.01}}))
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
            workflow_identifier = run(
                [*base, "workflow", "start", "--review-mode", "deep"],
                cwd=repo,
                env=environment,
            ).stdout.strip()
            scan = run(
                [
                    *base,
                    "scan",
                    "--repo",
                    str(repo),
                    "--uncommitted",
                    "--path",
                    "src/feature.py",
                    "--approve-findings",
                ],
                cwd=repo,
                env=environment,
            )
            token = json.loads(scan.stdout)["approved_token"]
            initial = run(
                [
                    *base,
                    "run",
                    "--repo",
                    str(repo),
                    "--workflow-id",
                    workflow_identifier,
                    "--phase",
                    "repair",
                    "--path",
                    "src/feature.py",
                    "--task",
                    "Verify exact lineage approval reuse.",
                    "--sensitive-scan-token",
                    token,
                    "--without-antigravity",
                    "--without-kimi",
                ],
                cwd=repo,
                env=environment,
            )
            self.assertIn("PASS_CLEAN", initial.stdout)
            fixture.write_text(
                "api" + 'Key = "different-credential-value-654321"\n',
                encoding="utf-8",
            )
            run(["git", "add", "credential-fixture.py"], cwd=repo)
            run(["git", "commit", "-qm", "change credential fixture"], cwd=repo)
            changed = run(
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
                    "--reuse-lineage-sensitive-approvals",
                    "--without-antigravity",
                    "--without-kimi",
                ],
                cwd=repo,
                env=environment,
                check=False,
            )
            self.assertEqual(changed.returncode, 2)
            self.assertIn("New or changed findings require", changed.stderr)
            self.assertEqual(invocation_count.read_text(encoding="utf-8"), "1")
            fixture.write_text(approved_content, encoding="utf-8")
            run(["git", "add", "credential-fixture.py"], cwd=repo)
            run(["git", "commit", "-qm", "restore credential fixture"], cwd=repo)
            confirmation = run(
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
                    "--reuse-lineage-sensitive-approvals",
                    "--without-antigravity",
                    "--without-kimi",
                ],
                cwd=repo,
                env=environment,
            )
            self.assertIn("PASS_CLEAN", confirmation.stdout)
            confirmation_metadata = max(
                (
                    json.loads(path.read_text(encoding="utf-8"))
                    for path in (home / ".codex" / "review-runs").glob(
                        "*/*/metadata.json"
                    )
                    if json.loads(path.read_text(encoding="utf-8")).get("status")
                    == "completed"
                ),
                key=lambda item: int(item["round"]),
            )
            final_invocation_count = invocation_count.read_text(encoding="utf-8")
        self.assertEqual(final_invocation_count, "2")
        self.assertEqual(
            confirmation_metadata["sensitive_approval_mode"], "lineage_reuse"
        )
        self.assertTrue(
            confirmation_metadata["lineage_sensitive_approval_sources"]
        )

    def test_run_snapshot_exclusion_controls_provider_manifest_and_freshness(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            repo = root / "repo"
            bin_dir = root / "bin"
            provider_observation = root / "provider-observation.json"
            home.mkdir()
            repo.mkdir()
            bin_dir.mkdir()
            initialize_repo(repo)
            npmrc = repo / ".npmrc"
            npmrc.write_text(
                "registry=https://registry.npmjs.org/\n", encoding="utf-8"
            )
            run(["git", "add", ".npmrc"], cwd=repo)
            run(["git", "commit", "-qm", "add npm config"], cwd=repo)
            (repo / "src" / "feature.py").write_text(
                "VALUE = 2\n", encoding="utf-8"
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
                    if sys.argv[1:] == ["auth", "status"]:
                        print(json.dumps({{"loggedIn": True, "authMethod": "oauth"}}))
                        raise SystemExit(0)
                    prompt = sys.stdin.read()
                    manifest_path = next(
                        line.split(": ", 1)[1]
                        for line in prompt.splitlines()
                        if line.startswith("Manifest artifact: ")
                    )
                    manifest = pathlib.Path(manifest_path).read_text()
                    pathlib.Path({str(provider_observation)!r}).write_text(json.dumps({{
                        "snapshot_has_npmrc": pathlib.Path(".npmrc").exists(),
                        "manifest_lists_npmrc": ".npmrc" in manifest,
                        "manifest_records_sha256": "sha256=" in manifest
                    }}))
                    report = "# Verdict\\nPASS_CLEAN\\n\\n# Findings\\nNone.\\n\\n# Test gaps\\nNone.\\n\\n# Observations\\nNone.\\n\\n# Coverage\\n- Complete: yes\\n- Unreviewed changed paths: []\\n- Limitations: []\\n\\n# Notes\\nNone.\\n"
                    print(json.dumps({{"result": report, "total_cost_usd": 0.01}}))
                    """
                ),
                encoding="utf-8",
            )
            fake_claude.chmod(0o755)
            environment = os.environ.copy()
            environment["HOME"] = str(home)
            environment["PATH"] = f"{bin_dir}:{environment['PATH']}"
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            reviewed = run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    "run",
                    "--repo",
                    str(repo),
                    "--uncommitted",
                    "--task",
                    "Verify snapshot exclusion plumbing.",
                    "--exclude-snapshot-path",
                    ".npmrc",
                    "--without-antigravity",
                    "--without-kimi",
                ],
                cwd=repo,
                env=environment,
            )
            self.assertIn("PASS_CLEAN", reviewed.stdout)
            observation = json.loads(
                provider_observation.read_text(encoding="utf-8")
            )
            metadata_path = next(
                (home / ".codex" / "review-runs").glob("*/*/metadata.json")
            )
            run_dir = metadata_path.parent
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            manifest = (run_dir / "manifest.md").read_text(encoding="utf-8")
            fresh = MM.freshness_status(
                run_dir, metadata, metadata["source_fingerprint"]
            )
            npmrc.write_text(
                "registry=https://example.invalid/\n", encoding="utf-8"
            )
            stale = MM.freshness_status(
                run_dir, metadata, metadata["source_fingerprint"]
            )
        self.assertFalse(observation["snapshot_has_npmrc"])
        self.assertTrue(observation["manifest_lists_npmrc"])
        self.assertTrue(observation["manifest_records_sha256"])
        self.assertEqual(metadata["snapshot_exclusions"][0]["path"], ".npmrc")
        self.assertIn("Snapshot exclusions (not transmitted):", manifest)
        self.assertTrue(fresh["fresh"])
        self.assertFalse(stale["fresh"])
        self.assertEqual(stale["mode"], "snapshot-exclusion-stale")


if __name__ == "__main__":
    unittest.main(buffer=True)
