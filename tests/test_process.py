from __future__ import annotations

import sys

from taskbundle.process import ProcessRunner
from taskbundle.session import sanitize_arguments


def test_process_runner_captures_output_and_exit_code() -> None:
    result = ProcessRunner().run(
        [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
        timeout_seconds=5,
    )

    assert result.succeeded
    assert result.stdout == "out\n"
    assert result.stderr == "err\n"


def test_process_runner_reports_timeout() -> None:
    result = ProcessRunner().run(
        [sys.executable, "-c", "import time; time.sleep(2)"], timeout_seconds=0.01
    )

    assert result.timed_out
    assert result.exit_code is None


def test_sensitive_cli_arguments_are_redacted() -> None:
    result = sanitize_arguments(
        ["run", "--token", "secret-value", "--api-key=other-secret", "--solver", "stub"]
    )

    assert result == [
        "run",
        "--token",
        "<redacted>",
        "--api-key=<redacted>",
        "--solver",
        "stub",
    ]
