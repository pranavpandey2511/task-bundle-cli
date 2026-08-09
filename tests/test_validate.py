from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from taskbundle.config import Bundle, load_bundle
from taskbundle.errors import InvalidTaskError
from taskbundle.lifecycle.initialize import build_fingerprint, image_tag, sha256_file
from taskbundle.lifecycle.validate import validate_task
from taskbundle.models import BuildMetadata
from taskbundle.process import ProcessResult
from taskbundle.session import CommandSession

IMAGE_ID = "sha256:" + "b" * 64


def process_result(
    argv: Sequence[str],
    *,
    exit_code: int | None = 0,
    stdout: str = "",
    stderr: str = "",
    timed_out: bool = False,
) -> ProcessResult:
    return ProcessResult(
        argv=tuple(argv),
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.02,
        timed_out=timed_out,
    )


class ValidationRunner:
    def __init__(self, *, flaky: bool = False, patch_failure: bool = False) -> None:
        self.flaky = flaky
        self.patch_failure = patch_failure
        self.calls: list[tuple[str, ...]] = []
        self.container_phases: dict[str, str] = {}
        self.test_attempts: defaultdict[tuple[str, str], int] = defaultdict(int)

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout_seconds: float | None = None,
        environment: Mapping[str, str] | None = None,
        stdin: str | None = None,
    ) -> ProcessResult:
        del cwd, timeout_seconds, environment, stdin
        command = tuple(argv)
        self.calls.append(command)
        assert argv[0] == "docker"

        if argv[1:3] == ["image", "inspect"]:
            return process_result(argv, stdout=f"{IMAGE_ID}\n")
        if argv[1] == "create":
            name = argv[argv.index("--name") + 1]
            phase = "baseline" if "baseline" in name else "golden"
            container_id = f"{phase}-container"
            self.container_phases[container_id] = phase
            return process_result(argv, stdout=f"{container_id}\n")
        if argv[1] in {"cp", "start", "rm"}:
            return process_result(argv)
        if argv[1] != "exec":
            raise AssertionError(f"Unexpected Docker command: {command}")

        container_id = argv[4]
        phase = self.container_phases[container_id]
        inner = list(argv[5:])
        if inner[:3] == ["git", "apply", "--check"]:
            if self.patch_failure and phase == "baseline":
                return process_result(argv, exit_code=1, stderr="patch does not apply\n")
            return process_result(argv)
        if inner[:2] == ["git", "apply"]:
            return process_result(argv)

        test_command = inner[-1]
        test_name = "add" if "add_remains_available" in test_command else "subtract"
        key = (phase, test_name)
        self.test_attempts[key] += 1
        if phase == "baseline" and test_name == "subtract":
            return process_result(argv, exit_code=1, stderr="expected failure\n")
        if (
            self.flaky
            and phase == "baseline"
            and test_name == "add"
            and self.test_attempts[key] == 2
        ):
            return process_result(argv, exit_code=1, stderr="intermittent failure\n")
        return process_result(argv, stdout="ok\n")


def write_initialized_metadata(bundle: Bundle) -> None:
    fingerprint = build_fingerprint(bundle)
    metadata = BuildMetadata(
        fingerprint=fingerprint,
        bundle_id=bundle.manifest.id,
        repository_url=bundle.manifest.repository.url,
        repository_commit=bundle.manifest.repository.commit,
        dockerfile_sha256=sha256_file(bundle.dockerfile_path),
        image_tag=image_tag(bundle.manifest.id, fingerprint),
        image_id=IMAGE_ID,
        git_version="git version test",
        docker_client_version="test",
        docker_server_version="test",
        created_at=datetime.now(UTC),
    )
    path = bundle.root / ".taskbundle" / "cache" / fingerprint / "build.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(metadata.model_dump(mode="json"), indent=2) + "\n",
        encoding="utf-8",
    )


def start_validation_session(bundle: Bundle) -> CommandSession:
    session = CommandSession.start(command_name="validate", bundle_path=bundle.root)
    session.attach_bundle(bundle.manifest.id)
    return session


def test_validate_records_full_baseline_and_golden_matrix(valid_bundle_path: Path) -> None:
    bundle = load_bundle(valid_bundle_path)
    write_initialized_metadata(bundle)
    runner = ValidationRunner()
    session = start_validation_session(bundle)
    try:
        result = validate_task(bundle=bundle, session=session, runner=runner)
        session.succeed(result)
        rows = session.database.get_test_results(session.command_id)
        artifacts = session.database.get_artifacts(session.command_id)
    finally:
        session.close()

    assert result["valid"] is True
    assert result["attempt_count"] == 12
    assert len(rows) == 12
    assert result["mismatches"] == []
    assert result["flaky"] == []
    assert len([call for call in runner.calls if call[1] == "create"]) == 2
    assert len([call for call in runner.calls if call[1] == "rm"]) == 2
    assert len([artifact for artifact in artifacts if artifact["kind"] == "patch_log"]) == 6
    assert all((bundle.root / ".taskbundle" / row["log_artifact"]).is_file() for row in rows)


def test_validate_rejects_inconsistent_repeated_outcomes(valid_bundle_path: Path) -> None:
    bundle = load_bundle(valid_bundle_path)
    write_initialized_metadata(bundle)
    runner = ValidationRunner(flaky=True)
    session = start_validation_session(bundle)
    try:
        with pytest.raises(InvalidTaskError) as caught:
            validate_task(bundle=bundle, session=session, runner=runner)
        session.fail(caught.value)
        rows = session.database.get_test_results(session.command_id)
    finally:
        session.close()

    assert len(rows) == 12
    assert caught.value.details["valid"] is False
    assert caught.value.details["flaky"][0]["test_id"] == "add-remains-available"
    assert len([call for call in runner.calls if call[1] == "rm"]) == 2


def test_patch_preflight_failure_still_removes_evaluator(valid_bundle_path: Path) -> None:
    bundle = load_bundle(valid_bundle_path)
    write_initialized_metadata(bundle)
    runner = ValidationRunner(patch_failure=True)
    session = start_validation_session(bundle)
    try:
        with pytest.raises(InvalidTaskError, match="cannot be applied cleanly") as caught:
            validate_task(bundle=bundle, session=session, runner=runner)
        session.fail(caught.value)
        artifacts = session.database.get_artifacts(session.command_id)
    finally:
        session.close()

    removals = [call for call in runner.calls if call[1] == "rm"]
    assert removals == [("docker", "rm", "--force", "baseline-container")]
    assert len([artifact for artifact in artifacts if artifact["kind"] == "patch_log"]) == 1
