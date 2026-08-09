from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest


def run_git(
    *arguments: str,
    cwd: Path,
    environment: Mapping[str, str] | None = None,
) -> str:
    process_environment = os.environ.copy()
    if environment:
        process_environment.update(environment)
    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        env=process_environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def fixture_assets() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def minimal_repository(tmp_path: Path, fixture_assets: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    shutil.copytree(fixture_assets / "minimal_repo", repository)
    run_git("init", "--quiet", cwd=repository)
    run_git("config", "user.name", "Task Bundle Tests", cwd=repository)
    run_git("config", "user.email", "taskbundle@example.invalid", cwd=repository)
    run_git("add", ".", cwd=repository)
    run_git(
        "commit",
        "--quiet",
        "-m",
        "baseline",
        cwd=repository,
        environment={
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
        },
    )
    return repository, run_git("rev-parse", "HEAD", cwd=repository)


@pytest.fixture
def valid_bundle_path(
    tmp_path: Path,
    fixture_assets: Path,
    minimal_repository: tuple[Path, str],
) -> Path:
    repository, commit = minimal_repository
    bundle = tmp_path / "bundle"
    (bundle / "environment").mkdir(parents=True)
    (bundle / "tests").mkdir()
    shutil.copy2(fixture_assets / "Dockerfile", bundle / "environment" / "Dockerfile")
    shutil.copy2(fixture_assets / "gold.patch", bundle / "gold.patch")
    shutil.copy2(fixture_assets / "hidden.patch", bundle / "tests" / "hidden.patch")
    (bundle / "description.md").write_text(
        "Fix subtract() so it returns the mathematical difference.\n", encoding="utf-8"
    )
    manifest = {
        "schema_version": 1,
        "id": "minimal-python",
        "repository": {"url": str(repository), "commit": commit},
        "environment": {
            "dockerfile": "environment/Dockerfile",
            "workdir": "/workspace",
            "smoke_command": "python -m unittest -q test_public.py",
            "build_timeout_seconds": 600,
            "smoke_timeout_seconds": 60,
        },
        "patches": {"gold": "gold.patch", "tests": "tests/hidden.patch"},
        "tests": {
            "pass_to_pass": [
                {
                    "id": "add-remains-available",
                    "command": (
                        "python -m unittest -q test_hidden.HiddenTests.test_add_remains_available"
                    ),
                    "timeout_seconds": 30,
                }
            ],
            "fail_to_pass": [
                {
                    "id": "subtracts",
                    "command": "python -m unittest -q test_hidden.HiddenTests.test_subtracts",
                    "timeout_seconds": 30,
                }
            ],
        },
        "validation": {"repetitions": 3},
        "runtime": {
            "cpus": 1,
            "memory": "512m",
            "pids": 64,
            "solver_timeout_seconds": 60,
            "solver_network": False,
        },
    }
    (bundle / "task.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return bundle
