from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from taskbundle.config import Bundle, load_bundle
from taskbundle.errors import InvalidTaskError
from taskbundle.lifecycle.initialize import (
    build_fingerprint,
    image_tag,
    sha256_file,
    solver_secrecy_contract_sha256,
)
from taskbundle.lifecycle.validate import snapshot_bundle_inputs, validate_task
from taskbundle.models import BuildMetadata
from taskbundle.process import ProcessResult
from taskbundle.session import CommandSession

IMAGE_ID = "sha256:" + "b" * 64


def test_trusted_manifest_snapshot_is_the_exact_source_that_was_parsed(
    valid_bundle_path: Path,
) -> None:
    bundle = load_bundle(valid_bundle_path)
    parsed_source = bundle.manifest_source
    bundle.manifest_path.write_text('{"changed": true}\n', encoding="utf-8")
    session = CommandSession.start(command_name="validate", bundle_path=valid_bundle_path)
    try:
        inputs = snapshot_bundle_inputs(bundle=bundle, session=session)
    finally:
        session.close()

    artifact = valid_bundle_path / ".taskbundle" / inputs.artifacts["task.json"]
    assert artifact.read_text(encoding="utf-8") == parsed_source


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
    def __init__(
        self,
        *,
        base_commit: str,
        flaky: bool = False,
        patch_failure: bool = False,
        dirty_pristine: bool = False,
        invalid_failure_exit: bool = False,
    ) -> None:
        self.base_commit = base_commit
        self.flaky = flaky
        self.patch_failure = patch_failure
        self.dirty_pristine = dirty_pristine
        self.invalid_failure_exit = invalid_failure_exit
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
        if argv[1:3] == ["exec", "--interactive"]:
            return process_result(argv)
        if argv[1] in {"start", "rm"}:
            return process_result(argv)
        if argv[1] != "exec":
            raise AssertionError(f"Unexpected Docker command: {command}")

        position = 2
        while argv[position] in {"--workdir", "--env"}:
            position += 2
        container_id = argv[position]
        phase = self.container_phases[container_id]
        inner = list(argv[position + 1 :])
        if inner[:3] == ["git", "rev-parse", "HEAD"]:
            return process_result(argv, stdout=f"{self.base_commit}\n")
        if inner[:2] == ["git", "status"]:
            status = " M calculator.py\n" if self.dirty_pristine else ""
            return process_result(argv, stdout=status)
        if inner[:3] == ["git", "diff", "--stat"]:
            return process_result(argv, stdout="calculator.py | 2 +-\n")
        if inner[:3] == ["git", "apply", "--check"]:
            if self.patch_failure and phase == "baseline":
                return process_result(argv, exit_code=1, stderr="patch does not apply\n")
            return process_result(argv)
        if inner[:2] == ["git", "apply"]:
            return process_result(argv)
        if inner[:1] == ["/bin/rm"]:
            return process_result(argv)
        if inner and inner[0] == "grep":
            return process_result(argv)

        test_command = inner[-1]
        test_name = "add" if "test_add" in test_command else "subtract"
        key = (phase, test_name)
        self.test_attempts[key] += 1
        if phase == "baseline" and test_name == "subtract":
            exit_code = 127 if self.invalid_failure_exit else 1
            return process_result(argv, exit_code=exit_code, stderr="expected failure\n")
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
        solver_view_sha256=sha256_file(bundle.solver_view_patch_path),
        secrecy_contract_sha256=solver_secrecy_contract_sha256(bundle),
        image_tag=image_tag(bundle.manifest.id, fingerprint),
        image_id=IMAGE_ID,
        solver_image_tag=f"taskbundle/{bundle.manifest.id}-solver:{fingerprint[:16]}",
        solver_image_id=IMAGE_ID,
        solver_base_commit="e" * 40,
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
    runner = ValidationRunner(base_commit=bundle.manifest.repository.commit)
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
    assert len([call for call in runner.calls if call[1] == "create"]) == 12
    assert len([call for call in runner.calls if call[1] == "rm"]) == 12
    assert len([artifact for artifact in artifacts if artifact["kind"] == "patch_log"]) == 36
    assert (
        len([artifact for artifact in artifacts if artifact["kind"] == "repository_snapshot"]) == 12
    )
    assert (
        len([artifact for artifact in artifacts if artifact["kind"] == "execution_provenance"]) == 1
    )
    assert len(result["snapshot_artifacts"]) == 12
    assert all((bundle.root / ".taskbundle" / row["log_artifact"]).is_file() for row in rows)


def test_validate_rejects_inconsistent_repeated_outcomes(valid_bundle_path: Path) -> None:
    bundle = load_bundle(valid_bundle_path)
    write_initialized_metadata(bundle)
    runner = ValidationRunner(base_commit=bundle.manifest.repository.commit, flaky=True)
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
    assert len([call for call in runner.calls if call[1] == "rm"]) == 12


def test_patch_preflight_failure_still_removes_evaluator(valid_bundle_path: Path) -> None:
    bundle = load_bundle(valid_bundle_path)
    write_initialized_metadata(bundle)
    runner = ValidationRunner(base_commit=bundle.manifest.repository.commit, patch_failure=True)
    session = start_validation_session(bundle)
    try:
        with pytest.raises(InvalidTaskError, match="cannot be applied cleanly") as caught:
            validate_task(bundle=bundle, session=session, runner=runner)
        session.fail(caught.value)
        artifacts = session.database.get_artifacts(session.command_id)
    finally:
        session.close()

    removals = [call for call in runner.calls if call[1] == "rm"]
    assert removals == [("docker", "rm", "--force", "--volumes", "baseline-container")]
    assert len([artifact for artifact in artifacts if artifact["kind"] == "patch_log"]) == 1


def test_validate_rejects_a_dirty_initialized_repository(valid_bundle_path: Path) -> None:
    bundle = load_bundle(valid_bundle_path)
    write_initialized_metadata(bundle)
    runner = ValidationRunner(
        base_commit=bundle.manifest.repository.commit,
        dirty_pristine=True,
    )
    session = start_validation_session(bundle)
    try:
        with pytest.raises(InvalidTaskError, match="not pristine") as caught:
            validate_task(bundle=bundle, session=session, runner=runner)
        session.fail(caught.value)
    finally:
        session.close()

    assert caught.value.details["dirty"] is True
    assert caught.value.details["status"] == [" M calculator.py"]
    assert caught.value.details["snapshot_artifact"].endswith(
        "baseline-pass_to_pass-add-remains-available-1-pristine.json"
    )
    assert [call for call in runner.calls if call[1] == "rm"] == [
        ("docker", "rm", "--force", "--volumes", "baseline-container")
    ]


def test_validate_does_not_accept_a_missing_test_command_as_fail_to_pass(
    valid_bundle_path: Path,
) -> None:
    bundle = load_bundle(valid_bundle_path)
    write_initialized_metadata(bundle)
    runner = ValidationRunner(
        base_commit=bundle.manifest.repository.commit,
        invalid_failure_exit=True,
    )
    session = start_validation_session(bundle)
    try:
        with pytest.raises(InvalidTaskError, match="valid pass/fail") as caught:
            validate_task(bundle=bundle, session=session, runner=runner, repetitions=1)
        session.fail(caught.value)
        rows = session.database.get_test_results(session.command_id)
    finally:
        session.close()

    error = next(row for row in rows if row["observed"] == "error")
    assert error["test_id"] == "subtracts"
    assert error["exit_code"] == 127
