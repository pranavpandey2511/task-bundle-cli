"""Create a minimal, intentionally editable task bundle."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from taskbundle.config import load_bundle
from taskbundle.errors import ConfigurationError, InfrastructureError
from taskbundle.models import TaskManifest


def scaffold_bundle(*, root: Path, repo: str, commit: str, bundle_id: str) -> dict[str, Any]:
    root = root.expanduser().resolve()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise InfrastructureError(f"Could not create bundle directory {root}: {error}") from error

    existing = [path.name for path in root.iterdir() if path.name != ".taskbundle"]
    if existing:
        raise ConfigurationError(
            f"Refusing to scaffold into a non-empty directory: {root}",
            hint="Choose an empty directory or edit the existing bundle manually.",
            details={"existing_entries": sorted(existing)},
        )

    try:
        manifest = TaskManifest.model_validate(
            {
                "schema_version": 1,
                "id": bundle_id,
                "repository": {"url": repo, "commit": commit},
                "environment": {
                    "dockerfile": "environment/Dockerfile",
                    "workdir": "/workspace",
                    "smoke_command": "python -m pytest --version",
                    "smoke_timeout_seconds": 300,
                },
                "patches": {"gold": "gold.patch", "tests": "tests/hidden.patch"},
                "tests": {
                    "pass_to_pass": [
                        {
                            "id": "replace-me-pass-to-pass",
                            "command": "python -m pytest -q path/to/test.py::test_existing",
                            "timeout_seconds": 120,
                        }
                    ],
                    "fail_to_pass": [
                        {
                            "id": "replace-me-fail-to-pass",
                            "command": "python -m pytest -q path/to/test.py::test_fix",
                            "timeout_seconds": 120,
                        }
                    ],
                },
                "validation": {"repetitions": 3},
                "runtime": {
                    "cpus": 2,
                    "memory": "4g",
                    "pids": 256,
                    "tmpfs_size": "512m",
                    "solver_timeout_seconds": 1800,
                    "solver_network": False,
                },
            }
        )
    except ValidationError as error:
        issues = [
            {
                "field": ".".join(str(part) for part in item["loc"]),
                "message": item["msg"],
            }
            for item in error.errors(include_url=False, include_context=False, include_input=False)
        ]
        raise ConfigurationError(
            "The scaffold arguments do not form a valid bundle manifest.",
            hint="Check --id and --commit, then retry.",
            details={"issues": issues},
        ) from error

    files = {
        "task.json": json.dumps(manifest.model_dump(mode="json"), indent=2) + "\n",
        "description.md": (
            "# Task description\n\nReplace this text with the solver-visible problem.\n"
        ),
        "gold.patch": "",
        "tests/hidden.patch": "",
        "environment/Dockerfile": (
            "FROM python:3.12-slim\n\n"
            "RUN apt-get update \\\n"
            "    && apt-get install --yes --no-install-recommends git \\\n"
            "    && rm -rf /var/lib/apt/lists/*\n\n"
            "WORKDIR /workspace\n"
            "COPY source/ /workspace/\n\n"
            "# Install this repository's dependencies here.\n"
        ),
        ".gitignore": ".taskbundle/\n",
    }

    try:
        for relative_path, content in files.items():
            destination = root / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
    except OSError as error:
        raise InfrastructureError(f"Could not write bundle scaffold: {error}") from error

    bundle = load_bundle(root)
    return {
        "bundle": str(bundle.root),
        "bundle_id": bundle.manifest.id,
        "created_files": sorted(files),
        "next_steps": [
            "Replace the placeholder description, tests, and patches.",
            "Customize environment/Dockerfile for the target repository.",
            "Run `task init` after the bundle is complete.",
        ],
    }
