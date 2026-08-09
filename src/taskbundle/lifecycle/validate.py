"""Baseline and golden validation in isolated evaluator containers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from taskbundle.config import Bundle
from taskbundle.engine.docker import DockerClient
from taskbundle.errors import InvalidTaskError, SolverError
from taskbundle.lifecycle.initialize import container_name, require_initialized_build
from taskbundle.models import TestObservation, TestResult, TestSpec
from taskbundle.process import ProcessResult, ProcessRunner, Runner
from taskbundle.provenance import write_execution_provenance
from taskbundle.session import CommandSession
from taskbundle.snapshots import capture_repository_snapshot, repository_snapshot_artifacts

Phase = Literal["baseline", "golden", "post_solver"]
Suite = Literal["pass_to_pass", "fail_to_pass"]


@dataclass(frozen=True, slots=True)
class TestExecution:
    result: TestResult
    matches_expectation: bool


def _expected_observation(phase: Phase, suite: Suite) -> TestObservation:
    if phase == "baseline" and suite == "fail_to_pass":
        return TestObservation.FAIL
    return TestObservation.PASS


def _observed(result: ProcessResult) -> TestObservation:
    if result.timed_out:
        return TestObservation.TIMEOUT
    if result.exit_code == 0:
        return TestObservation.PASS
    return TestObservation.FAIL


def _test_log(result: ProcessResult) -> str:
    return (
        f"command={' '.join(result.argv)}\n"
        f"exit={result.exit_code} timeout={result.timed_out} "
        f"duration={result.duration_seconds:.3f}s\n"
        f"--- stdout ---\n{result.stdout}"
        f"--- stderr ---\n{result.stderr}"
    )


def _apply_patch(
    *,
    docker: DockerClient,
    session: CommandSession,
    container_id: str,
    workdir: str,
    container_patch_path: str,
    label: str,
    artifact_name: str,
    solver_owned: bool = False,
) -> None:
    check = docker.exec_command(
        container_id=container_id,
        workdir=workdir,
        command=["git", "apply", "--check", container_patch_path],
        timeout_seconds=60,
    )
    check_log = session.artifacts.write_text(
        command_id=session.command_id,
        relative_path=f"patches/{artifact_name}-check.log",
        content=_test_log(check),
        kind="patch_log",
    )
    if not check.succeeded:
        error_type = SolverError if solver_owned else InvalidTaskError
        raise error_type(
            f"The {label} cannot be applied cleanly to the initialized repository.",
            hint="Regenerate the patch against the configured base commit.",
            details={
                "exit_code": check.exit_code,
                "timed_out": check.timed_out,
                "stdout": check.stdout[-4000:],
                "stderr": check.stderr[-4000:],
                "log_artifact": check_log.relative_to(session.state_dir).as_posix(),
            },
        )
    applied = docker.exec_command(
        container_id=container_id,
        workdir=workdir,
        command=["git", "apply", container_patch_path],
        timeout_seconds=60,
    )
    apply_log = session.artifacts.write_text(
        command_id=session.command_id,
        relative_path=f"patches/{artifact_name}-apply.log",
        content=_test_log(applied),
        kind="patch_log",
    )
    if not applied.succeeded:
        error_type = SolverError if solver_owned else InvalidTaskError
        raise error_type(
            f"The {label} passed preflight but failed during application.",
            details={
                "exit_code": applied.exit_code,
                "timed_out": applied.timed_out,
                "stdout": applied.stdout[-4000:],
                "stderr": applied.stderr[-4000:],
                "log_artifact": apply_log.relative_to(session.state_dir).as_posix(),
            },
        )


def _execute_test(
    *,
    docker: DockerClient,
    session: CommandSession,
    container_id: str,
    workdir: str,
    phase: Phase,
    suite: Suite,
    test: TestSpec,
    attempt: int,
) -> TestExecution:
    process = docker.exec_command(
        container_id=container_id,
        workdir=workdir,
        command=["/bin/sh", "-lc", test.command],
        timeout_seconds=test.timeout_seconds,
    )
    observed = _observed(process)
    expected = _expected_observation(phase, suite)
    relative_log = f"tests/{phase}-{suite}-{test.id}-{attempt}.log"
    log_path = session.artifacts.write_text(
        command_id=session.command_id,
        relative_path=relative_log,
        content=_test_log(process),
        kind="test_log",
    )
    stored_path = log_path.relative_to(session.state_dir).as_posix()
    result = TestResult(
        phase=phase,
        suite=suite,
        test_id=test.id,
        attempt=attempt,
        expected=expected,
        observed=observed,
        exit_code=process.exit_code,
        duration_seconds=process.duration_seconds,
        log_artifact=stored_path,
    )
    session.database.add_test_result(command_id=session.command_id, result=result)
    matches = observed == expected
    session.event(
        "info" if matches else "warning",
        "test",
        f"{phase} {suite} {test.id} attempt {attempt}: {observed.value}",
        {
            "expected": expected.value,
            "observed": observed.value,
            "matches": matches,
            "log_artifact": stored_path,
        },
    )
    return TestExecution(result=result, matches_expectation=matches)


def run_evaluation_phase(
    *,
    bundle: Bundle,
    session: CommandSession,
    docker: DockerClient,
    image_tag: str,
    phase: Phase,
    repetitions: int,
    candidate_patch: Path | None = None,
) -> list[TestExecution]:
    name = container_name(bundle.manifest.id, f"{session.command_id}-{phase}")
    container_id = docker.create_evaluator(
        image_tag=image_tag,
        container_name=name,
        workdir=bundle.manifest.environment.workdir,
        runtime=bundle.manifest.runtime,
    )
    session.event(
        "info",
        phase,
        "Evaluator container created.",
        {
            "container_name": name,
            "container_id": container_id,
            "isolation": docker.isolation_profile(
                runtime=bundle.manifest.runtime,
                workdir=bundle.manifest.environment.workdir,
                network="none",
            ),
        },
    )
    executions: list[TestExecution] = []
    try:
        docker.start_detached(container_id)
        capture_repository_snapshot(
            docker=docker,
            session=session,
            container_id=container_id,
            workdir=bundle.manifest.environment.workdir,
            base_commit=bundle.manifest.repository.commit,
            phase=phase,
            stage="pristine",
            require_pristine=True,
        )
        if phase == "golden":
            docker.stream_file(
                source=bundle.gold_patch_path,
                container_id=container_id,
                destination="/tmp/taskbundle-gold.patch",
            )
        elif candidate_patch is not None:
            docker.stream_file(
                source=candidate_patch,
                container_id=container_id,
                destination="/tmp/taskbundle-candidate.patch",
            )
        docker.stream_file(
            source=bundle.test_patch_path,
            container_id=container_id,
            destination="/tmp/taskbundle-tests.patch",
        )
        if phase == "golden":
            _apply_patch(
                docker=docker,
                session=session,
                container_id=container_id,
                workdir=bundle.manifest.environment.workdir,
                container_patch_path="/tmp/taskbundle-gold.patch",
                label="gold patch",
                artifact_name=f"{phase}-gold",
            )
        elif candidate_patch is not None:
            _apply_patch(
                docker=docker,
                session=session,
                container_id=container_id,
                workdir=bundle.manifest.environment.workdir,
                container_patch_path="/tmp/taskbundle-candidate.patch",
                label="candidate patch",
                artifact_name=f"{phase}-candidate",
                solver_owned=True,
            )
        if phase == "golden" or candidate_patch is not None:
            capture_repository_snapshot(
                docker=docker,
                session=session,
                container_id=container_id,
                workdir=bundle.manifest.environment.workdir,
                base_commit=bundle.manifest.repository.commit,
                phase=phase,
                stage="solution-applied",
            )
        _apply_patch(
            docker=docker,
            session=session,
            container_id=container_id,
            workdir=bundle.manifest.environment.workdir,
            container_patch_path="/tmp/taskbundle-tests.patch",
            label="hidden test patch",
            artifact_name=f"{phase}-hidden-tests",
        )
        capture_repository_snapshot(
            docker=docker,
            session=session,
            container_id=container_id,
            workdir=bundle.manifest.environment.workdir,
            base_commit=bundle.manifest.repository.commit,
            phase=phase,
            stage="hidden-tests-applied",
        )

        suites: tuple[tuple[Suite, list[TestSpec]], ...] = (
            ("pass_to_pass", bundle.manifest.tests.pass_to_pass),
            ("fail_to_pass", bundle.manifest.tests.fail_to_pass),
        )
        for suite, tests in suites:
            for test in tests:
                for attempt in range(1, repetitions + 1):
                    execution = _execute_test(
                        docker=docker,
                        session=session,
                        container_id=container_id,
                        workdir=bundle.manifest.environment.workdir,
                        phase=phase,
                        suite=suite,
                        test=test,
                        attempt=attempt,
                    )
                    executions.append(execution)
                    if execution.result.observed == TestObservation.TIMEOUT:
                        raise InvalidTaskError(
                            f"Test timed out: {phase} {suite} {test.id}",
                            hint=(
                                "Inspect its log and adjust timeout_seconds only if the test "
                                "is healthy."
                            ),
                            details={
                                "phase": phase,
                                "suite": suite,
                                "test_id": test.id,
                                "attempt": attempt,
                                "log_artifact": execution.result.log_artifact,
                            },
                        )
        capture_repository_snapshot(
            docker=docker,
            session=session,
            container_id=container_id,
            workdir=bundle.manifest.environment.workdir,
            base_commit=bundle.manifest.repository.commit,
            phase=phase,
            stage="after-tests",
        )
    finally:
        docker.remove_container(container_id)
        session.event(
            "info",
            "cleanup",
            "Evaluator container removed.",
            {"phase": phase, "container_id": container_id},
        )
    return executions


def summarize_executions(executions: list[TestExecution]) -> dict[str, Any]:
    tests: dict[tuple[str, str, str], list[TestExecution]] = {}
    for execution in executions:
        result = execution.result
        key = (result.phase, result.suite, result.test_id)
        tests.setdefault(key, []).append(execution)

    phases: dict[str, dict[str, list[dict[str, Any]]]] = {}
    mismatches: list[dict[str, Any]] = []
    flaky: list[dict[str, Any]] = []
    for (phase, suite, test_id), attempts in sorted(tests.items()):
        observations = [attempt.result.observed.value for attempt in attempts]
        stable = len(set(observations)) == 1
        matches = all(attempt.matches_expectation for attempt in attempts)
        aggregate = {
            "test_id": test_id,
            "expected": attempts[0].result.expected.value,
            "observations": observations,
            "stable": stable,
            "matches": matches,
        }
        phases.setdefault(phase, {}).setdefault(suite, []).append(aggregate)
        if not stable:
            flaky.append({"phase": phase, "suite": suite, **aggregate})
        if not matches:
            mismatches.append({"phase": phase, "suite": suite, **aggregate})
    return {
        "valid": not mismatches and not flaky,
        "phases": phases,
        "mismatches": mismatches,
        "flaky": flaky,
        "attempt_count": len(executions),
    }


def validate_task(
    *,
    bundle: Bundle,
    session: CommandSession,
    repetitions: int | None = None,
    runner: Runner | None = None,
) -> dict[str, Any]:
    selected_repetitions = (
        bundle.manifest.validation.repetitions if repetitions is None else repetitions
    )
    if not 1 <= selected_repetitions <= 20:
        raise InvalidTaskError("Validation repetitions must be between 1 and 20.")

    process_runner = runner or ProcessRunner()
    docker = DockerClient(
        process_runner,
        executable=os.environ.get("TASKBUNDLE_DOCKER_BIN", "docker"),
    )
    metadata = require_initialized_build(
        bundle=bundle,
        state_dir=session.state_dir,
        docker=docker,
    )
    session.event(
        "info",
        "validation",
        "Initialized image verified.",
        {"image_tag": metadata.image_tag, "image_id": metadata.image_id},
    )
    provenance = write_execution_provenance(
        bundle=bundle,
        metadata=metadata,
        session=session,
        command="validate",
        repetitions=selected_repetitions,
    )

    executions: list[TestExecution] = []
    for phase in ("baseline", "golden"):
        executions.extend(
            run_evaluation_phase(
                bundle=bundle,
                session=session,
                docker=docker,
                image_tag=metadata.image_tag,
                phase=phase,
                repetitions=selected_repetitions,
            )
        )
    summary = summarize_executions(executions)
    result = {
        "bundle": str(bundle.root),
        "bundle_id": bundle.manifest.id,
        "image_tag": metadata.image_tag,
        "image_id": metadata.image_id,
        "repetitions": selected_repetitions,
        "provenance": provenance,
        "isolation": docker.isolation_profile(
            runtime=bundle.manifest.runtime,
            workdir=bundle.manifest.environment.workdir,
            network="none",
        ),
        "snapshot_artifacts": repository_snapshot_artifacts(session),
        **summary,
    }
    validation_artifact = session.artifacts.write_json(
        command_id=session.command_id,
        relative_path="validation.json",
        payload=result,
        kind="validation_report",
    )
    result["validation_artifact"] = str(validation_artifact)
    if not summary["valid"]:
        raise InvalidTaskError(
            "Bundle validation did not match the baseline/golden truth table.",
            hint=f"Inspect {validation_artifact} and the per-test logs.",
            details=result,
        )
    return result
