"""Deterministic export of one command's verified evidence."""

from __future__ import annotations

import hashlib
import json
import tempfile
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from taskbundle.artifacts import verify_artifact_records
from taskbundle.errors import ConfigurationError, InfrastructureError, InvalidTaskError

_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n").encode("utf-8")


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(filename=name, date_time=_ZIP_TIME)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def export_command_evidence(
    *,
    state_dir: Path,
    destination: Path,
    command: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    test_results: Sequence[Mapping[str, Any]],
    artifact_records: Sequence[Mapping[str, Any]],
    force: bool = False,
) -> dict[str, Any]:
    """Write a byte-stable ZIP after verifying every source artifact."""

    verification = verify_artifact_records(state_dir=state_dir, records=artifact_records)
    if not verification["valid"]:
        raise InvalidTaskError(
            f"Cannot export unverified artifacts for {command['id']}.",
            hint="Run `task report` and repair missing or mismatched evidence first.",
            details={"command": dict(command), **verification},
        )

    resolved_state = state_dir.resolve()
    resolved_destination = destination.expanduser().resolve()
    command_dir = (resolved_state / "commands" / str(command["id"])).resolve()
    if resolved_destination.is_relative_to(command_dir):
        raise ConfigurationError(
            "Evidence export cannot be written inside the target command's artifact directory.",
            hint="Use the default .taskbundle/exports directory or another output path.",
        )
    if resolved_destination.exists() and not force:
        raise ConfigurationError(
            f"Evidence export already exists: {resolved_destination}",
            hint="Choose another --output path or pass --force to replace it.",
        )
    if resolved_destination.exists() and not resolved_destination.is_file():
        raise ConfigurationError(f"Evidence export output is not a file: {resolved_destination}")

    generated: dict[str, bytes] = {
        "ledger/command.json": _json_bytes(dict(command)),
        "ledger/events.json": _json_bytes([dict(event) for event in events]),
        "ledger/test-results.json": _json_bytes([dict(result) for result in test_results]),
        "ledger/artifacts.json": _json_bytes([dict(record) for record in artifact_records]),
        "ledger/integrity.json": _json_bytes(verification),
    }
    archive_entries: dict[str, bytes] = dict(generated)
    for record in artifact_records:
        relative_path = str(record["relative_path"])
        posix = PurePosixPath(relative_path)
        if posix.is_absolute() or ".." in posix.parts:
            raise InvalidTaskError(f"Unsafe recorded artifact path: {relative_path}")
        source = (resolved_state / relative_path).resolve()
        try:
            archive_entries[f"artifacts/{posix.as_posix()}"] = source.read_bytes()
        except OSError as error:
            raise InfrastructureError(
                f"Could not read verified artifact {source}: {error}"
            ) from error

    manifest_entries = [
        {
            "path": name,
            "sha256": _sha256(content),
            "size_bytes": len(content),
        }
        for name, content in sorted(archive_entries.items())
    ]
    archive_entries["manifest.json"] = _json_bytes(
        {
            "schema_version": 1,
            "format": "taskbundle-command-evidence",
            "command_id": command["id"],
            "bundle_id": command.get("bundle_id"),
            "entries": manifest_entries,
            "security_notice": (
                "Contains trusted evaluator inputs and solver logs; never expose this archive "
                "to a solver before grading."
            ),
        }
    )

    try:
        resolved_destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix=f".{resolved_destination.name}.",
            suffix=".tmp",
            dir=resolved_destination.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
        try:
            with zipfile.ZipFile(temporary_path, mode="w") as archive:
                for name, content in sorted(archive_entries.items()):
                    archive.writestr(_zip_info(name), content)
            temporary_path.replace(resolved_destination)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
        archive_content = resolved_destination.read_bytes()
    except OSError as error:
        raise InfrastructureError(
            f"Could not write evidence export {resolved_destination}: {error}"
        ) from error

    return {
        "command": dict(command),
        "output": str(resolved_destination),
        "sha256": _sha256(archive_content),
        "size_bytes": len(archive_content),
        "entry_count": len(archive_entries),
        "artifact_count": len(artifact_records),
        "artifact_integrity": "verified",
        "deterministic": True,
        "contains_evaluator_material": True,
    }
