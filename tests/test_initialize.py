from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from taskbundle.config import load_bundle
from taskbundle.engine.git import GitClient
from taskbundle.errors import InvalidTaskError
from taskbundle.lifecycle.initialize import build_fingerprint, initialize_task
from taskbundle.process import ProcessResult, ProcessRunner
from taskbundle.session import CommandSession

IMAGE_ID = "sha256:" + "a" * 64
SOLVER_IMAGE_ID = "sha256:" + "d" * 64


class LocalGitFakeDockerRunner:
    def __init__(
        self,
        *,
        dirty_image: bool = False,
        mutate_dockerfile: Path | None = None,
        marker_check_exit: int = 1,
        environment_output: str = "PATH=/usr/bin\n",
    ) -> None:
        self.local = ProcessRunner()
        self.docker_calls: list[tuple[str, ...]] = []
        self.build_context_entries: set[str] | None = None
        self.build_count = 0
        self.base_commit = ""
        self.original_commit = ""
        self.solver_commit = ""
        self.images: dict[str, str] = {}
        self.container_commits: dict[str, str] = {}
        self.container_count = 0
        self.dirty_image = dirty_image
        self.mutate_dockerfile = mutate_dockerfile
        self.mutated_dockerfile = False
        self.build_dockerfiles: list[bytes] = []
        self.marker_check_exit = marker_check_exit
        self.environment_output = environment_output

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
        if argv[0] == "git":
            result = self.local.run(
                argv,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                environment=environment,
                stdin=stdin,
            )
            if list(argv[-3:]) == ["rev-parse", "HEAD"] or list(argv[-2:]) == [
                "rev-parse",
                "HEAD",
            ]:
                self.base_commit = result.stdout.strip()
                if not self.original_commit:
                    self.original_commit = self.base_commit
                else:
                    self.solver_commit = self.base_commit
            return result

        assert argv[0] == "docker"
        command = tuple(argv)
        self.docker_calls.append(command)
        stdout = ""
        if argv[1] == "version":
            if self.mutate_dockerfile is not None and not self.mutated_dockerfile:
                self.mutate_dockerfile.write_text("FROM changed-during-init\n", encoding="utf-8")
                self.mutated_dockerfile = True
            stdout = "29.6.2|29.6.2\n"
        elif argv[1:3] == ["image", "inspect"]:
            tag = argv[-1]
            image = self.images.get(tag)
            if image is None:
                return ProcessResult(
                    argv=command,
                    exit_code=1,
                    stdout="",
                    stderr=f"No such image: {tag}\n",
                    duration_seconds=0.01,
                    timed_out=False,
                )
            stdout = f"{image}\n"
        elif argv[1] == "build":
            self.build_count += 1
            context = Path(argv[-1])
            self.build_context_entries = {entry.name for entry in context.iterdir()}
            self.build_dockerfiles.append((context / "Dockerfile").read_bytes())
            all_text = "".join(
                path.read_text(encoding="utf-8", errors="ignore")
                for path in context.rglob("*")
                if path.is_file()
            )
            assert "theta-hidden-evaluator-only" not in all_text
            tag = argv[argv.index("--tag") + 1]
            self.images[tag] = SOLVER_IMAGE_ID if "-solver:" in tag else IMAGE_ID
            stdout = "built\n"
        elif argv[1] == "create":
            self.container_count += 1
            container_id = f"container-{self.container_count}"
            image_id = SOLVER_IMAGE_ID if SOLVER_IMAGE_ID in argv else IMAGE_ID
            self.container_commits[container_id] = (
                self.solver_commit if image_id == SOLVER_IMAGE_ID else self.original_commit
            )
            stdout = f"{container_id}\n"
        elif argv[1] == "exec":
            container_id, inner = self._exec_parts(argv)
            if inner[:3] == ["git", "rev-parse", "HEAD"]:
                stdout = f"{self.container_commits[container_id]}\n"
            elif inner[:2] == ["git", "status"] or inner[:3] == [
                "git",
                "diff",
                "--stat",
            ]:
                dirty = self.dirty_image and inner[:2] == ["git", "status"]
                stdout = " M calculator.py\n" if dirty else ""
            elif inner[:2] == ["git", "remote"]:
                stdout = ""
            elif inner == ["/usr/bin/env"]:
                stdout = self.environment_output
            elif inner[:2] == ["/bin/sh", "-c"]:
                script = inner[2]
                if "taskbundle-evaluator-path-check" in inner:
                    stdout = ""
                    return ProcessResult(
                        argv=command,
                        exit_code=0,
                        stdout=stdout,
                        stderr="",
                        duration_seconds=0.01,
                        timed_out=False,
                    )
                if "test -e" in script:
                    return ProcessResult(
                        argv=command,
                        exit_code=1,
                        stdout="",
                        stderr="",
                        duration_seconds=0.01,
                        timed_out=False,
                    )
                if "taskbundle-filesystem-secrecy-check" in inner or "git grep" in script:
                    return ProcessResult(
                        argv=command,
                        exit_code=self.marker_check_exit,
                        stdout="",
                        stderr="scan failed\n" if self.marker_check_exit != 1 else "",
                        duration_seconds=0.01,
                        timed_out=False,
                    )
                if "taskbundle-git-metadata-check" in inner:
                    stdout = ""
                else:
                    raise AssertionError(f"Unexpected Docker exec command: {command}")
            else:
                raise AssertionError(f"Unexpected Docker exec command: {command}")
        elif argv[1] == "start":
            stdout = "public tests passed\n"
        elif argv[1] == "rm":
            stdout = "container-123\n"
        else:
            raise AssertionError(f"Unexpected Docker command: {command}")
        return ProcessResult(
            argv=command,
            exit_code=0,
            stdout=stdout,
            stderr="",
            duration_seconds=0.01,
            timed_out=False,
        )


