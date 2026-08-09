"""Durable, content-hashed command artifacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any

from taskbundle.database import Database
from taskbundle.errors import InfrastructureError


class ArtifactStore:
    def __init__(self, state_dir: Path, database: Database) -> None:
        self.state_dir = state_dir.resolve()
        self.database = database

    def command_dir(self, command_id: str) -> Path:
        path = self.state_dir / "commands" / command_id
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise InfrastructureError(
                f"Could not create artifact directory {path}: {error}"
            ) from error
        return path

    def write_text(
        self,
        *,
        command_id: str,
        relative_path: str,
        content: str,
        kind: str,
    ) -> Path:
        return self.write_bytes(
            command_id=command_id,
            relative_path=relative_path,
            content=content.encode("utf-8"),
            kind=kind,
        )

    def write_json(
        self,
        *,
        command_id: str,
        relative_path: str,
        payload: dict[str, Any],
        kind: str,
    ) -> Path:
        content = json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
        return self.write_text(
            command_id=command_id,
            relative_path=relative_path,
            content=content,
            kind=kind,
        )

    def write_bytes(
        self,
        *,
        command_id: str,
        relative_path: str,
        content: bytes,
        kind: str,
    ) -> Path:
        posix_path = PurePosixPath(relative_path)
        if posix_path.is_absolute() or ".." in posix_path.parts or not relative_path:
            raise InfrastructureError(f"Unsafe artifact path: {relative_path}")

        command_dir = self.command_dir(command_id)
        destination = (command_dir / relative_path).resolve()
        if not destination.is_relative_to(command_dir.resolve()):
            raise InfrastructureError(
                f"Artifact path escapes its command directory: {relative_path}"
            )

        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{destination.name}.tmp")
            temporary.write_bytes(content)
            temporary.replace(destination)
        except OSError as error:
            raise InfrastructureError(f"Could not write artifact {destination}: {error}") from error

        digest = hashlib.sha256(content).hexdigest()
        stored_relative = destination.relative_to(self.state_dir).as_posix()
        self.database.add_artifact(
            command_id=command_id,
            kind=kind,
            relative_path=stored_relative,
            sha256=digest,
            size_bytes=len(content),
        )
        return destination
