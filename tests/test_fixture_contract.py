from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def run(*arguments: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(arguments, cwd=cwd, text=True, capture_output=True, check=False)


def clone(source: Path, destination: Path) -> Path:
    result = run("git", "clone", "--quiet", str(source), str(destination), cwd=source.parent)
    assert result.returncode == 0, result.stderr
    return destination


def test_fixture_expresses_baseline_and_golden_truth_tables(
    tmp_path: Path,
    fixture_assets: Path,
    minimal_repository: tuple[Path, str],
) -> None:
    repository, _commit = minimal_repository

    baseline = clone(repository, tmp_path / "baseline")
    assert run("git", "apply", str(fixture_assets / "hidden.patch"), cwd=baseline).returncode == 0
    assert (
        run(
            "python3",
            "-m",
            "unittest",
            "-q",
            "test_hidden.HiddenTests.test_add_remains_available",
            cwd=baseline,
        ).returncode
        == 0
    )
    assert (
        run(
            "python3",
            "-m",
            "unittest",
            "-q",
            "test_hidden.HiddenTests.test_subtracts",
            cwd=baseline,
        ).returncode
        != 0
    )

    golden = clone(repository, tmp_path / "golden")
    assert run("git", "apply", str(fixture_assets / "gold.patch"), cwd=golden).returncode == 0
    assert run("git", "apply", str(fixture_assets / "hidden.patch"), cwd=golden).returncode == 0
    result = run("python3", "-m", "unittest", "-q", "test_hidden", cwd=golden)
    assert result.returncode == 0, result.stderr

    assert "theta-hidden-evaluator-only" not in "".join(
        path.read_text(encoding="utf-8")
        for path in repository.iterdir()
        if path.is_file() and path.suffix == ".py"
    )

    shutil.rmtree(baseline)
    shutil.rmtree(golden)
