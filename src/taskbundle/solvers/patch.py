"""Apply a caller-supplied candidate patch inside the solver boundary."""

from __future__ import annotations

from pathlib import Path

from taskbundle.solvers.base import SolverContext, SolverOutcome


class PatchSolver:
    name = "patch"

    def __init__(self, patch_path: Path) -> None:
        self.patch_path = patch_path

    def solve(self, context: SolverContext) -> SolverOutcome:
        destination = "/tmp/taskbundle-input.patch"
        context.docker.stream_file(
            source=self.patch_path,
            container_id=context.container_id,
            destination=destination,
        )
        process = context.docker.exec_command(
            container_id=context.container_id,
            workdir=context.workdir,
            command=[
                "/bin/sh",
                "-lc",
                f"git apply --check {destination} && git apply {destination}",
            ],
            timeout_seconds=context.timeout_seconds,
        )
        return SolverOutcome(adapter=self.name, process=process)
