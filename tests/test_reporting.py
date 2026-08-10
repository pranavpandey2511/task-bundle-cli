from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from taskbundle.artifacts import verify_artifact_records
from taskbundle.errors import UnresolvedError
from taskbundle.models import TestObservation as Observation
from taskbundle.models import TestResult as RecordedTestResult
from taskbundle.session import CommandSession


class ReportStructureParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.hrefs: list[str] = []

    def handle_starttag(self, _tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        element_id = attributes.get("id")
        href = attributes.get("href")
        if element_id is not None:
            self.ids.append(element_id)
        if href is not None:
            self.hrefs.append(href)


def _assert_valid_local_navigation(path: Path) -> None:
    parser = ReportStructureParser()
    parser.feed(path.read_text(encoding="utf-8"))
    assert len(parser.ids) == len(set(parser.ids))
    for href in parser.hrefs:
        if href.startswith(("#", "http://", "https://", "mailto:")):
            continue
        local_path = unquote(urlsplit(href).path)
        assert (path.parent / local_path).resolve().is_file(), href


def _record_attempt(
    session: CommandSession,
    *,
    observed: Observation,
    exit_code: int,
) -> None:
    log_path = session.artifacts.write_text(
        command_id=session.command_id,
        relative_path="tests/subtracts-1.log",
        content=(
            f"observed={observed.value}\nexit_code={exit_code}\n"
            "AssertionError: expected subtract(10, 7) to equal 3, but received 17\n"
            '<script>alert("log")</script>\n'
        ),
        kind="test_log",
    )
    session.database.add_test_result(
        command_id=session.command_id,
        result=RecordedTestResult(
            phase="post_solver",
            suite="fail_to_pass",
            test_id="subtracts",
            attempt=1,
            expected=Observation.PASS,
            observed=observed,
            exit_code=exit_code,
            duration_seconds=0.12,
            log_artifact=log_path.relative_to(session.state_dir).as_posix(),
        ),
    )
    session.event(
        "info" if observed == Observation.PASS else "error",
        "post_solver",
        "Recorded the post-solver target outcome.",
    )


def _solver_payload(*, resolved: bool, patch_artifact: str | None = None) -> dict[str, object]:
    solver: dict[str, object] = {
        "adapter": "command",
        "patch_bytes": 128,
        "patch_paths": ["calculator.py"],
        "exit_code": 0,
        "duration_seconds": 1.25,
    }
    if patch_artifact is not None:
        solver["patch_artifact"] = patch_artifact
    return {
        "resolved": resolved,
        "solver": solver,
        "provenance": {
            "execution_fingerprint": "f" * 64,
            "repository": {"commit": "a" * 40},
            "image": {"id": "sha256:" + "b" * 64},
            "solver_image": {"id": "sha256:" + "c" * 64},
            "repetitions": 1,
            "runtime": {
                "cpus": 1,
                "memory": "512m",
                "pids": 64,
                "solver_network": False,
            },
        },
    }


def test_run_publishes_safe_evidence_linked_html_for_failure(
    valid_bundle_path: Path,
) -> None:
    (valid_bundle_path / "description.md").write_text(
        'Fix subtract(). <script>alert("review")</script>\n',
        encoding="utf-8",
    )
    session = CommandSession.start(command_name="run", bundle_path=valid_bundle_path)
    session.attach_bundle("minimal-python")
    _record_attempt(session, observed=Observation.FAIL, exit_code=1)
    patch_path = session.artifacts.write_text(
        command_id=session.command_id,
        relative_path="solver.patch",
        content=(
            "diff --git a/calculator.py b/calculator.py\n"
            "--- a/calculator.py\n+++ b/calculator.py\n"
            "@@ -1 +1 @@\n-return left + right\n+return left - right\n"
        ),
        kind="solver_patch",
    )
    patch_artifact = patch_path.relative_to(session.state_dir).as_posix()
    try:
        report = session.fail(
            UnresolvedError(
                "Candidate did not satisfy every post-solver expectation.",
                hint="Review the failing target log and candidate patch.",
                details=_solver_payload(resolved=False, patch_artifact=patch_artifact),
            )
        )
        assert report.html_report is not None
        html_path = session.state_dir / report.html_report
        html = html_path.read_text(encoding="utf-8")
        artifacts = session.database.get_artifacts(session.command_id)
        verification = verify_artifact_records(state_dir=session.state_dir, records=artifacts)
    finally:
        session.close()

    assert html_path.is_file()
    assert (valid_bundle_path / ".taskbundle" / "reports" / "index.html").is_file()
    assert (valid_bundle_path / ".taskbundle" / "reports" / "latest.html").is_file()
    assert "Needs revision" in html
    assert "Candidate did not satisfy every post-solver expectation." in html
    assert "expected pass, observed fail" in html
    assert "Why tests failed" in html
    assert "AssertionError: expected subtract(10, 7) to equal 3" in html
    assert "Candidate diff preview" in html
    assert "+return left - right" in html
    assert "Reproducibility context" in html
    assert "f" * 64 in html
    assert 'href="tests/subtracts-1.log"' in html
    assert "&lt;script&gt;alert" in html
    assert '<script>alert("review")</script>' not in html
    assert {artifact["kind"] for artifact in artifacts} >= {
        "command_report",
        "html_report",
        "solver_patch",
        "test_log",
    }
    assert verification["valid"] is True
    _assert_valid_local_navigation(html_path)


def test_report_dashboard_versions_runs_and_latest_prefers_newest_run(
    valid_bundle_path: Path,
) -> None:
    first = CommandSession.start(command_name="validate", bundle_path=valid_bundle_path)
    first.attach_bundle("minimal-python")
    first_report = first.succeed({"valid": True, "attempt_count": 0})
    first.close()

    second = CommandSession.start(command_name="run", bundle_path=valid_bundle_path)
    second.attach_bundle("minimal-python")
    _record_attempt(second, observed=Observation.PASS, exit_code=0)
    second_report = second.succeed(_solver_payload(resolved=True))
    second.close()

    state_dir = valid_bundle_path / ".taskbundle"
    index = (state_dir / "reports" / "index.html").read_text(encoding="utf-8")
    latest = (state_dir / "reports" / "latest.html").read_text(encoding="utf-8")

    assert first_report.html_report == f"commands/{first_report.command_id}/report.html"
    assert second_report.html_report == f"commands/{second_report.command_id}/report.html"
    assert (state_dir / str(first_report.html_report)).is_file()
    assert (state_dir / str(second_report.html_report)).is_file()
    assert "v001" in index
    assert "v002" in index
    assert first_report.command_id in index
    assert second_report.command_id in index
    assert "2 review version(s)" in index
    assert second_report.command_id in latest
    _assert_valid_local_navigation(state_dir / "reports" / "index.html")


def test_non_lifecycle_command_keeps_the_ledger_without_html(
    valid_bundle_path: Path,
) -> None:
    session = CommandSession.start(command_name="history", bundle_path=valid_bundle_path)
    try:
        report = session.succeed({"commands": [], "count": 0})
    finally:
        session.close()

    assert report.html_report is None
    assert report.report_index is None
    assert not (valid_bundle_path / ".taskbundle" / "reports").exists()


def test_html_uses_the_verified_description_snapshot(valid_bundle_path: Path) -> None:
    session = CommandSession.start(command_name="validate", bundle_path=valid_bundle_path)
    session.attach_bundle("minimal-python")
    session.artifacts.write_text(
        command_id=session.command_id,
        relative_path="trusted-inputs/description.md",
        content="Original snapshotted problem.\n",
        kind="trusted_input",
    )
    (valid_bundle_path / "description.md").write_text(
        "Changed after lifecycle input capture.\n",
        encoding="utf-8",
    )
    try:
        report = session.succeed({"valid": True, "attempt_count": 0})
        html = (session.state_dir / str(report.html_report)).read_text(encoding="utf-8")
    finally:
        session.close()

    assert "Original snapshotted problem." in html
    assert "Changed after lifecycle input capture." not in html
