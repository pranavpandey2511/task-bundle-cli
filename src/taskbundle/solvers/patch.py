"""Apply a caller-supplied candidate patch inside the solver boundary."""

from __future__ import annotations

from taskbundle.solvers.base import SolverContext, SolverOutcome


class PatchSolver:
    name = "patch"

    def __init__(self, patch_content: str) -> None:
        self.patch_content = patch_content

    def solve(self, context: SolverContext) -> SolverOutcome:
        destination = "/tmp/taskbundle-input.patch"
        context.docker.stream_text(
            content=self.patch_content,
            container_id=context.container_id,
            destination=destination,
        )
        check = context.docker.exec_command(
            container_id=context.container_id,
            workdir=context.workdir,
            command=["git", "apply", "--check", "--index", destination],
            timeout_seconds=context.timeout_seconds,
            trusted_path=context.trusted_path,
        )
        if not check.succeeded:
            return SolverOutcome(adapter=self.name, process=check)
        process = context.docker.exec_command(
            container_id=context.container_id,
            workdir=context.workdir,
            command=["git", "apply", "--index", destination],
            timeout_seconds=context.timeout_seconds,
            trusted_path=context.trusted_path,
        )
        return SolverOutcome(adapter=self.name, process=process)
