"""Bundle discovery, loading, and secure path resolution."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import ValidationError

from taskbundle.errors import ConfigurationError
from taskbundle.models import TaskManifest


@dataclass(frozen=True, slots=True)
class Bundle:
    root: Path
    manifest_path: Path
    manifest_source: str
    manifest: TaskManifest

    def resolve_file(self, relative_path: str, *, label: str) -> Path:
        """Resolve a required file and reject path/symlink escapes."""

        try:
            candidate = (self.root / relative_path).resolve(strict=True)
        except FileNotFoundError as error:
            raise ConfigurationError(
                f"Required {label} does not exist: {relative_path}",
                hint=f"Create the file inside {self.root} and retry.",
            ) from error

        if not candidate.is_relative_to(self.root):
            raise ConfigurationError(
                f"The {label} resolves outside the bundle: {relative_path}",
                hint="Use a regular file beneath the bundle root; external symlinks are rejected.",
            )
        if not candidate.is_file():
            raise ConfigurationError(f"Expected {label} to be a file: {relative_path}")
        return candidate

    @property
    def description_path(self) -> Path:
        return self.resolve_file("description.md", label="task description")

    @property
    def dockerfile_path(self) -> Path:
        return self.resolve_file(self.manifest.environment.dockerfile, label="Dockerfile")

    @property
    def gold_patch_path(self) -> Path:
        return self.resolve_file(self.manifest.patches.gold, label="gold patch")

    @property
    def test_patch_path(self) -> Path:
        return self.resolve_file(self.manifest.patches.tests, label="hidden test patch")

    @property
    def solver_view_patch_path(self) -> Path:
        return self.resolve_file(
            self.manifest.patches.solver_view,
            label="solver-view redaction patch",
        )

    def validate_required_files(self) -> None:
        _ = self.description_path
        _ = self.dockerfile_path
        _ = self.gold_patch_path
        _ = self.test_patch_path
        _ = self.solver_view_patch_path


SCP_STYLE_REPOSITORY = re.compile(r"^[^/@\s]+@[^:\s]+:.+$")


def normalize_repository_url(value: str, *, relative_to: Path) -> str:
    """Make filesystem repositories unambiguous without rewriting remote URLs."""

    parsed = urlsplit(value)
    if parsed.scheme or SCP_STYLE_REPOSITORY.fullmatch(value):
        return value
    return str((relative_to / Path(value).expanduser()).resolve())


def _format_validation_error(error: ValidationError) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for item in error.errors(include_url=False, include_context=False, include_input=False):
        location = ".".join(str(part) for part in item["loc"])
        issues.append({"field": location or "task.json", "message": item["msg"]})
    return issues


def load_bundle(bundle_path: Path | str, *, require_files: bool = True) -> Bundle:
    root = Path(bundle_path).expanduser().resolve()
    if not root.is_dir():
        raise ConfigurationError(
            f"Bundle directory does not exist: {root}",
            hint="Run `task new` to scaffold a bundle or pass the correct directory.",
        )

    manifest_path = root / "task.json"
    try:
        raw = manifest_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise ConfigurationError(
            f"Bundle manifest does not exist: {manifest_path}",
            hint="A bundle must contain task.json.",
        ) from error
    except OSError as error:
        raise ConfigurationError(f"Could not read bundle manifest: {error}") from error

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ConfigurationError(
            f"Invalid JSON in {manifest_path.name} at line {error.lineno}, column {error.colno}.",
            details={"reason": error.msg},
        ) from error

    try:
        manifest = TaskManifest.model_validate(payload)
    except ValidationError as error:
        raise ConfigurationError(
            "Bundle manifest validation failed.",
            hint="Fix the listed task.json fields and retry.",
            details={"issues": _format_validation_error(error)},
        ) from error

    normalized_repository = normalize_repository_url(
        manifest.repository.url,
        relative_to=root,
    )
    if normalized_repository != manifest.repository.url:
        manifest = manifest.model_copy(
            update={
                "repository": manifest.repository.model_copy(update={"url": normalized_repository})
            }
        )

    bundle = Bundle(
        root=root,
        manifest_path=manifest_path,
        manifest_source=raw,
        manifest=manifest,
    )
    if require_files:
        bundle.validate_required_files()
    return bundle
