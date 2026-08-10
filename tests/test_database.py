from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from taskbundle.artifacts import ArtifactStore
from taskbundle.database import Database
from taskbundle.models import CommandStatus
from taskbundle.models import TestObservation as Observation
from taskbundle.models import TestResult as BundleTestResult


def test_command_ledger_round_trip(tmp_path: Path) -> None:
    database_path = tmp_path / "state" / "taskbundle.db"
    now = datetime.now(UTC).isoformat()

    with Database(database_path) as database:
        database.create_command(
            command_id="command-1",
            command_name="validate",
            bundle_id="fixture",
            arguments=["validate", "."],
            started_at=now,
        )
        database.add_event(
            command_id="command-1",
            occurred_at=now,
            level="info",
            phase="baseline",
            message="Started baseline.",
            data={"attempt": 1},
        )
        database.add_test_result(
            command_id="command-1",
            result=BundleTestResult(
                phase="baseline",
                suite="pass_to_pass",
                test_id="existing",
                attempt=1,
                expected=Observation.PASS,
                observed=Observation.PASS,
                exit_code=0,
                duration_seconds=0.25,
            ),
        )
        store = ArtifactStore(tmp_path / "state", database)
        artifact = store.write_text(
            command_id="command-1",
            relative_path="logs/output.log",
            content="hello\n",
            kind="test_log",
        )
        database.finish_command(
            command_id="command-1",
            status=CommandStatus.SUCCEEDED,
            ended_at=now,
            exit_code=0,
        )

        command = database.get_command("command-1")
        assert command is not None
        assert command["arguments"] == ["validate", "."]
        assert command["status"] == "succeeded"
        assert database.get_events("command-1")[0]["data"] == {"attempt": 1}
        assert database.get_test_results("command-1")[0]["observed"] == "pass"
        artifacts = database.get_artifacts("command-1")
        assert artifacts[0]["sha256"]
        assert artifact.read_text(encoding="utf-8") == "hello\n"

    with Database(database_path) as reopened:
        assert reopened.get_command("command-1") is not None


def test_latest_command_can_skip_query_commands(tmp_path: Path) -> None:
    database_path = tmp_path / "state" / "taskbundle.db"
    now = datetime.now(UTC).isoformat()

    with Database(database_path) as database:
        for command_id, command_name in (
            ("command-1", "run"),
            ("command-2", "diagnose"),
            ("command-3", "artifacts"),
        ):
            database.create_command(
                command_id=command_id,
                command_name=command_name,
                bundle_id="fixture",
                arguments=[command_name],
                started_at=now,
            )

        latest = database.get_latest_command(
            exclude_id="command-4",
            exclude_names=("artifacts", "diagnose", "export", "history", "logs"),
        )

        assert latest is not None
        assert latest["id"] == "command-1"
