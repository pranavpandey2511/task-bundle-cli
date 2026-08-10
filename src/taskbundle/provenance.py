"""Deterministic execution provenance for reproducible task runs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from taskbundle import __version__
from taskbundle.config import Bundle
from taskbundle.models import BuildMetadata
from taskbundle.session import CommandSession


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_text(content: str) -> str:
    return sha256_bytes(content.encode("utf-8"))


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_execution_provenance(
    *,
    bundle: Bundle,
    metadata: BuildMetadata,
    command: str,
    repetitions: int,
    solver: dict[str, Any] | None = None,
    input_hashes: dict[str, str] | None = None,
) -> dict[str, Any]:
    stable_input_hashes = (
        dict(input_hashes)
        if input_hashes is not None
        else {
            "task.json": sha256_path(bundle.manifest_path),
            "description.md": sha256_path(bundle.description_path),
            bundle.manifest.environment.dockerfile: sha256_path(bundle.dockerfile_path),
            bundle.manifest.patches.gold: sha256_path(bundle.gold_patch_path),
            bundle.manifest.patches.tests: sha256_path(bundle.test_patch_path),
            bundle.manifest.patches.solver_view: sha256_path(bundle.solver_view_patch_path),
        }
    )
    payload: dict[str, Any] = {
        "schema_version": 1,
        "cli_version": __version__,
        "command": command,
        "bundle_id": bundle.manifest.id,
        "repository": {
            "url": bundle.manifest.repository.url,
            "commit": bundle.manifest.repository.commit.lower(),
        },
        "image": {
            "tag": metadata.image_tag,
            "id": metadata.image_id,
            "build_fingerprint": metadata.fingerprint,
        },
        "solver_image": {
            "tag": metadata.solver_image_tag,
            "id": metadata.solver_image_id,
            "base_commit": metadata.solver_base_commit,
        },
        "inputs_sha256": stable_input_hashes,
        "runtime": bundle.manifest.runtime.model_dump(mode="json"),
        "repetitions": repetitions,
        "solver": solver,
    }
    normalized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {**payload, "execution_fingerprint": sha256_bytes(normalized)}


def write_execution_provenance(
    *,
    bundle: Bundle,
    metadata: BuildMetadata,
    session: CommandSession,
    command: str,
    repetitions: int,
    solver: dict[str, Any] | None = None,
    input_hashes: dict[str, str] | None = None,
) -> dict[str, Any]:
    provenance = build_execution_provenance(
        bundle=bundle,
        metadata=metadata,
        command=command,
        repetitions=repetitions,
        solver=solver,
        input_hashes=input_hashes,
    )
    artifact = session.artifacts.write_json(
        command_id=session.command_id,
        relative_path="provenance.json",
        payload=provenance,
        kind="execution_provenance",
    )
    relative_artifact = artifact.relative_to(session.state_dir).as_posix()
    session.event(
        "info",
        "provenance",
        "Execution fingerprint and input provenance recorded.",
        {
            "execution_fingerprint": provenance["execution_fingerprint"],
            "artifact": relative_artifact,
        },
    )
    return {**provenance, "artifact": relative_artifact}
