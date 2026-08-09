"""No-op solver used to exercise a genuine unresolved run."""

from __future__ import annotations

from taskbundle.process import ProcessResult
from taskbundle.solvers.base import SolverContext, SolverOutcome


class StubSolver:
    name = "stub"

    def solve(self, context: SolverContext) -> SolverOutcome:
        del context
        return SolverOutcome(
            adapter=self.name,
            process=ProcessResult(
                argv=("stub",),
                exit_code=0,
                stdout="Stub solver intentionally made no changes.\n",
                stderr="",
                duration_seconds=0.0,
                timed_out=False,
            ),
        )
