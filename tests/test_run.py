from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from taskbundle.config import Bundle, load_bundle
from taskbundle.errors import ConfigurationError, SolverError, UnresolvedError
from taskbundle.lifecycle.initialize import build_fingerprint, image_tag, sha256_file
from taskbundle.lifecycle.run import run_task
from taskbundle.models import BuildMetadata
from taskbundle.process import ProcessResult
from taskbundle.session import CommandSession, sanitize_arguments

IMAGE_ID = "sha256:" + "c" * 64
CANDIDATE_PATCH = """diff --git a/calculator.py b/calculator.py
index 1111111111111111111111111111111111111111..2222222222222222222222222222222222222222 100644
--- a/calculator.py
+++ b/calculator.py
@@ -1 +1 @@
-return left + right
+return left - right
diff --git a/solver-note.txt b/solver-note.txt
new file mode 100644
index 0000000000000000000000000000000000000000..257cc5642cb1a054f08cc83f2d943e56fd3ebe99
--- /dev/null
+++ b/solver-note.txt
@@ -0,0 +1 @@
+captured
"""


def process_result(
    argv: Sequence[str],
    *,
    exit_code: int | None = 0,
    stdout: str = "",
    stderr: str = "",
    timed_out: bool = False,
) -> ProcessResult:
    return ProcessResult(
        argv=tuple(argv),
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.02,
        timed_out=timed_out,
    )


class RunRunner:
    def __init__(
        self,
        *,
        solver_timeout: bool = False,
        solver_changes: bool = False,
        solver_interrupt: bool = False,
    ) -> None:
        self.solver_timeout = solver_timeout
        self.solver_changes = solver_changes
        self.solver_interrupt = solver_interrupt
        self.calls: list[tuple[str, ...]] = []
        self.container_phases: dict[str, str] = {}
        self.has_candidate = False

    @staticmethod
    def _exec_parts(argv: Sequence[str]) -> tuple[str, list[str]]:
        position = 2
        while argv[position] in {"--workdir", "--env"}:
            position += 2
        return argv[position], list(argv[position + 1 :])

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout_seconds: float | None = None,
        environment: Mapping[str, str] | None = None,
        stdin: str | None = None,
    ) -> ProcessResult:
        del cwd, timeout_seconds, environment, stdin
        command = tuple(argv)
        self.calls.append(command)
        assert argv[0] == "docker"

        if argv[1:3] == ["image", "inspect"]:
            return process_result(argv, stdout=f"{IMAGE_ID}\n")
        if argv[1] == "create":
            name = argv[argv.index("--name") + 1]
            if "post_solver" in name:
                phase = "post_solver"
            elif "solver" in name:
                phase = "solver"
            else:
                phase = "baseline"
            container_id = f"{phase}-container"
            self.container_phases[container_id] = phase
            return process_result(argv, stdout=f"{container_id}\n")
        if argv[1] == "cp":
            if argv[-1].endswith(":/tmp/taskbundle-candidate.patch"):
                self.has_candidate = True
            return process_result(argv)
        if argv[1] in {"start", "rm"}:
            return process_result(argv)
        if argv[1] != "exec":
            raise AssertionError(f"Unexpected Docker command: {command}")

        container_id, inner = self._exec_parts(argv)
        phase = self.container_phases[container_id]
        if phase == "solver":
            if inner[:3] == ["/bin/sh", "-lc", inner[-1]]:
                if self.solver_interrupt:
                    raise KeyboardInterrupt
                if self.solver_timeout:
                    return process_result(argv, exit_code=None, timed_out=True)
                return process_result(argv, stdout="solver completed\n")
            if inner[:2] == ["git", "status"]:
                status = " M calculator.py\n?? solver-note.txt\n" if self.solver_changes else ""
                return process_result(argv, stdout=status)
            if inner[:3] == ["git", "add", "-A"]:
                return process_result(argv)
            if inner[:3] == ["git", "diff", "--cached"]:
                return process_result(argv, stdout=CANDIDATE_PATCH if self.solver_changes else "")
            raise AssertionError(f"Unexpected solver command: {inner}")

        if inner[:3] == ["git", "apply", "--check"]:
            return process_result(argv)
        if inner[:2] == ["git", "apply"]:
            return process_result(argv)
        test_command = inner[-1]
        is_subtract = "test_subtracts" in test_command
        if phase == "baseline" and is_subtract:
            return process_result(argv, exit_code=1, stderr="expected baseline failure\n")
        if phase == "post_solver" and is_subtract and not self.has_candidate:
            return process_result(argv, exit_code=1, stderr="not fixed\n")
        return process_result(argv, stdout="ok\n")


