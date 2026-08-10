from __future__ import annotations

import json
from pathlib import Path

from taskbundle.config import load_bundle
from taskbundle.lifecycle.initialize import sha256_file

PROJECT_ROOT = Path(__file__).parents[1]
EXAMPLE = PROJECT_ROOT / "examples" / "swe-bench-pro-ansible"
SAFE_EVAL_EXAMPLE = PROJECT_ROOT / "examples" / "swe-bench-pro-ansible-safe-eval"


def test_checked_in_swebench_pro_bundle_has_immutable_provenance() -> None:
    bundle = load_bundle(EXAMPLE)
    provenance = json.loads((EXAMPLE / "dataset.json").read_text(encoding="utf-8"))

    assert bundle.manifest.repository.url == "https://github.com/ansible/ansible.git"
    assert bundle.manifest.repository.commit == "de01db08d00c8d2438e1ba5989c313ba16a145b0"
    assert bundle.manifest.schema_version == 3
    assert provenance["dataset_revision"] == "7ab5114912baf22bb098818e604c02fe7ad2c11f"
    assert provenance["harness_revision"] == "ca10a60a5fcae51e6948ffe1485d4153d421e6c5"
    assert sha256_file(bundle.gold_patch_path) == (
        "787d63373c26ae760362464bc2b763771b51f83a1e61df830bb996bf5c72f7b3"
    )
    assert sha256_file(bundle.test_patch_path) == (
        "abfad56065c5dc1fd42a45ff5b6d74173ecb1ebbb2e1e5292fad42401c1f3720"
    )
    assert sha256_file(bundle.solver_view_patch_path) == (
        "280e884c70e19af1db6c7eca83efde0b963ed147f5707014458d1fd7a743ead4"
    )
    assert len(bundle.manifest.tests.pass_to_pass) == 4
    assert len(bundle.manifest.tests.fail_to_pass) == 1
    assert set(bundle.manifest.candidate.allowed_patch_paths) == {
        "changelogs/fragments/75072_undefined_yaml.yml",
        "lib/ansible/parsing/yaml/dumper.py",
        "lib/ansible/plugins/filter/core.py",
    }
    assert bundle.manifest.candidate.disallowed_patch_paths == []
    selected_tests = bundle.manifest.tests.pass_to_pass + bundle.manifest.tests.fail_to_pass
    assert all("/usr/local/bin/python -I -m pytest" in test.command for test in selected_tests)
    assert {test.path for test in bundle.manifest.tests.pass_to_pass} == {
        "test/units/parsing/yaml/test_dumper.py"
    }
    redaction = bundle.solver_view_patch_path.read_text(encoding="utf-8")
    for test in bundle.manifest.tests.pass_to_pass:
        assert any(line.startswith("-") and test.marker in line for line in redaction.splitlines())


def test_checked_in_evaluation_summarizes_a_resolved_run() -> None:
    evaluation = json.loads((EXAMPLE / "evaluation.json").read_text(encoding="utf-8"))

    assert evaluation["schema_version"] == 1
    assert evaluation["task_id"] == "swebench-pro-ansible-12734fa2"
    assert evaluation["dataset_instance_id"] == provenance_instance_id()
    assert evaluation["resolved"] is True
    assert evaluation["source_run"]["command_id"] == "20260810T164458035453Z-841b8f79"
    assert evaluation["source_run"]["cli_version"] == "0.3.0"
    assert evaluation["source_run"]["repetitions"] == 1
    assert evaluation["source_run"]["evaluator_isolation"] == "phase"
    assert evaluation["source_run"]["candidate_input_sha256"] == sha256_file(EXAMPLE / "gold.patch")
    captured_patch = (
        EXAMPLE
        / ".taskbundle"
        / "commands"
        / evaluation["source_run"]["command_id"]
        / "solver.patch"
    )
    if captured_patch.is_file():
        assert evaluation["source_run"]["patch_sha256"] == sha256_file(captured_patch)

    baseline = evaluation["phases"]["baseline"]
    post_solver = evaluation["phases"]["post_solver"]
    target_baseline = next(result for result in baseline if result["suite"] == "fail_to_pass")

    assert len(baseline) == 5
    assert all(result["matched"] for result in baseline)
    assert target_baseline["expected"] == target_baseline["observed"] == "fail"
    assert len(post_solver) == 5
    assert all(result["matched"] and result["observed"] == "pass" for result in post_solver)


