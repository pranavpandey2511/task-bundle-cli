"""Exact, detached Git checkout operations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from taskbundle.errors import InfrastructureError
from taskbundle.process import ProcessResult, Runner


@dataclass(frozen=True, slots=True)
class CheckoutResult:
    commit: str
    log: str


class GitClient:
    def __init__(self, runner: Runner) -> None:
        self.runner = runner

    def checkout_exact(
        self,
        *,
        repository_url: str,
        commit: str,
        destination: Path,
        timeout_seconds: int,
    ) -> CheckoutResult:
        if destination.exists():
            raise InfrastructureError(f"Checkout destination already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)

        environment = {"GIT_TERMINAL_PROMPT": "0"}
        commands = (
            ["git", "init", "--quiet", str(destination)],
            ["git", "-C", str(destination), "remote", "add", "origin", repository_url],
            [
                "git",
                "-C",
                str(destination),
                "fetch",
                "--depth=1",
                "--no-tags",
                "origin",
                commit,
            ],
            ["git", "-C", str(destination), "checkout", "--quiet", "--detach", "FETCH_HEAD"],
        )
        logs: list[str] = []
        for command in commands:
            result = self.runner.run(
                command,
                timeout_seconds=timeout_seconds,
                environment=environment,
            )
            logs.append(self._format_result(result))
            self._require_success(result, action="prepare exact Git checkout")

        revision = self.runner.run(
            ["git", "-C", str(destination), "rev-parse", "HEAD"],
            timeout_seconds=30,
            environment=environment,
        )
        logs.append(self._format_result(revision))
        self._require_success(revision, action="verify checked-out Git commit")
        actual = revision.stdout.strip().lower()
        if actual != commit.lower():
            raise InfrastructureError(
                "Git checkout resolved to an unexpected commit.",
                details={"expected": commit.lower(), "actual": actual},
            )

        status = self.runner.run(
            ["git", "-C", str(destination), "status", "--porcelain"],
            timeout_seconds=30,
            environment=environment,
        )
        logs.append(self._format_result(status))
        self._require_success(status, action="verify checkout cleanliness")
        if status.stdout.strip():
            raise InfrastructureError(
                "The generated source checkout is unexpectedly dirty.",
                details={"status": status.stdout.strip()},
            )
        return CheckoutResult(commit=actual, log="\n".join(logs))

    def version(self) -> str:
        result = self.runner.run(["git", "--version"], timeout_seconds=10)
        self._require_success(result, action="read Git version")
        return (result.stdout or result.stderr).strip()

    @staticmethod
    def _require_success(result: ProcessResult, *, action: str) -> None:
        if result.timed_out:
            raise InfrastructureError(
                f"Timed out while attempting to {action}.",
                details={"stderr": result.stderr[-4000:]},
            )
        if result.exit_code != 0:
            raise InfrastructureError(
                f"Could not {action}.",
                details={
                    "exit_code": result.exit_code,
                    "stdout": result.stdout[-4000:],
                    "stderr": result.stderr[-4000:],
                },
            )

    @staticmethod
    def _format_result(result: ProcessResult) -> str:
        executable = " ".join(result.argv[:2])
        return (
            f"$ {executable} ...\n"
            f"exit={result.exit_code} timeout={result.timed_out} "
            f"duration={result.duration_seconds:.3f}s\n"
            f"{result.stdout}{result.stderr}"
        )
