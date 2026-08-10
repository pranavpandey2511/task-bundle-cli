from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from taskbundle.cli import app
from taskbundle.errors import UnresolvedError
from taskbundle.models import TestObservation as Observation
from taskbundle.models import TestResult as Result
from taskbundle.session import CommandSession

runner = CliRunner()


def invoke_json(*arguments: str) -> tuple[int, dict[str, Any]]:
    result = runner.invoke(app, [*arguments, "--json"])
    assert result.stdout, result.output
    return result.exit_code, json.loads(result.stdout)


def test_check_is_docker_free_and_accepts_arbitrary_test_commands(
    valid_bundle_path: Path,
) -> None:
    manifest_path = valid_bundle_path / "task.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["environment"]["smoke_command"] = "cargo metadata --offline"
    manifest["tests"]["pass_to_pass"][0]["command"] = "go test ./pkg/... -run TestExisting"
    manifest["tests"]["fail_to_pass"][0]["command"] = "npm test -- --run target"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    exit_code, report = invoke_json("check", str(valid_bundle_path))

    assert exit_code == 0
    data = report["data"]
    assert data["valid"] is True
    assert data["docker_required"] is False
    assert {test["command"] for test in data["test_commands"]} == {
        "go test ./pkg/... -run TestExisting",
        "npm test -- --run target",
    }
    statuses = {check["name"]: check["status"] for check in data["checks"]}
    assert statuses["language-adapter"] == "pass"
    assert statuses["base-image-pinning"] == "warning"
    assert statuses["repository-portability"] == "warning"
    assert len(data["inputs_sha256"]["task.json"]) == 64


def test_validate_static_replaces_public_check_workflow(
    valid_bundle_path: Path,
) -> None:
    exit_code, report = invoke_json("validate", str(valid_bundle_path), "--static")

    assert exit_code == 0
    assert report["command"] == "validate"
    assert report["data"]["mode"] == "static"
    assert report["data"]["docker_required"] is False
    assert report["data"]["valid"] is True
    assert report["html_report"].endswith("/report.html")


def test_check_rejects_evaluator_marker_in_solver_description(valid_bundle_path: Path) -> None:
    description = valid_bundle_path / "description.md"
    description.write_text(
        description.read_text(encoding="utf-8")
        + "Accidentally leaked: self.assertEqual(subtract(10, 7), 3)\n",
        encoding="utf-8",
    )

    exit_code, report = invoke_json("check", str(valid_bundle_path))

    assert exit_code == 1
    assert report["error"]["kind"] == "invalid_task"
    assert report["error"]["details"]["conflicts"] == [
        {"path": "test_hidden.py", "test_id": "subtracts"}
    ]


def _failed_target(bundle: Path) -> str:
    session = CommandSession.start(command_name="run", bundle_path=bundle, arguments=["run"])
    session.attach_bundle("minimal-python")
    log = session.artifacts.write_text(
        command_id=session.command_id,
        relative_path="tests/post-solver-subtracts.log",
        content="expected 3, observed 17\n",
        kind="test_log",
    )
    session.database.add_test_result(
        command_id=session.command_id,
        result=Result(
            phase="post_solver",
            suite="fail_to_pass",
            test_id="subtracts",
            attempt=1,
            expected=Observation.PASS,
            observed=Observation.FAIL,
            exit_code=1,
            duration_seconds=0.2,
            log_artifact=log.relative_to(session.state_dir).as_posix(),
        ),
    )
    target_id = session.command_id
    session.fail(
        UnresolvedError(
            "Candidate patch did not resolve every selected test.",
            hint="Inspect the post-solver test log.",
        )
    )
    session.close()
    return target_id


def test_diagnose_aggregates_failed_attempts_and_actions(valid_bundle_path: Path) -> None:
    target_id = _failed_target(valid_bundle_path)

    exit_code, report = invoke_json("diagnose", target_id, "--bundle", str(valid_bundle_path))

    assert exit_code == 0
    data = report["data"]
    assert data["summary"] == {
        "artifact_integrity": "verified",
        "error_attempts": 0,
        "error_kind": "unresolved",
        "exit_code": 1,
        "failing_attempts": 1,
        "flaky_tests": 0,
        "snapshot_count": 0,
        "status": "failed",
        "timeout_attempts": 0,
    }
    assert any(finding["category"] == "post_solver_expectation" for finding in data["findings"])
    assert any("candidate patch" in action.lower() for action in data["next_actions"])
    assert any(
        artifact["relative_path"].endswith("post-solver-subtracts.log")
        for artifact in data["relevant_artifacts"]
    )


