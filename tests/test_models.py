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
