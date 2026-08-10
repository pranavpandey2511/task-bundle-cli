from __future__ import annotations

from pathlib import Path
from typing import Any

from taskbundle.agent_config import AgentSettings
from taskbundle.openrouter import OpenRouterTurn
from taskbundle.process import ProcessResult
from taskbundle.solvers import AgentSolver, SolverContext


def process_result(
    argv: list[str], *, exit_code: int = 0, stdout: str = "", stderr: str = ""
) -> ProcessResult:
    return ProcessResult(
        argv=tuple(argv),
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.01,
        timed_out=False,
    )


class FakeAgentClient:
    def __init__(self, turns: list[OpenRouterTurn]) -> None:
        self.turns = turns
        self.requests: list[list[dict[str, Any]]] = []

    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        timeout_seconds: float,
    ) -> OpenRouterTurn:
        assert tools
        assert timeout_seconds > 0
        self.requests.append(list(messages))
        return self.turns.pop(0)


class FakeDocker:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.writes: list[tuple[str, str]] = []

    def exec_command(
        self,
        *,
        container_id: str,
        workdir: str,
        command: list[str],
        timeout_seconds: int,
        trusted_path: str | None = None,
    ) -> ProcessResult:
        del container_id, workdir, timeout_seconds
        assert trusted_path == "/usr/bin:/bin"
        self.commands.append(command)
        if command[:2] == ["/bin/cat", "--"]:
            return process_result(
                command, stdout="def subtract(left, right):\n    return left + right\n"
            )
        return process_result(command, stdout="1 passed\n")

    def stream_text(self, *, content: str, container_id: str, destination: str) -> None:
        del container_id
        self.writes.append((destination, content))


def tool_turn(call_id: str, name: str, arguments: dict[str, Any]) -> OpenRouterTurn:
    return OpenRouterTurn(
        message={
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": __import__("json").dumps(arguments)},
                }
            ],
        },
        model="openai/test-model",
        usage={"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
    )


def test_agent_reads_edits_checks_and_finishes_without_container_network() -> None:
    client = FakeAgentClient(
        [
            tool_turn("read", "read_file", {"path": "calculator.py"}),
            tool_turn(
                "write",
                "write_file",
                {
                    "path": "calculator.py",
                    "content": "def subtract(left, right):\n    return left - right\n",
                },
            ),
            tool_turn("test", "run_command", {"argv": ["python", "-m", "pytest", "-q"]}),
            tool_turn("finish", "finish", {"summary": "Fixed subtraction and tests pass."}),
        ]
    )
    docker = FakeDocker()
    settings = AgentSettings(
        provider="openrouter",
        model="openrouter/auto",
        api_key_env="OPENROUTER_API_KEY",
        api_key="secret-key",
        endpoint="https://openrouter.ai/api/v1/chat/completions",
        max_steps=8,
        env_file=Path(".env"),
    )
    solver = AgentSolver(
        settings=settings,
        description="Fix subtraction.",
        allowed_paths=["calculator.py"],
        client=client,
    )

    outcome = solver.solve(
        SolverContext(
            docker=docker,  # type: ignore[arg-type]
            container_id="solver",
            workdir="/workspace",
            timeout_seconds=60,
            trusted_path="/usr/bin:/bin",
        )
    )

    assert outcome.process.succeeded
    assert outcome.details["requested_model"] == "openrouter/auto"
    assert outcome.details["actual_models"] == ["openai/test-model"]
    assert outcome.details["steps"] == 4
    assert docker.writes == [
        (
            "/workspace/calculator.py",
            "def subtract(left, right):\n    return left - right\n",
        )
    ]
    assert docker.commands[-1] == ["python", "-m", "pytest", "-q"]
    assert all("secret-key" not in str(request) for request in client.requests)


def test_agent_rejects_write_outside_candidate_policy() -> None:
    client = FakeAgentClient(
        [
            tool_turn("write", "write_file", {"path": "hidden_test.py", "content": "bad\n"}),
            tool_turn("finish", "finish", {"summary": "Could not edit protected path."}),
        ]
    )
    docker = FakeDocker()
    settings = AgentSettings(
        provider="openrouter",
        model="openrouter/auto",
        api_key_env="OPENROUTER_API_KEY",
        api_key="secret-key",
        endpoint="https://openrouter.ai/api/v1/chat/completions",
        max_steps=4,
        env_file=Path(".env"),
    )

    AgentSolver(
        settings=settings,
        description="Fix code.",
        allowed_paths=["calculator.py"],
        client=client,
    ).solve(
        SolverContext(
            docker=docker,  # type: ignore[arg-type]
            container_id="solver",
            workdir="/workspace",
            timeout_seconds=60,
            trusted_path="/usr/bin:/bin",
        )
    )

    assert docker.writes == []
    tool_result = client.requests[1][-1]
    assert "outside the allowed candidate policy" in str(tool_result)


def test_agent_rejects_write_in_explicitly_disallowed_subtree() -> None:
    client = FakeAgentClient(
        [
            tool_turn("write", "write_file", {"path": "src/vendor/code.py", "content": "bad\n"}),
            tool_turn("finish", "finish", {"summary": "Could not edit excluded path."}),
        ]
    )
    docker = FakeDocker()
    settings = AgentSettings(
        provider="openrouter",
        model="openrouter/auto",
        api_key_env="OPENROUTER_API_KEY",
        api_key="secret-key",
        endpoint="https://openrouter.ai/api/v1/chat/completions",
        max_steps=4,
        env_file=Path(".env"),
    )

    AgentSolver(
        settings=settings,
        description="Fix code.",
        allowed_paths=["src"],
        disallowed_paths=["src/vendor"],
        client=client,
    ).solve(
        SolverContext(
            docker=docker,  # type: ignore[arg-type]
            container_id="solver",
            workdir="/workspace",
            timeout_seconds=60,
            trusted_path="/usr/bin:/bin",
        )
    )

    assert docker.writes == []
    assert "Disallowed candidate paths" in str(client.requests[0])
    assert "src/vendor" in str(client.requests[0])
    assert "explicitly disallowed" in str(client.requests[1][-1])
