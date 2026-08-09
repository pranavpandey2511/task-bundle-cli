"""Run an existing coding-agent command inside the solver container."""

from __future__ import annotations

from taskbundle.solvers.base import SolverContext, SolverOutcome


class CommandSolver:
    name = "command"

    def __init__(self, command: str) -> None:
        self.command = command

    def solve(self, context: SolverContext) -> SolverOutcome:
        process = context.docker.exec_command(
            container_id=context.container_id,
            workdir=context.workdir,
            command=["/bin/sh", "-lc", self.command],
            timeout_seconds=context.timeout_seconds,
            environment_names=context.environment_names,
            environment_values={
                "TASKBUNDLE_DESCRIPTION": "/tmp/taskbundle-description.md",
            },
        )
        return SolverOutcome(adapter=self.name, process=process)
