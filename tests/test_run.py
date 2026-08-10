from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from taskbundle.config import Bundle, load_bundle
from taskbundle.errors import ConfigurationError, InvalidTaskError, SolverError, UnresolvedError
from taskbundle.lifecycle.initialize import (
    build_fingerprint,
    image_tag,
    sha256_file,
    solver_secrecy_contract_sha256,
)
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

SHADOW_RUNNER_PATCH = (
    CANDIDATE_PATCH
    + """diff --git a/unittest.py b/unittest.py
new file mode 100644
index 0000000000000000000000000000000000000000..e69de29bb2d1d6434b8b29ae775ad8c2e48c5391 100644
--- /dev/null
+++ b/unittest.py
"""
)


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
        base_commit: str,
        solver_timeout: bool = False,
        solver_changes: bool = False,
        solver_interrupt: bool = False,
        breaks_regression: bool = False,
        solver_commits: bool = False,
        post_solver_timeout: bool = False,
        hidden_patch_conflict: bool = False,
        mutate_test_patch: Path | None = None,
        marker_check_exit: int = 1,
        candidate_patch: str = CANDIDATE_PATCH,
    ) -> None:
        self.base_commit = base_commit
        self.solver_timeout = solver_timeout
        self.solver_changes = solver_changes
        self.solver_interrupt = solver_interrupt
        self.breaks_regression = breaks_regression
        self.solver_commits = solver_commits
        self.post_solver_timeout = post_solver_timeout
        self.hidden_patch_conflict = hidden_patch_conflict
        self.mutate_test_patch = mutate_test_patch
        self.marker_check_exit = marker_check_exit
        self.candidate_patch = candidate_patch
        self.calls: list[tuple[str, ...]] = []
        self.container_phases: dict[str, str] = {}
        self.has_candidate = False
        self.solver_completed = False
        self.streams: list[tuple[str, str, str]] = []

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
        del cwd, timeout_seconds, environment
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
        if argv[1:3] == ["exec", "--interactive"]:
            container_id = argv[3]
            phase = self.container_phases[container_id]
            self.streams.append((phase, argv[-1], stdin or ""))
            if argv[-1] == "/tmp/taskbundle-candidate.patch":
                self.has_candidate = True
            return process_result(argv)
        if argv[1] in {"start", "rm"}:
            return process_result(argv)
        if argv[1] != "exec":
            raise AssertionError(f"Unexpected Docker command: {command}")

        container_id, inner = self._exec_parts(argv)
        phase = self.container_phases[container_id]
        if phase == "solver":
            if inner[:3] == ["git", "apply", "--check"] or inner[:2] == ["git", "apply"]:
                return process_result(argv)
            if inner[:2] == ["git", "remote"]:
                return process_result(argv)
            if inner[:2] == ["/bin/sh", "-c"]:
                if "taskbundle-git-metadata-check" in inner:
                    return process_result(argv)
                if "test -e" in inner[2]:
                    return process_result(argv, exit_code=1)
                if "taskbundle-filesystem-secrecy-check" in inner or "git grep" in inner[2]:
                    return process_result(argv, exit_code=self.marker_check_exit)
            if inner[:3] == ["/bin/sh", "-lc", inner[-1]]:
                if self.solver_interrupt:
                    raise KeyboardInterrupt
                if self.solver_timeout:
                    return process_result(argv, exit_code=None, timed_out=True)
                if self.mutate_test_patch is not None:
                    self.mutate_test_patch.write_text(
                        "changed while the solver was running\n",
                        encoding="utf-8",
                    )
                self.solver_completed = True
                return process_result(argv, stdout="solver completed\n")
            if inner[:3] == ["git", "rev-parse", "HEAD"]:
                if self.solver_commits and self.solver_completed:
                    return process_result(argv, stdout=f"{'d' * 40}\n")
                return process_result(argv, stdout=f"{'e' * 40}\n")
            if inner[:2] == ["git", "status"]:
                status = (
                    " M calculator.py\n?? solver-note.txt\n"
                    if self.solver_changes and self.solver_completed and not self.solver_commits
                    else ""
                )
                return process_result(argv, stdout=status)
            if inner[:3] == ["git", "diff", "--stat"]:
                stat = "calculator.py | 2 +-\n" if self.solver_changes else ""
                return process_result(argv, stdout=stat)
            if inner[:3] == ["git", "add", "-A"]:
                return process_result(argv)
            if inner[:3] == ["git", "diff", "--cached"]:
                return process_result(
                    argv,
                    stdout=self.candidate_patch if self.solver_changes else "",
                )
            raise AssertionError(f"Unexpected solver command: {inner}")

        if inner[:3] == ["git", "apply", "--check"]:
            if (
                self.hidden_patch_conflict
                and phase == "post_solver"
                and inner[-1] == "/tmp/taskbundle-tests.patch"
            ):
                return process_result(argv, exit_code=1, stderr="candidate conflicts with tests\n")
            return process_result(argv)
        if inner[:2] == ["git", "apply"]:
            return process_result(argv)
        if inner[:1] == ["/bin/rm"]:
            return process_result(argv)
        if inner[:3] == ["git", "rev-parse", "HEAD"]:
            return process_result(argv, stdout=f"{self.base_commit}\n")
        if inner[:2] == ["git", "status"]:
            return process_result(argv)
        if inner[:3] == ["git", "diff", "--stat"]:
            return process_result(argv)
        if inner and inner[0] == "grep":
            return process_result(argv)
        test_command = inner[-1]
        is_subtract = "test_subtracts" in test_command
        if phase == "baseline" and is_subtract:
            return process_result(argv, exit_code=1, stderr="expected baseline failure\n")
        if phase == "post_solver" and not is_subtract and self.breaks_regression:
            return process_result(argv, exit_code=1, stderr="regression introduced\n")
        if phase == "post_solver" and is_subtract and not self.has_candidate:
            return process_result(argv, exit_code=1, stderr="not fixed\n")
        if phase == "post_solver" and is_subtract and self.post_solver_timeout:
            return process_result(argv, exit_code=None, timed_out=True)
        return process_result(argv, stdout="ok\n")


