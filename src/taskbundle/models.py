"""Versioned bundle and report contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_EVALUATOR_PATH = [
    "/usr/local/sbin",
    "/usr/local/bin",
    "/usr/sbin",
    "/usr/bin",
    "/sbin",
    "/bin",
]


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
    evaluator_path: list[str] = Field(
        default_factory=lambda: list(DEFAULT_EVALUATOR_PATH),
        min_length=1,
    )
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

    @field_validator("evaluator_path")
    @classmethod
    def evaluator_path_entries_are_absolute(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("must not contain duplicate directories")
        for entry in value:
            path = PurePosixPath(entry)
            if not path.is_absolute() or ".." in path.parts or ":" in entry:
                raise ValueError("must contain absolute container directories without '..' or ':'")
        return value

    @model_validator(mode="after")
    def evaluator_path_is_immutable_at_runtime(self) -> Self:
        writable_roots = (PurePosixPath(self.workdir), PurePosixPath("/tmp"))
        unsafe = [
            entry
            for entry in self.evaluator_path
            if any(
                PurePosixPath(entry) == root or root in PurePosixPath(entry).parents
                for root in writable_roots
            )
        ]
        if unsafe:
            raise ValueError(
                "evaluator_path must exclude the writable workdir and /tmp: " + ", ".join(unsafe)
            )
        return self

    @property
    def evaluator_path_value(self) -> str:
        return ":".join(self.evaluator_path)


class PatchSpec(StrictModel):
    gold: str = "gold.patch"
    tests: str = "tests/hidden.patch"
    solver_view: str = "tests/solver-view.patch"

    _gold_is_relative = field_validator("gold")(_validate_bundle_relative_path)
    _tests_is_relative = field_validator("tests")(_validate_bundle_relative_path)
    _solver_view_is_relative = field_validator("solver_view")(_validate_bundle_relative_path)

    @model_validator(mode="after")
    def patch_paths_are_distinct(self) -> Self:
        paths = [self.gold, self.tests, self.solver_view]
        if len(set(paths)) != len(paths):
            raise ValueError("gold, tests, and solver_view must reference distinct files")
        return self


class TestSpec(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    command: str = Field(min_length=1)
    path: str
    marker: str = Field(min_length=1, max_length=512)
    failure_exit_codes: list[int] = Field(default_factory=lambda: [1], min_length=1)
    timeout_seconds: int = Field(default=120, ge=1, le=86_400)

    _path_is_relative = field_validator("path")(_validate_bundle_relative_path)

    @field_validator("command")
    @classmethod
    def command_must_not_be_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("marker")
    @classmethod
    def marker_must_be_single_line(cls, value: str) -> str:
        if not value.strip() or "\n" in value or "\r" in value or "\x00" in value:
            raise ValueError("must be a non-empty single-line string")
        return value

    @field_validator("failure_exit_codes")
    @classmethod
    def failure_exit_codes_are_valid(cls, value: list[int]) -> list[int]:
        if any(code < 1 or code > 255 for code in value):
            raise ValueError("must contain only exit codes between 1 and 255")
        if len(set(value)) != len(value):
            raise ValueError("must not contain duplicate exit codes")
        return value


class TestSuites(StrictModel):
    additional_protected_paths: list[str] = Field(default_factory=list)
    pass_to_pass: list[TestSpec] = Field(min_length=1)
    fail_to_pass: list[TestSpec] = Field(min_length=1)

    @field_validator("additional_protected_paths")
    @classmethod
    def additional_paths_are_safe_and_unique(cls, value: list[str]) -> list[str]:
        normalized = [_validate_bundle_relative_path(path) for path in value]
        if len(set(normalized)) != len(normalized):
            raise ValueError("must not contain duplicate paths")
        return normalized

    @model_validator(mode="after")
    def selected_tests_are_unambiguous(self) -> Self:
        selected_tests = self.pass_to_pass + self.fail_to_pass
        ids = [test.id for test in selected_tests]
        duplicates = sorted({test_id for test_id in ids if ids.count(test_id) > 1})
        if duplicates:
            joined = ", ".join(duplicates)
            raise ValueError(f"test IDs must be unique across suites: {joined}")
        markers = [test.marker for test in selected_tests]
        duplicate_markers = sorted({marker for marker in markers if markers.count(marker) > 1})
        if duplicate_markers:
            raise ValueError("test markers must be unique across suites")
        selected_paths = {test.path for test in selected_tests}
        overlap = selected_paths & set(self.additional_protected_paths)
        if overlap:
            raise ValueError(
                "additional_protected_paths must not repeat selected test paths: "
                + ", ".join(sorted(overlap))
            )
        return self

    @property
    def evaluator_owned_paths(self) -> set[str]:
        selected = {test.path for test in self.pass_to_pass + self.fail_to_pass}
        return selected | set(self.additional_protected_paths)


class ValidationSpec(StrictModel):
    repetitions: int = Field(default=3, ge=1, le=20)


class RuntimeSpec(StrictModel):
    cpus: float = Field(default=2.0, gt=0, le=64)
    memory: str = Field(default="4g", pattern=r"^[1-9][0-9]*(?:[bkmgBKMG])$")
    pids: int = Field(default=256, ge=16, le=65_536)
    tmpfs_size: str = Field(default="512m", pattern=r"^[1-9][0-9]*(?:[bkmgBKMG])$")
    solver_timeout_seconds: int = Field(default=1800, ge=1, le=86_400)
    max_patch_bytes: int = Field(default=10_000_000, ge=1, le=100_000_000)
    solver_network: Literal[False] = False
    user: None = None


class CandidateSpec(StrictModel):
    allowed_patch_paths: list[str] = Field(min_length=1)

    @field_validator("allowed_patch_paths")
    @classmethod
    def allowed_paths_are_safe_and_unique(cls, value: list[str]) -> list[str]:
        normalized = [_validate_bundle_relative_path(path) for path in value]
        if len(set(normalized)) != len(normalized):
            raise ValueError("must not contain duplicate paths")
        return normalized

    def allows(self, candidate_path: str) -> bool:
        path = PurePosixPath(candidate_path)
        return any(
            path == PurePosixPath(root) or PurePosixPath(root) in path.parents
            for root in self.allowed_patch_paths
        )


class TaskManifest(StrictModel):
    schema_version: Literal[3]
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,62}$")
    repository: RepositorySpec
    environment: EnvironmentSpec
    patches: PatchSpec = Field(default_factory=PatchSpec)
    tests: TestSuites
    candidate: CandidateSpec
    validation: ValidationSpec = Field(default_factory=ValidationSpec)
    runtime: RuntimeSpec = Field(default_factory=RuntimeSpec)

    @model_validator(mode="after")
    def bundle_asset_paths_are_distinct(self) -> Self:
        named_paths = {
            "description": "description.md",
            "dockerfile": self.environment.dockerfile,
            "gold": self.patches.gold,
            "tests": self.patches.tests,
            "solver_view": self.patches.solver_view,
        }
        values = list(named_paths.values())
        duplicates = sorted({path for path in values if values.count(path) > 1})
        if duplicates:
            raise ValueError(
                "description, dockerfile, and trusted patch paths must be distinct: "
                + ", ".join(duplicates)
            )
        protected = self.tests.evaluator_owned_paths
        unsafe_roots = sorted(
            root
            for root in self.candidate.allowed_patch_paths
            if any(
                PurePosixPath(root) == PurePosixPath(path)
                or PurePosixPath(root) in PurePosixPath(path).parents
                or PurePosixPath(path) in PurePosixPath(root).parents
                for path in protected
            )
        )
        if unsafe_roots:
            raise ValueError(
                "candidate.allowed_patch_paths must be disjoint from evaluator-owned paths: "
                + ", ".join(unsafe_roots)
            )
        return self


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
    html_report: str | None = None
    report_index: str | None = None


class BuildMetadata(StrictModel):
    schema_version: Literal[4] = 4
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    bundle_id: str
    repository_url: str
    repository_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    dockerfile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    solver_view_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    secrecy_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    image_tag: str
    image_id: str
    solver_image_tag: str
    solver_image_id: str
    solver_base_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    git_version: str
    docker_client_version: str
    docker_server_version: str
    created_at: datetime
