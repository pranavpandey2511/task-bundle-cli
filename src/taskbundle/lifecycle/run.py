"""Solver execution, patch capture, and fresh-container grading."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from taskbundle.agent_config import (
    DEFAULT_AGENT_MAX_STEPS,
    DEFAULT_OPENROUTER_API_KEY_ENV,
    resolve_agent_settings,
)
from taskbundle.config import Bundle
from taskbundle.engine.docker import DockerClient
from taskbundle.errors import (
    ConfigurationError,
    InvalidTaskError,
    SolverError,
    TaskBundleError,
    UnresolvedError,
)
from taskbundle.lifecycle.initialize import container_name, require_initialized_build
from taskbundle.lifecycle.validate import (
    run_evaluation_phase,
    snapshot_bundle_inputs,
    summarize_executions,
    verify_build_input_snapshot,
)
from taskbundle.models import EvaluatorIsolation
from taskbundle.patches import PatchFormatError, changed_paths_from_patch, validate_patch_contract
from taskbundle.process import ProcessRunner, Runner
from taskbundle.provenance import sha256_text, write_execution_provenance
from taskbundle.session import CommandSession
from taskbundle.snapshots import capture_repository_snapshot, repository_snapshot_artifacts
from taskbundle.solvers import AgentSolver, PatchSolver, Solver, SolverContext, StubSolver


def _solver_for(
    *,
    name: str,
    candidate_patch: Path | None,
    max_patch_bytes: int,
    description: str,
    allowed_paths: list[str],
    disallowed_paths: list[str],
    agent_model: str | None,
    agent_api_key_env: str,
    agent_env_file: Path,
    agent_max_steps: int,
) -> tuple[Solver, str | None, Path | None]:
    if name in {"patch", "stub"}:
        unsupported_agent_options: list[str] = []
        if agent_model is not None:
            unsupported_agent_options.append("--model")
        if agent_api_key_env != DEFAULT_OPENROUTER_API_KEY_ENV:
            unsupported_agent_options.append("--api-key-env")
        if agent_env_file != Path(".env"):
            unsupported_agent_options.append("--env-file")
        if agent_max_steps != DEFAULT_AGENT_MAX_STEPS:
            unsupported_agent_options.append("--agent-max-steps")
        if unsupported_agent_options:
            raise ConfigurationError(
                f"The {name} solver does not accept OpenRouter agent options.",
                hint="Remove the agent-only options or use `--solver agent`.",
                details={"unsupported_options": unsupported_agent_options},
            )
    if name == "stub":
        if candidate_patch is not None:
            raise ConfigurationError("The stub solver does not accept a candidate patch.")
        return StubSolver(), None, None
    if name == "agent":
        if candidate_patch is not None:
            raise ConfigurationError("The agent solver does not accept --candidate-patch.")
        settings = resolve_agent_settings(
            model=agent_model,
            api_key_env=agent_api_key_env,
            env_file=agent_env_file,
            max_steps=agent_max_steps,
        )
        return (
            AgentSolver(
                settings=settings,
                description=description,
                allowed_paths=allowed_paths,
                disallowed_paths=disallowed_paths,
            ),
            None,
            None,
        )
    if name == "patch":
        if candidate_patch is None:
            raise ConfigurationError("The patch solver requires --candidate-patch.")
        resolved = candidate_patch.expanduser().resolve()
        if not resolved.is_file():
            raise ConfigurationError(f"Candidate patch does not exist: {resolved}")
        try:
            size = resolved.stat().st_size
            if size > max_patch_bytes:
                raise ConfigurationError(
                    "Candidate patch exceeds runtime.max_patch_bytes.",
                    details={"patch_bytes": size, "max_patch_bytes": max_patch_bytes},
                )
            content = resolved.read_text(encoding="utf-8")
        except ConfigurationError:
            raise
        except (OSError, UnicodeError) as error:
            raise ConfigurationError(f"Could not read candidate patch: {resolved}") from error
        return PatchSolver(content), content, resolved
    raise ConfigurationError(
        f"Unknown solver adapter: {name}",
        hint="Choose one of: agent, patch, stub.",
    )


def _capture_patch(
    *,
    docker: DockerClient,
    session: CommandSession,
    container_id: str,
    workdir: str,
    base_commit: str,
    max_patch_bytes: int,
    trusted_path: str,
) -> tuple[Path, int, str, str, str]:
    status = docker.exec_command(
        container_id=container_id,
        workdir=workdir,
        command=["git", "status", "--short", "--untracked-files=all"],
        timeout_seconds=60,
        trusted_path=trusted_path,
    )
    if not status.succeeded:
        raise SolverError(
            "Could not inspect the solver's repository changes.",
            details={"exit_code": status.exit_code, "stderr": status.stderr[-4000:]},
        )
    status_path = session.artifacts.write_text(
        command_id=session.command_id,
        relative_path="repository-status.txt",
        content=status.stdout,
        kind="repository_status",
    )

    staged = docker.exec_command(
        container_id=container_id,
        workdir=workdir,
        command=["git", "add", "-A"],
        timeout_seconds=60,
        trusted_path=trusted_path,
    )
    if not staged.succeeded:
        raise SolverError(
            "Could not stage the solver's repository changes for patch capture.",
            details={"exit_code": staged.exit_code, "stderr": staged.stderr[-4000:]},
        )

    diff = docker.exec_command(
        container_id=container_id,
        workdir=workdir,
        command=[
            "git",
            "diff",
            "--cached",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            base_commit,
            "--",
        ],
        timeout_seconds=60,
        trusted_path=trusted_path,
    )
    if not diff.succeeded:
        raise SolverError(
            "Could not capture the solver patch.",
            details={"exit_code": diff.exit_code, "stderr": diff.stderr[-4000:]},
        )
    patch_bytes = len(diff.stdout.encode("utf-8"))
    if patch_bytes > max_patch_bytes:
        raise SolverError(
            "The solver patch exceeds the configured size limit.",
            details={"patch_bytes": patch_bytes, "max_patch_bytes": max_patch_bytes},
        )
    patch_path = session.artifacts.write_text(
        command_id=session.command_id,
        relative_path="solver.patch",
        content=diff.stdout,
        kind="solver_patch",
    )
    return (
        patch_path,
        patch_bytes,
        status_path.relative_to(session.state_dir).as_posix(),
        diff.stdout,
        sha256_text(diff.stdout),
    )


def _write_run_report(session: CommandSession, result: dict[str, Any]) -> Path:
    result["run_artifact"] = f"commands/{session.command_id}/run.json"
    path = session.artifacts.write_json(
        command_id=session.command_id,
        relative_path="run.json",
        payload=result,
        kind="run_report",
    )
    return path


def _verify_solver_view(
    *,
    bundle: Bundle,
    session: CommandSession,
    docker: DockerClient,
    container_id: str,
    solver_base_commit: str,
) -> str:
    workdir = bundle.manifest.environment.workdir
    capture_repository_snapshot(
        docker=docker,
        session=session,
        container_id=container_id,
        workdir=workdir,
        base_commit=solver_base_commit,
        phase="solver",
        stage="sanitized-pristine",
        require_pristine=True,
        trusted_path=bundle.manifest.environment.evaluator_path_value,
    )
    session.event(
        "info",
        "solver",
        "Immutable sanitized solver image and pristine workspace verified.",
        {"solver_base_commit": solver_base_commit},
    )
    return solver_base_commit


def _candidate_paths(content: str) -> set[str]:
    try:
        return changed_paths_from_patch(content)
    except PatchFormatError as error:
        raise SolverError(
            "The captured solver output is not a valid Git-style patch.",
            details={"reason": str(error)},
        ) from error


def run_task(
    *,
    bundle: Bundle,
    session: CommandSession,
    solver_name: str = "agent",
    candidate_patch: Path | None = None,
    allow_network: bool = False,
    agent_model: str | None = None,
    agent_api_key_env: str = DEFAULT_OPENROUTER_API_KEY_ENV,
    agent_env_file: Path = Path(".env"),
    agent_max_steps: int = DEFAULT_AGENT_MAX_STEPS,
    repetitions: int | None = None,
    evaluator_isolation: EvaluatorIsolation | None = None,
    runner: Runner | None = None,
    solver_override: Solver | None = None,
) -> dict[str, Any]:
    selected_repetitions = (
        bundle.manifest.validation.repetitions if repetitions is None else repetitions
    )
    selected_evaluator_isolation = (
        bundle.manifest.validation.evaluator_isolation
        if evaluator_isolation is None
        else evaluator_isolation
    )
    if not 1 <= selected_repetitions <= 20:
        raise ConfigurationError("Run repetitions must be between 1 and 20.")
    if allow_network:
        raise ConfigurationError(
            "Direct solver-container network access is disabled by the strict "
            "test-secrecy contract.",
            hint=(
                "The agent reaches OpenRouter from the trusted host control plane; it does not "
                "need --allow-network."
            ),
        )

    inputs = snapshot_bundle_inputs(bundle=bundle, session=session)
    patch_contract = validate_patch_contract(
        bundle=bundle,
        gold_patch=inputs.gold_patch,
        test_patch=inputs.test_patch,
        solver_view_patch=inputs.solver_view_patch,
    )
    description_conflicts = [
        {"test_id": test.id, "path": test.path}
        for test in bundle.manifest.tests.pass_to_pass + bundle.manifest.tests.fail_to_pass
        if test.marker in inputs.description
    ]
    if description_conflicts:
        raise InvalidTaskError(
            "Evaluator test markers are visible in the solver description.",
            hint="Remove evaluator-test source from description.md.",
            details={"conflicts": description_conflicts},
        )
    if solver_override is not None:
        if candidate_patch is not None:
            raise ConfigurationError("A solver override cannot be combined with a candidate patch.")
        solver = solver_override
        candidate_input = None
        candidate_input_path = None
    else:
        solver, candidate_input, candidate_input_path = _solver_for(
            name=solver_name,
            candidate_patch=candidate_patch,
            max_patch_bytes=bundle.manifest.runtime.max_patch_bytes,
            description=inputs.description,
            allowed_paths=bundle.manifest.candidate.allowed_patch_paths,
            disallowed_paths=bundle.manifest.candidate.disallowed_patch_paths,
            agent_model=agent_model,
            agent_api_key_env=agent_api_key_env,
            agent_env_file=agent_env_file,
            agent_max_steps=agent_max_steps,
        )
    effective_network = False

    candidate_input_artifact: str | None = None
    if candidate_input is not None:
        artifact = session.artifacts.write_text(
            command_id=session.command_id,
            relative_path="candidate-input.patch",
            content=candidate_input,
            kind="candidate_input",
        )
        candidate_input_artifact = artifact.relative_to(session.state_dir).as_posix()

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

    metadata = require_initialized_build(
        bundle=bundle,
        state_dir=session.state_dir,
        docker=docker,
        dockerfile_sha256=inputs.sha256[bundle.manifest.environment.dockerfile],
        solver_view_sha256=inputs.sha256[bundle.manifest.patches.solver_view],
    )
    verify_build_input_snapshot(bundle=bundle, inputs=inputs, metadata=metadata)
    solver_provenance: dict[str, Any] = {
        "adapter": solver.name,
        "network": "bridge" if effective_network else "none",
    }
    if isinstance(solver, AgentSolver):
        solver_provenance.update(solver.provenance)
    if candidate_input is not None:
        solver_provenance["candidate_patch_sha256"] = sha256_text(candidate_input)
        solver_provenance["candidate_patch_source"] = str(candidate_input_path)
    provenance = write_execution_provenance(
        bundle=bundle,
        metadata=metadata,
        session=session,
        command="run",
        repetitions=selected_repetitions,
        evaluator_isolation=selected_evaluator_isolation,
        solver=solver_provenance,
        input_hashes=inputs.sha256,
    )
    session.event(
        "info",
        "preflight",
        "Initialized image verified; starting baseline preflight.",
        {"image_tag": metadata.image_tag, "image_id": metadata.image_id},
    )

    baseline = run_evaluation_phase(
        bundle=bundle,
        session=session,
        docker=docker,
        image_ref=metadata.image_id,
        phase="baseline",
        repetitions=selected_repetitions,
        inputs=inputs,
        evaluator_isolation=selected_evaluator_isolation,
    )
    baseline_summary = summarize_executions(baseline)
    partial: dict[str, Any] = {
        "bundle": str(bundle.root),
        "bundle_id": bundle.manifest.id,
        "image_tag": metadata.image_tag,
        "image_id": metadata.image_id,
        "solver_image_tag": metadata.solver_image_tag,
        "solver_image_id": metadata.solver_image_id,
        "repetitions": selected_repetitions,
        "evaluator_isolation": selected_evaluator_isolation.value,
        "baseline": baseline_summary,
        "provenance": provenance,
        "trusted_inputs": {"sha256": inputs.sha256, "artifacts": inputs.artifacts},
        "candidate_input_artifact": candidate_input_artifact,
        "patch_contract": patch_contract,
        "isolation": {
            "evaluator": docker.isolation_profile(
                runtime=bundle.manifest.runtime,
                workdir=bundle.manifest.environment.workdir,
                network="none",
            ),
            "solver": docker.isolation_profile(
                runtime=bundle.manifest.runtime,
                workdir=bundle.manifest.environment.workdir,
                network="bridge" if effective_network else "none",
            ),
        },
        "snapshot_artifacts": repository_snapshot_artifacts(session),
    }
    if not baseline_summary["valid"]:
        report_path = _write_run_report(session, partial)
        raise InvalidTaskError(
            "Baseline preflight did not match the task truth table.",
            hint=f"Inspect {report_path} and the baseline test logs before running a solver.",
            details=partial,
        )

    solver_container = docker.create_solver(
        image_tag=metadata.solver_image_id,
        container_name=container_name(bundle.manifest.id, f"{session.command_id}-solver"),
        workdir=bundle.manifest.environment.workdir,
        runtime=bundle.manifest.runtime,
    )
    session.event(
        "info",
        "solver",
        "Solver container created.",
        {
            "container_id": solver_container,
            "adapter": solver.name,
            "isolation": partial["isolation"]["solver"],
            "agent": solver.provenance if isinstance(solver, AgentSolver) else None,
        },
    )

    outcome = None
    patch_path: Path | None = None
    patch_bytes = 0
    patch_content = ""
    patch_sha256 = sha256_text("")
    patch_paths: set[str] = set()
    status_artifact: str | None = None
    solver_base_commit: str | None = None
    try:
        docker.start_detached(solver_container)
        docker.stream_text(
            content=inputs.description,
            container_id=solver_container,
            destination="/tmp/taskbundle-description.md",
        )
        solver_base_commit = _verify_solver_view(
            bundle=bundle,
            session=session,
            docker=docker,
            container_id=solver_container,
            solver_base_commit=metadata.solver_base_commit,
        )
        outcome = solver.solve(
            SolverContext(
                docker=docker,
                container_id=solver_container,
                workdir=bundle.manifest.environment.workdir,
                timeout_seconds=bundle.manifest.runtime.solver_timeout_seconds,
                trusted_path=bundle.manifest.environment.evaluator_path_value,
            )
        )
        stdout_path = session.artifacts.write_text(
            command_id=session.command_id,
            relative_path="solver.stdout.log",
            content=outcome.process.stdout,
            kind="solver_stdout",
        )
        stderr_path = session.artifacts.write_text(
            command_id=session.command_id,
            relative_path="solver.stderr.log",
            content=outcome.process.stderr,
            kind="solver_stderr",
        )
        if not outcome.process.timed_out:
            capture_repository_snapshot(
                docker=docker,
                session=session,
                container_id=solver_container,
                workdir=bundle.manifest.environment.workdir,
                base_commit=solver_base_commit,
                phase="solver",
                stage="complete",
                trusted_path=bundle.manifest.environment.evaluator_path_value,
            )
            (
                patch_path,
                patch_bytes,
                status_artifact,
                patch_content,
                patch_sha256,
            ) = _capture_patch(
                docker=docker,
                session=session,
                container_id=solver_container,
                workdir=bundle.manifest.environment.workdir,
                base_commit=solver_base_commit,
                max_patch_bytes=bundle.manifest.runtime.max_patch_bytes,
                trusted_path=bundle.manifest.environment.evaluator_path_value,
            )
            patch_paths = _candidate_paths(patch_content)
        partial["solver"] = {
            "adapter": outcome.adapter,
            "exit_code": outcome.process.exit_code,
            "timed_out": outcome.process.timed_out,
            "duration_seconds": outcome.process.duration_seconds,
            "network": "bridge" if effective_network else "none",
            "stdout_artifact": stdout_path.relative_to(session.state_dir).as_posix(),
            "stderr_artifact": stderr_path.relative_to(session.state_dir).as_posix(),
            "status_artifact": status_artifact,
            "patch_artifact": (
                patch_path.relative_to(session.state_dir).as_posix() if patch_path else None
            ),
            "patch_bytes": patch_bytes,
            "patch_sha256": patch_sha256,
            "patch_paths": sorted(patch_paths),
            "solver_base_commit": solver_base_commit,
            **outcome.details,
        }
    finally:
        docker.remove_container(solver_container)
        session.event(
            "info",
            "cleanup",
            "Solver container removed.",
            {"container_id": solver_container},
        )

    assert outcome is not None
    if outcome.process.timed_out:
        partial["snapshot_artifacts"] = repository_snapshot_artifacts(session)
        report_path = _write_run_report(session, partial)
        raise SolverError(
            "The solver timed out before a patch could be graded.",
            hint=f"Inspect {report_path} and increase runtime.solver_timeout_seconds if needed.",
            details=partial,
        )

    protected_overlap = set(patch_contract["protected_paths"]) & patch_paths
    if protected_overlap:
        partial["snapshot_artifacts"] = repository_snapshot_artifacts(session)
        report_path = _write_run_report(session, partial)
        raise SolverError(
            "The solver attempted to modify evaluator-owned test paths.",
            hint=(
                f"Inspect {report_path}; solver patches may change implementation/public "
                "files only."
            ),
            details={**partial, "protected_overlap": sorted(protected_overlap)},
        )

    outside_allowed = {
        path for path in patch_paths if not bundle.manifest.candidate.is_within_allowed(path)
    }
    disallowed_paths = {
        path for path in patch_paths if bundle.manifest.candidate.is_disallowed(path)
    }
    if outside_allowed or disallowed_paths:
        partial["snapshot_artifacts"] = repository_snapshot_artifacts(session)
        report_path = _write_run_report(session, partial)
        raise SolverError(
            "The solver attempted to modify paths outside the task's candidate-edit policy.",
            hint=(
                f"Inspect {report_path}; only allowed, non-disallowed implementation paths "
                "may change."
            ),
            details={
                **partial,
                "outside_allowed_paths": sorted(outside_allowed),
                "disallowed_paths": sorted(disallowed_paths),
            },
        )

    if outcome.process.exit_code != 0 and not patch_content:
        partial["snapshot_artifacts"] = repository_snapshot_artifacts(session)
        report_path = _write_run_report(session, partial)
        raise SolverError(
            "The solver process exited unsuccessfully and produced no patch to grade.",
            hint=f"Inspect {report_path} and the solver stderr artifact.",
            details=partial,
        )

    try:
        post_solver = run_evaluation_phase(
            bundle=bundle,
            session=session,
            docker=docker,
            image_ref=metadata.image_id,
            phase="post_solver",
            repetitions=selected_repetitions,
            inputs=inputs,
            evaluator_isolation=selected_evaluator_isolation,
            candidate_patch=patch_content if patch_bytes else None,
        )
    except TaskBundleError as error:
        partial["resolved"] = False
        partial["post_solver_error"] = error.as_dict()
        partial["snapshot_artifacts"] = repository_snapshot_artifacts(session)
        report_path = _write_run_report(session, partial)
        error.details = {
            **error.details,
            "run_artifact": report_path.relative_to(session.state_dir).as_posix(),
        }
        raise
    post_summary = summarize_executions(post_solver)
    partial["post_solver"] = post_summary
    partial["resolved"] = post_summary["valid"]
    partial["snapshot_artifacts"] = repository_snapshot_artifacts(session)
    report_path = _write_run_report(session, partial)
    if outcome.process.exit_code != 0:
        raise SolverError(
            "The solver process exited unsuccessfully; its non-empty patch was graded.",
            hint=f"Inspect {report_path}, the solver stderr artifact, and post-solver results.",
            details=partial,
        )
    if not post_summary["valid"]:
        raise UnresolvedError(
            "The solver did not resolve every required test.",
            hint=f"Inspect {report_path}, solver.patch, and the post-solver test logs.",
            details=partial,
        )
    session.event(
        "info",
        "result",
        "Solver patch resolved the task.",
        {"patch_bytes": patch_bytes, "run_artifact": str(report_path)},
    )
    return partial
