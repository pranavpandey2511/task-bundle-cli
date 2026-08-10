"""Repository-state snapshots captured inside disposable containers."""

from __future__ import annotations

from typing import Any

from taskbundle.engine.docker import DockerClient
from taskbundle.errors import InfrastructureError, InvalidTaskError
from taskbundle.session import CommandSession


def _git_output(
    *,
    docker: DockerClient,
    container_id: str,
    workdir: str,
    command: list[str],
    trusted_path: str | None,
) -> str:
    result = docker.exec_command(
        container_id=container_id,
        workdir=workdir,
        command=command,
        timeout_seconds=60,
        trusted_path=trusted_path,
    )
    if not result.succeeded:
        raise InfrastructureError(
            "Could not capture repository state inside a task container.",
            details={
                "command": command,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "stderr": result.stderr[-4000:],
            },
        )
    return result.stdout


def capture_repository_snapshot(
    *,
    docker: DockerClient,
    session: CommandSession,
    container_id: str,
    workdir: str,
    base_commit: str,
    phase: str,
    stage: str,
    require_pristine: bool = False,
    trusted_path: str | None = None,
) -> str:
    head = _git_output(
        docker=docker,
        container_id=container_id,
        workdir=workdir,
        command=["git", "rev-parse", "HEAD"],
        trusted_path=trusted_path,
    ).strip()
    status = _git_output(
        docker=docker,
        container_id=container_id,
        workdir=workdir,
        command=["git", "status", "--porcelain=v1", "--untracked-files=all"],
        trusted_path=trusted_path,
    )
    diff_stat = _git_output(
        docker=docker,
        container_id=container_id,
        workdir=workdir,
        command=["git", "diff", "--stat", "--no-ext-diff"],
        trusted_path=trusted_path,
    )
    payload: dict[str, Any] = {
        "schema_version": 1,
        "phase": phase,
        "stage": stage,
        "head": head,
        "base_commit": base_commit.lower(),
        "head_matches_base": head.lower() == base_commit.lower(),
        "dirty": bool(status.strip()),
        "status": status.splitlines(),
        "diff_stat": diff_stat.splitlines(),
    }
    artifact = session.artifacts.write_json(
        command_id=session.command_id,
        relative_path=f"snapshots/{phase}-{stage}.json",
        payload=payload,
        kind="repository_snapshot",
    )
    relative = artifact.relative_to(session.state_dir).as_posix()
    session.event(
        "info",
        "snapshot",
        f"Captured {phase} repository snapshot at {stage}.",
        {"artifact": relative, "dirty": payload["dirty"], "status_count": len(payload["status"])},
    )
    if require_pristine and (not payload["head_matches_base"] or payload["dirty"]):
        raise InvalidTaskError(
            f"The repository is not pristine at the start of the {phase} phase.",
            hint=(
                "Ensure the Dockerfile leaves the repository at the configured commit with no "
                "tracked or untracked changes."
            ),
            details={
                "expected_commit": payload["base_commit"],
                "observed_commit": payload["head"],
                "dirty": payload["dirty"],
                "status": payload["status"],
                "snapshot_artifact": relative,
            },
        )
    return relative


def repository_snapshot_artifacts(session: CommandSession) -> list[str]:
    return [
        artifact["relative_path"]
        for artifact in session.database.get_artifacts(session.command_id)
        if artifact["kind"] == "repository_snapshot"
    ]
