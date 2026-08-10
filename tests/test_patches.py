from __future__ import annotations

import json
from pathlib import Path

import pytest

from taskbundle.config import load_bundle
from taskbundle.errors import InvalidTaskError
from taskbundle.patches import PatchFormatError, changed_paths_from_patch, validate_patch_contract


def test_changed_paths_reads_and_cross_checks_every_path_header() -> None:
    patch = """diff --git a/test_hidden.py b/test_hidden.py
index 1111111..2222222 100644
--- a/test_hidden.py
+++ b/test_hidden.py
@@ -1 +1 @@
-old
+new
diff --git "a/old name.py" "b/new name.py"
similarity index 100%
rename from old name.py
rename to new name.py
diff --git "a/source name.py" "b/copied name.py"
similarity index 100%
copy from source name.py
copy to copied name.py
"""

    assert changed_paths_from_patch(patch) == {
        "test_hidden.py",
        "old name.py",
        "new name.py",
        "source name.py",
        "copied name.py",
    }


def test_changed_paths_rejects_a_mismatched_pre_hunk_path() -> None:
    patch = """diff --git a/test_hidden.py b/test_hidden.py
--- a/calculator.py
+++ b/calculator.py
@@ -1 +1 @@
-old
+new
"""

    with pytest.raises(PatchFormatError, match="--- path does not match"):
        changed_paths_from_patch(patch)


def test_changed_paths_rejects_an_appended_traditional_diff() -> None:
    patch = """diff --git a/test_hidden.py b/test_hidden.py
--- a/test_hidden.py
+++ b/test_hidden.py
@@ -1 +1 @@
-old
+new
--- a/calculator.py
+++ b/calculator.py
@@ -1 +1 @@
-old implementation
+new implementation
"""

    with pytest.raises(PatchFormatError, match="duplicate --- header"):
        changed_paths_from_patch(patch)


def test_changed_paths_does_not_treat_hunk_content_as_file_headers() -> None:
    patch = """diff --git a/test_hidden.py b/test_hidden.py
--- a/test_hidden.py
+++ b/test_hidden.py
@@ -1 +1 @@
--- deleted source beginning with two dashes
+++ added source beginning with two pluses
"""

    assert changed_paths_from_patch(patch) == {"test_hidden.py"}


def test_changed_paths_decodes_git_quoted_octal_paths() -> None:
    patch = r"""diff --git "a/t\303\251st.py" "b/t\303\251st.py"
--- "a/t\303\251st.py"
+++ "b/t\303\251st.py"
@@ -1 +1 @@
-old
+new
"""

    assert changed_paths_from_patch(patch) == {"tést.py"}


def test_changed_paths_rejects_a_non_byte_octal_escape() -> None:
    patch = r"""diff --git "a/bad\777.py" "b/bad\777.py"
old mode 100644
new mode 100755
"""

    with pytest.raises(PatchFormatError, match="not one byte"):
        changed_paths_from_patch(patch)


@pytest.mark.parametrize(
    "metadata",
    [
        "rename from ../outside.py\nrename to inside.py",
        "copy from inside.py\ncopy to ../outside.py",
        "rename from old.py",
    ],
)
def test_changed_paths_rejects_unsafe_or_unpaired_move_metadata(metadata: str) -> None:
    patch = f"diff --git a/old.py b/inside.py\nsimilarity index 100%\n{metadata}\n"

    with pytest.raises(PatchFormatError):
        changed_paths_from_patch(patch)


def test_fixture_patch_contract_protects_only_declared_evaluator_paths(
    valid_bundle_path: Path,
) -> None:
    bundle = load_bundle(valid_bundle_path)
    contract = validate_patch_contract(
        bundle=bundle,
        gold_patch=bundle.gold_patch_path.read_text(encoding="utf-8"),
        test_patch=bundle.test_patch_path.read_text(encoding="utf-8"),
        solver_view_patch=bundle.solver_view_patch_path.read_text(encoding="utf-8"),
    )

    assert contract["protected_paths"] == ["test_bucket.py", "test_hidden.py"]
    assert contract["test_paths"] == ["test_hidden.py"]
    assert contract["solver_view_paths"] == ["test_bucket.py"]
    assert changed_paths_from_patch(bundle.gold_patch_path.read_text(encoding="utf-8")) == {
        "calculator.py"
    }


def test_patch_contract_rejects_hidden_implementation_changes(valid_bundle_path: Path) -> None:
    bundle = load_bundle(valid_bundle_path)
    implementation_patch = bundle.gold_patch_path.read_text(encoding="utf-8")

    with pytest.raises(InvalidTaskError, match="trust boundary") as caught:
        validate_patch_contract(
            bundle=bundle,
            gold_patch="",
            test_patch=implementation_patch,
            solver_view_patch="",
        )

    assert caught.value.details["unexpected_evaluator_paths"] == ["calculator.py"]
    assert caught.value.details["uncovered_protected_paths"] == [
        "test_bucket.py",
        "test_hidden.py",
    ]


def test_patch_contract_requires_the_gold_solution_to_fit_candidate_policy(
    valid_bundle_path: Path,
) -> None:
    payload = json.loads((valid_bundle_path / "task.json").read_text(encoding="utf-8"))
    payload["candidate"]["allowed_patch_paths"] = ["other.py"]
    (valid_bundle_path / "task.json").write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )
    bundle = load_bundle(valid_bundle_path)

    with pytest.raises(InvalidTaskError, match="trust boundary") as caught:
        validate_patch_contract(
            bundle=bundle,
            gold_patch=bundle.gold_patch_path.read_text(encoding="utf-8"),
            test_patch=bundle.test_patch_path.read_text(encoding="utf-8"),
            solver_view_patch=bundle.solver_view_patch_path.read_text(encoding="utf-8"),
        )

    assert caught.value.details["gold_outside_allowed_paths"] == ["calculator.py"]


def test_patch_contract_covers_declared_derived_evaluator_artifacts(
    valid_bundle_path: Path,
) -> None:
    payload = json.loads((valid_bundle_path / "task.json").read_text(encoding="utf-8"))
    payload["tests"]["additional_protected_paths"] = ["generated/evaluator-tests.bin"]
    (valid_bundle_path / "task.json").write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )
    bundle = load_bundle(valid_bundle_path)
    derived_patch = """diff --git a/generated/evaluator-tests.bin b/generated/evaluator-tests.bin
new file mode 100644
--- /dev/null
+++ b/generated/evaluator-tests.bin
@@ -0,0 +1 @@
+trusted derived evaluator data
"""

    contract = validate_patch_contract(
        bundle=bundle,
        gold_patch=bundle.gold_patch_path.read_text(encoding="utf-8"),
        test_patch=bundle.test_patch_path.read_text(encoding="utf-8") + derived_patch,
        solver_view_patch=bundle.solver_view_patch_path.read_text(encoding="utf-8"),
    )

    assert "generated/evaluator-tests.bin" in contract["protected_paths"]
