"""Built-in solver adapters."""

from taskbundle.solvers.agent import AgentSolver
from taskbundle.solvers.base import Solver, SolverContext, SolverOutcome
from taskbundle.solvers.patch import PatchSolver
from taskbundle.solvers.stub import StubSolver

__all__ = [
    "AgentSolver",
    "PatchSolver",
    "Solver",
    "SolverContext",
    "SolverOutcome",
    "StubSolver",
]
