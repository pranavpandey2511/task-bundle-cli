"""Versioned bundle and report contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _validate_bundle_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value.strip() or path.is_absolute() or ".." in path.parts:
        raise ValueError("must be a non-empty path relative to the bundle root")
    return value


class RepositorySpec(StrictModel):
    url: str = Field(min_length=1)
    commit: str = Field(pattern=r"^[0-9a-fA-F]{40}$")

    @field_validator("url")
    @classmethod
    def url_must_not_be_whitespace(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and (parsed.username or parsed.password):
            raise ValueError(
                "must not embed credentials; configure Git authentication outside task.json"
            )
        return value


class EnvironmentSpec(StrictModel):
    dockerfile: str = "environment/Dockerfile"
    workdir: str = "/workspace"
    smoke_command: str = Field(min_length=1)
    build_timeout_seconds: int = Field(default=1800, ge=1, le=86_400)
    smoke_timeout_seconds: int = Field(default=300, ge=1, le=86_400)

    _dockerfile_is_relative = field_validator("dockerfile")(_validate_bundle_relative_path)

    @field_validator("workdir")
    @classmethod
    def workdir_is_absolute(cls, value: str) -> str:
        path = PurePosixPath(value)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("must be an absolute container path without '..'")
        return value

    @field_validator("smoke_command")
    @classmethod
    def command_must_not_be_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class PatchSpec(StrictModel):
    gold: str = "gold.patch"
    tests: str = "tests/hidden.patch"

    _gold_is_relative = field_validator("gold")(_validate_bundle_relative_path)
    _tests_is_relative = field_validator("tests")(_validate_bundle_relative_path)


class TestSpec(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    command: str = Field(min_length=1)
    timeout_seconds: int = Field(default=120, ge=1, le=86_400)

    @field_validator("command")
    @classmethod
    def command_must_not_be_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class TestSuites(StrictModel):
    pass_to_pass: list[TestSpec] = Field(min_length=1)
    fail_to_pass: list[TestSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def test_ids_are_unique(self) -> Self:
        ids = [test.id for test in self.pass_to_pass + self.fail_to_pass]
        duplicates = sorted({test_id for test_id in ids if ids.count(test_id) > 1})
        if duplicates:
            joined = ", ".join(duplicates)
            raise ValueError(f"test IDs must be unique across suites: {joined}")
        return self


class ValidationSpec(StrictModel):
    repetitions: int = Field(default=3, ge=1, le=20)


class RuntimeSpec(StrictModel):
    cpus: float = Field(default=2.0, gt=0, le=64)
    memory: str = Field(default="4g", pattern=r"^[1-9][0-9]*(?:[bkmgBKMG])$")
    pids: int = Field(default=256, ge=16, le=65_536)
    tmpfs_size: str = Field(default="512m", pattern=r"^[1-9][0-9]*(?:[bkmgBKMG])$")
    solver_timeout_seconds: int = Field(default=1800, ge=1, le=86_400)
    max_patch_bytes: int = Field(default=10_000_000, ge=1, le=100_000_000)
    solver_network: bool = False
    user: str | None = None


class TaskManifest(StrictModel):
    schema_version: int = Field(ge=1, le=1)
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,62}$")
    repository: RepositorySpec
    environment: EnvironmentSpec
    patches: PatchSpec = Field(default_factory=PatchSpec)
    tests: TestSuites
    validation: ValidationSpec = Field(default_factory=ValidationSpec)
    runtime: RuntimeSpec = Field(default_factory=RuntimeSpec)


class CommandStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class TestObservation(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"
    TIMEOUT = "timeout"


class TestResult(StrictModel):
    phase: str
    suite: str
    test_id: str
    attempt: int = Field(ge=1)
    expected: TestObservation
    observed: TestObservation
    exit_code: int | None
    duration_seconds: float = Field(ge=0)
    log_artifact: str | None = None


class ErrorPayload(StrictModel):
    kind: str
    message: str
    hint: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class CommandReport(StrictModel):
    schema_version: int = 1
    command_id: str
    command: str
    bundle_id: str | None
    status: CommandStatus
    started_at: datetime
    ended_at: datetime
    data: dict[str, Any] = Field(default_factory=dict)
    error: ErrorPayload | None = None


class BuildMetadata(StrictModel):
    schema_version: int = 1
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    bundle_id: str
    repository_url: str
    repository_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    dockerfile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    image_tag: str
    image_id: str
    git_version: str
    docker_client_version: str
    docker_server_version: str
    created_at: datetime
