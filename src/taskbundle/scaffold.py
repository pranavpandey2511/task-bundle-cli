"""Create a minimal, intentionally editable task bundle."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from taskbundle.config import load_bundle, normalize_repository_url
from taskbundle.errors import ConfigurationError, InfrastructureError
from taskbundle.models import TaskManifest

SUPPORTED_PROFILES = ("auto", "python", "node", "go", "rust", "custom")


def _selected_profile(repository: str, requested: str) -> tuple[str, str]:
    normalized = requested.strip().lower()
    if normalized not in SUPPORTED_PROFILES:
        raise ConfigurationError(
            f"Unsupported starter profile: {requested}",
            hint="Choose auto, python, node, go, rust, or custom.",
        )
    if normalized != "auto":
        return normalized, "explicit"

    root = Path(repository)
    if root.is_dir():
        indicators = (
            ("pyproject.toml", "python"),
            ("package.json", "node"),
            ("go.mod", "go"),
            ("Cargo.toml", "rust"),
        )
        for indicator, profile in indicators:
            if (root / indicator).is_file():
                return profile, f"detected from {indicator}"
    return "python", "default for a remote or unrecognized repository"


def _profile_template(profile: str) -> dict[str, str]:
    templates = {
        "python": {
            "smoke": (
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTEST_ADDOPTS= "
                "/usr/local/bin/python -I -m pytest --version"
            ),
            "test": (
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTEST_ADDOPTS= "
                "/usr/local/bin/python -I -m pytest -q -c /dev/null --noconftest"
            ),
            "test_path": "path/to/test.py",
            "dockerfile": (
                "FROM python:3.12-slim\n\n"
                "RUN apt-get update \\\n"
                "    && apt-get install --yes --no-install-recommends git \\\n"
                "    && rm -rf /var/lib/apt/lists/*\n\n"
                "WORKDIR /workspace\nCOPY source/ /workspace/\n\n"
                "# Install this repository's dependencies here.\n"
            ),
        },
        "node": {
            "smoke": "node --version && npm --version",
            "test": "npm test --",
            "test_path": "test/private/task.test.js",
            "dockerfile": (
                "FROM node:22-slim\n\n"
                "RUN apt-get update \\\n"
                "    && apt-get install --yes --no-install-recommends git \\\n"
                "    && rm -rf /var/lib/apt/lists/*\n\n"
                "WORKDIR /workspace\nCOPY source/ /workspace/\n\n"
                "# Install this repository's dependencies here.\n"
            ),
        },
        "go": {
            "smoke": "go version",
            "test": "go test ./... -run",
            "test_path": "internal/task/task_test.go",
            "dockerfile": (
                "FROM golang:1.24-bookworm\n\n"
                "RUN apt-get update \\\n"
                "    && apt-get install --yes --no-install-recommends git \\\n"
                "    && rm -rf /var/lib/apt/lists/*\n\n"
                "WORKDIR /workspace\nCOPY source/ /workspace/\n\n"
                "# Download this repository's dependencies here.\n"
            ),
        },
        "rust": {
            "smoke": "cargo --version",
            "test": "cargo test",
            "test_path": "tests/task.rs",
            "dockerfile": (
                "FROM rust:1-bookworm\n\n"
                "RUN apt-get update \\\n"
                "    && apt-get install --yes --no-install-recommends git \\\n"
                "    && rm -rf /var/lib/apt/lists/*\n\n"
                "WORKDIR /workspace\nCOPY source/ /workspace/\n\n"
                "# Fetch this repository's dependencies here.\n"
            ),
        },
        "custom": {
            "smoke": "git --version",
            "test": "replace-with-an-isolated-test-command",
            "test_path": "tests/private/task.test",
            "dockerfile": (
                "FROM debian:bookworm-slim\n\n"
                "RUN apt-get update \\\n"
                "    && apt-get install --yes --no-install-recommends git \\\n"
                "    && rm -rf /var/lib/apt/lists/*\n\n"
                "WORKDIR /workspace\nCOPY source/ /workspace/\n\n"
                "# Install the required language runtime and dependencies here.\n"
            ),
        },
    }
    return templates[profile]


def scaffold_bundle(
    *, root: Path, repo: str, commit: str, bundle_id: str, profile: str = "auto"
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    repo = normalize_repository_url(repo, relative_to=Path.cwd())
    selected_profile, profile_source = _selected_profile(repo, profile)
    template = _profile_template(selected_profile)
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
                "schema_version": 3,
                "id": bundle_id,
                "repository": {"url": repo, "commit": commit},
                "environment": {
                    "dockerfile": "environment/Dockerfile",
                    "workdir": "/workspace",
                    "smoke_command": template["smoke"],
                    "smoke_timeout_seconds": 300,
                },
                "patches": {
                    "gold": "gold.patch",
                    "tests": "tests/hidden.patch",
                    "solver_view": "tests/solver-view.patch",
                },
                "tests": {
                    "additional_protected_paths": [],
                    "pass_to_pass": [
                        {
                            "id": "replace-me-pass-to-pass",
                            "command": f"{template['test']} replace-me-existing-test",
                            "path": template["test_path"],
                            "marker": "unique source line from test_existing",
                            "timeout_seconds": 120,
                        }
                    ],
                    "fail_to_pass": [
                        {
                            "id": "replace-me-fail-to-pass",
                            "command": f"{template['test']} replace-me-target-test",
                            "path": template["test_path"],
                            "marker": "unique source line from test_fix",
                            "timeout_seconds": 120,
                        }
                    ],
                },
                "candidate": {
                    "allowed_patch_paths": ["src"],
                    "disallowed_patch_paths": [],
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
        "tests/solver-view.patch": "",
        "environment/Dockerfile": template["dockerfile"],
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
        "profile": {"requested": profile, "selected": selected_profile, "source": profile_source},
        "readiness": {
            "status": "draft",
            "completed": [
                "Repository and exact base commit recorded.",
                f"{selected_profile} starter environment generated.",
                "Evaluator-only and candidate-policy placeholders created.",
            ],
            "todo": [
                "Replace the solver-visible problem description.",
                "Define dedicated evaluator-only PASS_TO_PASS and FAIL_TO_PASS tests.",
                "Generate the hidden, solver-view, and gold patches.",
                "Restrict candidate.allowed_patch_paths and add any needed disallowed carve-outs.",
                "Review and pin the generated Dockerfile and dependency installation.",
            ],
        },
        "next_steps": [
            "Replace the placeholder description, tests, and patches.",
            "Set candidate.allowed_patch_paths to implementation files or subtrees and add "
            "disallowed_patch_paths for excluded subtrees.",
            "Customize environment/Dockerfile for the target repository.",
            "Run `task validate --static` before `task init`.",
        ],
    }
