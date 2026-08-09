from __future__ import annotations

import json
from pathlib import Path

from taskbundle.config import load_bundle
from taskbundle.lifecycle.initialize import sha256_file

PROJECT_ROOT = Path(__file__).parents[1]
EXAMPLE = PROJECT_ROOT / "examples" / "swe-bench-pro-ansible"


def test_checked_in_swebench_pro_bundle_has_immutable_provenance() -> None:
    bundle = load_bundle(EXAMPLE)
    provenance = json.loads((EXAMPLE / "dataset.json").read_text(encoding="utf-8"))

    assert bundle.manifest.repository.url == "https://github.com/ansible/ansible.git"
    assert bundle.manifest.repository.commit == "de01db08d00c8d2438e1ba5989c313ba16a145b0"
    assert provenance["dataset_revision"] == "7ab5114912baf22bb098818e604c02fe7ad2c11f"
    assert provenance["harness_revision"] == "ca10a60a5fcae51e6948ffe1485d4153d421e6c5"
    assert sha256_file(bundle.gold_patch_path) == (
        "787d63373c26ae760362464bc2b763771b51f83a1e61df830bb996bf5c72f7b3"
    )
    assert sha256_file(bundle.test_patch_path) == (
        "abfad56065c5dc1fd42a45ff5b6d74173ecb1ebbb2e1e5292fad42401c1f3720"
    )
    assert len(bundle.manifest.tests.pass_to_pass) == 4
    assert len(bundle.manifest.tests.fail_to_pass) == 1


def test_checked_in_evaluation_summarizes_a_resolved_run() -> None:
    evaluation = json.loads((EXAMPLE / "evaluation.json").read_text(encoding="utf-8"))

    assert evaluation["schema_version"] == 1
    assert evaluation["task_id"] == "swebench-pro-ansible-12734fa2"
    assert evaluation["dataset_instance_id"] == provenance_instance_id()
    assert evaluation["resolved"] is True
    assert evaluation["source_run"]["command_id"] == "20260809T194247906772Z-7e578b0b"
    assert evaluation["source_run"]["patch_sha256"] == sha256_file(EXAMPLE / "gold.patch")

    baseline = evaluation["phases"]["baseline"]
    post_solver = evaluation["phases"]["post_solver"]
    target_baseline = next(result for result in baseline if result["suite"] == "fail_to_pass")

    assert len(baseline) == 5
    assert all(result["matched"] for result in baseline)
    assert target_baseline["expected"] == target_baseline["observed"] == "fail"
    assert len(post_solver) == 5
    assert all(result["matched"] and result["observed"] == "pass" for result in post_solver)


def provenance_instance_id() -> str:
    provenance = json.loads((EXAMPLE / "dataset.json").read_text(encoding="utf-8"))
    return str(provenance["instance_id"])


def test_submission_docs_cover_usage_and_tradeoffs() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    design = (PROJECT_ROOT / "DESIGN.md").read_text(encoding="utf-8")

    for command in ("task init", "task validate", "task run", "task logs"):
        assert command in readme
    assert "evaluation.json" in readme
    assert "## Isolation" in design
    assert "## Observability and reproducibility" in design
    assert len([paragraph for paragraph in design.split("\n\n") if paragraph.strip()]) >= 3
