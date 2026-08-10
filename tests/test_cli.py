from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from taskbundle.cli import app
from taskbundle.errors import InfrastructureError
from taskbundle.models import TestObservation as Observation
from taskbundle.models import TestResult as Result
from taskbundle.session import CommandSession

runner = CliRunner()
FULL_COMMIT = "0123456789abcdef0123456789abcdef01234567"


def invoke_json(*arguments: str) -> tuple[int, dict[str, object]]:
    result = runner.invoke(app, [*arguments, "--json"])
    assert result.stdout, result.output
    return result.exit_code, json.loads(result.stdout)


def test_new_scaffolds_valid_bundle_and_is_logged(tmp_path: Path) -> None:
    bundle = tmp_path / "created"
    exit_code, report = invoke_json(
        "new",
        str(bundle),
        "--repo",
        "https://example.invalid/repo.git",
        "--commit",
        FULL_COMMIT,
        "--id",
        "created-bundle",
    )

    assert exit_code == 0
    assert report["status"] == "succeeded"
    assert (bundle / "task.json").is_file()
    assert (bundle / ".taskbundle" / "taskbundle.db").is_file()
    assert (
        bundle / ".taskbundle" / "commands" / str(report["command_id"]) / "report.json"
    ).is_file()
    assert (
        bundle / ".taskbundle" / "commands" / str(report["command_id"]) / "report.html"
    ).is_file()
    assert report["data"]["profile"]["selected"] == "python"
    assert report["data"]["readiness"]["status"] == "draft"
    assert len(report["data"]["readiness"]["todo"]) == 5

    history_code, history = invoke_json("history", str(bundle))
    assert history_code == 0
    commands = history["data"]["commands"]
    assert commands[0]["id"] == report["command_id"]

    manifest = json.loads((bundle / "task.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 3
    assert manifest["candidate"]["allowed_patch_paths"] == ["src"]
    assert "/usr/local/bin/python -I -m pytest" in manifest["tests"]["fail_to_pass"][0]["command"]


def test_new_auto_detects_local_node_repository(tmp_path: Path) -> None:
    repository = tmp_path / "node-repository"
    repository.mkdir()
    (repository / "package.json").write_text('{"name":"fixture"}\n', encoding="utf-8")
    bundle = tmp_path / "node-task"

    exit_code, report = invoke_json(
        "new",
        str(bundle),
        "--repo",
        str(repository),
        "--commit",
        FULL_COMMIT,
        "--id",
        "node-task",
    )

    assert exit_code == 0
    assert report["data"]["profile"] == {
        "requested": "auto",
        "selected": "node",
        "source": "detected from package.json",
    }
    manifest = json.loads((bundle / "task.json").read_text(encoding="utf-8"))
    assert manifest["environment"]["smoke_command"] == "node --version && npm --version"
    assert (
        (bundle / "environment" / "Dockerfile")
        .read_text(encoding="utf-8")
        .startswith("FROM node:22-slim")
    )


def test_primary_help_hides_compatibility_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0, result.output
    for command in ("new", "init", "validate", "run", "report", "doctor"):
        assert re.search(rf"│\s+{command}\s+", result.output)
    for legacy in ("check", "history", "logs", "diagnose", "artifacts", "export"):
        assert re.search(rf"│\s+{legacy}\s+", result.output) is None


def test_lifecycle_help_describes_dual_images_and_secrecy_order() -> None:
    init_help = runner.invoke(app, ["init", "--help"])
    validate_help = runner.invoke(app, ["validate", "--help"])
    run_help = runner.invoke(app, ["run", "--help"])

    assert init_help.exit_code == validate_help.exit_code == run_help.exit_code == 0
    normalized_init = " ".join(init_help.output.split())
    normalized_validate = " ".join(validate_help.output.split())
    normalized_run = " ".join(run_help.output.split())
    assert "evaluator and redacted solver images" in normalized_init
    assert "baseline and golden truth tables before running a solver" in normalized_validate
    assert "sanitized solver" in normalized_run
    assert "fresh evaluators" in normalized_run
    assert "strict test secrecy" in normalized_run
    assert "always rejects networking" in normalized_run


def test_new_invalid_commit_returns_configuration_error(tmp_path: Path) -> None:
    exit_code, report = invoke_json(
        "new",
        str(tmp_path / "invalid"),
        "--repo",
        "https://example.invalid/repo.git",
        "--commit",
        "main",
        "--id",
        "invalid-bundle",
    )

    assert exit_code == 2
    assert report["status"] == "failed"
    assert report["error"]["kind"] == "configuration_error"


def test_new_normalizes_a_relative_local_repository(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    previous = Path.cwd()
    os.chdir(tmp_path)
    try:
        bundle = tmp_path / "local-bundle"
        exit_code, _report = invoke_json(
            "new",
            str(bundle),
            "--repo",
            "repository",
            "--commit",
            FULL_COMMIT,
            "--id",
            "local-bundle",
        )
    finally:
        os.chdir(previous)

    assert exit_code == 0
    manifest = json.loads((bundle / "task.json").read_text(encoding="utf-8"))
    assert manifest["repository"]["url"] == str(repository.resolve())
    assert (bundle / "tests" / "solver-view.patch").is_file()


def test_logs_returns_target_command(
    valid_bundle_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_initialize(**_: object) -> dict[str, object]:
        raise InfrastructureError("Synthetic image build failure.")

    monkeypatch.setattr("taskbundle.cli.initialize_task", fail_initialize)
    init_code, init_report = invoke_json("init", str(valid_bundle_path))
    assert init_code == 3
    target_id = str(init_report["command_id"])

    logs_code, logs_report = invoke_json("logs", target_id, "--bundle", str(valid_bundle_path))
    assert logs_code == 0
    assert logs_report["data"]["command"]["id"] == target_id
    assert logs_report["data"]["events"][-1]["level"] == "error"


def test_logs_human_output_includes_tests_and_artifacts(valid_bundle_path: Path) -> None:
    session = CommandSession.start(command_name="synthetic", bundle_path=valid_bundle_path)
    session.attach_bundle("minimal-python")
    session.database.add_test_result(
        command_id=session.command_id,
        result=Result(
            phase="post_solver",
            suite="fail_to_pass",
            test_id="subtracts",
            attempt=1,
            expected=Observation.PASS,
            observed=Observation.PASS,
            exit_code=0,
            duration_seconds=0.1,
            log_artifact="commands/example/tests/subtracts.log",
        ),
    )
    session.artifacts.write_text(
        command_id=session.command_id,
        relative_path="evidence.txt",
        content="passed\n",
        kind="evidence",
    )
    target_id = session.command_id
    session.succeed({"resolved": True})
    session.close()

    result = runner.invoke(app, ["logs", target_id, "--bundle", str(valid_bundle_path)])

    assert result.exit_code == 0, result.output
    assert "Test results" in result.output
    assert "subtracts" in result.output
    assert "Artifacts" in result.output
    assert "evidence.txt" in result.output


def test_logs_missing_id_has_a_recovery_hint(valid_bundle_path: Path) -> None:
    result = runner.invoke(app, ["logs", "missing-id", "--bundle", str(valid_bundle_path)])

    assert result.exit_code == 2
    assert "Command ID was not found: missing-id" in result.output
    assert "Hint: Run `task report --list`" in result.output


def test_artifacts_verifies_target_and_detects_tampering(valid_bundle_path: Path) -> None:
    session = CommandSession.start(command_name="synthetic", bundle_path=valid_bundle_path)
    session.attach_bundle("minimal-python")
    artifact = session.artifacts.write_text(
        command_id=session.command_id,
        relative_path="evidence.txt",
        content="trusted evidence\n",
        kind="evidence",
    )
    target_id = session.command_id
    session.succeed({"artifact": str(artifact)})
    session.close()

    verified_code, verified = invoke_json(
        "artifacts", target_id, "--bundle", str(valid_bundle_path)
    )
    assert verified_code == 0
    assert verified["data"]["valid"] is True
    assert verified["data"]["count"] == 2
    assert {item["status"] for item in verified["data"]["artifacts"]} == {"ok"}

    artifact.write_text("tampered\n", encoding="utf-8")
    failed_code, failed = invoke_json("artifacts", target_id, "--bundle", str(valid_bundle_path))
    assert failed_code == 1
    assert failed["error"]["kind"] == "invalid_task"
    failures = failed["error"]["details"]["artifacts"]
    assert (
        next(item for item in failures if item["relative_path"].endswith("evidence.txt"))["status"]
        == "mismatch"
    )


def test_keyboard_interrupt_is_persisted_as_infrastructure_failure(
    valid_bundle_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupt(**_: object) -> dict[str, object]:
        raise KeyboardInterrupt

    monkeypatch.setattr("taskbundle.cli.initialize_task", interrupt)
    exit_code, report = invoke_json("init", str(valid_bundle_path))

    assert exit_code == 3
    assert report["error"]["kind"] == "infrastructure_error"
    assert report["error"]["message"] == "Command interrupted by the user."

    history_code, history = invoke_json("history", str(valid_bundle_path))
    assert history_code == 0
    interrupted = next(
        command for command in history["data"]["commands"] if command["id"] == report["command_id"]
    )
    assert interrupted["status"] == "failed"
    assert interrupted["exit_code"] == 3


def test_human_output_ends_with_summary_and_copyable_next_commands(
    valid_bundle_path: Path,
) -> None:
    result = runner.invoke(app, ["check", str(valid_bundle_path)])

    assert result.exit_code == 0, result.output
    assert "Summary & next steps" in result.output
    assert "Static contract passed with 2 warning(s)." in result.output
    assert f"Bundle: {valid_bundle_path}" in result.output
    assert f"$ task init {valid_bundle_path}" in result.output
    assert "`task report` needs no ID or --bundle" in result.output


def test_inspection_commands_without_an_id_keep_targeting_latest_non_inspection_command(
    valid_bundle_path: Path,
) -> None:
    session = CommandSession.start(
        command_name="run",
        bundle_path=valid_bundle_path,
        arguments=["run"],
    )
    session.attach_bundle("minimal-python")
    target_id = session.command_id
    session.succeed({"resolved": True})
    session.close()

    previous = Path.cwd()
    os.chdir(valid_bundle_path)
    try:
        diagnose_code, diagnosis = invoke_json("diagnose")
        artifacts_code, artifacts = invoke_json("artifacts")
        logs_code, logs = invoke_json("logs")
    finally:
        os.chdir(previous)

    assert diagnose_code == artifacts_code == logs_code == 0
    assert diagnosis["data"]["command"]["id"] == target_id
    assert artifacts["data"]["command"]["id"] == target_id
    assert logs["data"]["command"]["id"] == target_id


def test_inspection_still_works_when_the_bundle_manifest_is_broken(
    valid_bundle_path: Path,
) -> None:
    session = CommandSession.start(
        command_name="check",
        bundle_path=valid_bundle_path,
        arguments=["check"],
    )
    session.attach_bundle("minimal-python")
    target_id = session.command_id
    session.succeed({"valid": True})
    session.close()
    (valid_bundle_path / "task.json").write_text("{ broken json\n", encoding="utf-8")

    logs_code, logs = invoke_json("logs", target_id, "--bundle", str(valid_bundle_path))
    history_code, history = invoke_json("history", str(valid_bundle_path))

    assert logs_code == history_code == 0
    assert logs["data"]["command"]["id"] == target_id
    assert any(command["id"] == target_id for command in history["data"]["commands"])


def test_missing_latest_target_has_guided_recovery(tmp_path: Path) -> None:
    result = runner.invoke(app, ["logs", "--bundle", str(tmp_path)])

    assert result.exit_code == 2
    assert "No prior non-inspection command was found." in result.output
    assert "Summary & next steps" in result.output
    assert "$ task report" in result.output
    assert "$ task logs --help" in result.output
