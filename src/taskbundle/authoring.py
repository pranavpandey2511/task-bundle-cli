"""Fast, language-neutral checks for task authors."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from taskbundle.config import Bundle
from taskbundle.errors import ConfigurationError, InvalidTaskError
from taskbundle.patches import validate_patch_contract
from taskbundle.provenance import sha256_path

_FROM = re.compile(r"^\s*FROM\s+(?:(?:--platform=\S+)\s+)?(\S+)(?:\s+AS\s+(\S+))?\s*$", re.I)
_DIGEST_PIN = re.compile(r"^.+@sha256:[0-9a-fA-F]{64}$")


def _read_text(path: Path, *, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ConfigurationError(f"Could not read {label}: {error}") from error


def _external_base_images(dockerfile: str) -> tuple[list[str], int]:
    """Return external FROM references while ignoring prior named stages."""

    stages: set[str] = set()
    images: list[str] = []
    from_count = 0
    for line in dockerfile.splitlines():
        match = _FROM.fullmatch(line)
        if match is None:
            continue
        from_count += 1
        image, alias = match.groups()
        if image.lower() != "scratch" and image not in stages:
            images.append(image)
        if alias:
            stages.add(alias)
    return images, from_count


def _check(
    name: str, status: str, detail: str, recommendation: str | None = None
) -> dict[str, Any]:
    result: dict[str, Any] = {"name": name, "status": status, "detail": detail}
    if recommendation:
        result["recommendation"] = recommendation
    return result


def check_bundle(bundle: Bundle) -> dict[str, Any]:
    """Inspect a complete bundle without cloning, building, or starting Docker."""

    description = _read_text(bundle.description_path, label="description.md")
    dockerfile = _read_text(bundle.dockerfile_path, label="Dockerfile")
    gold_patch = _read_text(bundle.gold_patch_path, label="gold patch")
    test_patch = _read_text(bundle.test_patch_path, label="hidden test patch")
    solver_view_patch = _read_text(bundle.solver_view_patch_path, label="solver-view patch")
    patch_contract = validate_patch_contract(
        bundle=bundle,
        gold_patch=gold_patch,
        test_patch=test_patch,
        solver_view_patch=solver_view_patch,
    )

    selected_tests = bundle.manifest.tests.pass_to_pass + bundle.manifest.tests.fail_to_pass
    marker_conflicts = [
        {"test_id": test.id, "path": test.path}
        for test in selected_tests
        if test.marker in description
    ]
    if marker_conflicts:
        raise InvalidTaskError(
            "Evaluator test markers are visible in the solver description.",
            hint="Remove evaluator-test source from description.md.",
            details={"conflicts": marker_conflicts},
        )

    checks = [
        _check(
            "bundle-contract",
            "pass",
            "Manifest, required files, paths, and strict field types are valid.",
        ),
        _check(
            "language-adapter",
            "pass",
            (
                f"Dockerfile plus {len(selected_tests)} author-owned shell test commands; "
                "no language or test framework is inferred by the CLI."
            ),
        ),
        _check(
            "test-secrecy",
            "pass",
            (
                f"{len(bundle.manifest.tests.evaluator_owned_paths)} evaluator-owned paths and "
                f"{len(selected_tests)} unique markers are declared outside the description."
            ),
        ),
        _check(
            "candidate-policy",
            "pass",
            (
                f"Gold changes fit {len(bundle.manifest.candidate.allowed_patch_paths)} allowed "
                f"path roots, avoid {len(bundle.manifest.candidate.disallowed_patch_paths)} "
                "disallowed roots, and are disjoint from evaluator-owned paths."
            ),
        ),
    ]

    images, from_count = _external_base_images(dockerfile)
    unpinned_images = sorted(image for image in images if _DIGEST_PIN.fullmatch(image) is None)
    if from_count == 0:
        checks.append(
            _check(
                "base-image-pinning",
                "warning",
                "No statically inspectable FROM instruction was found.",
                (
                    "Confirm the Dockerfile is complete; `task init` remains the authoritative "
                    "build check."
                ),
            )
        )
    elif unpinned_images:
        checks.append(
            _check(
                "base-image-pinning",
                "warning",
                "Base image references are not digest-pinned: " + ", ".join(unpinned_images),
                "Pin external FROM images with @sha256:<digest> for cross-machine identity.",
            )
        )
    else:
        checks.append(
            _check(
                "base-image-pinning",
                "pass",
                "Every external base image is scratch or pinned by SHA-256 digest.",
            )
        )

    repository = bundle.manifest.repository.url
    parsed_repository = urlsplit(repository)
    if parsed_repository.scheme == "file" or (
        not parsed_repository.scheme and Path(repository).is_absolute()
    ):
        checks.append(
            _check(
                "repository-portability",
                "warning",
                f"Repository is a machine-local path: {repository}",
                "Use an immutable remote URL when the bundle must run on another machine.",
            )
        )
    else:
        checks.append(
            _check(
                "repository-portability",
                "pass",
                "Repository location is not tied to an absolute local filesystem path.",
            )
        )

    repetitions = bundle.manifest.validation.repetitions
    if repetitions == 1:
        checks.append(
            _check(
                "flakiness-sampling",
                "pass",
                "One repetition uses the fast default and does not sample for flakiness.",
                "Use --repetitions 2 or more when certifying a task or investigating flakes.",
            )
        )
    else:
        checks.append(
            _check(
                "flakiness-sampling",
                "pass",
                f"Final manifest requests {repetitions} attempts per selected test.",
            )
        )

    checks.append(
        _check(
            "evaluator-isolation",
            "pass",
            (
                "Selected tests share one evaluator per phase and repetition."
                if bundle.manifest.validation.evaluator_isolation.value == "phase"
                else "Every selected test attempt receives its own evaluator container."
            ),
        )
    )

    input_paths = {
        "task.json": bundle.manifest_path,
        "description.md": bundle.description_path,
        bundle.manifest.environment.dockerfile: bundle.dockerfile_path,
        bundle.manifest.patches.gold: bundle.gold_patch_path,
        bundle.manifest.patches.tests: bundle.test_patch_path,
        bundle.manifest.patches.solver_view: bundle.solver_view_patch_path,
    }
    warnings = [check for check in checks if check["status"] == "warning"]
    return {
        "valid": True,
        "docker_required": False,
        "bundle": str(bundle.root),
        "bundle_id": bundle.manifest.id,
        "repository": {
            "url": repository,
            "commit": bundle.manifest.repository.commit.lower(),
        },
        "checks": checks,
        "warning_count": len(warnings),
        "warnings": warnings,
        "inputs_sha256": {name: sha256_path(path) for name, path in input_paths.items()},
        "patch_contract": patch_contract,
        "test_commands": [
            {
                "suite": suite,
                "id": test.id,
                "command": test.command,
                "path": test.path,
                "timeout_seconds": test.timeout_seconds,
                "failure_exit_codes": test.failure_exit_codes,
            }
            for suite, tests in (
                ("pass_to_pass", bundle.manifest.tests.pass_to_pass),
                ("fail_to_pass", bundle.manifest.tests.fail_to_pass),
            )
            for test in tests
        ],
    }
