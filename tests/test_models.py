from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from taskbundle.config import load_bundle
from taskbundle.errors import ConfigurationError
from taskbundle.models import CommandReport, CommandStatus, TaskManifest


def test_loads_valid_bundle(valid_bundle_path: Path) -> None:
    bundle = load_bundle(valid_bundle_path)

    assert bundle.manifest.id == "minimal-python"
    assert bundle.manifest.repository.commit
    assert bundle.test_patch_path.name == "hidden.patch"
    assert bundle.solver_view_patch_path.name == "solver-view.patch"


def test_rejects_short_commit(valid_bundle_path: Path) -> None:
    payload = json.loads((valid_bundle_path / "task.json").read_text(encoding="utf-8"))
    payload["repository"]["commit"] = "main"
    (valid_bundle_path / "task.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ConfigurationError) as caught:
        load_bundle(valid_bundle_path)

    assert caught.value.exit_code == 2
    assert caught.value.details["issues"][0]["field"] == "repository.commit"


def test_rejects_manifest_path_traversal(valid_bundle_path: Path) -> None:
    payload = json.loads((valid_bundle_path / "task.json").read_text(encoding="utf-8"))
    payload["patches"]["gold"] = "../outside.patch"
    (valid_bundle_path / "task.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="manifest validation failed"):
        load_bundle(valid_bundle_path)


def test_rejects_reused_trusted_patch_paths(valid_bundle_path: Path) -> None:
    payload = json.loads((valid_bundle_path / "task.json").read_text(encoding="utf-8"))
    payload["patches"]["solver_view"] = payload["patches"]["tests"]

    with pytest.raises(ValidationError, match="must reference distinct files"):
        TaskManifest.model_validate(payload)


def test_rejects_solver_network_access(valid_bundle_path: Path) -> None:
    payload = json.loads((valid_bundle_path / "task.json").read_text(encoding="utf-8"))
    payload["runtime"]["solver_network"] = True

    with pytest.raises(ValidationError, match="Input should be False"):
        TaskManifest.model_validate(payload)


def test_additional_protected_paths_are_explicit_and_safe(valid_bundle_path: Path) -> None:
    payload = json.loads((valid_bundle_path / "task.json").read_text(encoding="utf-8"))
    payload["tests"]["additional_protected_paths"] = ["generated/evaluator-tests.bin"]

    manifest = TaskManifest.model_validate(payload)

    assert "generated/evaluator-tests.bin" in manifest.tests.evaluator_owned_paths
    payload["tests"]["additional_protected_paths"] = ["../outside.bin"]
    with pytest.raises(ValidationError, match="path relative to the bundle root"):
        TaskManifest.model_validate(payload)


def test_candidate_patch_paths_are_safe_and_disjoint_from_evaluator_files(
    valid_bundle_path: Path,
) -> None:
    payload = json.loads((valid_bundle_path / "task.json").read_text(encoding="utf-8"))
    payload["candidate"]["allowed_patch_paths"] = ["../outside.py"]
    with pytest.raises(ValidationError, match="path relative to the bundle root"):
        TaskManifest.model_validate(payload)

    payload["candidate"]["allowed_patch_paths"] = ["test_hidden.py"]
    with pytest.raises(ValidationError, match="disjoint from evaluator-owned paths"):
        TaskManifest.model_validate(payload)


def test_candidate_disallowed_paths_carve_out_allowed_subtrees(valid_bundle_path: Path) -> None:
    payload = json.loads((valid_bundle_path / "task.json").read_text(encoding="utf-8"))
    payload["candidate"] = {
        "allowed_patch_paths": ["src"],
        "disallowed_patch_paths": ["src/generated", "src/vendor.py"],
    }

    manifest = TaskManifest.model_validate(payload)

    assert manifest.candidate.allows("src/main.py")
    assert not manifest.candidate.allows("src/generated/schema.py")
    assert not manifest.candidate.allows("src/vendor.py")
    assert not manifest.candidate.allows("tests/test_main.py")


@pytest.mark.parametrize("path", [".", "src/*", "src/?.py", "src/[a-z].py"])
def test_candidate_policy_paths_must_be_literal_non_root_prefixes(
    valid_bundle_path: Path, path: str
) -> None:
    payload = json.loads((valid_bundle_path / "task.json").read_text(encoding="utf-8"))
    payload["candidate"]["allowed_patch_paths"] = [path]

    with pytest.raises(ValidationError, match=r"repository root|literal path prefix"):
        TaskManifest.model_validate(payload)


def test_candidate_disallowed_paths_must_be_beneath_an_allowed_root(
    valid_bundle_path: Path,
) -> None:
    payload = json.loads((valid_bundle_path / "task.json").read_text(encoding="utf-8"))
    payload["candidate"]["disallowed_patch_paths"] = ["other/private.py"]

    with pytest.raises(ValidationError, match="beneath an allowed_patch_paths root"):
        TaskManifest.model_validate(payload)


def test_evaluator_path_cannot_include_the_writable_repository(valid_bundle_path: Path) -> None:
    payload = json.loads((valid_bundle_path / "task.json").read_text(encoding="utf-8"))
    payload["environment"]["evaluator_path"] = ["/workspace/bin", "/usr/bin"]

    with pytest.raises(ValidationError, match="exclude the writable workdir"):
        TaskManifest.model_validate(payload)


def test_rejects_symlink_escape(valid_bundle_path: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.patch"
    outside.write_text("", encoding="utf-8")
    gold = valid_bundle_path / "gold.patch"
    gold.unlink()
    gold.symlink_to(outside)

    with pytest.raises(ConfigurationError, match="resolves outside"):
        load_bundle(valid_bundle_path)


def test_rejects_duplicate_test_ids(valid_bundle_path: Path) -> None:
    payload = json.loads((valid_bundle_path / "task.json").read_text(encoding="utf-8"))
    payload["tests"]["fail_to_pass"][0]["id"] = payload["tests"]["pass_to_pass"][0]["id"]

    with pytest.raises(ValidationError, match="test IDs must be unique"):
        TaskManifest.model_validate(payload)


def test_rejects_duplicate_test_markers(valid_bundle_path: Path) -> None:
    payload = json.loads((valid_bundle_path / "task.json").read_text(encoding="utf-8"))
    payload["tests"]["fail_to_pass"][0]["marker"] = payload["tests"]["pass_to_pass"][0]["marker"]

    with pytest.raises(ValidationError, match="test markers must be unique"):
        TaskManifest.model_validate(payload)


def test_relative_local_repository_is_resolved_from_bundle(
    valid_bundle_path: Path,
    minimal_repository: tuple[Path, str],
) -> None:
    repository, _ = minimal_repository
    payload = json.loads((valid_bundle_path / "task.json").read_text(encoding="utf-8"))
    payload["repository"]["url"] = "../repository"
    (valid_bundle_path / "task.json").write_text(json.dumps(payload), encoding="utf-8")

    assert load_bundle(valid_bundle_path).manifest.repository.url == str(repository.resolve())


def test_report_schema_forbids_unknown_fields() -> None:
    now = datetime.now(UTC)
    valid = {
        "command_id": "command-1",
        "command": "doctor",
        "bundle_id": None,
        "status": CommandStatus.SUCCEEDED,
        "started_at": now,
        "ended_at": now,
    }
    report = CommandReport.model_validate(valid)
    assert report.schema_version == 1

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CommandReport.model_validate({**valid, "surprise": True})
