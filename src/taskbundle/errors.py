"""Typed errors and stable exit semantics for the CLI."""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Any


class ExitCode(IntEnum):
    """Public process exit codes."""

    SUCCESS = 0
    EXPECTATION_FAILED = 1
    CONFIGURATION = 2
    INFRASTRUCTURE = 3
    SOLVER = 4


class ErrorKind(StrEnum):
    """Machine-readable failure classifications."""

    CONFIGURATION = "configuration_error"
    INFRASTRUCTURE = "infrastructure_error"
    INVALID_TASK = "invalid_task"
    UNRESOLVED = "unresolved"
    SOLVER = "solver_error"
    NOT_FOUND = "not_found"
    INTERNAL = "internal_error"


class TaskBundleError(Exception):
    """Expected failure that can be presented without a traceback."""

    def __init__(
        self,
        message: str,
        *,
        kind: ErrorKind,
        exit_code: ExitCode,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.exit_code = exit_code
        self.hint = hint
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind.value,
            "message": self.message,
        }
        if self.hint:
            payload["hint"] = self.hint
        if self.details:
            payload["details"] = self.details
        return payload


class ConfigurationError(TaskBundleError):
    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message,
            kind=ErrorKind.CONFIGURATION,
            exit_code=ExitCode.CONFIGURATION,
            hint=hint,
            details=details,
        )


class InfrastructureError(TaskBundleError):
    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message,
            kind=ErrorKind.INFRASTRUCTURE,
            exit_code=ExitCode.INFRASTRUCTURE,
            hint=hint,
            details=details,
        )


class InvalidTaskError(TaskBundleError):
    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message,
            kind=ErrorKind.INVALID_TASK,
            exit_code=ExitCode.EXPECTATION_FAILED,
            hint=hint,
            details=details,
        )


class UnresolvedError(TaskBundleError):
    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message,
            kind=ErrorKind.UNRESOLVED,
            exit_code=ExitCode.EXPECTATION_FAILED,
            hint=hint,
            details=details,
        )


class SolverError(TaskBundleError):
    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message,
            kind=ErrorKind.SOLVER,
            exit_code=ExitCode.SOLVER,
            hint=hint,
            details=details,
        )


class NotImplementedLifecycleError(TaskBundleError):
    def __init__(self, command: str) -> None:
        super().__init__(
            f"`task {command}` is not implemented in the current milestone.",
            kind=ErrorKind.INFRASTRUCTURE,
            exit_code=ExitCode.INFRASTRUCTURE,
            hint=(
                "The bundle contract and command ledger are implemented first; "
                "see the README build plan."
            ),
        )
