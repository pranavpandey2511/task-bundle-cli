"""Baseline and golden validation in isolated evaluator containers."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Any, Literal

from taskbundle.config import Bundle
from taskbundle.engine.docker import DockerClient
from taskbundle.errors import InfrastructureError, InvalidTaskError, SolverError, UnresolvedError
from taskbundle.lifecycle.initialize import require_initialized_build
from taskbundle.models import BuildMetadata, TestObservation, TestResult, TestSpec
from taskbundle.patches import validate_patch_contract
from taskbundle.process import ProcessResult, ProcessRunner, Runner
from taskbundle.provenance import sha256_text, write_execution_provenance
from taskbundle.session import CommandSession
from taskbundle.snapshots import capture_repository_snapshot, repository_snapshot_artifacts

Phase = Literal["baseline", "golden", "post_solver"]
Suite = Literal["pass_to_pass", "fail_to_pass"]
LifecycleErrorType = type[InvalidTaskError] | type[SolverError] | type[UnresolvedError]


@dataclass(frozen=True, slots=True)
class TestExecution:
    result: TestResult
    matches_expectation: bool


@dataclass(frozen=True, slots=True)
class BundleInputs:
    description: str
    gold_patch: str
    test_patch: str
    solver_view_patch: str
    sha256: dict[str, str]
    artifacts: dict[str, str]


def snapshot_bundle_inputs(*, bundle: Bundle, session: CommandSession) -> BundleInputs:
    """Read trusted inputs once so a run cannot observe mid-command mutations."""

    sources = (
        (
            "dockerfile",
            bundle.manifest.environment.dockerfile,
            bundle.dockerfile_path,
            "Dockerfile",
        ),
        ("description", "description.md", bundle.description_path, "description.md"),
        ("gold", bundle.manifest.patches.gold, bundle.gold_patch_path, "gold.patch"),
        ("tests", bundle.manifest.patches.tests, bundle.test_patch_path, "hidden.patch"),
        (
            "solver_view",
            bundle.manifest.patches.solver_view,
            bundle.solver_view_patch_path,
            "solver-view.patch",
        ),
    )
    content: dict[str, str] = {}
    artifacts: dict[str, str] = {}
    hashes: dict[str, str] = {}
    manifest_artifact = session.artifacts.write_text(
        command_id=session.command_id,
        relative_path="trusted-inputs/task.json",
        content=bundle.manifest_source,
        kind="trusted_input",
    )
    content["manifest"] = bundle.manifest_source
    hashes["task.json"] = sha256_text(bundle.manifest_source)
    artifacts["task.json"] = manifest_artifact.relative_to(session.state_dir).as_posix()
    for name, report_key, source, artifact_name in sources:
        try:
            value = source.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise InvalidTaskError(
                f"Could not snapshot trusted bundle input: {name}",
                details={"reason": str(error)},
            ) from error
        artifact = session.artifacts.write_text(
            command_id=session.command_id,
            relative_path=f"trusted-inputs/{artifact_name}",
            content=value,
            kind="trusted_input",
        )
        content[name] = value
        hashes[report_key] = sha256_text(value)
        artifacts[report_key] = artifact.relative_to(session.state_dir).as_posix()
    return BundleInputs(
        description=content["description"],
        gold_patch=content["gold"],
        test_patch=content["tests"],
        solver_view_patch=content["solver_view"],
        sha256=hashes,
        artifacts=artifacts,
    )


def verify_build_input_snapshot(
    *, bundle: Bundle, inputs: BundleInputs, metadata: BuildMetadata
) -> None:
    """Ensure the image was built from the same trusted bytes this command snapshotted."""

    expected = {
        bundle.manifest.environment.dockerfile: metadata.dockerfile_sha256,
        bundle.manifest.patches.solver_view: metadata.solver_view_sha256,
    }
    mismatches = {
        path: {"snapshot": inputs.sha256[path], "initialized": digest}
        for path, digest in expected.items()
        if inputs.sha256[path] != digest
    }
    if mismatches:
        raise InvalidTaskError(
            "Trusted build inputs changed while the command was starting.",
            hint="Restore the bundle inputs, rerun `task init`, and retry.",
            details={"mismatches": mismatches},
        )


def _expected_observation(phase: Phase, suite: Suite) -> TestObservation:
    if phase == "baseline" and suite == "fail_to_pass":
        return TestObservation.FAIL
    return TestObservation.PASS


def _observed(result: ProcessResult, test: TestSpec) -> TestObservation:
    if result.timed_out:
        return TestObservation.TIMEOUT
    if result.exit_code == 0:
        return TestObservation.PASS
    if result.exit_code in test.failure_exit_codes:
        return TestObservation.FAIL
    return TestObservation.ERROR


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
    trusted_path: str,
    error_type: LifecycleErrorType = InvalidTaskError,
) -> None:
    check = docker.exec_command(
        container_id=container_id,
        workdir=workdir,
        command=["git", "apply", "--check", container_patch_path],
        timeout_seconds=60,
        trusted_path=trusted_path,
    )
    check_log = session.artifacts.write_text(
        command_id=session.command_id,
        relative_path=f"patches/{artifact_name}-check.log",
        content=_test_log(check),
        kind="patch_log",
    )
    if not check.succeeded:
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
        trusted_path=trusted_path,
    )
    apply_log = session.artifacts.write_text(
        command_id=session.command_id,
        relative_path=f"patches/{artifact_name}-apply.log",
        content=_test_log(applied),
        kind="patch_log",
    )
    if not applied.succeeded:
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
    removed = docker.exec_command(
        container_id=container_id,
        workdir=workdir,
        command=["/bin/rm", "-f", "--", container_patch_path],
        timeout_seconds=60,
        trusted_path=trusted_path,
    )
    if not removed.succeeded:
        raise InfrastructureError(
            f"Could not remove the streamed {label} after application.",
            details={"exit_code": removed.exit_code, "stderr": removed.stderr[-4000:]},
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
    trusted_path: str,
) -> TestExecution:
    process = docker.exec_command(
        container_id=container_id,
        workdir=workdir,
        command=["/bin/sh", "-c", test.command],
        timeout_seconds=test.timeout_seconds,
        trusted_path=trusted_path,
    )
    observed = _observed(process, test)
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


def _evaluation_container_name(
    *,
    bundle_id: str,
    command_id: str,
    phase: Phase,
    suite: Suite,
    test_id: str,
    attempt: int,
) -> str:
    identity = f"{command_id}:{phase}:{suite}:{test_id}:{attempt}"
    suffix = hashlib.sha256(identity.encode()).hexdigest()[:10]
    return f"taskbundle-{bundle_id[:24]}-{phase}-{suffix}"[:63]


def _verify_test_marker(
    *,
    docker: DockerClient,
    container_id: str,
    workdir: str,
    test: TestSpec,
    phase: Phase,
    trusted_path: str,
) -> None:
    result = docker.exec_command(
        container_id=container_id,
        workdir=workdir,
        command=["grep", "--fixed-strings", "--quiet", "--", test.marker, test.path],
        timeout_seconds=60,
        trusted_path=trusted_path,
    )
    if result.succeeded:
        return
    error_type: LifecycleErrorType = UnresolvedError if phase == "post_solver" else InvalidTaskError
    raise error_type(
        f"Evaluator test marker is missing after test injection: {test.id}",
        details={
            "phase": phase,
            "test_id": test.id,
            "path": test.path,
            "exit_code": result.exit_code,
            "stderr": result.stderr[-4000:],
        },
    )


def _run_evaluation_attempt(
    *,
    bundle: Bundle,
    session: CommandSession,
    docker: DockerClient,
    image_ref: str,
    phase: Phase,
    suite: Suite,
    test: TestSpec,
    attempt: int,
    inputs: BundleInputs,
    candidate_patch: str | None,
) -> TestExecution:
    name = _evaluation_container_name(
        bundle_id=bundle.manifest.id,
        command_id=session.command_id,
        phase=phase,
        suite=suite,
        test_id=test.id,
        attempt=attempt,
    )
    container_id = docker.create_evaluator(
        image_tag=image_ref,
        container_name=name,
        workdir=bundle.manifest.environment.workdir,
        runtime=bundle.manifest.runtime,
        evaluator_path=bundle.manifest.environment.evaluator_path_value,
    )
    session.event(
        "info",
        phase,
        "Fresh evaluator container created for one test attempt.",
        {
            "container_name": name,
            "container_id": container_id,
            "suite": suite,
            "test_id": test.id,
            "attempt": attempt,
        },
    )
    artifact_prefix = f"{phase}-{suite}-{test.id}-{attempt}"
    trusted_path = bundle.manifest.environment.evaluator_path_value
    try:
        docker.start_detached(container_id)
        capture_repository_snapshot(
            docker=docker,
            session=session,
            container_id=container_id,
            workdir=bundle.manifest.environment.workdir,
            base_commit=bundle.manifest.repository.commit,
            phase=phase,
            stage=f"{suite}-{test.id}-{attempt}-pristine",
            require_pristine=True,
            trusted_path=trusted_path,
        )
        # Inject evaluator material while the repository is still pristine. In
        # particular, an untrusted candidate must never run or influence PATH,
        # Git attributes, or helpers before hidden bytes have been applied and
        # removed from /tmp.
        if inputs.test_patch.strip():
            docker.stream_text(
                content=inputs.test_patch,
                container_id=container_id,
                destination="/tmp/taskbundle-tests.patch",
            )
            hidden_error: LifecycleErrorType = (
                UnresolvedError if phase == "post_solver" else InvalidTaskError
            )
            _apply_patch(
                docker=docker,
                session=session,
                container_id=container_id,
                workdir=bundle.manifest.environment.workdir,
                container_patch_path="/tmp/taskbundle-tests.patch",
                label="hidden test patch",
                artifact_name=f"{artifact_prefix}-hidden-tests",
                trusted_path=trusted_path,
                error_type=hidden_error,
            )

        if phase == "golden":
            docker.stream_text(
                content=inputs.gold_patch,
                container_id=container_id,
                destination="/tmp/taskbundle-gold.patch",
            )
            _apply_patch(
                docker=docker,
                session=session,
                container_id=container_id,
                workdir=bundle.manifest.environment.workdir,
                container_patch_path="/tmp/taskbundle-gold.patch",
                label="gold patch",
                artifact_name=f"{artifact_prefix}-gold",
                trusted_path=trusted_path,
            )
        elif candidate_patch:
            docker.stream_text(
                content=candidate_patch,
                container_id=container_id,
                destination="/tmp/taskbundle-candidate.patch",
            )
            _apply_patch(
                docker=docker,
                session=session,
                container_id=container_id,
                workdir=bundle.manifest.environment.workdir,
                container_patch_path="/tmp/taskbundle-candidate.patch",
                label="candidate patch",
                artifact_name=f"{artifact_prefix}-candidate",
                trusted_path=trusted_path,
                error_type=SolverError,
            )

        _verify_test_marker(
            docker=docker,
            container_id=container_id,
            workdir=bundle.manifest.environment.workdir,
            test=test,
            phase=phase,
            trusted_path=trusted_path,
        )
        execution = _execute_test(
            docker=docker,
            session=session,
            container_id=container_id,
            workdir=bundle.manifest.environment.workdir,
            phase=phase,
            suite=suite,
            test=test,
            attempt=attempt,
            trusted_path=trusted_path,
        )
        if phase != "post_solver" and execution.result.observed in {
            TestObservation.TIMEOUT,
            TestObservation.ERROR,
        }:
            raise InvalidTaskError(
                f"Test did not produce a valid pass/fail result: {phase} {suite} {test.id}",
                hint="Inspect the test log and fix its command, dependencies, or timeout.",
                details={
                    "phase": phase,
                    "suite": suite,
                    "test_id": test.id,
                    "attempt": attempt,
                    "observed": execution.result.observed.value,
                    "exit_code": execution.result.exit_code,
                    "log_artifact": execution.result.log_artifact,
                },
            )
        return execution
    finally:
        docker.remove_container(container_id)
        session.event(
            "info",
            "cleanup",
            "Evaluator attempt container removed.",
            {
                "phase": phase,
                "suite": suite,
                "test_id": test.id,
                "attempt": attempt,
                "container_id": container_id,
            },
        )


def run_evaluation_phase(
    *,
    bundle: Bundle,
    session: CommandSession,
    docker: DockerClient,
    image_ref: str,
    phase: Phase,
    repetitions: int,
    inputs: BundleInputs,
    candidate_patch: str | None = None,
) -> list[TestExecution]:
    executions: list[TestExecution] = []
    suites: tuple[tuple[Suite, list[TestSpec]], ...] = (
        ("pass_to_pass", bundle.manifest.tests.pass_to_pass),
        ("fail_to_pass", bundle.manifest.tests.fail_to_pass),
    )
    for suite, tests in suites:
        for test in tests:
            for attempt in range(1, repetitions + 1):
                executions.append(
                    _run_evaluation_attempt(
                        bundle=bundle,
                        session=session,
                        docker=docker,
                        image_ref=image_ref,
                        phase=phase,
                        suite=suite,
                        test=test,
                        attempt=attempt,
                        inputs=inputs,
                        candidate_patch=candidate_patch,
                    )
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
    docker.versions()
    if docker.readiness.auto_started:
        session.event(
            "info",
            "docker",
            "Started the configured Colima Docker daemon automatically.",
            {"profile": docker.readiness.profile},
        )
    inputs = snapshot_bundle_inputs(bundle=bundle, session=session)
    patch_contract = validate_patch_contract(
        bundle=bundle,
        gold_patch=inputs.gold_patch,
        test_patch=inputs.test_patch,
        solver_view_patch=inputs.solver_view_patch,
    )
    metadata = require_initialized_build(
        bundle=bundle,
        state_dir=session.state_dir,
        docker=docker,
        dockerfile_sha256=inputs.sha256[bundle.manifest.environment.dockerfile],
        solver_view_sha256=inputs.sha256[bundle.manifest.patches.solver_view],
    )
    verify_build_input_snapshot(bundle=bundle, inputs=inputs, metadata=metadata)
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
        input_hashes=inputs.sha256,
    )

    executions: list[TestExecution] = []
    for phase in ("baseline", "golden"):
        executions.extend(
            run_evaluation_phase(
                bundle=bundle,
                session=session,
                docker=docker,
                image_ref=metadata.image_id,
                phase=phase,
                repetitions=selected_repetitions,
                inputs=inputs,
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
        "trusted_inputs": {"sha256": inputs.sha256, "artifacts": inputs.artifacts},
        "patch_contract": patch_contract,
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
