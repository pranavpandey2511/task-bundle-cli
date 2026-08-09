from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from taskbundle.cli import app


def docker_is_available(executable: str) -> bool:
    result = subprocess.run(
        [executable, "info", "--format", "{{.ServerVersion}}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    return result.returncode == 0


@pytest.mark.docker
def test_task_init_builds_smokes_reuses_and_cleans_up(valid_bundle_path: Path) -> None:
    if os.environ.get("TASKBUNDLE_RUN_DOCKER_TESTS") != "1":
        pytest.skip("set TASKBUNDLE_RUN_DOCKER_TESTS=1 to run Docker integration tests")

    docker_executable = os.environ.get("TASKBUNDLE_DOCKER_BIN", "docker")
    if not docker_is_available(docker_executable):
        pytest.skip(f"Docker daemon is unavailable through {docker_executable}")

    test_root = Path.cwd() / ".taskbundle" / "integration-tests" / uuid4().hex
    bundle = test_root / "bundle"
    bundle.parent.mkdir(parents=True, exist_ok=False)
    shutil.copytree(valid_bundle_path, bundle)
    runner = CliRunner()

    try:
        first = runner.invoke(app, ["init", str(bundle), "--json"])
        assert first.exit_code == 0, first.output
        first_report = json.loads(first.stdout)
        assert first_report["data"]["reused"] is False
        assert first_report["data"]["smoke"]["exit_code"] == 0

        second = runner.invoke(app, ["init", str(bundle), "--json"])
        assert second.exit_code == 0, second.output
        second_report = json.loads(second.stdout)
        assert second_report["data"]["reused"] is True
        assert second_report["data"]["image_id"] == first_report["data"]["image_id"]

        validation = runner.invoke(app, ["validate", str(bundle), "--json"])
        assert validation.exit_code == 0, validation.output
        validation_report = json.loads(validation.stdout)
        assert validation_report["data"]["valid"] is True
        assert validation_report["data"]["attempt_count"] == 12
        assert validation_report["data"]["mismatches"] == []
        assert validation_report["data"]["flaky"] == []

        stub_run = runner.invoke(
            app,
            ["run", str(bundle), "--solver", "stub", "--repetitions", "1", "--json"],
        )
        assert stub_run.exit_code == 1, stub_run.output
        stub_report = json.loads(stub_run.stdout)
        assert stub_report["error"]["kind"] == "unresolved"
        assert stub_report["error"]["details"]["resolved"] is False
        assert stub_report["error"]["details"]["solver"]["patch_bytes"] == 0

        solver_script = (
            "if grep -R --exclude-dir=.git theta-hidden-evaluator-only "
            "/workspace /tmp/taskbundle-description.md; then exit 91; fi; "
            "sed -i '/def subtract/,/return left + right/"
            "s/return left + right/return left - right/' calculator.py; "
            "printf 'captured\\n' > solver-note.txt"
        )
        command_run = runner.invoke(
            app,
            [
                "run",
                str(bundle),
                "--solver",
                "command",
                "--solver-cmd",
                solver_script,
                "--repetitions",
                "1",
                "--json",
            ],
        )
        assert command_run.exit_code == 0, command_run.output
        command_report = json.loads(command_run.stdout)
        assert command_report["data"]["resolved"] is True
        assert command_report["data"]["solver"]["patch_bytes"] > 0
        pristine_snapshots = [
            bundle / ".taskbundle" / artifact
            for artifact in command_report["data"]["snapshot_artifacts"]
            if artifact.endswith("-pristine.json")
        ]
        assert len(pristine_snapshots) == 3
        for snapshot_path in pristine_snapshots:
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            assert snapshot["head_matches_base"] is True
            assert snapshot["dirty"] is False
        command_patch = (
            bundle / ".taskbundle" / command_report["data"]["solver"]["patch_artifact"]
        ).read_text(encoding="utf-8")
        assert "solver-note.txt" in command_patch
        assert "theta-hidden-evaluator-only" not in command_patch

        patch_run = runner.invoke(
            app,
            [
                "run",
                str(bundle),
                "--solver",
                "patch",
                "--candidate-patch",
                str(bundle / "gold.patch"),
                "--repetitions",
                "1",
                "--json",
            ],
        )
        assert patch_run.exit_code == 0, patch_run.output
        patch_report = json.loads(patch_run.stdout)
        assert patch_report["data"]["resolved"] is True

        logs = runner.invoke(
            app,
            [
                "logs",
                validation_report["command_id"],
                "--bundle",
                str(bundle),
                "--json",
            ],
        )
        assert logs.exit_code == 0, logs.output
        logs_report = json.loads(logs.stdout)
        assert len(logs_report["data"]["test_results"]) == 12
        assert any(
            artifact["kind"] == "validation_report" for artifact in logs_report["data"]["artifacts"]
        )

        run_logs = runner.invoke(
            app,
            [
                "logs",
                command_report["command_id"],
                "--bundle",
                str(bundle),
                "--json",
            ],
        )
        assert run_logs.exit_code == 0, run_logs.output
        run_logs_report = json.loads(run_logs.stdout)
        assert len(run_logs_report["data"]["test_results"]) == 4
        artifact_kinds = {artifact["kind"] for artifact in run_logs_report["data"]["artifacts"]}
        assert {"solver_patch", "repository_status", "run_report"} <= artifact_kinds

        artifacts = runner.invoke(
            app,
            [
                "artifacts",
                command_report["command_id"],
                "--bundle",
                str(bundle),
                "--json",
            ],
        )
        assert artifacts.exit_code == 0, artifacts.output
        artifacts_report = json.loads(artifacts.stdout)
        assert artifacts_report["data"]["valid"] is True
        assert artifacts_report["data"]["count"] > 0
        assert {item["status"] for item in artifacts_report["data"]["artifacts"]} == {"ok"}

        image_tag = first_report["data"]["image_tag"]
        inspect = subprocess.run(
            [docker_executable, "image", "inspect", "--format", "{{.Id}}", image_tag],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert inspect.returncode == 0, inspect.stderr
        assert inspect.stdout.strip() == first_report["data"]["image_id"]

        containers = subprocess.run(
            [
                docker_executable,
                "ps",
                "--all",
                "--filter",
                "name=taskbundle-minimal-python",
                "--format",
                "{{.ID}}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert containers.returncode == 0, containers.stderr
        assert containers.stdout.strip() == ""
    finally:
        shutil.rmtree(test_root, ignore_errors=True)