def _successful_target(bundle: Path) -> tuple[str, Path]:
    session = CommandSession.start(
        command_name="validate", bundle_path=bundle, arguments=["validate"]
    )
    session.attach_bundle("minimal-python")
    evidence = session.artifacts.write_text(
        command_id=session.command_id,
        relative_path="evidence.txt",
        content="all expectations passed\n",
        kind="evidence",
    )
    target_id = session.command_id
    session.succeed({"valid": True})
    session.close()
    return target_id, evidence


def test_export_is_integrity_checked_and_byte_deterministic(
    valid_bundle_path: Path, tmp_path: Path
) -> None:
    target_id, _evidence = _successful_target(valid_bundle_path)
    first_output = tmp_path / "first.zip"
    second_output = tmp_path / "second.zip"

    first_code, first = invoke_json(
        "export",
        target_id,
        "--bundle",
        str(valid_bundle_path),
        "--output",
        str(first_output),
    )
    second_code, second = invoke_json(
        "export",
        target_id,
        "--bundle",
        str(valid_bundle_path),
        "--output",
        str(second_output),
    )

    assert first_code == second_code == 0
    assert first["data"]["sha256"] == second["data"]["sha256"]
    assert first_output.read_bytes() == second_output.read_bytes()
    assert first["data"]["artifact_integrity"] == "verified"
    assert first["data"]["contains_evaluator_material"] is True
    with zipfile.ZipFile(first_output) as archive:
        names = set(archive.namelist())
        assert "manifest.json" in names
        assert "ledger/command.json" in names
        assert "ledger/integrity.json" in names
        assert f"artifacts/commands/{target_id}/evidence.txt" in names
        assert f"artifacts/commands/{target_id}/report.json" in names
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["command_id"] == target_id
        assert "never expose this archive" in manifest["security_notice"]


def test_export_refuses_tampered_evidence(valid_bundle_path: Path, tmp_path: Path) -> None:
    target_id, evidence = _successful_target(valid_bundle_path)
    evidence.write_text("tampered\n", encoding="utf-8")

    exit_code, report = invoke_json(
        "export",
        target_id,
        "--bundle",
        str(valid_bundle_path),
        "--output",
        str(tmp_path / "invalid.zip"),
    )

    assert exit_code == 1
    assert report["error"]["kind"] == "invalid_task"
    assert not (tmp_path / "invalid.zip").exists()


def test_report_combines_diagnosis_integrity_html_and_events(valid_bundle_path: Path) -> None:
    target_id = _failed_target(valid_bundle_path)

    exit_code, report = invoke_json(
        "report",
        target_id,
        "--bundle",
        str(valid_bundle_path),
        "--events",
    )

    assert exit_code == 0
    data = report["data"]
    assert data["mode"] == "show"
    assert data["command"]["id"] == target_id
    assert data["summary"]["artifact_integrity"] == "verified"
    assert data["summary"]["failing_attempts"] == 1
    assert data["event_count"] == len(data["events"])
    assert data["html_report"].endswith(f"commands/{target_id}/report.html")
    assert data["report_index"].endswith("reports/index.html")


def test_report_lists_only_lifecycle_versions_and_exports(
    valid_bundle_path: Path, tmp_path: Path
) -> None:
    target_id, _evidence = _successful_target(valid_bundle_path)
    invoke_json("diagnose", target_id, "--bundle", str(valid_bundle_path))

    list_code, listed = invoke_json("report", "--bundle", str(valid_bundle_path), "--list")
    assert list_code == 0
    assert listed["data"]["mode"] == "list"
    assert [command["id"] for command in listed["data"]["commands"]] == [target_id]

    destination = tmp_path / "report-evidence.zip"
    export_code, exported = invoke_json(
        "report",
        target_id,
        "--bundle",
        str(valid_bundle_path),
        "--export",
        str(destination),
    )
    assert export_code == 0
    assert exported["data"]["mode"] == "export"
    assert exported["data"]["artifact_integrity"] == "verified"
    assert destination.is_file()


def test_report_fails_closed_when_lifecycle_evidence_is_tampered(
    valid_bundle_path: Path,
) -> None:
    target_id, evidence = _successful_target(valid_bundle_path)
    evidence.write_text("tampered\n", encoding="utf-8")

    exit_code, report = invoke_json("report", target_id, "--bundle", str(valid_bundle_path))

    assert exit_code == 1
    assert report["error"]["kind"] == "invalid_task"
    assert report["error"]["details"]["summary"]["artifact_integrity"] == "failed"
