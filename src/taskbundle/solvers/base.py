"""Small provider-neutral interface for code-changing solver adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from taskbundle.engine.docker import DockerClient
from taskbundle.process import ProcessResult


@dataclass(frozen=True, slots=True)
class SolverContext:
    docker: DockerClient
    container_id: str
    workdir: str
    timeout_seconds: int
    environment_names: list[str]


@dataclass(frozen=True, slots=True)
class SolverOutcome:
    adapter: str
    process: ProcessResult


class Solver(Protocol):
    name: str

    def solve(self, context: SolverContext) -> SolverOutcome: ...