def write_initialized_metadata(bundle: Bundle) -> None:
    fingerprint = build_fingerprint(bundle)
    metadata = BuildMetadata(
        fingerprint=fingerprint,
        bundle_id=bundle.manifest.id,
        repository_url=bundle.manifest.repository.url,
        repository_commit=bundle.manifest.repository.commit,
        dockerfile_sha256=sha256_file(bundle.dockerfile_path),
        solver_view_sha256=sha256_file(bundle.solver_view_patch_path),
        secrecy_contract_sha256=solver_secrecy_contract_sha256(bundle),
        image_tag=image_tag(bundle.manifest.id, fingerprint),
        image_id=IMAGE_ID,
        solver_image_tag=f"taskbundle/{bundle.manifest.id}-solver:{fingerprint[:16]}",
        solver_image_id=IMAGE_ID,
        solver_base_commit="e" * 40,
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
    runner = RunRunner(base_commit=bundle.manifest.repository.commit)
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
    assert len([call for call in runner.calls if call[1] == "create"]) == 5
    assert len([call for call in runner.calls if call[1] == "rm"]) == 5
    assert any(artifact["kind"] == "run_report" for artifact in artifacts)
    assert any(artifact["kind"] == "execution_provenance" for artifact in artifacts)
    assert (
        len([artifact for artifact in artifacts if artifact["kind"] == "repository_snapshot"]) == 6
    )


def test_command_run_resolves_and_captures_untracked_files(valid_bundle_path: Path) -> None:
    bundle = load_bundle(valid_bundle_path)
    write_initialized_metadata(bundle)
    runner = RunRunner(base_commit=bundle.manifest.repository.commit, solver_changes=True)
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
    assert len(result["snapshot_artifacts"]) == 6
    assert len(result["provenance"]["execution_fingerprint"]) == 64
    assert any(
        runner._exec_parts(call)[1][:3] == ["git", "add", "-A"]
        for call in runner.calls
        if call[1] == "exec" and call[2] != "--interactive"
    )
    assert any(
        runner._exec_parts(call)[1][:6]
        == ["git", "diff", "--cached", "--binary", "--full-index", "--no-ext-diff"]
        for call in runner.calls
        if call[1] == "exec" and call[2] != "--interactive"
    )
    solver_streams = [stream for stream in runner.streams if stream[0] == "solver"]
    assert [destination for _phase, destination, _content in solver_streams] == [
        "/tmp/taskbundle-description.md"
    ]
    post_solver_destinations = [
        destination for phase, destination, _content in runner.streams if phase == "post_solver"
    ]
    assert post_solver_destinations == [
        "/tmp/taskbundle-tests.patch",
        "/tmp/taskbundle-candidate.patch",
        "/tmp/taskbundle-tests.patch",
        "/tmp/taskbundle-candidate.patch",
    ]
    assert all(IMAGE_ID in call for call in runner.calls if call[1] == "create")


def test_command_run_captures_changes_even_when_solver_commits(
    valid_bundle_path: Path,
) -> None:
    bundle = load_bundle(valid_bundle_path)
    write_initialized_metadata(bundle)
    runner = RunRunner(
        base_commit=bundle.manifest.repository.commit,
        solver_changes=True,
        solver_commits=True,
    )
    session = start_run_session(bundle)
    try:
        result = run_task(
            bundle=bundle,
            session=session,
            solver_name="command",
            solver_command="fix-and-commit",
            repetitions=1,
            runner=runner,
        )
    finally:
        session.close()

    assert result["resolved"] is True
    assert result["solver"]["patch_bytes"] == len(CANDIDATE_PATCH.encode())
    assert result["solver"]["solver_base_commit"] == "e" * 40
    captured_diff = next(call for call in runner.calls if "--cached" in call and "--binary" in call)
    assert "e" * 40 in captured_diff
    assert captured_diff.index("e" * 40) > captured_diff.index("--no-ext-diff")


def test_run_rejects_candidate_test_runner_shadow_files_before_grading(
    valid_bundle_path: Path,
) -> None:
    bundle = load_bundle(valid_bundle_path)
    write_initialized_metadata(bundle)
    runner = RunRunner(
        base_commit=bundle.manifest.repository.commit,
        solver_changes=True,
        candidate_patch=SHADOW_RUNNER_PATCH,
    )
    session = start_run_session(bundle)
    try:
        with pytest.raises(SolverError, match="candidate-edit policy") as caught:
            run_task(
                bundle=bundle,
                session=session,
                solver_name="command",
                solver_command="fix-and-shadow-runner",
                repetitions=1,
                runner=runner,
            )
    finally:
        session.close()

    assert caught.value.details["outside_allowed_paths"] == ["unittest.py"]
    assert not any(
        phase == "post_solver" and destination == "/tmp/taskbundle-tests.patch"
        for phase, destination, _content in runner.streams
    )


def test_run_uses_one_immutable_hidden_patch_snapshot(valid_bundle_path: Path) -> None:
    bundle = load_bundle(valid_bundle_path)
    write_initialized_metadata(bundle)
    original = bundle.test_patch_path.read_text(encoding="utf-8")
    runner = RunRunner(
        base_commit=bundle.manifest.repository.commit,
        solver_changes=True,
        mutate_test_patch=bundle.test_patch_path,
    )
    session = start_run_session(bundle)
    try:
        result = run_task(
            bundle=bundle,
            session=session,
            solver_name="command",
            solver_command="mutate-host-input",
            repetitions=1,
            runner=runner,
        )
    finally:
        session.close()

    assert result["resolved"] is True
    streamed_hidden = [
        content
        for _phase, destination, content in runner.streams
        if destination == "/tmp/taskbundle-tests.patch"
    ]
    assert streamed_hidden and set(streamed_hidden) == {original}
    assert bundle.test_patch_path.read_text(encoding="utf-8") != original


def test_solver_view_redacts_base_p2p_and_discards_original_git_history(
    valid_bundle_path: Path,
) -> None:
    solver_view_patch = (
        "diff --git a/test_public.py b/test_public.py\n"
        "deleted file mode 100644\n"
        "--- a/test_public.py\n"
        "+++ /dev/null\n"
        "@@ -1,12 +0,0 @@\n"
        "-import unittest\n"
        "-\n"
        "-from calculator import add\n"
        "-\n"
        "-\n"
        "-class PublicTests(unittest.TestCase):\n"
        "-    def test_add(self) -> None:\n"
        "-        self.assertEqual(add(2, 3), 5)\n"
        "-\n"
        "-\n"
        '-if __name__ == "__main__":\n'
        "-    unittest.main()\n"
    )
    hidden_patch = """diff --git a/test_hidden.py b/test_hidden.py
new file mode 100644
--- /dev/null
+++ b/test_hidden.py
@@ -0,0 +1,8 @@
+import unittest
+
+from calculator import subtract
+
+
+class HiddenTests(unittest.TestCase):
+    def test_subtracts(self) -> None:
+        self.assertEqual(subtract(10, 7), 3)
"""
    payload = json.loads((valid_bundle_path / "task.json").read_text(encoding="utf-8"))
    payload["tests"]["pass_to_pass"] = [
        {
            "id": "public-add",
            "command": "python -m unittest -q test_public.PublicTests.test_add",
            "path": "test_public.py",
            "marker": "self.assertEqual(add(2, 3), 5)",
            "timeout_seconds": 30,
        }
    ]
    (valid_bundle_path / "task.json").write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )
    (valid_bundle_path / "tests" / "solver-view.patch").write_text(
        solver_view_patch,
        encoding="utf-8",
    )
    (valid_bundle_path / "tests" / "hidden.patch").write_text(
        hidden_patch,
        encoding="utf-8",
    )
    bundle = load_bundle(valid_bundle_path)
    write_initialized_metadata(bundle)
    runner = RunRunner(base_commit=bundle.manifest.repository.commit, solver_changes=True)
    session = start_run_session(bundle)
    try:
        result = run_task(
            bundle=bundle,
            session=session,
            solver_name="command",
            solver_command="solve-sanitized-task",
            repetitions=1,
            runner=runner,
        )
    finally:
        session.close()

    assert result["resolved"] is True
    assert result["solver"]["solver_base_commit"] == "e" * 40
    assert "test_public.py" not in result["solver"]["patch_paths"]
    solver_destinations = [
        destination for phase, destination, _content in runner.streams if phase == "solver"
    ]
    assert solver_destinations == ["/tmp/taskbundle-description.md"]
    assert all(
        destination not in {"/tmp/taskbundle-tests.patch", "/tmp/taskbundle-gold.patch"}
        for destination in solver_destinations
    )
    worktree_checks = [
        call
        for call in runner.calls
        if call[1] == "exec" and "taskbundle-filesystem-secrecy-check" in call
    ]
    assert worktree_checks == []


