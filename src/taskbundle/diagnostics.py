"""Evidence-backed diagnosis for recorded task commands."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from taskbundle.artifacts import verify_artifact_records


def _artifact_path(state_dir: Path, record: Mapping[str, Any]) -> Path:
    return (state_dir.resolve() / str(record["relative_path"])).resolve()


def _load_json_artifact(
    *,
    state_dir: Path,
    record: Mapping[str, Any],
    verified_paths: set[str],
) -> tuple[dict[str, Any] | None, str | None]:
    relative_path = str(record["relative_path"])
    if relative_path not in verified_paths:
        return None, "artifact did not pass integrity verification"
    try:
        payload = json.loads(_artifact_path(state_dir, record).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return None, str(error)
    if not isinstance(payload, dict):
        return None, "JSON root is not an object"
    return payload, None


def diagnose_command(
    *,
    state_dir: Path,
    command: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    test_results: Sequence[Mapping[str, Any]],
    artifact_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Summarize actionable failures without rerunning untrusted code."""

    verification = verify_artifact_records(state_dir=state_dir, records=artifact_records)
    verified_paths = {
        str(item["relative_path"]) for item in verification["artifacts"] if item["status"] == "ok"
    }
    findings: list[dict[str, Any]] = []
    next_actions: list[str] = []

    def add_finding(
        severity: str,
        category: str,
        message: str,
        *,
        artifacts: Sequence[str] = (),
    ) -> None:
        finding: dict[str, Any] = {
            "severity": severity,
            "category": category,
            "message": message,
        }
        if artifacts:
            finding["artifacts"] = list(artifacts)
        findings.append(finding)

    def add_action(message: str) -> None:
        if message not in next_actions:
            next_actions.append(message)

    if not verification["valid"]:
        failures = [
            f"{item['relative_path']} ({item['status']})"
            for item in verification["artifacts"]
            if item["status"] != "ok"
        ]
        add_finding(
            "error",
            "evidence_integrity",
            "Recorded evidence cannot be trusted: " + ", ".join(failures),
        )
        add_action("Restore or recover the artifacts, then run `task report` again.")

    report_payload: dict[str, Any] | None = None
    report_record = next(
        (record for record in artifact_records if record["kind"] == "command_report"),
        None,
    )
    if report_record is not None:
        report_payload, report_error = _load_json_artifact(
            state_dir=state_dir,
            record=report_record,
            verified_paths=verified_paths,
        )
        if report_error:
            add_finding(
                "error",
                "command_report",
                f"Could not read the command report: {report_error}",
                artifacts=[str(report_record["relative_path"])],
            )

    report_failure = report_payload.get("error") if report_payload else None
    if isinstance(report_failure, dict):
        message = str(report_failure.get("message") or command.get("error_message") or "failed")
        hint = report_failure.get("hint")
        if hint:
            message = f"{message} Hint: {hint}"
        add_finding("error", "command_failure", message)
    elif command.get("status") == "failed":
        add_finding(
            "error",
            "command_failure",
            str(command.get("error_message") or "The command failed without a readable report."),
        )

    grouped: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for result in test_results:
        grouped[(str(result["phase"]), str(result["suite"]), str(result["test_id"]))].append(result)

    mismatches = 0
    flaky_tests = 0
    timeout_attempts = 0
    error_attempts = 0
    for (phase, suite, test_id), attempts in sorted(grouped.items()):
        observations = {str(attempt["observed"]) for attempt in attempts}
        logs = [str(attempt["log_artifact"]) for attempt in attempts if attempt.get("log_artifact")]
        if len(observations) > 1:
            flaky_tests += 1
            add_finding(
                "error",
                "flaky_test",
                f"{phase}/{suite}/{test_id} produced inconsistent observations: "
                + ", ".join(sorted(observations)),
                artifacts=logs,
            )
            add_action("Inspect the inconsistent attempt logs and remove nondeterministic state.")

        unexpected = [attempt for attempt in attempts if attempt["observed"] != attempt["expected"]]
        mismatches += len(unexpected)
        timeout_attempts += sum(attempt["observed"] == "timeout" for attempt in attempts)
        error_attempts += sum(attempt["observed"] == "error" for attempt in attempts)
        if unexpected:
            expected = sorted({str(attempt["expected"]) for attempt in unexpected})
            observed = sorted({str(attempt["observed"]) for attempt in unexpected})
            add_finding(
                "error",
                f"{phase}_expectation",
                (
                    f"{phase}/{suite}/{test_id}: expected {', '.join(expected)}, observed "
                    f"{', '.join(observed)} in {len(unexpected)} attempt(s)."
                ),
                artifacts=logs,
            )
            if phase == "baseline" and suite == "fail_to_pass":
                add_action("Confirm the configured base commit still contains the target bug.")
            elif phase == "baseline":
                add_action("Repair the baseline environment or PASS_TO_PASS test selection.")
            elif phase == "post_solver":
                add_action("Review the candidate patch beside the failing post-solver test log.")

    if timeout_attempts:
        add_action("Check for hangs before increasing the affected test or solver timeout.")
    if error_attempts:
        add_action(
            "Treat error exits as runner or infrastructure failures, not assertion failures."
        )

    snapshots: list[dict[str, Any]] = []
    for record in artifact_records:
        if record["kind"] != "repository_snapshot":
            continue
        payload, load_error = _load_json_artifact(
            state_dir=state_dir,
            record=record,
            verified_paths=verified_paths,
        )
        if load_error:
            add_finding(
                "warning",
                "repository_snapshot",
                f"Could not read repository snapshot: {load_error}",
                artifacts=[str(record["relative_path"])],
            )
            continue
        assert payload is not None
        snapshots.append(
            {
                "phase": payload.get("phase"),
                "stage": payload.get("stage"),
                "head": payload.get("head"),
                "head_matches_base": payload.get("head_matches_base"),
                "dirty": payload.get("dirty"),
                "status": payload.get("status", []),
                "diff_stat": payload.get("diff_stat", []),
                "artifact": record["relative_path"],
            }
        )

    error_kind = command.get("error_kind")
    if error_kind == "infrastructure_error" or error_kind == "internal_error":
        add_action("Run `task doctor`, then retry after fixing the reported host dependency.")
    elif error_kind == "solver_error":
        add_action("Inspect solver stdout/stderr and the candidate patch-policy failure.")
    elif error_kind == "invalid_task":
        add_action(
            "Run `task validate --static` for a fast contract review before retrying Docker."
        )
    elif error_kind == "unresolved":
        add_action("Use the post-solver findings to revise or replace the candidate patch.")

    if command.get("status") == "succeeded" and not findings:
        add_finding(
            "info",
            "result",
            "The target command succeeded and its recorded artifacts pass integrity checks.",
        )
        add_action("Use `task report --export FILE.zip` if a portable evidence package is needed.")

    relevant_kinds = {
        "build_metadata",
        "candidate_input",
        "checkout_log",
        "command_report",
        "docker_build_log",
        "execution_provenance",
        "patch_log",
        "repository_snapshot",
        "repository_status",
        "run_report",
        "smoke_log",
        "solver_patch",
        "solver_stderr",
        "solver_stdout",
        "test_log",
        "trusted_input",
        "validation_report",
    }
    relevant_artifacts = [
        {
            "kind": record["kind"],
            "relative_path": record["relative_path"],
            "sha256": record["sha256"],
            "size_bytes": record["size_bytes"],
        }
        for record in artifact_records
        if record["kind"] in relevant_kinds
    ]
    return {
        "command": dict(command),
        "summary": {
            "status": command.get("status"),
            "exit_code": command.get("exit_code"),
            "error_kind": error_kind,
            "failing_attempts": mismatches,
            "flaky_tests": flaky_tests,
            "timeout_attempts": timeout_attempts,
            "error_attempts": error_attempts,
            "artifact_integrity": "verified" if verification["valid"] else "failed",
            "snapshot_count": len(snapshots),
        },
        "findings": findings,
        "next_actions": next_actions,
        "snapshots": snapshots,
        "relevant_artifacts": relevant_artifacts,
        "artifact_verification": verification,
        "report_error": report_failure,
        "last_error_event": next(
            (dict(event) for event in reversed(events) if event["level"] == "error"),
            None,
        ),
    }
