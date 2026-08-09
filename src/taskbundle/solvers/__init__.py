"""Built-in solver adapters."""

from taskbundle.solvers.base import Solver, SolverContext, SolverOutcome
from taskbundle.solvers.command import CommandSolver
from taskbundle.solvers.patch import PatchSolver
from taskbundle.solvers.stub import StubSolver

__all__ = [
    "CommandSolver",
    "PatchSolver",
    "Solver",
    "SolverContext",
    "SolverOutcome",
    "StubSolver",
]