def test_solver_description_cannot_contain_evaluator_marker(
    valid_bundle_path: Path,
) -> None:
    bundle = load_bundle(valid_bundle_path)
    marker = bundle.manifest.tests.fail_to_pass[0].marker
    bundle.description_path.write_text(f"Leaked source: {marker}\n", encoding="utf-8")
    write_initialized_metadata(bundle)
    runner = RunRunner(base_commit=bundle.manifest.repository.commit)
    session = start_run_session(bundle)
    try:
        with pytest.raises(InvalidTaskError, match="visible in the solver description"):
            run_task(
                bundle=bundle,
                session=session,
                solver_name="command",
                solver_command="never-started",
                repetitions=1,
                runner=runner,
            )
    finally:
        session.close()

    assert runner.solver_completed is False


def test_solver_timeout_is_solver_error_and_skips_post_grading(valid_bundle_path: Path) -> None:
    bundle = load_bundle(valid_bundle_path)
    write_initialized_metadata(bundle)
    runner = RunRunner(base_commit=bundle.manifest.repository.commit, solver_timeout=True)
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

    assert len([call for call in runner.calls if call[1] == "create"]) == 3
    assert len([call for call in runner.calls if call[1] == "rm"]) == 3


def test_post_solver_timeout_is_unresolved_not_an_invalid_task(valid_bundle_path: Path) -> None:
    bundle = load_bundle(valid_bundle_path)
    write_initialized_metadata(bundle)
    runner = RunRunner(
        base_commit=bundle.manifest.repository.commit,
        solver_changes=True,
        post_solver_timeout=True,
    )
    session = start_run_session(bundle)
    try:
        with pytest.raises(UnresolvedError) as caught:
            run_task(
                bundle=bundle,
                session=session,
                solver_name="command",
                solver_command="candidate-that-hangs-target",
                repetitions=1,
                runner=runner,
            )
    finally:
        session.close()

    target = caught.value.details["post_solver"]["phases"]["post_solver"]["fail_to_pass"][0]
    assert target["observations"] == ["timeout"]


