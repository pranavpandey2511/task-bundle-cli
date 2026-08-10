"""Deterministic repository materialization and image initialization."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from taskbundle import __version__
from taskbundle.config import Bundle
from taskbundle.engine.docker import DockerClient
from taskbundle.engine.git import GitClient
from taskbundle.errors import ConfigurationError, InfrastructureError, InvalidTaskError
from taskbundle.models import BuildMetadata
from taskbundle.patches import validate_patch_contract
from taskbundle.process import ProcessRunner, Runner
from taskbundle.secrecy import verify_solver_secrecy
from taskbundle.session import CommandSession
from taskbundle.snapshots import capture_repository_snapshot, repository_snapshot_artifacts

BUILD_CONTEXT_SCHEMA_VERSION = 4


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def solver_secrecy_contract_sha256(bundle: Bundle) -> str:
    """Hash every manifest field that controls what the solver image may reveal."""

    tests = bundle.manifest.tests.pass_to_pass + bundle.manifest.tests.fail_to_pass
    payload = {
        "evaluator_owned_paths": sorted(bundle.manifest.tests.evaluator_owned_paths),
        "test_markers": sorted(
            ({"marker": test.marker, "path": test.path} for test in tests),
            key=lambda item: (item["path"], item["marker"]),
        ),
    }
    normalized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def build_fingerprint(
    bundle: Bundle,
    *,
    dockerfile_sha256: str | None = None,
    solver_view_sha256: str | None = None,
    secrecy_contract_sha256: str | None = None,
) -> str:
    payload = {
        "context_schema_version": BUILD_CONTEXT_SCHEMA_VERSION,
        "cli_version": __version__,
        "bundle_id": bundle.manifest.id,
        "repository": {
            "url": bundle.manifest.repository.url,
            "commit": bundle.manifest.repository.commit.lower(),
        },
        "workdir": bundle.manifest.environment.workdir,
        "evaluator_path": bundle.manifest.environment.evaluator_path,
        "dockerfile_sha256": dockerfile_sha256 or sha256_file(bundle.dockerfile_path),
        "solver_view_sha256": solver_view_sha256 or sha256_file(bundle.solver_view_patch_path),
        "secrecy_contract_sha256": (
            secrecy_contract_sha256 or solver_secrecy_contract_sha256(bundle)
        ),
    }
    normalized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def image_tag(bundle_id: str, fingerprint: str) -> str:
    return f"taskbundle/{bundle_id}:{fingerprint[:16]}"


def solver_image_tag(bundle_id: str, fingerprint: str) -> str:
    return f"taskbundle/{bundle_id}-solver:{fingerprint[:16]}"


def container_name(bundle_id: str, command_id: str) -> str:
    compact_id = command_id.replace("T", "").replace("Z", "")[-20:]
    return f"taskbundle-{bundle_id}-{compact_id}"[:63]


def prepare_build_context(
    *,
    bundle: Bundle,
    context: Path,
    git: GitClient,
    dockerfile_content: bytes | None = None,
    solver_view_patch: Path | None = None,
) -> str:
    context.mkdir(parents=True, exist_ok=False)
    checkout = git.checkout_exact(
        repository_url=bundle.manifest.repository.url,
        commit=bundle.manifest.repository.commit,
        destination=context / "source",
        timeout_seconds=bundle.manifest.environment.build_timeout_seconds,
    )
    solver_view_check = git.check_patch(
        repository=context / "source",
        patch=solver_view_patch or bundle.solver_view_patch_path,
        label="solver-view redaction patch",
    )
    if dockerfile_content is None:
        dockerfile_content = bundle.dockerfile_path.read_bytes()
    (context / "Dockerfile").write_bytes(dockerfile_content)
    entries = {entry.name for entry in context.iterdir()}
    if entries != {"Dockerfile", "source"}:
        raise InfrastructureError(
            "Generated Docker build context contains unexpected entries.",
            details={"entries": sorted(entries)},
        )
    return f"{checkout.log}\n{solver_view_check}"


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
    dockerfile_sha256: str | None = None,
    solver_view_sha256: str | None = None,
) -> BuildMetadata:
    secrecy_digest = solver_secrecy_contract_sha256(bundle)
    fingerprint = build_fingerprint(
        bundle,
        dockerfile_sha256=dockerfile_sha256,
        solver_view_sha256=solver_view_sha256,
        secrecy_contract_sha256=secrecy_digest,
    )
    metadata = _read_metadata(_metadata_path(state_dir, fingerprint))
    if metadata is None:
        raise ConfigurationError(
            "This bundle has not been initialized for its current build inputs.",
            hint="Run `task init` before validating the bundle.",
            details={"fingerprint": fingerprint},
        )
    _validate_metadata(
        bundle=bundle,
        fingerprint=fingerprint,
        metadata=metadata,
        dockerfile_sha256=dockerfile_sha256,
        solver_view_sha256=solver_view_sha256,
        secrecy_contract_sha256=secrecy_digest,
    )
    current_ids = {
        "evaluator": docker.inspect_image(metadata.image_tag),
        "solver": docker.inspect_image(metadata.solver_image_tag),
    }
    expected_ids = {
        "evaluator": metadata.image_id,
        "solver": metadata.solver_image_id,
    }
    mismatches = {
        role: {"expected": expected_ids[role], "current": current_ids[role]}
        for role in expected_ids
        if current_ids[role] != expected_ids[role]
    }
    if mismatches:
        raise InfrastructureError(
            "An initialized Docker image is missing or no longer matches its metadata.",
            hint="Run `task init` to rebuild the task environment.",
            details={"mismatches": mismatches},
        )
    return metadata


def _validate_metadata(
    *,
    bundle: Bundle,
    fingerprint: str,
    metadata: BuildMetadata,
    dockerfile_sha256: str | None = None,
    solver_view_sha256: str | None = None,
    secrecy_contract_sha256: str | None = None,
) -> None:
    expected: dict[str, str] = {
        "fingerprint": fingerprint,
        "bundle_id": bundle.manifest.id,
        "repository_url": bundle.manifest.repository.url,
        "repository_commit": bundle.manifest.repository.commit.lower(),
        "dockerfile_sha256": dockerfile_sha256 or sha256_file(bundle.dockerfile_path),
        "solver_view_sha256": solver_view_sha256 or sha256_file(bundle.solver_view_patch_path),
        "secrecy_contract_sha256": (
            secrecy_contract_sha256 or solver_secrecy_contract_sha256(bundle)
        ),
        "image_tag": image_tag(bundle.manifest.id, fingerprint),
        "solver_image_tag": solver_image_tag(bundle.manifest.id, fingerprint),
    }
    mismatches = {
        field: {"expected": value, "observed": getattr(metadata, field)}
        for field, value in expected.items()
        if getattr(metadata, field) != value
    }
    if mismatches:
        raise InfrastructureError(
            "Cached build metadata does not match the current bundle inputs.",
            hint="Run `task init --force-rebuild` to replace the cache entry.",
            details={"mismatches": mismatches},
        )


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


def _verify_image_repository(
    *,
    bundle: Bundle,
    session: CommandSession,
    docker: DockerClient,
    image_id: str,
    base_commit: str,
    role: str,
    verify_secrecy: bool = False,
) -> None:
    name = container_name(bundle.manifest.id, f"{session.command_id}-{role}-verify")
    container_id = docker.create_evaluator(
        image_tag=image_id,
        container_name=name,
        workdir=bundle.manifest.environment.workdir,
        runtime=bundle.manifest.runtime,
        evaluator_path=bundle.manifest.environment.evaluator_path_value,
    )
    try:
        docker.start_detached(container_id)
        trusted_path = bundle.manifest.environment.evaluator_path_value
        path_check = docker.exec_command(
            container_id=container_id,
            workdir=bundle.manifest.environment.workdir,
            command=[
                "/bin/sh",
                "-c",
                (
                    "workspace=$1; shift; "
                    "for path do "
                    'if [ ! -e "$path" ] && [ ! -L "$path" ]; then exit 40; fi; '
                    'resolved=$(/usr/bin/readlink -f -- "$path") || exit 41; '
                    '[ -d "$resolved" ] || exit 42; '
                    'case "$resolved" in "$workspace"|"$workspace"/*|/tmp|/tmp/*) '
                    'printf "%s -> %s\\n" "$path" "$resolved"; exit 43;; esac; '
                    'probe="$resolved/.taskbundle-write-probe-$$"; '
                    'if (umask 077 && : > "$probe") 2>/dev/null; then '
                    '/bin/rm -f -- "$probe"; printf "%s is writable\\n" "$resolved"; exit 44; fi; '
                    "done; "
                    "for tool in git grep find /usr/bin/env /bin/cat /bin/rm /bin/sh "
                    "/bin/sleep /usr/bin/readlink; do "
                    'selected=$(command -v -- "$tool") || exit 45; '
                    'resolved=$(/usr/bin/readlink -f -- "$selected") || exit 46; '
                    '[ -x "$resolved" ] || exit 47; '
                    'case "$resolved" in "$workspace"|"$workspace"/*|/tmp|/tmp/*) '
                    'printf "%s -> %s\\n" "$tool" "$resolved"; exit 48;; esac; '
                    "done"
                ),
                "taskbundle-evaluator-path-check",
                bundle.manifest.environment.workdir,
                *bundle.manifest.environment.evaluator_path,
            ],
            timeout_seconds=60,
            trusted_path=trusted_path,
        )
        if not path_check.succeeded:
            raise InvalidTaskError(
                "The evaluator tool PATH does not resolve exclusively to immutable directories.",
                hint=(
                    "Use existing read-only directories outside the workdir and /tmp; remove "
                    "writable volumes and unsafe symlinks."
                ),
                details={
                    "image_role": role,
                    "exit_code": path_check.exit_code,
                    "stdout": path_check.stdout[-4000:],
                    "stderr": path_check.stderr[-4000:],
                },
            )
        capture_repository_snapshot(
            docker=docker,
            session=session,
            container_id=container_id,
            workdir=bundle.manifest.environment.workdir,
            base_commit=base_commit,
            phase="init",
            stage=f"{role}-image-pristine",
            require_pristine=True,
            trusted_path=trusted_path,
        )
        if verify_secrecy:
            verify_solver_secrecy(
                bundle=bundle,
                docker=docker,
                container_id=container_id,
            )
    finally:
        docker.remove_container(container_id)
        session.event(
            "info",
            "cleanup",
            "Initialization verification container removed.",
            {"container_id": container_id, "image_role": role},
        )


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
    try:
        dockerfile_content = bundle.dockerfile_path.read_bytes()
        gold_patch = bundle.gold_patch_path.read_text(encoding="utf-8")
        test_patch = bundle.test_patch_path.read_text(encoding="utf-8")
        solver_view_patch = bundle.solver_view_patch_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise InfrastructureError(
            "Could not read trusted bundle patches for initialization."
        ) from error
    patch_contract = validate_patch_contract(
        bundle=bundle,
        gold_patch=gold_patch,
        test_patch=test_patch,
        solver_view_patch=solver_view_patch,
    )
    dockerfile_digest = hashlib.sha256(dockerfile_content).hexdigest()
    solver_view_digest = hashlib.sha256(solver_view_patch.encode("utf-8")).hexdigest()
    secrecy_digest = solver_secrecy_contract_sha256(bundle)
    fingerprint = build_fingerprint(
        bundle,
        dockerfile_sha256=dockerfile_digest,
        solver_view_sha256=solver_view_digest,
        secrecy_contract_sha256=secrecy_digest,
    )
    tag = image_tag(bundle.manifest.id, fingerprint)
    solver_tag = solver_image_tag(bundle.manifest.id, fingerprint)
    cached_path = _metadata_path(session.state_dir, fingerprint)

    session.event(
        "info",
        "fingerprint",
        "Build fingerprint calculated.",
        {
            "fingerprint": fingerprint,
            "image_tag": tag,
            "solver_image_tag": solver_tag,
        },
    )

    versions = docker.versions()
    metadata = None if force_rebuild else _read_metadata(cached_path)
    reused = False
    if metadata is not None:
        _validate_metadata(
            bundle=bundle,
            fingerprint=fingerprint,
            metadata=metadata,
            dockerfile_sha256=dockerfile_digest,
            solver_view_sha256=solver_view_digest,
            secrecy_contract_sha256=secrecy_digest,
        )
        current_id = docker.inspect_image(tag)
        current_solver_id = docker.inspect_image(solver_tag)
        if current_id == metadata.image_id and current_solver_id == metadata.solver_image_id:
            reused = True
            session.event(
                "info",
                "build",
                "Reusing matching evaluator and sanitized solver images.",
                {
                    "image_id": current_id,
                    "image_tag": tag,
                    "solver_image_id": current_solver_id,
                    "solver_image_tag": solver_tag,
                },
            )
        else:
            session.event(
                "warning",
                "build",
                "Cached image metadata did not match a local image; rebuilding.",
                {
                    "cached_image_id": metadata.image_id,
                    "local_image_id": current_id,
                    "cached_solver_image_id": metadata.solver_image_id,
                    "local_solver_image_id": current_solver_id,
                },
            )
            metadata = None

    checkout_log_path: Path | None = None
    build_log_path: Path | None = None
    solver_source_log_path: Path | None = None
    solver_build_log_path: Path | None = None
    if metadata is None:
        cache_root = session.state_dir / "cache"
        cache_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="build-context-", dir=cache_root) as temporary:
            temporary_root = Path(temporary)
            solver_view_snapshot = temporary_root / "solver-view.patch"
            solver_view_snapshot.write_text(solver_view_patch, encoding="utf-8")
            context = temporary_root / "context"
            session.event("info", "checkout", "Preparing exact source checkout.")
            checkout_log = prepare_build_context(
                bundle=bundle,
                context=context,
                git=git,
                dockerfile_content=dockerfile_content,
                solver_view_patch=solver_view_snapshot,
            )
            checkout_log_path = session.artifacts.write_text(
                command_id=session.command_id,
                relative_path="checkout.log",
                content=checkout_log,
                kind="checkout_log",
            )
            session.event(
                "info",
                "build",
                "Building the exact evaluator image from the restricted context.",
                {"entries": ["Dockerfile", "source"], "image_role": "evaluator"},
            )
            built = docker.build_image(
                context=context,
                image_tag=tag,
                labels={
                    "io.taskbundle.bundle-id": bundle.manifest.id,
                    "io.taskbundle.fingerprint": fingerprint,
                    "io.taskbundle.image-role": "evaluator",
                    "org.opencontainers.image.revision": bundle.manifest.repository.commit.lower(),
                },
                timeout_seconds=bundle.manifest.environment.build_timeout_seconds,
                no_cache=force_rebuild,
            )
            build_log_path = session.artifacts.write_text(
                command_id=session.command_id,
                relative_path="docker-build.log",
                content=built.log,
                kind="docker_build_log",
            )
            protected_paths = bundle.manifest.tests.evaluator_owned_paths
            session.event(
                "info",
                "solver-source",
                "Deleting evaluator-owned files and all inherited Git metadata.",
                {"protected_paths": sorted(protected_paths)},
            )
            solver_base_commit, solver_source_log = git.sanitize_solver_source(
                repository=context / "source",
                patch=solver_view_snapshot,
                protected_paths=protected_paths,
            )
            solver_source_log_path = session.artifacts.write_text(
                command_id=session.command_id,
                relative_path="solver-source.log",
                content=solver_source_log,
                kind="checkout_log",
            )
            session.event(
                "info",
                "build",
                "Building a distinct image from the sanitized solver source.",
                {
                    "entries": ["Dockerfile", "source"],
                    "image_role": "solver",
                    "solver_base_commit": solver_base_commit,
                },
            )
            solver_built = docker.build_image(
                context=context,
                image_tag=solver_tag,
                labels={
                    "io.taskbundle.bundle-id": bundle.manifest.id,
                    "io.taskbundle.fingerprint": fingerprint,
                    "io.taskbundle.image-role": "solver",
                    "org.opencontainers.image.revision": solver_base_commit,
                },
                timeout_seconds=bundle.manifest.environment.build_timeout_seconds,
                no_cache=force_rebuild,
            )
            solver_build_log_path = session.artifacts.write_text(
                command_id=session.command_id,
                relative_path="solver-docker-build.log",
                content=solver_built.log,
                kind="docker_build_log",
            )
            metadata = BuildMetadata(
                fingerprint=fingerprint,
                bundle_id=bundle.manifest.id,
                repository_url=bundle.manifest.repository.url,
                repository_commit=bundle.manifest.repository.commit.lower(),
                dockerfile_sha256=dockerfile_digest,
                solver_view_sha256=solver_view_digest,
                secrecy_contract_sha256=secrecy_digest,
                image_tag=tag,
                image_id=built.image_id,
                solver_image_tag=solver_tag,
                solver_image_id=solver_built.image_id,
                solver_base_commit=solver_base_commit,
                git_version=git.version(),
                docker_client_version=versions.client,
                docker_server_version=versions.server,
                created_at=datetime.now(UTC),
            )

    assert metadata is not None
    _verify_image_repository(
        bundle=bundle,
        session=session,
        docker=docker,
        image_id=metadata.image_id,
        base_commit=bundle.manifest.repository.commit,
        role="evaluator",
    )
    _verify_image_repository(
        bundle=bundle,
        session=session,
        docker=docker,
        image_id=metadata.solver_image_id,
        base_commit=metadata.solver_base_commit,
        role="solver",
        verify_secrecy=True,
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
        image_tag=metadata.image_id,
        container_name=smoke_name,
        workdir=bundle.manifest.environment.workdir,
        command=bundle.manifest.environment.smoke_command,
        runtime=bundle.manifest.runtime,
        timeout_seconds=bundle.manifest.environment.smoke_timeout_seconds,
        evaluator_path=bundle.manifest.environment.evaluator_path_value,
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
        "solver_image_tag": metadata.solver_image_tag,
        "solver_image_id": metadata.solver_image_id,
        "solver_base_commit": metadata.solver_base_commit,
        "repository_commit": metadata.repository_commit,
        "solver_view_sha256": metadata.solver_view_sha256,
        "patch_contract": patch_contract,
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
            "solver_source_log": (str(solver_source_log_path) if solver_source_log_path else None),
            "solver_build_log": (str(solver_build_log_path) if solver_build_log_path else None),
            "metadata": str(metadata_artifact),
        },
        "snapshot_artifacts": repository_snapshot_artifacts(session),
    }