def test_safe_eval_swebench_pro_bundle_has_immutable_provenance() -> None:
    bundle = load_bundle(SAFE_EVAL_EXAMPLE)
    provenance = json.loads((SAFE_EVAL_EXAMPLE / "dataset.json").read_text(encoding="utf-8"))

    assert bundle.manifest.repository.url == "https://github.com/ansible/ansible.git"
    assert bundle.manifest.repository.commit == "59ca05b70994b07a9507f61a0871146a4991b262"
    assert provenance["instance_id"] == (
        "instance_ansible__ansible-d9f1866249756efc264b00ff7497e92c11a9885f-"
        "v0f01c69f1e2528b935359cfe578530722bca2c59"
    )
    assert provenance["dataset_revision"] == "7ab5114912baf22bb098818e604c02fe7ad2c11f"
    assert provenance["harness_revision"] == "ca10a60a5fcae51e6948ffe1485d4153d421e6c5"
    assert sha256_file(bundle.gold_patch_path) == (
        "085236f733a15425970deb71e82f48a39d8c959fd2bd47ea79adc5b1c16a8374"
    )
    assert sha256_file(bundle.test_patch_path) == (
        "ef22b72858cfa7b69f0c860fbf87fe296e7d7b1516d6c30a59e3b328e345a832"
    )
    assert sha256_file(bundle.solver_view_patch_path) == (
        "88fb511150e3117bd617eb577bd407c14f7fdbf549c263d489f4ea0693fa718e"
    )
    assert len(bundle.manifest.tests.pass_to_pass) == 1
    assert len(bundle.manifest.tests.fail_to_pass) == 1
    assert bundle.manifest.runtime.memory == "2g"
    assert set(bundle.manifest.candidate.allowed_patch_paths) == {
        "changelogs/fragments/deprecate-safe-evals.yml",
        "lib/ansible/module_utils/basic.py",
        "lib/ansible/module_utils/common/validation.py",
    }
    assert bundle.manifest.candidate.disallowed_patch_paths == []
    assert "deleted file mode 100644" in bundle.solver_view_patch_path.read_text(encoding="utf-8")


def test_safe_eval_checked_in_evaluation_summarizes_a_resolved_run() -> None:
    evaluation = json.loads((SAFE_EVAL_EXAMPLE / "evaluation.json").read_text(encoding="utf-8"))
    provenance = json.loads((SAFE_EVAL_EXAMPLE / "dataset.json").read_text(encoding="utf-8"))

    assert evaluation["schema_version"] == 1
    assert evaluation["task_id"] == "swebench-pro-ansible-d9f18662"
    assert evaluation["dataset_instance_id"] == provenance["instance_id"]
    assert evaluation["resolved"] is True
    assert evaluation["source_run"]["command_id"] == provenance["resolved_run_command_id"]
    assert evaluation["source_run"]["cli_version"] == "0.3.0"
    assert evaluation["source_run"]["repetitions"] == 1
    assert evaluation["source_run"]["evaluator_isolation"] == "phase"
    assert evaluation["source_run"]["candidate_input_sha256"] == sha256_file(
        SAFE_EVAL_EXAMPLE / "gold.patch"
    )
    assert evaluation["summary"] == {
        "baseline": {"matched": 2, "total": 2},
        "post_solver": {"passed": 2, "total": 2},
    }
    assert all(result["matched"] for result in evaluation["phases"]["baseline"])
    assert all(
        result["matched"] and result["observed"] == "pass"
        for result in evaluation["phases"]["post_solver"]
    )


def provenance_instance_id() -> str:
    provenance = json.loads((EXAMPLE / "dataset.json").read_text(encoding="utf-8"))
    return str(provenance["instance_id"])


def test_submission_docs_cover_usage_and_tradeoffs() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    design = (PROJECT_ROOT / "DESIGN.md").read_text(encoding="utf-8")

    for command in (
        "task new",
        "task init",
        "task validate",
        "task run",
        "task report",
        "task doctor",
    ):
        assert command in readme
    assert "evaluation.json" in readme
    for screenshot in (
        "docs/report-screenshots/resolved-run-overview.png",
        "docs/report-screenshots/problem-statement.png",
        "docs/report-screenshots/diagnosis-and-tests.png",
        "docs/report-screenshots/artifact-inventory.png",
    ):
        assert screenshot in readme
        assert (PROJECT_ROOT / screenshot).is_file()
    for decision in (
        "## Lifecycle and correctness",
        "## Test secrecy and trust boundary",
        "## Runtime isolation",
        "## Reproducibility and determinism",
        "## Evidence and observability",
        "## UX and error model",
        "## Performance tradeoffs",
    ):
        assert decision in design
    assert len([paragraph for paragraph in design.split("\n\n") if paragraph.strip()]) >= 3