def test_candidate_conflict_with_hidden_tests_is_unresolved(valid_bundle_path: Path) -> None:
    bundle = load_bundle(valid_bundle_path)
    write_initialized_metadata(bundle)
    runner = RunRunner(
        base_commit=bundle.manifest.repository.commit,
        solver_changes=True,
        hidden_patch_conflict=True,
    )
    session = start_run_session(bundle)
    try:
        with pytest.raises(UnresolvedError, match="hidden test patch") as caught:
            run_task(
                bundle=bundle,
                session=session,
                solver_name="command",
                solver_command="candidate-conflicts-with-tests",
                repetitions=1,
                runner=runner,
            )
    finally:
        session.close()

    report_path = valid_bundle_path / ".taskbundle" / caught.value.details["run_artifact"]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["resolved"] is False
    assert report["post_solver_error"]["kind"] == "unresolved"


def test_solver_interrupt_still_removes_solver_container(valid_bundle_path: Path) -> None:
    bundle = load_bundle(valid_bundle_path)
    write_initialized_metadata(bundle)
    runner = RunRunner(base_commit=bundle.manifest.repository.commit, solver_interrupt=True)
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
        ("docker", "rm", "--force", "--volumes", "baseline-container"),
        ("docker", "rm", "--force", "--volumes", "baseline-container"),
        ("docker", "rm", "--force", "--volumes", "solver-container"),
    ]


