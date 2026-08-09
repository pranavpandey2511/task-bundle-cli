"""Deterministic repository materialization and image initialization."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from taskbundle import __version__
from taskbundle.config import Bundle
from taskbundle.engine.docker import DockerClient
from taskbundle.engine.git import GitClient
from taskbundle.errors import ConfigurationError, InfrastructureError
from taskbundle.models import BuildMetadata
from taskbundle.process import ProcessRunner, Runner
from taskbundle.session import CommandSession

BUILD_CONTEXT_SCHEMA_VERSION = 1


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_fingerprint(bundle: Bundle) -> str:
    payload = {
        "context_schema_version": BUILD_CONTEXT_SCHEMA_VERSION,
        "cli_version": __version__,
        "repository": {
            "url": bundle.manifest.repository.url,
            "commit": bundle.manifest.repository.commit.lower(),
        },
        "dockerfile_sha256": sha256_file(bundle.dockerfile_path),
    }
    normalized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def image_tag(bundle_id: str, fingerprint: str) -> str:
    return f"taskbundle/{bundle_id}:{fingerprint[:16]}"


def container_name(bundle_id: str, command_id: str) -> str:
    compact_id = command_id.replace("T", "").replace("Z", "")[-20:]
    return f"taskbundle-{bundle_id}-{compact_id}"[:63]


def prepare_build_context(
    *,
    bundle: Bundle,
    context: Path,
    git: GitClient,
) -> str:
    context.mkdir(parents=True, exist_ok=False)
    checkout = git.checkout_exact(
        repository_url=bundle.manifest.repository.url,
        commit=bundle.manifest.repository.commit,
        destination=context / "source",
        timeout_seconds=bundle.manifest.environment.build_timeout_seconds,
    )
    shutil.copy2(bundle.dockerfile_path, context / "Dockerfile")
    entries = {entry.name for entry in context.iterdir()}
    if entries != {"Dockerfile", "source"}:
        raise InfrastructureError(
            "Generated Docker build context contains unexpected entries.",
            details={"entries": sorted(entries)},
        )
    return checkout.log


def _metadata_path(state_dir: Path, fingerprint: str) -> Path:
    return state_dir / "cache" / fingerprint / "build.json"


def _read_metadata(path: Path) -> BuildMetadata | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return BuildMetadata.model_validate(payload)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError, ValidationError) as error:
        raise InfrastructureError(
            f"Cached build metadata is invalid: {path}",
            hint="Run `task init --force-rebuild` to replace the cache entry.",
            details={"reason": str(error)},
        ) from error


def require_initialized_build(
    *,
    bundle: Bundle,
    state_dir: Path,
    docker: DockerClient,
) -> BuildMetadata:
    fingerprint = build_fingerprint(bundle)
    metadata = _read_metadata(_metadata_path(state_dir, fingerprint))
    if metadata is None:
        raise ConfigurationError(
            "This bundle has not been initialized for its current build inputs.",
            hint="Run `task init` before validating the bundle.",
            details={"fingerprint": fingerprint},
        )
    current_id = docker.inspect_image(metadata.image_tag)
    if current_id != metadata.image_id:
        raise InfrastructureError(
            "The initialized Docker image is missing or no longer matches its metadata.",
            hint="Run `task init` to rebuild the task environment.",
            details={
                "image_tag": metadata.image_tag,
                "expected_image_id": metadata.image_id,
                "current_image_id": current_id,
            },
        )
    return metadata


def _write_metadata(path: Path, metadata: BuildMetadata) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(".build.json.tmp")
        temporary.write_text(
            json.dumps(metadata.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError as error:
        raise InfrastructureError(f"Could not write build metadata {path}: {error}") from error


def initialize_task(
    *,
    bundle: Bundle,
    session: CommandSession,
    force_rebuild: bool = False,
    runner: Runner | None = None,
) -> dict[str, Any]:
    process_runner = runner or ProcessRunner()
    git = GitClient(process_runner)
    docker = DockerClient(
        process_runner,
        executable=os.environ.get("TASKBUNDLE_DOCKER_BIN", "docker"),
    )
    fingerprint = build_fingerprint(bundle)
    tag = image_tag(bundle.manifest.id, fingerprint)
    cached_path = _metadata_path(session.state_dir, fingerprint)

    session.event(
        "info",
        "fingerprint",
        "Build fingerprint calculated.",
        {"fingerprint": fingerprint, "image_tag": tag},
    )

    versions = docker.versions()
    metadata = None if force_rebuild else _read_metadata(cached_path)
    reused = False
    if metadata is not None:
        current_id = docker.inspect_image(tag)
        if current_id == metadata.image_id:
            reused = True
            session.event(
                "info",
                "build",
                "Reusing matching initialized image.",
                {"image_id": current_id, "image_tag": tag},
            )
        else:
            session.event(
                "warning",
                "build",
                "Cached image metadata did not match a local image; rebuilding.",
                {"cached_image_id": metadata.image_id, "local_image_id": current_id},
            )
            metadata = None

    checkout_log_path: Path | None = None
    build_log_path: Path | None = None
    if metadata is None:
        cache_root = session.state_dir / "cache"
        cache_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="build-context-", dir=cache_root) as temporary:
            context = Path(temporary) / "context"
            session.event("info", "checkout", "Preparing exact source checkout.")
            checkout_log = prepare_build_context(bundle=bundle, context=context, git=git)
            checkout_log_path = session.artifacts.write_text(
                command_id=session.command_id,
                relative_path="checkout.log",
                content=checkout_log,
                kind="checkout_log",
            )
            session.event(
                "info",
                "build",
                "Building restricted Docker context.",
                {"entries": ["Dockerfile", "source"]},
            )
            built = docker.build_image(
                context=context,
                image_tag=tag,
                labels={
                    "io.taskbundle.bundle-id": bundle.manifest.id,
                    "io.taskbundle.fingerprint": fingerprint,
                    "org.opencontainers.image.revision": bundle.manifest.repository.commit.lower(),
                },
                timeout_seconds=bundle.manifest.environment.build_timeout_seconds,
            )
            build_log_path = session.artifacts.write_text(
                command_id=session.command_id,
                relative_path="docker-build.log",
                content=built.log,
                kind="docker_build_log",
            )
            metadata = BuildMetadata(
                fingerprint=fingerprint,
                bundle_id=bundle.manifest.id,
                repository_url=bundle.manifest.repository.url,
                repository_commit=bundle.manifest.repository.commit.lower(),
                dockerfile_sha256=sha256_file(bundle.dockerfile_path),
                image_tag=tag,
                image_id=built.image_id,
                git_version=git.version(),
                docker_client_version=versions.client,
                docker_server_version=versions.server,
                created_at=datetime.now(UTC),
            )

    smoke_name = container_name(bundle.manifest.id, session.command_id)
    isolation = docker.isolation_profile(
        runtime=bundle.manifest.runtime,
        workdir=bundle.manifest.environment.workdir,
        network="none",
    )
    session.event(
        "info",
        "smoke",
        "Running isolated environment smoke command.",
        {"container_name": smoke_name, "isolation": isolation},
    )
    smoke = docker.run_smoke(
        image_tag=tag,
        container_name=smoke_name,
        workdir=bundle.manifest.environment.workdir,
        command=bundle.manifest.environment.smoke_command,
        runtime=bundle.manifest.runtime,
        timeout_seconds=bundle.manifest.environment.smoke_timeout_seconds,
    )
    smoke_log = (
        f"exit={smoke.exit_code} timeout={smoke.timed_out} "
        f"duration={smoke.duration_seconds:.3f}s\n"
        f"--- stdout ---\n{smoke.stdout}"
        f"--- stderr ---\n{smoke.stderr}"
    )
    smoke_log_path = session.artifacts.write_text(
        command_id=session.command_id,
        relative_path="smoke.log",
        content=smoke_log,
        kind="smoke_log",
    )
    if smoke.timed_out:
        raise InfrastructureError(
            "Environment smoke command timed out.",
            hint=f"Inspect {smoke_log_path} and adjust smoke_timeout_seconds if appropriate.",
            details={"artifact": str(smoke_log_path)},
        )
    if smoke.exit_code != 0:
        raise InfrastructureError(
            "Environment smoke command failed.",
            hint=f"Inspect {smoke_log_path} and fix environment/Dockerfile or smoke_command.",
            details={"exit_code": smoke.exit_code, "artifact": str(smoke_log_path)},
        )

    assert metadata is not None
    if not reused:
        _write_metadata(cached_path, metadata)
    metadata_artifact = session.artifacts.write_json(
        command_id=session.command_id,
        relative_path="build-metadata.json",
        payload=metadata.model_dump(mode="json"),
        kind="build_metadata",
    )
    session.event(
        "info",
        "cleanup",
        "Smoke container removed.",
        {"container_name": smoke_name},
    )
    return {
        "bundle": str(bundle.root),
        "bundle_id": bundle.manifest.id,
        "fingerprint": fingerprint,
        "image_tag": tag,
        "image_id": metadata.image_id,
        "repository_commit": metadata.repository_commit,
        "reused": reused,
        "isolation": isolation,
        "smoke": {
            "command": bundle.manifest.environment.smoke_command,
            "exit_code": smoke.exit_code,
            "duration_seconds": smoke.duration_seconds,
            "artifact": str(smoke_log_path),
        },
        "artifacts": {
            "checkout_log": str(checkout_log_path) if checkout_log_path else None,
            "build_log": str(build_log_path) if build_log_path else None,
            "metadata": str(metadata_artifact),
        },
    }
