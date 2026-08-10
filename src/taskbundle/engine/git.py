"""Exact, detached Git checkout operations."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from taskbundle.errors import InfrastructureError, InvalidTaskError
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
            ["git", "-C", str(destination), "submodule", "update", "--init", "--recursive"],
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

        submodules = self.runner.run(
            ["git", "-C", str(destination), "submodule", "status", "--recursive"],
            timeout_seconds=timeout_seconds,
            environment=environment,
        )
        logs.append(self._format_result(submodules))
        self._require_success(submodules, action="verify Git submodules")
        invalid_submodules = [
            line for line in submodules.stdout.splitlines() if line.startswith(("-", "+", "U"))
        ]
        if invalid_submodules:
            raise InfrastructureError(
                "One or more Git submodules are not at their pinned commits.",
                details={"submodules": invalid_submodules},
            )
        return CheckoutResult(commit=actual, log="\n".join(logs))

    def check_patch(
        self,
        *,
        repository: Path,
        patch: Path,
        label: str,
        timeout_seconds: int = 60,
    ) -> str:
        if patch.stat().st_size == 0:
            return f"{label}: empty patch\n"
        result = self.runner.run(
            ["git", "-C", str(repository), "apply", "--check", str(patch)],
            timeout_seconds=timeout_seconds,
            environment={"GIT_TERMINAL_PROMPT": "0"},
        )
        if not result.succeeded:
            raise InvalidTaskError(
                f"The {label} does not apply to the configured repository commit.",
                hint="Regenerate the trusted patch against repository.commit.",
                details={
                    "exit_code": result.exit_code,
                    "timed_out": result.timed_out,
                    "stdout": result.stdout[-4000:],
                    "stderr": result.stderr[-4000:],
                },
            )
        return self._format_result(result)

    def sanitize_solver_source(
        self,
        *,
        repository: Path,
        patch: Path,
        protected_paths: set[str],
        timeout_seconds: int = 120,
    ) -> tuple[str, str]:
        """Create a deterministic, remote-free Git root from the redacted source tree."""

        environment = {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "Task Bundle",
            "GIT_AUTHOR_EMAIL": "taskbundle@example.invalid",
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
            "GIT_COMMITTER_NAME": "Task Bundle",
            "GIT_COMMITTER_EMAIL": "taskbundle@example.invalid",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
        }
        logs: list[str] = []
        if patch.stat().st_size:
            applied = self.runner.run(
                ["git", "-C", str(repository), "apply", str(patch)],
                timeout_seconds=timeout_seconds,
                environment=environment,
            )
            logs.append(self._format_result(applied))
            self._require_success(applied, action="apply the solver-view redaction patch")

        remaining = sorted(
            path
            for path in protected_paths
            if (repository / Path(path)).exists() or (repository / Path(path)).is_symlink()
        )
        if remaining:
            raise InvalidTaskError(
                "The sanitized solver source still contains evaluator-owned test paths.",
                hint=(
                    "Make solver-view.patch delete every protected file completely; tests that "
                    "are injected only may already be absent."
                ),
                details={"remaining_protected_paths": remaining},
            )

        git_metadata = sorted(
            repository.rglob(".git"),
            key=lambda candidate: len(candidate.parts),
            reverse=True,
        )
        root_git = repository / ".git"
        if root_git.exists() and root_git not in git_metadata:
            git_metadata.append(root_git)
        for metadata in git_metadata:
            if metadata.is_dir() and not metadata.is_symlink():
                shutil.rmtree(metadata)
            else:
                metadata.unlink(missing_ok=True)

        commands = (
            ["git", "-C", str(repository), "init", "--quiet"],
            ["git", "-C", str(repository), "add", "--force", "-A"],
            [
                "git",
                "-C",
                str(repository),
                "commit",
                "--quiet",
                "-m",
                "taskbundle sanitized solver baseline",
            ],
        )
        for command in commands:
            result = self.runner.run(
                command,
                timeout_seconds=timeout_seconds,
                environment=environment,
            )
            logs.append(self._format_result(result))
            self._require_success(result, action="create the sanitized solver Git root")

        nested_metadata = sorted(
            path.relative_to(repository).as_posix()
            for path in repository.rglob(".git")
            if path != repository / ".git"
        )
        if nested_metadata:
            raise InvalidTaskError(
                "Nested Git metadata remains in the sanitized solver source.",
                details={"paths": nested_metadata},
            )

        revision = self.runner.run(
            ["git", "-C", str(repository), "rev-parse", "HEAD"],
            timeout_seconds=30,
            environment=environment,
        )
        logs.append(self._format_result(revision))
        self._require_success(revision, action="identify the sanitized solver baseline")
        commit = revision.stdout.strip().lower()

        status = self.runner.run(
            ["git", "-C", str(repository), "status", "--porcelain"],
            timeout_seconds=30,
            environment=environment,
        )
        logs.append(self._format_result(status))
        self._require_success(status, action="verify the sanitized solver source")
        if status.stdout.strip():
            raise InvalidTaskError(
                "The sanitized solver source is unexpectedly dirty.",
                details={"status": status.stdout.splitlines()},
            )
        return commit, "\n".join(logs)

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
