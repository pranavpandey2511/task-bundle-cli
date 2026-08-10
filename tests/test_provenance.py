from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from taskbundle.config import load_bundle
from taskbundle.lifecycle.initialize import (
    build_fingerprint,
    image_tag,
    sha256_file,
    solver_secrecy_contract_sha256,
)
from taskbundle.models import BuildMetadata, EvaluatorIsolation
from taskbundle.provenance import build_execution_provenance


def test_execution_fingerprint_is_stable_and_changes_with_inputs(
    valid_bundle_path: Path,
) -> None:
    bundle = load_bundle(valid_bundle_path)
    fingerprint = build_fingerprint(bundle)
    metadata = BuildMetadata(
        fingerprint=fingerprint,
        bundle_id=bundle.manifest.id,
        repository_url=bundle.manifest.repository.url,
        repository_commit=bundle.manifest.repository.commit,
        dockerfile_sha256=sha256_file(bundle.dockerfile_path),
        solver_view_sha256=sha256_file(bundle.solver_view_patch_path),
        secrecy_contract_sha256=solver_secrecy_contract_sha256(bundle),
        image_tag=image_tag(bundle.manifest.id, fingerprint),
        image_id="sha256:" + "e" * 64,
        solver_image_tag=f"taskbundle/{bundle.manifest.id}-solver:{fingerprint[:16]}",
        solver_image_id="sha256:" + "f" * 64,
        solver_base_commit="d" * 40,
        git_version="git version test",
        docker_client_version="test",
        docker_server_version="test",
        created_at=datetime.now(UTC),
    )

    first = build_execution_provenance(
        bundle=bundle,
        metadata=metadata,
        command="validate",
        repetitions=3,
    )
    second = build_execution_provenance(
        bundle=bundle,
        metadata=metadata,
        command="validate",
        repetitions=3,
    )
    assert first["execution_fingerprint"] == second["execution_fingerprint"]
    assert len(first["execution_fingerprint"]) == 64

    strict = build_execution_provenance(
        bundle=bundle,
        metadata=metadata,
        command="validate",
        repetitions=3,
        evaluator_isolation=EvaluatorIsolation.TEST_ATTEMPT,
    )
    assert first["evaluator_isolation"] == "phase"
    assert strict["evaluator_isolation"] == "test-attempt"
    assert strict["execution_fingerprint"] != first["execution_fingerprint"]

    bundle.description_path.write_text("changed task description\n", encoding="utf-8")
    changed = build_execution_provenance(
        bundle=load_bundle(valid_bundle_path),
        metadata=metadata,
        command="validate",
        repetitions=3,
    )
    assert changed["execution_fingerprint"] != first["execution_fingerprint"]
