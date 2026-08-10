"""Docker CLI adapter with explicit isolation and cleanup."""

from __future__ import annotations

import os
import platform
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from taskbundle.errors import InfrastructureError
from taskbundle.models import DEFAULT_EVALUATOR_PATH, RuntimeSpec
from taskbundle.process import ProcessResult, Runner


@dataclass(frozen=True, slots=True)
class DockerVersions:
    client: str
    server: str


@dataclass(frozen=True, slots=True)
class DockerReadiness:
    """How the Docker daemon became available for the current command."""

    auto_started: bool = False
    provider: str | None = None
    profile: str | None = None
    context: str | None = None

    @property
    def provider_label(self) -> str | None:
        if self.provider is None:
            return None
        return {
            "colima": "Colima",
            "docker-desktop": "Docker Desktop",
        }.get(self.provider)


@dataclass(frozen=True, slots=True)
class DockerProvider:
    name: str
    source: str
    profile: str | None = None
    context: str | None = None
    context_override: str | None = None


@dataclass(frozen=True, slots=True)
class ImageBuildResult:
    image_id: str
    log: str


class DockerClient:
    def __init__(
        self,
        runner: Runner,
        *,
        executable: str = "docker",
        colima_executable: str | None = None,
        docker_desktop_launcher: str | None = None,
        docker_desktop_available: bool | None = None,
        readiness_attempts: int = 60,
        readiness_delay_seconds: float = 2,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.runner = runner
        self.executable = executable
        self.colima_executable = colima_executable or shutil.which("colima")
        self.docker_desktop_launcher = docker_desktop_launcher or shutil.which("open")
        self.docker_desktop_available = (
            self._detect_docker_desktop()
            if docker_desktop_available is None
            else docker_desktop_available
        )
        self.readiness_attempts = readiness_attempts
        self.readiness_delay_seconds = readiness_delay_seconds
        self.sleep = sleep
        self._readiness = DockerReadiness()
        self._context_override: str | None = None

    def _command(self, *arguments: str) -> list[str]:
        command = [self.executable]
        if self._context_override is not None:
            command.extend(["--context", self._context_override])
        return [*command, *arguments]

    @property
    def readiness(self) -> DockerReadiness:
        return self._readiness

    def versions(self) -> DockerVersions:
        result = self._version_result()
        if (
            not result.succeeded
            and self._is_daemon_unavailable(result)
            and self._auto_start_docker_enabled()
        ):
            provider = self._selected_docker_provider()
            if provider is not None and self._provider_auto_start_enabled(provider):
                original_error = result
                self._context_override = provider.context_override
                self._start_provider(provider=provider, original_error=original_error)
                result = self._wait_for_daemon()
                if result.succeeded:
                    self._readiness = DockerReadiness(
                        auto_started=True,
                        provider=provider.name,
                        profile=provider.profile,
                        context=provider.context,
                    )
                else:
                    provider_label = self._provider_label(provider)
                    raise InfrastructureError(
                        f"{provider_label} started, but its Docker daemon did not become "
                        "available.",
                        hint=(
                            "Run `docker version` to inspect the active daemon, then restart "
                            f"{provider_label} before retrying if interrupting existing "
                            "containers is safe."
                        ),
                        details={
                            "docker_provider": provider.name,
                            "docker_context": provider.context,
                            "provider_source": provider.source,
                            "colima_profile": provider.profile,
                            "original_docker_error": self._result_details(original_error),
                            "retry_docker_error": self._result_details(result),
                        },
                    )
        self._require_success(result, action="query Docker client and daemon versions")
        parts = result.stdout.strip().split("|", maxsplit=1)
        if len(parts) != 2 or not all(parts):
            raise InfrastructureError(
                "Docker returned an unexpected version response.",
                details={"output": result.stdout.strip()},
            )
        return DockerVersions(client=parts[0], server=parts[1])

    def _version_result(self) -> ProcessResult:
        return self.runner.run(
            self._command(
                "version",
                "--format",
                "{{.Client.Version}}|{{.Server.Version}}",
            ),
            timeout_seconds=15,
        )

    def _wait_for_daemon(self) -> ProcessResult:
        """Allow the selected provider a bounded interval to expose its Docker daemon."""

        attempts = max(1, self.readiness_attempts)
        result = self._version_result()
        for _ in range(1, attempts):
            if result.succeeded:
                break
            self.sleep(self.readiness_delay_seconds)
            result = self._version_result()
        return result

    @staticmethod
    def _is_daemon_unavailable(result: ProcessResult) -> bool:
        combined = f"{result.stdout}\n{result.stderr}".lower()
        indicators = (
            "cannot connect to the docker daemon",
            "is the docker daemon running",
            "failed to connect to the docker api",
            "daemon is running",
            "connection refused",
            "error during connect",
        )
        return any(indicator in combined for indicator in indicators)

    @staticmethod
    def _auto_start_docker_enabled() -> bool:
        value = os.environ.get("TASKBUNDLE_AUTO_START_DOCKER", "1").strip().lower()
        return value not in {"0", "false", "no", "off"}

    @staticmethod
    def _provider_auto_start_enabled(provider: DockerProvider) -> bool:
        if provider.name != "colima":
            return True
        value = os.environ.get("TASKBUNDLE_AUTO_START_COLIMA", "1").strip().lower()
        return value not in {"0", "false", "no", "off"}

    def _selected_docker_provider(self) -> DockerProvider | None:
        host = os.environ.get("DOCKER_HOST", "")
        match = re.search(r"\.colima/([^/]+)/docker\.sock(?:$|[?#])", host)
        if match is not None:
            return DockerProvider(
                name="colima",
                source="DOCKER_HOST",
                profile=match.group(1),
            )
        if host:
            return None

        preference = os.environ.get("TASKBUNDLE_DOCKER_PROVIDER", "auto").strip().lower()
        preference = preference.replace("_", "-")
        if preference not in {"auto", "colima", "docker-desktop"}:
            raise InfrastructureError(
                "TASKBUNDLE_DOCKER_PROVIDER has an unsupported value.",
                hint="Use `auto`, `colima`, or `docker-desktop`.",
                details={"docker_provider": preference},
            )
        if preference == "colima":
            return self._colima_provider(source="TASKBUNDLE_DOCKER_PROVIDER")
        if preference == "docker-desktop":
            return DockerProvider(
                name="docker-desktop",
                source="TASKBUNDLE_DOCKER_PROVIDER",
                context="desktop-linux",
                context_override="desktop-linux",
            )

        context = self.runner.run(self._command("context", "show"), timeout_seconds=10)
        if not context.succeeded:
            return None
        selected = context.stdout.strip()
        if selected == "colima":
            return DockerProvider(
                name="colima",
                source="docker context",
                profile="default",
                context=selected,
                context_override=selected,
            )
        if selected.startswith("colima-") and len(selected) > len("colima-"):
            return DockerProvider(
                name="colima",
                source="docker context",
                profile=selected[len("colima-") :],
                context=selected,
                context_override=selected,
            )
        if selected == "desktop-linux":
            return DockerProvider(
                name="docker-desktop",
                source="docker context",
                context=selected,
                context_override=selected,
            )
        if selected not in {"", "default"}:
            return None
        if self.colima_executable is not None:
            return self._colima_provider(source="provider discovery")
        if self.docker_desktop_available:
            return DockerProvider(
                name="docker-desktop",
                source="provider discovery",
                context="desktop-linux",
                context_override="desktop-linux",
            )
        return None

    @staticmethod
    def _colima_provider(*, source: str) -> DockerProvider:
        return DockerProvider(
            name="colima",
            source=source,
            profile="default",
            context="colima",
            context_override="colima",
        )

    def _start_provider(
        self,
        *,
        provider: DockerProvider,
        original_error: ProcessResult,
    ) -> None:
        if provider.name == "colima":
            assert provider.profile is not None
            self._start_colima(
                profile=provider.profile,
                context=provider.source,
                original_error=original_error,
            )
            return
        self._start_docker_desktop(provider=provider, original_error=original_error)

    @staticmethod
    def _provider_label(provider: DockerProvider) -> str:
        if provider.name == "docker-desktop":
            return "Docker Desktop"
        return "Colima"

    def _start_colima(
        self,
        *,
        profile: str,
        context: str,
        original_error: ProcessResult,
    ) -> None:
        if self.colima_executable is None:
            raise InfrastructureError(
                "The selected Docker context requires Colima, but Colima is not installed.",
                hint=(
                    "Install Colima and the Docker CLI (for example, "
                    "`brew install colima docker`), "
                    f"then run `colima start {profile}` and retry."
                ),
                details={
                    "docker_context": context,
                    "colima_profile": profile,
                    "docker_error": self._result_details(original_error),
                },
            )
        started = self.runner.run(
            [self.colima_executable, "start", profile],
            timeout_seconds=120,
        )
        if not started.succeeded:
            raise InfrastructureError(
                "Could not start the configured Colima Docker daemon.",
                hint=(
                    f"Run `colima start {profile}` directly to view its diagnostics, then retry "
                    "the Task Bundle command."
                ),
                details={
                    "docker_context": context,
                    "colima_profile": profile,
                    "docker_error": self._result_details(original_error),
                    "colima_start": self._result_details(started),
                },
            )

    def _start_docker_desktop(
        self,
        *,
        provider: DockerProvider,
        original_error: ProcessResult,
    ) -> None:
        if not self.docker_desktop_available or self.docker_desktop_launcher is None:
            raise InfrastructureError(
                "The selected Docker context requires Docker Desktop, but it is not installed.",
                hint="Install Docker Desktop, select Colima, or start a compatible Docker daemon.",
                details={
                    "docker_context": provider.context,
                    "provider_source": provider.source,
                    "docker_error": self._result_details(original_error),
                },
            )
        started = self.runner.run(
            [self.docker_desktop_launcher, "-a", "Docker"],
            timeout_seconds=30,
        )
        if not started.succeeded:
            raise InfrastructureError(
                "Could not start Docker Desktop.",
                hint="Open Docker Desktop directly to view its diagnostics, then retry.",
                details={
                    "docker_context": provider.context,
                    "provider_source": provider.source,
                    "docker_error": self._result_details(original_error),
                    "docker_desktop_start": self._result_details(started),
                },
            )

    @staticmethod
    def _detect_docker_desktop() -> bool:
        if platform.system() != "Darwin":
            return False
        return any(
            path.exists()
            for path in (
                Path("/Applications/Docker.app"),
                Path.home() / "Applications" / "Docker.app",
            )
        )

    @staticmethod
    def _result_details(result: ProcessResult) -> dict[str, object]:
        return {
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "stdout": result.stdout[-4000:],
            "stderr": result.stderr[-4000:],
        }

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
        no_cache: bool = False,
    ) -> ImageBuildResult:
        command = self._command(
            "build",
            "--progress=plain",
            "--file",
            str(context / "Dockerfile"),
            "--tag",
            image_tag,
        )
        if no_cache:
            command.append("--no-cache")
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
        evaluator_path: str = ":".join(DEFAULT_EVALUATOR_PATH),
    ) -> str:
        create = self._isolated_create_command(
            image_tag=image_tag,
            container_name=container_name,
            workdir=workdir,
            runtime=runtime,
            network="none",
            path_override=evaluator_path,
            container_command=[
                "-c",
                "while :; do /bin/sleep 3600; done",
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
    ) -> str:
        create = self._isolated_create_command(
            image_tag=image_tag,
            container_name=container_name,
            workdir=workdir,
            runtime=runtime,
            network="none",
            container_command=["-c", "while :; do /bin/sleep 3600; done"],
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

    def stream_file(self, *, source: Path, container_id: str, destination: str) -> None:
        try:
            content = source.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise InfrastructureError(
                f"Could not read container input {source}: {error}"
            ) from error
        self.stream_text(
            content=content,
            container_id=container_id,
            destination=destination,
        )

    def stream_text(self, *, content: str, container_id: str, destination: str) -> None:
        result = self.runner.run(
            self._command(
                "exec",
                "--interactive",
                container_id,
                "/bin/sh",
                "-c",
                'umask 077 && exec /bin/cat > "$1"',
                "taskbundle-copy",
                destination,
            ),
            timeout_seconds=60,
            stdin=content,
        )
        self._require_success(result, action=f"stream evaluator input to {destination}")

    def exec_command(
        self,
        *,
        container_id: str,
        workdir: str,
        command: list[str],
        timeout_seconds: int,
        trusted_path: str | None = None,
    ) -> ProcessResult:
        docker_arguments = ["exec", "--workdir", workdir]
        if trusted_path is not None:
            docker_arguments.extend(["--env", f"PATH={trusted_path}"])
        docker_arguments.extend([container_id, *command])
        return self.runner.run(
            self._command(*docker_arguments),
            timeout_seconds=timeout_seconds,
        )

    def remove_container(self, container_id: str) -> None:
        result = self.runner.run(
            self._command("rm", "--force", "--volumes", container_id),
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
        evaluator_path: str = ":".join(DEFAULT_EVALUATOR_PATH),
    ) -> ProcessResult:
        create = self._isolated_create_command(
            image_tag=image_tag,
            container_name=container_name,
            workdir=workdir,
            runtime=runtime,
            network="none",
            path_override=evaluator_path,
            container_command=["-c", command],
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
                self._command("rm", "--force", "--volumes", container_id),
                timeout_seconds=30,
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
        path_override: str | None = None,
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
            "--read-only",
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,size={runtime.tmpfs_size.lower()}",
            "--env",
            "HOME=/tmp/taskbundle-home",
            "--env",
            "XDG_CACHE_HOME=/tmp/taskbundle-cache",
            "--env",
            "XDG_CONFIG_HOME=/tmp/taskbundle-config",
            "--env",
            "XDG_DATA_HOME=/tmp/taskbundle-data",
            "--mount",
            f"type=volume,destination={workdir}",
            "--workdir",
            workdir,
        )
        if runtime.user:
            create.extend(["--user", runtime.user])
        if path_override is not None:
            create.extend(["--env", f"PATH={path_override}"])
        create.extend(["--entrypoint", "/bin/sh"])
        create.append(image_tag)
        create.extend(container_command)
        return create

    @staticmethod
    def isolation_profile(*, runtime: RuntimeSpec, workdir: str, network: str) -> dict[str, object]:
        return {
            "network": network,
            "read_only_rootfs": True,
            "ephemeral_workdir": workdir,
            "tmpfs": {"path": "/tmp", "size": runtime.tmpfs_size.lower()},
            "ephemeral_home": "/tmp/taskbundle-home",
            "ephemeral_xdg_dirs": True,
            "cpus": runtime.cpus,
            "memory": runtime.memory.lower(),
            "pids": runtime.pids,
            "capabilities": [],
            "no_new_privileges": True,
            "host_mounts": False,
            "docker_socket": False,
            "entrypoint": "/bin/sh",
        }

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
