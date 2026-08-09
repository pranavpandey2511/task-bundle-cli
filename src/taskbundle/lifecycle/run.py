"""Solver execution, patch capture, and fresh-container grading."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from taskbundle.config import Bundle
from taskbundle.engine.docker import DockerClient
from taskbundle.errors import ConfigurationError, InvalidTaskError, SolverError, UnresolvedError
from taskbundle.lifecycle.initialize import container_name, require_initialized_build
from taskbundle.lifecycle.validate import run_evaluation_phase, summarize_executions
from taskbundle.process import ProcessRunner, Runner
from taskbundle.session import CommandSession
from taskbundle.solvers import CommandSolver, PatchSolver, Solver, SolverContext, StubSolver

SECRET_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _solver_for(
    *,
    name: str,
    command: str | None,
    candidate_patch: Path | None,
) -> Solver:
    if name == "stub":
        if command is not None or candidate_patch is not None:
            raise ConfigurationError(
                "The stub solver does not accept a command or candidate patch."
            )
        return StubSolver()
    if name == "command":
        if command is None or not command.strip():
            raise ConfigurationError("The command solver requires a non-empty --solver-cmd.")
        if candidate_patch is not None:
            raise ConfigurationError("The command solver does not accept --candidate-patch.")
        return CommandSolver(command)
    if name == "patch":
        if command is not None:
            raise ConfigurationError("The patch solver does not accept --solver-cmd.")
        if candidate_patch is None:
            raise ConfigurationError("The patch solver requires --candidate-patch.")
        resolved = candidate_patch.expanduser().resolve()
        if not resolved.is_file():
            raise ConfigurationError(f"Candidate patch does not exist: {resolved}")
        return PatchSolver(resolved)
    raise ConfigurationError(
        f"Unknown solver adapter: {name}",
        hint="Choose one of: stub, patch, command.",
    )


def _validated_secret_names(names: list[str]) -> list[str]:
    selected: list[str] = []
    for name in names:
        if not SECRET_NAME.fullmatch(name):
            raise ConfigurationError(
                f"Invalid environment-variable name: {name}",
                hint="--secret-env accepts names such as OPENAI_API_KEY, never secret values.",
            )
        if name not in os.environ:
            raise ConfigurationError(f"Secret environment variable is not set: {name}")
        if name not in selected:
            selected.append(name)
    return selected


def _capture_patch(
    *,
    docker: DockerClient,
    session: CommandSession,
    container_id: str,
    workdir: str,
    max_patch_bytes: int,
) -> tuple[Path, int, str]:
    status = docker.exec_command(
        container_id=container_id,
        workdir=workdir,
        command=["git", "status", "--short", "--untracked-files=all"],
        timeout_seconds=60,
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
    )
    if not staged.succeeded:
        raise SolverError(
            "Could not stage the solver's repository changes for patch capture.",
            details={"exit_code": staged.exit_code, "stderr": staged.stderr[-4000:]},
        )

    diff = docker.exec_command(
        container_id=container_id,
        workdir=workdir,
        command=["git", "diff", "--cached", "--binary", "--full-index", "--no-ext-diff"],
        timeout_seconds=60,
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
    return patch_path, patch_bytes, status_path.relative_to(session.state_dir).as_posix()


def _write_run_report(session: CommandSession, result: dict[str, Any]) -> Path:
    path = session.artifacts.write_json(
        command_id=session.command_id,
        relative_path="run.json",
        payload=result,
        kind="run_report",
    )
    result["run_artifact"] = str(path)
    return path


def run_task(
    *,
    bundle: Bundle,
    session: CommandSession,
    solver_name: str = "stub",
    solver_command: str | None = None,
    candidate_patch: Path | None = None,
    allow_network: bool = False,
    secret_environment_names: list[str] | None = None,
    repetitions: int | None = None,
    runner: Runner | None = None,
) -> dict[str, Any]:
    selected_repetitions = (
        bundle.manifest.validation.repetitions if repetitions is None else repetitions
    )
    if not 1 <= selected_repetitions <= 20:
        raise ConfigurationError("Run repetitions must be between 1 and 20.")
    if allow_network and not bundle.manifest.runtime.solver_network:
        raise ConfigurationError(
            "Solver network access is disabled by this bundle.",
            hint="Set runtime.solver_network=true and pass --allow-network for explicit consent.",
        )

    solver = _solver_for(
        name=solver_name,
        command=solver_command,
        candidate_patch=candidate_patch,
    )
    secret_names = _validated_secret_names(secret_environment_names or [])
    process_runner = runner or ProcessRunner()
    docker = DockerClient(
        process_runner,
        executable=os.environ.get("TASKBUNDLE_DOCKER_BIN", "docker"),
    )
    metadata = require_initialized_build(bundle=bundle, state_dir=session.state_dir, docker=docker)
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
        image_tag=metadata.image_tag,
        phase="baseline",
        repetitions=selected_repetitions,
    )
    baseline_summary = summarize_executions(baseline)
    partial: dict[str, Any] = {
        "bundle": str(bundle.root),
        "bundle_id": bundle.manifest.id,
        "image_tag": metadata.image_tag,
        "image_id": metadata.image_id,
        "repetitions": selected_repetitions,
        "baseline": baseline_summary,
    }
    if not baseline_summary["valid"]:
        report_path = _write_run_report(session, partial)
        raise InvalidTaskError(
            "Baseline preflight did not match the task truth table.",
            hint=f"Inspect {report_path} and the baseline test logs before running a solver.",
            details=partial,
        )

    effective_network = allow_network and bundle.manifest.runtime.solver_network
    solver_container = docker.create_solver(
        image_tag=metadata.image_tag,
        container_name=container_name(bundle.manifest.id, f"{session.command_id}-solver"),
        workdir=bundle.manifest.environment.workdir,
        runtime=bundle.manifest.runtime,
        network_enabled=effective_network,
    )
    session.event(
        "info",
        "solver",
        "Solver container created.",
        {
            "container_id": solver_container,
            "adapter": solver.name,
            "network": "bridge" if effective_network else "none",
            "secret_environment_names": secret_names,
        },
    )

    outcome = None
    patch_path: Path | None = None
    patch_bytes = 0
    status_artifact: str | None = None
    try:
        docker.copy_file(
            source=bundle.description_path,
            container_id=solver_container,
            destination="/tmp/taskbundle-description.md",
        )
        docker.start_detached(solver_container)
        outcome = solver.solve(
            SolverContext(
                docker=docker,
                container_id=solver_container,
                workdir=bundle.manifest.environment.workdir,
                timeout_seconds=bundle.manifest.runtime.solver_timeout_seconds,
                environment_names=secret_names,
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
            patch_path, patch_bytes, status_artifact = _capture_patch(
                docker=docker,
                session=session,
                container_id=solver_container,
                workdir=bundle.manifest.environment.workdir,
                max_patch_bytes=bundle.manifest.runtime.max_patch_bytes,
            )
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
        report_path = _write_run_report(session, partial)
        raise SolverError(
            "The solver timed out before a patch could be graded.",
            hint=f"Inspect {report_path} and increase runtime.solver_timeout_seconds if needed.",
            details=partial,
        )
    if outcome.process.exit_code != 0:
        report_path = _write_run_report(session, partial)
        raise SolverError(
            "The solver process exited unsuccessfully.",
            hint=f"Inspect {report_path} and the solver stderr artifact.",
            details=partial,
        )

    post_solver = run_evaluation_phase(
        bundle=bundle,
        session=session,
        docker=docker,
        image_tag=metadata.image_tag,
        phase="post_solver",
        repetitions=selected_repetitions,
        candidate_patch=patch_path if patch_bytes else None,
    )
    post_summary = summarize_executions(post_solver)
    partial["post_solver"] = post_summary
    partial["resolved"] = post_summary["valid"]
    report_path = _write_run_report(session, partial)
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
