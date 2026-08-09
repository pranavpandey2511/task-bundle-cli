"""One auditable subprocess boundary for Git, Docker, and local diagnostics."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from taskbundle.errors import InfrastructureError


@dataclass(frozen=True, slots=True)
class ProcessResult:
    argv: tuple[str, ...]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool

    @property
    def succeeded(self) -> bool:
        return not self.timed_out and self.exit_code == 0


class Runner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout_seconds: float | None = None,
        environment: Mapping[str, str] | None = None,
        stdin: str | None = None,
    ) -> ProcessResult: ...


class ProcessRunner:
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout_seconds: float | None = None,
        environment: Mapping[str, str] | None = None,
        stdin: str | None = None,
    ) -> ProcessResult:
        if not argv:
            raise InfrastructureError("Refusing to run an empty command.")

        command = [str(part) for part in argv]
        process_environment = os.environ.copy()
        if environment:
            process_environment.update(environment)

        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=process_environment,
                input=stdin,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_seconds,
            )
        except FileNotFoundError as error:
            raise InfrastructureError(
                f"Required executable was not found: {command[0]}",
                hint=f"Install `{command[0]}` and ensure it is on PATH.",
            ) from error
        except OSError as error:
            raise InfrastructureError(f"Could not start {command[0]}: {error}") from error
        except subprocess.TimeoutExpired as error:
            duration = time.monotonic() - started
            return ProcessResult(
                argv=tuple(command),
                exit_code=None,
                stdout=self._coerce_output(error.stdout),
                stderr=self._coerce_output(error.stderr),
                duration_seconds=duration,
                timed_out=True,
            )

        return ProcessResult(
            argv=tuple(command),
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_seconds=time.monotonic() - started,
            timed_out=False,
        )

    @staticmethod
    def _coerce_output(value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value