def write_initialized_metadata(bundle: Bundle) -> None:
    fingerprint = build_fingerprint(bundle)
    metadata = BuildMetadata(
        fingerprint=fingerprint,
        bundle_id=bundle.manifest.id,
        repository_url=bundle.manifest.repository.url,
        repository_commit=bundle.manifest.repository.commit,
        dockerfile_sha256=sha256_file(bundle.dockerfile_path),
        image_tag=image_tag(bundle.manifest.id, fingerprint),
        image_id=IMAGE_ID,
        git_version="git version test",
        docker_client_version="test",
        docker_server_version="test",
        created_at=datetime.now(UTC),
    )
    path = bundle.root / ".taskbundle" / "cache" / fingerprint / "build.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metadata.model_dump(mode="json")), encoding="utf-8")


def start_run_session(bundle: Bundle) -> CommandSession:
    session = CommandSession.start(command_name="run", bundle_path=bundle.root)
    session.attach_bundle(bundle.manifest.id)
    return session


def test_stub_run_is_unresolved_and_cleans_up_all_containers(valid_bundle_path: Path) -> None:
    bundle = load_bundle(valid_bundle_path)
    write_initialized_metadata(bundle)
    runner = RunRunner()
    session = start_run_session(bundle)
    try:
        with pytest.raises(UnresolvedError) as caught:
            run_task(bundle=bundle, session=session, runner=runner, repetitions=1)
        session.fail(caught.value)
        rows = session.database.get_test_results(session.command_id)
        artifacts = session.database.get_artifacts(session.command_id)
    finally:
        session.close()

    assert caught.value.details["resolved"] is False
    assert caught.value.details["solver"]["patch_bytes"] == 0
    assert len(rows) == 4
    assert len([call for call in runner.calls if call[1] == "create"]) == 3
    assert len([call for call in runner.calls if call[1] == "rm"]) == 3
    assert any(artifact["kind"] == "run_report" for artifact in artifacts)


def test_command_run_resolves_and_captures_untracked_files(valid_bundle_path: Path) -> None:
    bundle = load_bundle(valid_bundle_path)
    write_initialized_metadata(bundle)
    runner = RunRunner(solver_changes=True)
    session = start_run_session(bundle)
    try:
        result = run_task(
            bundle=bundle,
            session=session,
            solver_name="command",
            solver_command="fix-the-repository",
            repetitions=1,
            runner=runner,
        )
        session.succeed(result)
        patch = (bundle.root / ".taskbundle" / result["solver"]["patch_artifact"]).read_text(
            encoding="utf-8"
        )
    finally:
        session.close()

    assert result["resolved"] is True
    assert "solver-note.txt" in patch
    assert result["solver"]["patch_bytes"] == len(CANDIDATE_PATCH.encode())
    assert any(
        call[-6:] == ("git", "diff", "--cached", "--binary", "--full-index", "--no-ext-diff")
        for call in runner.calls
    )


def test_solver_timeout_is_solver_error_and_skips_post_grading(valid_bundle_path: Path) -> None:
    bundle = load_bundle(valid_bundle_path)
    write_initialized_metadata(bundle)
    runner = RunRunner(solver_timeout=True)
    session = start_run_session(bundle)
    try:
        with pytest.raises(SolverError, match="timed out") as caught:
            run_task(
                bundle=bundle,
                session=session,
                solver_name="command",
                solver_command="slow-agent",
                repetitions=1,
                runner=runner,
            )
        session.fail(caught.value)
    finally:
        session.close()

    assert len([call for call in runner.calls if call[1] == "create"]) == 2
    assert len([call for call in runner.calls if call[1] == "rm"]) == 2


def test_solver_interrupt_still_removes_solver_container(valid_bundle_path: Path) -> None:
    bundle = load_bundle(valid_bundle_path)
    write_initialized_metadata(bundle)
    runner = RunRunner(solver_interrupt=True)
    session = start_run_session(bundle)
    try:
        with pytest.raises(KeyboardInterrupt):
            run_task(
                bundle=bundle,
                session=session,
                solver_name="command",
                solver_command="interrupted-agent",
                repetitions=1,
                runner=runner,
            )
    finally:
        session.close()

    removals = [call for call in runner.calls if call[1] == "rm"]
    assert removals == [
        ("docker", "rm", "--force", "baseline-container"),
        ("docker", "rm", "--force", "solver-container"),
    ]


def test_network_needs_manifest_and_cli_consent(valid_bundle_path: Path) -> None:
    bundle = load_bundle(valid_bundle_path)
    session = start_run_session(bundle)
    try:
        with pytest.raises(ConfigurationError, match="disabled by this bundle"):
            run_task(bundle=bundle, session=session, allow_network=True, runner=RunRunner())
    finally:
        session.close()


def test_solver_command_is_redacted_from_command_arguments() -> None:
    arguments = ["run", ".", "--solver-cmd", "agent --token sensitive", "--json"]

    assert sanitize_arguments(arguments) == ["run", ".", "--solver-cmd", "<redacted>", "--json"]
