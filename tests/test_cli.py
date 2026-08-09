from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from taskbundle.cli import app
from taskbundle.errors import InfrastructureError

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

    history_code, history = invoke_json("history", str(bundle))
    assert history_code == 0
    commands = history["data"]["commands"]
    assert commands[0]["id"] == report["command_id"]


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
