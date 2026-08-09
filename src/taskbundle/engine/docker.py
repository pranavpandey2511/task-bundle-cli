"""Docker CLI adapter with explicit isolation and cleanup."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from taskbundle.errors import InfrastructureError
from taskbundle.models import RuntimeSpec
from taskbundle.process import ProcessResult, Runner


@dataclass(frozen=True, slots=True)
class DockerVersions:
    client: str
    server: str


@dataclass(frozen=True, slots=True)
class ImageBuildResult:
    image_id: str
    log: str


class DockerClient:
    def __init__(self, runner: Runner, *, executable: str = "docker") -> None:
        self.runner = runner
        self.executable = executable

    def _command(self, *arguments: str) -> list[str]:
        return [self.executable, *arguments]

    def versions(self) -> DockerVersions:
        result = self.runner.run(
            self._command(
                "version",
                "--format",
                "{{.Client.Version}}|{{.Server.Version}}",
            ),
            timeout_seconds=15,
        )
        self._require_success(result, action="query Docker client and daemon versions")
        parts = result.stdout.strip().split("|", maxsplit=1)
        if len(parts) != 2 or not all(parts):
            raise InfrastructureError(
                "Docker returned an unexpected version response.",
                details={"output": result.stdout.strip()},
            )
        return DockerVersions(client=parts[0], server=parts[1])

    def inspect_image(self, image_tag: str) -> str | None:
        result = self.runner.run(
            self._command("image", "inspect", "--format", "{{.Id}}", image_tag),
            timeout_seconds=30,
        )
        if result.succeeded:
            image_id = result.stdout.strip()
            if not image_id:
                raise InfrastructureError(f"Docker returned an empty image ID for {image_tag}.")
            return image_id
        combined = f"{result.stdout}\n{result.stderr}".lower()
        if "no such image" in combined:
            return None
        self._require_success(result, action=f"inspect Docker image {image_tag}")
        return None

    def build_image(
        self,
        *,
        context: Path,
        image_tag: str,
        labels: dict[str, str],
        timeout_seconds: int,
    ) -> ImageBuildResult:
        command = self._command(
            "build",
            "--progress=plain",
            "--file",
            str(context / "Dockerfile"),
            "--tag",
            image_tag,
        )
        for key, value in sorted(labels.items()):
            command.extend(["--label", f"{key}={value}"])
        command.append(str(context))
        result = self.runner.run(command, timeout_seconds=timeout_seconds)
        log = self._format_result(result)
        self._require_success(result, action=f"build Docker image {image_tag}")
        image_id = self.inspect_image(image_tag)
        if image_id is None:
            raise InfrastructureError(f"Built Docker image could not be inspected: {image_tag}")
        return ImageBuildResult(image_id=image_id, log=log)

    def create_evaluator(
        self,
        *,
        image_tag: str,
        container_name: str,
        workdir: str,
        runtime: RuntimeSpec,
    ) -> str:
        create = self._isolated_create_command(
            image_tag=image_tag,
            container_name=container_name,
            workdir=workdir,
            runtime=runtime,
            network="none",
            container_command=[
                "/bin/sh",
                "-lc",
                "while :; do sleep 3600; done",
            ],
        )
        created = self.runner.run(create, timeout_seconds=30)
        self._require_success(created, action="create evaluator container")
        container_id = created.stdout.strip()
        if not container_id:
            raise InfrastructureError("Docker returned an empty evaluator container ID.")
        return container_id

    def create_solver(
        self,
        *,
        image_tag: str,
        container_name: str,
        workdir: str,
        runtime: RuntimeSpec,
        network_enabled: bool,
    ) -> str:
        create = self._isolated_create_command(
            image_tag=image_tag,
            container_name=container_name,
            workdir=workdir,
            runtime=runtime,
            network="bridge" if network_enabled else "none",
            container_command=["/bin/sh", "-lc", "while :; do sleep 3600; done"],
        )
        created = self.runner.run(create, timeout_seconds=30)
        self._require_success(created, action="create solver container")
        container_id = created.stdout.strip()
        if not container_id:
            raise InfrastructureError("Docker returned an empty solver container ID.")
        return container_id

    def start_detached(self, container_id: str) -> None:
        result = self.runner.run(
            self._command("start", container_id),
            timeout_seconds=30,
        )
        self._require_success(result, action=f"start evaluator container {container_id}")

    def copy_file(self, *, source: Path, container_id: str, destination: str) -> None:
        result = self.runner.run(
            self._command("cp", str(source), f"{container_id}:{destination}"),
            timeout_seconds=60,
        )
        self._require_success(result, action=f"copy evaluator input to {destination}")

    def exec_command(
        self,
        *,
        container_id: str,
        workdir: str,
        command: list[str],
        timeout_seconds: int,
        environment_names: list[str] | None = None,
        environment_values: dict[str, str] | None = None,
    ) -> ProcessResult:
        docker_arguments = ["exec", "--workdir", workdir]
        for name in environment_names or []:
            docker_arguments.extend(["--env", name])
        for name, value in sorted((environment_values or {}).items()):
            docker_arguments.extend(["--env", f"{name}={value}"])
        docker_arguments.extend([container_id, *command])
        return self.runner.run(
            self._command(*docker_arguments),
            timeout_seconds=timeout_seconds,
        )

    def remove_container(self, container_id: str) -> None:
        result = self.runner.run(
            self._command("rm", "--force", container_id),
            timeout_seconds=30,
        )
        self._require_success(result, action=f"remove evaluator container {container_id}")

    def run_smoke(
        self,
        *,
        image_tag: str,
        container_name: str,
        workdir: str,
        command: str,
        runtime: RuntimeSpec,
        timeout_seconds: int,
    ) -> ProcessResult:
        create = self._isolated_create_command(
            image_tag=image_tag,
            container_name=container_name,
            workdir=workdir,
            runtime=runtime,
            network="none",
            container_command=["/bin/sh", "-lc", command],
        )

        created = self.runner.run(create, timeout_seconds=30)
        self._require_success(created, action="create smoke-test container")
        container_id = created.stdout.strip()
        if not container_id:
            raise InfrastructureError("Docker returned an empty smoke-test container ID.")

        result: ProcessResult | None = None
        cleanup: ProcessResult | None = None
        try:
            result = self.runner.run(
                self._command("start", "--attach", container_id),
                timeout_seconds=timeout_seconds,
            )
        finally:
            cleanup = self.runner.run(
                self._command("rm", "--force", container_id), timeout_seconds=30
            )

        if not cleanup.succeeded:
            raise InfrastructureError(
                f"Could not remove smoke-test container {container_id}.",
                hint="Remove it manually with `docker rm --force`.",
                details={
                    "container_id": container_id,
                    "exit_code": cleanup.exit_code,
                    "stderr": cleanup.stderr[-4000:],
                },
            )
        assert result is not None
        return result

    def _isolated_create_command(
        self,
        *,
        image_tag: str,
        container_name: str,
        workdir: str,
        runtime: RuntimeSpec,
        network: str,
        container_command: list[str],
    ) -> list[str]:
        create = self._command(
            "create",
            "--name",
            container_name,
            "--network",
            network,
            "--cpus",
            str(runtime.cpus),
            "--memory",
            runtime.memory.lower(),
            "--pids-limit",
            str(runtime.pids),
            "--security-opt",
            "no-new-privileges:true",
            "--cap-drop",
            "ALL",
            "--workdir",
            workdir,
        )
        if runtime.user:
            create.extend(["--user", runtime.user])
        create.append(image_tag)
        create.extend(container_command)
        return create

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
        return (
            f"exit={result.exit_code} timeout={result.timed_out} "
            f"duration={result.duration_seconds:.3f}s\n"
            f"{result.stdout}{result.stderr}"
        )
