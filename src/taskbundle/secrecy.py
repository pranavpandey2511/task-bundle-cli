"""Fail-closed checks for the solver-visible repository and container."""

from __future__ import annotations

from taskbundle.config import Bundle
from taskbundle.engine.docker import DockerClient
from taskbundle.errors import InvalidTaskError
from taskbundle.process import ProcessResult


def _absence_error(result: ProcessResult) -> bool:
    return result.timed_out or result.exit_code != 1


def verify_solver_secrecy(
    *,
    bundle: Bundle,
    docker: DockerClient,
    container_id: str,
) -> None:
    """Prove evaluator paths/content, original Git state, and environment leaks are absent."""

    workdir = bundle.manifest.environment.workdir
    trusted_path = bundle.manifest.environment.evaluator_path_value
    tests = bundle.manifest.tests.pass_to_pass + bundle.manifest.tests.fail_to_pass
    environment = docker.exec_command(
        container_id=container_id,
        workdir=workdir,
        command=["/usr/bin/env"],
        timeout_seconds=60,
        trusted_path=trusted_path,
    )
    if not environment.succeeded:
        raise InvalidTaskError(
            "Could not inspect the solver container environment for evaluator content.",
            details={
                "exit_code": environment.exit_code,
                "timed_out": environment.timed_out,
                "stderr": environment.stderr[-4000:],
            },
        )
    environment_conflicts = [
        {"test_id": test.id, "path": test.path}
        for test in tests
        if test.marker in environment.stdout
    ]
    if environment_conflicts:
        raise InvalidTaskError(
            "Evaluator test content remains visible in the solver container environment.",
            details={"conflicts": environment_conflicts},
        )

    for path in sorted(bundle.manifest.tests.evaluator_owned_paths):
        present = docker.exec_command(
            container_id=container_id,
            workdir=workdir,
            command=[
                "/bin/sh",
                "-c",
                'test -e "$1" || test -L "$1"',
                "taskbundle-protected-path-check",
                path,
            ],
            timeout_seconds=60,
            trusted_path=trusted_path,
        )
        if present.succeeded:
            raise InvalidTaskError(
                "An evaluator-owned test path remains visible in the solver image.",
                hint="Make solver-view.patch delete every protected file completely.",
                details={"path": path},
            )
        if _absence_error(present):
            raise InvalidTaskError(
                "Could not prove an evaluator-owned test path is absent.",
                details={
                    "path": path,
                    "exit_code": present.exit_code,
                    "timed_out": present.timed_out,
                    "stderr": present.stderr[-4000:],
                },
            )

    foreign_git = docker.exec_command(
        container_id=container_id,
        workdir=workdir,
        command=[
            "/bin/sh",
            "-c",
            "find / "
            r'\( -path /proc -o -path /sys -o -path /dev -o -path "$1/.git" \) '
            r"-prune -o \( -type d ! -readable \) -prune -o -name .git -print -quit",
            "taskbundle-git-metadata-check",
            workdir,
        ],
        timeout_seconds=60,
        trusted_path=trusted_path,
    )
    if not foreign_git.succeeded or foreign_git.stdout.strip():
        raise InvalidTaskError(
            "Unexpected Git metadata remains visible in the solver image.",
            details={
                "paths": foreign_git.stdout.splitlines(),
                "exit_code": foreign_git.exit_code,
                "timed_out": foreign_git.timed_out,
                "stderr": foreign_git.stderr[-4000:],
            },
        )

    remotes = docker.exec_command(
        container_id=container_id,
        workdir=workdir,
        command=["git", "remote"],
        timeout_seconds=60,
        trusted_path=trusted_path,
    )
    if not remotes.succeeded or remotes.stdout.strip():
        raise InvalidTaskError(
            "The solver repository contains a Git remote.",
            details={"remotes": remotes.stdout.splitlines(), "stderr": remotes.stderr[-4000:]},
        )

    filesystem_scan = (
        "rm -f /tmp/taskbundle-scan-found /tmp/taskbundle-scan-error; "
        "find / "
        r'\( -path /proc -o -path /sys -o -path /dev -o -path "$2/.git" \) -prune '
        r"-o \( -type d ! -readable \) -prune -o -type f -readable -exec /bin/sh -c '"
        "marker=$1; shift; "
        "for file do "
        'grep -F -q -- "$marker" "$file"; status=$?; '
        'if [ "$status" -eq 0 ]; then : > /tmp/taskbundle-scan-found; '
        'elif [ "$status" -ne 1 ]; then : > /tmp/taskbundle-scan-error; fi; '
        'done\' taskbundle-scan "$1" {} +; find_status=$?; '
        'if [ "$find_status" -ne 0 ] || [ -e /tmp/taskbundle-scan-error ]; then exit 2; fi; '
        "if [ -e /tmp/taskbundle-scan-found ]; then exit 0; fi; "
        "exit 1"
    )
    for test in tests:
        filesystem = docker.exec_command(
            container_id=container_id,
            workdir=workdir,
            command=[
                "/bin/sh",
                "-c",
                filesystem_scan,
                "taskbundle-filesystem-secrecy-check",
                test.marker,
                workdir,
            ],
            timeout_seconds=120,
            trusted_path=trusted_path,
        )
        history = docker.exec_command(
            container_id=container_id,
            workdir=workdir,
            command=[
                "/bin/sh",
                "-c",
                'git grep --fixed-strings --quiet -e "$1" $(git rev-list --all)',
                "taskbundle-history-secrecy-check",
                test.marker,
            ],
            timeout_seconds=60,
            trusted_path=trusted_path,
        )
        if filesystem.succeeded or history.succeeded:
            raise InvalidTaskError(
                f"Evaluator test content remains visible to the solver: {test.id}",
                details={"test_id": test.id, "path": test.path},
            )
        errors = {
            name: {
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "stderr": result.stderr[-4000:],
            }
            for name, result in {"filesystem": filesystem, "history": history}.items()
            if _absence_error(result)
        }
        if errors:
            raise InvalidTaskError(
                f"Could not prove evaluator test content is absent: {test.id}",
                details={"test_id": test.id, "path": test.path, "checks": errors},
            )