def run_initialize(
    *, valid_bundle_path: Path, runner: LocalGitFakeDockerRunner
) -> dict[str, object]:
    bundle = load_bundle(valid_bundle_path)
    session = CommandSession.start(command_name="init", bundle_path=valid_bundle_path)
    try:
        session.attach_bundle(bundle.manifest.id)
        data = initialize_task(bundle=bundle, session=session, runner=runner)
        session.succeed(data)
        return data
    finally:
        session.close()


def test_fingerprint_excludes_hidden_tests_but_includes_dockerfile(valid_bundle_path: Path) -> None:
    bundle = load_bundle(valid_bundle_path)
    original = build_fingerprint(bundle)

    bundle.test_patch_path.write_text("changed hidden tests\n", encoding="utf-8")
    assert build_fingerprint(load_bundle(valid_bundle_path)) == original

    bundle.dockerfile_path.write_text(
        bundle.dockerfile_path.read_text(encoding="utf-8") + "\nENV EXAMPLE=1\n",
        encoding="utf-8",
    )
    assert build_fingerprint(load_bundle(valid_bundle_path)) != original


@pytest.mark.parametrize("mutation", ["marker", "selected_path", "additional_path"])
def test_fingerprint_includes_the_complete_solver_secrecy_contract(
    valid_bundle_path: Path,
    mutation: str,
) -> None:
    original = build_fingerprint(load_bundle(valid_bundle_path))
    manifest_path = valid_bundle_path / "task.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))

    if mutation == "marker":
        payload["tests"]["pass_to_pass"][0]["marker"] = "new globally unique marker"
    elif mutation == "selected_path":
        payload["tests"]["pass_to_pass"][0]["path"] = "test_public.py"
    else:
        payload["tests"]["additional_protected_paths"] = ["generated/evaluator.bin"]
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    assert build_fingerprint(load_bundle(valid_bundle_path)) != original


def test_initialize_builds_restricted_context_then_reuses_image(valid_bundle_path: Path) -> None:
    runner = LocalGitFakeDockerRunner()

    first = run_initialize(valid_bundle_path=valid_bundle_path, runner=runner)
    second = run_initialize(valid_bundle_path=valid_bundle_path, runner=runner)

    assert first["reused"] is False
    assert second["reused"] is True
    assert first["image_id"] == IMAGE_ID
    assert first["solver_image_id"] == SOLVER_IMAGE_ID
    assert first["solver_base_commit"] == runner.solver_commit
    assert runner.build_count == 2
    assert runner.build_context_entries == {"Dockerfile", "source"}
    assert len([call for call in runner.docker_calls if call[1] == "create"]) == 6
    assert len([call for call in runner.docker_calls if call[1] == "rm"]) == 6
    cache_files = list((valid_bundle_path / ".taskbundle" / "cache").glob("*/build.json"))
    assert len(cache_files) == 1


