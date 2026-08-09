"""Lifecycle wrapper that makes every command durable and queryable."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from taskbundle.artifacts import ArtifactStore
from taskbundle.database import Database
from taskbundle.errors import ErrorKind, ExitCode, InfrastructureError, TaskBundleError
from taskbundle.ids import new_command_id
from taskbundle.models import CommandReport, CommandStatus, ErrorPayload

SENSITIVE_ARGUMENT_NAMES = {
    "--api-key",
    "--password",
    "--repo",
    "--secret",
    "--solver-cmd",
    "--token",
}


def utc_now() -> datetime:
    return datetime.now(UTC)


def sanitize_arguments(arguments: list[str]) -> list[str]:
    sanitized: list[str] = []
    redact_next = False
    for argument in arguments:
        if redact_next:
            sanitized.append("<redacted>")
            redact_next = False
            continue

        name, separator, _value = argument.partition("=")
        normalized = name.lower().replace("_", "-")
        is_sensitive = normalized in SENSITIVE_ARGUMENT_NAMES or any(
            marker in normalized for marker in ("secret", "token", "password", "api-key")
        )
        if is_sensitive and separator:
            sanitized.append(f"{name}=<redacted>")
        else:
            sanitized.append(argument)
            redact_next = is_sensitive
    return sanitized


def state_directory(bundle_path: Path) -> Path:
    resolved = bundle_path.expanduser().resolve()
    if resolved.is_dir():
        return resolved / ".taskbundle"
    return Path.cwd().resolve() / ".taskbundle"


@dataclass(slots=True)
class CommandSession:
    command_id: str
    command_name: str
    state_dir: Path
    started_at: datetime
    database: Database
    artifacts: ArtifactStore
    bundle_id: str | None = None
    _finished: bool = False

    @classmethod
    def start(
        cls,
        *,
        command_name: str,
        bundle_path: Path,
        arguments: list[str] | None = None,
    ) -> Self:
        started_at = utc_now()
        command_id = new_command_id(started_at)
        state_dir = state_directory(bundle_path)
        database = Database(state_dir / "taskbundle.db")
        database.create_command(
            command_id=command_id,
            command_name=command_name,
            bundle_id=None,
            arguments=sanitize_arguments(arguments if arguments is not None else sys.argv[1:]),
            started_at=started_at.isoformat(),
        )
        artifacts = ArtifactStore(state_dir, database)
        session = cls(
            command_id=command_id,
            command_name=command_name,
            state_dir=state_dir,
            started_at=started_at,
            database=database,
            artifacts=artifacts,
        )
        session.event("info", "command", "Command started.")
        return session

    def attach_bundle(self, bundle_id: str) -> None:
        self.bundle_id = bundle_id
        self.database.set_command_bundle_id(command_id=self.command_id, bundle_id=bundle_id)

    def event(
        self,
        level: str,
        phase: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.database.add_event(
            command_id=self.command_id,
            occurred_at=utc_now().isoformat(),
            level=level,
            phase=phase,
            message=message,
            data=data,
        )

    def succeed(self, data: dict[str, Any] | None = None) -> CommandReport:
        return self._finish(
            status=CommandStatus.SUCCEEDED,
            exit_code=ExitCode.SUCCESS,
            data=data or {},
            error=None,
        )

    def fail(self, error: TaskBundleError) -> CommandReport:
        self.event("error", "command", error.message, error.as_dict())
        payload = ErrorPayload(
            kind=error.kind.value,
            message=error.message,
            hint=error.hint,
            details=error.details,
        )
        return self._finish(
            status=CommandStatus.FAILED,
            exit_code=error.exit_code,
            data={},
            error=payload,
        )

    def fail_unexpected(self, error: Exception) -> CommandReport:
        wrapped = TaskBundleError(
            "An unexpected internal error occurred.",
            kind=ErrorKind.INTERNAL,
            exit_code=ExitCode.INFRASTRUCTURE,
            hint="Inspect the command events and rerun with a debugger during development.",
            details={"exception_type": type(error).__name__, "reason": str(error)},
        )
        return self.fail(wrapped)

    def _finish(
        self,
        *,
        status: CommandStatus,
        exit_code: ExitCode,
        data: dict[str, Any],
        error: ErrorPayload | None,
    ) -> CommandReport:
        if self._finished:
            raise InfrastructureError(f"Command session already finished: {self.command_id}")
        ended_at = utc_now()
        report = CommandReport(
            command_id=self.command_id,
            command=self.command_name,
            bundle_id=self.bundle_id,
            status=status,
            started_at=self.started_at,
            ended_at=ended_at,
            data=data,
            error=error,
        )
        self.artifacts.write_json(
            command_id=self.command_id,
            relative_path="report.json",
            payload=report.model_dump(mode="json"),
            kind="command_report",
        )
        self.database.finish_command(
            command_id=self.command_id,
            status=status,
            ended_at=ended_at.isoformat(),
            exit_code=int(exit_code),
            error_kind=error.kind if error else None,
            error_message=error.message if error else None,
        )
        self._finished = True
        return report

    def close(self) -> None:
        self.database.close()
