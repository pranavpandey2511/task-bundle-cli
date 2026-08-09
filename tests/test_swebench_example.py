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
