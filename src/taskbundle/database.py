"""Small SQLite command ledger with explicit forward-only migrations."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from taskbundle.errors import InfrastructureError
from taskbundle.models import CommandStatus, TestResult

SCHEMA_VERSION = 1


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(path)
        except (OSError, sqlite3.Error) as error:
            raise InfrastructureError(
                f"Could not open command database at {path}: {error}"
            ) from error
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            with self._connection:
                yield self._connection
        except sqlite3.Error as error:
            raise InfrastructureError(f"Database operation failed: {error}") from error

    def _migrate(self) -> None:
        version_row = self._connection.execute("PRAGMA user_version").fetchone()
        version = int(version_row[0]) if version_row else 0
        if version > SCHEMA_VERSION:
            raise InfrastructureError(
                f"Database schema {version} is newer than this CLI supports ({SCHEMA_VERSION}).",
                hint="Upgrade Task Bundle CLI before using this state directory.",
            )
        if version == 0:
            with self.transaction() as connection:
                connection.executescript(
                    """
                    CREATE TABLE commands (
                        id TEXT PRIMARY KEY,
                        command_name TEXT NOT NULL,
                        bundle_id TEXT,
                        arguments_json TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        ended_at TEXT,
                        status TEXT NOT NULL,
                        exit_code INTEGER,
                        error_kind TEXT,
                        error_message TEXT
                    );

                    CREATE TABLE events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        command_id TEXT NOT NULL REFERENCES commands(id) ON DELETE CASCADE,
                        occurred_at TEXT NOT NULL,
                        level TEXT NOT NULL,
                        phase TEXT NOT NULL,
                        message TEXT NOT NULL,
                        data_json TEXT NOT NULL
                    );

                    CREATE INDEX events_command_order
                    ON events(command_id, id);

                    CREATE TABLE test_results (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        command_id TEXT NOT NULL REFERENCES commands(id) ON DELETE CASCADE,
                        phase TEXT NOT NULL,
                        suite TEXT NOT NULL,
                        test_id TEXT NOT NULL,
                        attempt INTEGER NOT NULL,
                        expected TEXT NOT NULL,
                        observed TEXT NOT NULL,
                        exit_code INTEGER,
                        duration_seconds REAL NOT NULL,
                        log_artifact TEXT
                    );

                    CREATE INDEX test_results_command
                    ON test_results(command_id, phase, suite, test_id, attempt);

                    CREATE TABLE artifacts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        command_id TEXT NOT NULL REFERENCES commands(id) ON DELETE CASCADE,
                        kind TEXT NOT NULL,
                        relative_path TEXT NOT NULL,
                        sha256 TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL,
                        UNIQUE(command_id, relative_path)
                    );

                    PRAGMA user_version = 1;
                    """
                )

    def create_command(
        self,
        *,
        command_id: str,
        command_name: str,
        bundle_id: str | None,
        arguments: Sequence[str],
        started_at: str,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO commands (
                    id, command_name, bundle_id, arguments_json, started_at, status
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    command_id,
                    command_name,
                    bundle_id,
                    json.dumps(list(arguments)),
                    started_at,
                    CommandStatus.RUNNING.value,
                ),
            )

    def finish_command(
        self,
        *,
        command_id: str,
        status: CommandStatus,
        ended_at: str,
        exit_code: int,
        error_kind: str | None = None,
        error_message: str | None = None,
    ) -> None:
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE commands
                SET ended_at = ?, status = ?, exit_code = ?, error_kind = ?, error_message = ?
                WHERE id = ?
                """,
                (ended_at, status.value, exit_code, error_kind, error_message, command_id),
            )
            if cursor.rowcount != 1:
                raise InfrastructureError(f"Command record disappeared: {command_id}")

    def set_command_bundle_id(self, *, command_id: str, bundle_id: str) -> None:
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE commands SET bundle_id = ? WHERE id = ?", (bundle_id, command_id)
            )
            if cursor.rowcount != 1:
                raise InfrastructureError(f"Command record disappeared: {command_id}")

    def add_event(
        self,
        *,
        command_id: str,
        occurred_at: str,
        level: str,
        phase: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO events (
                    command_id, occurred_at, level, phase, message, data_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (command_id, occurred_at, level, phase, message, json.dumps(data or {})),
            )

    def add_test_result(self, *, command_id: str, result: TestResult) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO test_results (
                    command_id, phase, suite, test_id, attempt, expected, observed,
                    exit_code, duration_seconds, log_artifact
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    command_id,
                    result.phase,
                    result.suite,
                    result.test_id,
                    result.attempt,
                    result.expected.value,
                    result.observed.value,
                    result.exit_code,
                    result.duration_seconds,
                    result.log_artifact,
                ),
            )

    def add_artifact(
        self,
        *,
        command_id: str,
        kind: str,
        relative_path: str,
        sha256: str,
        size_bytes: int,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO artifacts (command_id, kind, relative_path, sha256, size_bytes)
                VALUES (?, ?, ?, ?, ?)
                """,
                (command_id, kind, relative_path, sha256, size_bytes),
            )

    def get_command(self, command_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM commands WHERE id = ?", (command_id,)
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def list_commands(self, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT * FROM commands ORDER BY started_at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_events(self, command_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT * FROM events WHERE command_id = ? ORDER BY id", (command_id,)
        ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_test_results(self, command_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            """
            SELECT * FROM test_results
            WHERE command_id = ?
            ORDER BY phase, suite, test_id, attempt
            """,
            (command_id,),
        ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_artifacts(self, command_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT * FROM artifacts WHERE command_id = ? ORDER BY id", (command_id,)
        ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for field in ("arguments_json", "data_json"):
            if field in result:
                result[field.removesuffix("_json")] = json.loads(result.pop(field))
        return result