def test_network_is_rejected_by_the_strict_secrecy_contract(valid_bundle_path: Path) -> None:
    bundle = load_bundle(valid_bundle_path)
    session = start_run_session(bundle)
    try:
        with pytest.raises(ConfigurationError, match="strict test-secrecy contract"):
            run_task(
                bundle=bundle,
                session=session,
                allow_network=True,
                runner=RunRunner(base_commit=bundle.manifest.repository.commit),
            )
    finally:
        session.close()


def test_solver_command_is_redacted_from_command_arguments() -> None:
    arguments = ["run", ".", "--solver-cmd", "agent --token sensitive", "--json"]

    assert sanitize_arguments(arguments) == ["run", ".", "--solver-cmd", "<redacted>", "--json"]


def test_run_reports_a_fixed_target_and_broken_regression(valid_bundle_path: Path) -> None:
    bundle = load_bundle(valid_bundle_path)
    write_initialized_metadata(bundle)
    runner = RunRunner(
        base_commit=bundle.manifest.repository.commit,
        solver_changes=True,
        breaks_regression=True,
    )
    session = start_run_session(bundle)
    try:
        with pytest.raises(UnresolvedError) as caught:
            run_task(
                bundle=bundle,
                session=session,
                solver_name="command",
                solver_command="fix-target-break-regression",
                repetitions=1,
                runner=runner,
            )
        session.fail(caught.value)
    finally:
        session.close()

    post_solver = caught.value.details["post_solver"]
    regression = post_solver["phases"]["post_solver"]["pass_to_pass"][0]
    target = post_solver["phases"]["post_solver"]["fail_to_pass"][0]
    assert regression["observations"] == ["fail"]
    assert regression["matches"] is False
    assert target["observations"] == ["pass"]
    assert target["matches"] is True
    assert caught.value.details["resolved"] is False