def test_initialize_builds_and_records_one_immutable_dockerfile_snapshot(
    valid_bundle_path: Path,
) -> None:
    bundle = load_bundle(valid_bundle_path)
    original = bundle.dockerfile_path.read_bytes()
    original_fingerprint = build_fingerprint(bundle)
    runner = LocalGitFakeDockerRunner(mutate_dockerfile=bundle.dockerfile_path)

    result = run_initialize(valid_bundle_path=valid_bundle_path, runner=runner)

    assert result["fingerprint"] == original_fingerprint
    assert runner.build_dockerfiles == [original, original]
    metadata = json.loads(Path(str(result["artifacts"]["metadata"])).read_text(encoding="utf-8"))
    assert metadata["dockerfile_sha256"] == hashlib.sha256(original).hexdigest()
    assert bundle.dockerfile_path.read_bytes() != original


def test_initialize_rejects_an_image_that_changes_the_pinned_repository(
    valid_bundle_path: Path,
) -> None:
    runner = LocalGitFakeDockerRunner(dirty_image=True)

    with pytest.raises(InvalidTaskError, match="not pristine"):
        run_initialize(valid_bundle_path=valid_bundle_path, runner=runner)

    assert len([call for call in runner.docker_calls if call[1] == "rm"]) == 1


def test_initialize_rejects_partial_test_redaction(valid_bundle_path: Path) -> None:
    (valid_bundle_path / "tests" / "solver-view.patch").write_text(
        """diff --git a/test_bucket.py b/test_bucket.py
--- a/test_bucket.py
+++ b/test_bucket.py
@@ -6,3 +6,2 @@
 class BucketTests(unittest.TestCase):
     def test_add_remains_available(self) -> None:
-        self.assertEqual(add(10, 7), 17)
""",
        encoding="utf-8",
    )

    with pytest.raises(InvalidTaskError, match="still contains evaluator-owned test paths"):
        run_initialize(valid_bundle_path=valid_bundle_path, runner=LocalGitFakeDockerRunner())


def test_initialize_marker_scan_errors_fail_closed(valid_bundle_path: Path) -> None:
    with pytest.raises(InvalidTaskError, match="Could not prove evaluator test content"):
        run_initialize(
            valid_bundle_path=valid_bundle_path,
            runner=LocalGitFakeDockerRunner(marker_check_exit=2),
        )


def test_initialize_rejects_test_markers_in_the_solver_environment(
    valid_bundle_path: Path,
) -> None:
    bundle = load_bundle(valid_bundle_path)
    marker = bundle.manifest.tests.fail_to_pass[0].marker

    with pytest.raises(InvalidTaskError, match="container environment"):
        run_initialize(
            valid_bundle_path=valid_bundle_path,
            runner=LocalGitFakeDockerRunner(environment_output=f"LEAK={marker}\n"),
        )


def test_force_rebuild_disables_docker_layer_cache(valid_bundle_path: Path) -> None:
    runner = LocalGitFakeDockerRunner()
    bundle = load_bundle(valid_bundle_path)
    first_session = CommandSession.start(command_name="init", bundle_path=valid_bundle_path)
    second_session = CommandSession.start(command_name="init", bundle_path=valid_bundle_path)
    try:
        first_session.attach_bundle(bundle.manifest.id)
        initialize_task(bundle=bundle, session=first_session, runner=runner)
        second_session.attach_bundle(bundle.manifest.id)
        initialize_task(
            bundle=bundle,
            session=second_session,
            runner=runner,
            force_rebuild=True,
        )
    finally:
        first_session.close()
        second_session.close()

    builds = [call for call in runner.docker_calls if call[1] == "build"]
    assert len(builds) == 4
    assert all("--no-cache" not in build for build in builds[:2])
    assert all("--no-cache" in build for build in builds[2:])


def test_sanitized_baseline_force_tracks_an_upstream_ignored_file(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    ignored = repository / "tracked-but-ignored.txt"
    ignored.write_text("original\n", encoding="utf-8")
    (repository / ".gitignore").write_text("tracked-but-ignored.txt\n", encoding="utf-8")
    runner = ProcessRunner()
    for arguments in (
        ("init", "--quiet"),
        ("config", "user.name", "Task Bundle Tests"),
        ("config", "user.email", "taskbundle@example.invalid"),
        ("add", ".gitignore"),
        ("add", "--force", "tracked-but-ignored.txt"),
        ("commit", "--quiet", "-m", "baseline"),
    ):
        assert runner.run(["git", "-C", str(repository), *arguments]).succeeded
    empty_patch = tmp_path / "empty.patch"
    empty_patch.write_text("", encoding="utf-8")

    GitClient(runner).sanitize_solver_source(
        repository=repository,
        patch=empty_patch,
        protected_paths=set(),
    )

    tracked = runner.run(
        ["git", "-C", str(repository), "ls-files", "--error-unmatch", ignored.name]
    )
    assert tracked.succeeded
