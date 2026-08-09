from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from taskbundle.engine.docker import DockerClient
from taskbundle.models import RuntimeSpec
from taskbundle.process import ProcessResult


def result(
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
        duration_seconds=0.01,
        timed_out=timed_out,
    )


class SequenceRunner:
    def __init__(self, responses: list[ProcessResult]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

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
        self.calls.append(tuple(argv))
        response = self.responses.pop(0)
        return ProcessResult(
            argv=tuple(argv),
            exit_code=response.exit_code,
            stdout=response.stdout,
            stderr=response.stderr,
            duration_seconds=response.duration_seconds,
            timed_out=response.timed_out,
        )


def test_smoke_container_is_isolated_and_removed_after_timeout() -> None:
    runner = SequenceRunner(
        [
            result([], stdout="container-123\n"),
            result([], exit_code=None, timed_out=True),
            result([]),
        ]
    )
    docker = DockerClient(runner)

    smoke = docker.run_smoke(
        image_tag="taskbundle/example:abc",
        container_name="taskbundle-example-command",
        workdir="/workspace",
        command="python -m unittest",
        runtime=RuntimeSpec(cpus=1, memory="512m", pids=64),
        timeout_seconds=1,
    )

    assert smoke.timed_out
    create = runner.calls[0]
    assert create[:2] == ("docker", "create")
    assert create[create.index("--network") : create.index("--network") + 2] == (
        "--network",
        "none",
    )
    assert "--cap-drop" in create
    assert "ALL" in create
    assert "no-new-privileges:true" in create
    assert "--volume" not in create
    assert "--mount" not in create
    assert runner.calls[-1] == ("docker", "rm", "--force", "container-123")


def test_missing_image_is_not_an_infrastructure_error() -> None:
    runner = SequenceRunner([result([], exit_code=1, stderr="Error: No such image: missing")])

    assert DockerClient(runner).inspect_image("missing") is None


def test_solver_network_and_secret_forwarding_are_explicit() -> None:
    runner = SequenceRunner(
        [
            result([], stdout="solver-123\n"),
            result([], stdout="ok\n"),
        ]
    )
    docker = DockerClient(runner)
    container_id = docker.create_solver(
        image_tag="taskbundle/example:abc",
        container_name="taskbundle-example-solver",
        workdir="/workspace",
        runtime=RuntimeSpec(cpus=1, memory="512m", pids=64),
        network_enabled=True,
    )
    docker.exec_command(
        container_id=container_id,
        workdir="/workspace",
        command=["agent"],
        timeout_seconds=10,
        environment_names=["OPENAI_API_KEY"],
        environment_values={"TASKBUNDLE_DESCRIPTION": "/tmp/description.md"},
    )

    create = runner.calls[0]
    assert create[create.index("--network") + 1] == "bridge"
    assert "--volume" not in create
    assert "--mount" not in create
    execute = runner.calls[1]
    assert execute[4:6] == ("--env", "OPENAI_API_KEY")
    assert "TASKBUNDLE_DESCRIPTION=/tmp/description.md" in execute
    assert all("secret-value" not in argument for argument in execute)
