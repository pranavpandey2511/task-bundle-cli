from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from taskbundle.config import load_bundle
from taskbundle.lifecycle.initialize import build_fingerprint, initialize_task
from taskbundle.process import ProcessResult, ProcessRunner
from taskbundle.session import CommandSession

IMAGE_ID = "sha256:" + "a" * 64


class LocalGitFakeDockerRunner:
    def __init__(self) -> None:
        self.local = ProcessRunner()
        self.docker_calls: list[tuple[str, ...]] = []
        self.build_context_entries: set[str] | None = None
        self.build_count = 0

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
            return self.local.run(
                argv,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                environment=environment,
                stdin=stdin,
            )

        assert argv[0] == "docker"
        command = tuple(argv)
        self.docker_calls.append(command)
        stdout = ""
        if argv[1] == "version":
            stdout = "29.6.2|29.6.2\n"
        elif argv[1:3] == ["image", "inspect"]:
            stdout = f"{IMAGE_ID}\n"
        elif argv[1] == "build":
            self.build_count += 1
            context = Path(argv[-1])
            self.build_context_entries = {entry.name for entry in context.iterdir()}
            all_text = "".join(
                path.read_text(encoding="utf-8", errors="ignore")
                for path in context.rglob("*")
                if path.is_file()
            )
            assert "theta-hidden-evaluator-only" not in all_text
            stdout = "built\n"
        elif argv[1] == "create":
            stdout = "container-123\n"
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


def test_initialize_builds_restricted_context_then_reuses_image(valid_bundle_path: Path) -> None:
    runner = LocalGitFakeDockerRunner()

    first = run_initialize(valid_bundle_path=valid_bundle_path, runner=runner)
    second = run_initialize(valid_bundle_path=valid_bundle_path, runner=runner)

    assert first["reused"] is False
    assert second["reused"] is True
    assert first["image_id"] == IMAGE_ID
    assert runner.build_count == 1
    assert runner.build_context_entries == {"Dockerfile", "source"}
    assert len([call for call in runner.docker_calls if call[1] == "create"]) == 2
    assert len([call for call in runner.docker_calls if call[1] == "rm"]) == 2
    cache_files = list((valid_bundle_path / ".taskbundle" / "cache").glob("*/build.json"))
    assert len(cache_files) == 1
