"""First-class OpenRouter coding agent over a sanitized solver container."""

from __future__ import annotations

import json
import time
from pathlib import PurePosixPath
from typing import Any

from taskbundle.agent_config import AgentSettings
from taskbundle.openrouter import AgentClient, OpenRouterClient
from taskbundle.process import ProcessResult
from taskbundle.solvers.base import SolverContext, SolverOutcome

MAX_TOOL_OUTPUT_CHARS = 20_000
MAX_WRITE_CHARS = 1_000_000

AGENT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List tracked repository files under an optional relative path.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "default": "."}},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 repository file using a relative path.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Replace an allowed candidate file with UTF-8 content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Run one command directly in the repository without a shell. Use an argv array, "
                'for example ["python", "-m", "pytest", "-q"].'
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "argv": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 64,
                    }
                },
                "required": ["argv"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Finish after implementing and checking the solution.",
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
                "additionalProperties": False,
            },
        },
    },
]


class AgentSolver:
    name = "agent"

    def __init__(
        self,
        *,
        settings: AgentSettings,
        description: str,
        allowed_paths: list[str],
        disallowed_paths: list[str] | None = None,
        client: AgentClient | None = None,
    ) -> None:
        self.settings = settings
        self.description = description
        self.allowed_paths = [PurePosixPath(path) for path in allowed_paths]
        self.disallowed_paths = [PurePosixPath(path) for path in (disallowed_paths or [])]
        self.client = client or OpenRouterClient(
            api_key=settings.api_key,
            model=settings.model,
            endpoint=settings.endpoint,
        )

    @property
    def provenance(self) -> dict[str, Any]:
        return {
            "provider": self.settings.provider,
            "requested_model": self.settings.model,
            "api_key_env": self.settings.api_key_env,
            "max_steps": self.settings.max_steps,
        }

    def solve(self, context: SolverContext) -> SolverOutcome:
        started = time.monotonic()
        deadline = started + context.timeout_seconds
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "You are a coding agent working in a sanitized repository. Diagnose and fix "
                    "the stated task. Use the provided tools to inspect files, edit only allowed "
                    "candidate paths, and run relevant public checks. Hidden evaluator tests and "
                    "the reference patch are unavailable. Do not merely describe a patch: edit "
                    "the repository, verify the result, then call finish."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Problem:\n{self.description}\n\n"
                    "Allowed candidate paths:\n"
                    + "\n".join(f"- {path.as_posix()}" for path in self.allowed_paths)
                    + "\n\nDisallowed candidate paths:\n"
                    + (
                        "\n".join(f"- {path.as_posix()}" for path in self.disallowed_paths)
                        or "- (none)"
                    )
                ),
            },
        ]
        log: list[str] = []
        actual_models: list[str] = []
        total_usage: dict[str, int] = {}
        final_summary = ""

        for step in range(1, self.settings.max_steps + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._outcome(
                    started=started,
                    log=log,
                    summary=final_summary,
                    actual_models=actual_models,
                    usage=total_usage,
                    steps=step - 1,
                    timed_out=True,
                    exit_code=None,
                    stderr="Agent exceeded runtime.solver_timeout_seconds.\n",
                )
            turn = self.client.complete(
                messages=messages,
                tools=AGENT_TOOLS,
                timeout_seconds=remaining,
            )
            actual_models.append(turn.model)
            for key, value in turn.usage.items():
                if isinstance(value, int):
                    total_usage[key] = total_usage.get(key, 0) + value

            assistant = self._assistant_message(turn.message)
            messages.append(assistant)
            tool_calls = assistant.get("tool_calls")
            if not isinstance(tool_calls, list) or not tool_calls:
                final_summary = self._text_content(assistant.get("content"))
                log.append(f"step {step}: model returned a final response")
                return self._outcome(
                    started=started,
                    log=log,
                    summary=final_summary,
                    actual_models=actual_models,
                    usage=total_usage,
                    steps=step,
                    timed_out=False,
                    exit_code=0,
                    stderr="",
                )

            for raw_call in tool_calls:
                try:
                    call_id, name, arguments = self._tool_call(raw_call)
                except ValueError as error:
                    log.append(f"step {step}: invalid tool call")
                    return self._outcome(
                        started=started,
                        log=log,
                        summary=final_summary,
                        actual_models=actual_models,
                        usage=total_usage,
                        steps=step,
                        timed_out=False,
                        exit_code=1,
                        stderr=f"Model returned an invalid tool call: {error}\n",
                    )
                if name == "finish":
                    summary = arguments.get("summary")
                    final_summary = summary if isinstance(summary, str) else "Agent finished."
                    log.append(f"step {step}: finish")
                    return self._outcome(
                        started=started,
                        log=log,
                        summary=final_summary,
                        actual_models=actual_models,
                        usage=total_usage,
                        steps=step,
                        timed_out=False,
                        exit_code=0,
                        stderr="",
                    )
                result = self._execute_tool(
                    context=context,
                    name=name,
                    arguments=arguments,
                    deadline=deadline,
                )
                log.append(f"step {step}: {name} -> {result['status']}")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": name,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )

        return self._outcome(
            started=started,
            log=log,
            summary=final_summary,
            actual_models=actual_models,
            usage=total_usage,
            steps=self.settings.max_steps,
            timed_out=False,
            exit_code=1,
            stderr=(f"Agent reached the {self.settings.max_steps}-step limit before finishing.\n"),
        )

    def _execute_tool(
        self,
        *,
        context: SolverContext,
        name: str,
        arguments: dict[str, Any],
        deadline: float,
    ) -> dict[str, Any]:
        try:
            if name == "list_files":
                path = self._safe_path(arguments.get("path", "."), allow_dot=True)
                command = ["git", "ls-files"]
                if path != ".":
                    command.extend(["--", path])
                return self._run(context, command, deadline)
            if name == "read_file":
                path = self._safe_path(arguments.get("path"))
                return self._run(context, ["/bin/cat", "--", path], deadline)
            if name == "write_file":
                path = self._safe_path(arguments.get("path"))
                content = arguments.get("content")
                if not isinstance(content, str):
                    raise ValueError("content must be a string")
                if len(content) > MAX_WRITE_CHARS:
                    raise ValueError(f"content exceeds {MAX_WRITE_CHARS} characters")
                if not self._allows(path):
                    if self._is_disallowed(path):
                        raise ValueError(
                            f"path is explicitly disallowed by candidate policy: {path}"
                        )
                    raise ValueError(f"path is outside the allowed candidate policy: {path}")
                destination = str(PurePosixPath(context.workdir) / path)
                context.docker.stream_text(
                    content=content,
                    container_id=context.container_id,
                    destination=destination,
                )
                return {"status": "ok", "path": path, "characters": len(content)}
            if name == "run_command":
                raw_argv = arguments.get("argv")
                if (
                    not isinstance(raw_argv, list)
                    or not raw_argv
                    or len(raw_argv) > 64
                    or not all(isinstance(value, str) and value for value in raw_argv)
                ):
                    raise ValueError("argv must contain 1 to 64 non-empty strings")
                return self._run(context, list(raw_argv), deadline)
            raise ValueError(f"unknown tool: {name}")
        except ValueError as error:
            return {"status": "error", "error": str(error)}

    def _run(
        self,
        context: SolverContext,
        command: list[str],
        deadline: float,
    ) -> dict[str, Any]:
        remaining = max(1, int(deadline - time.monotonic()))
        result = context.docker.exec_command(
            container_id=context.container_id,
            workdir=context.workdir,
            command=command,
            timeout_seconds=min(remaining, 120),
            trusted_path=context.trusted_path,
        )
        return {
            "status": "ok" if result.succeeded else "error",
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "stdout": self._bounded(result.stdout),
            "stderr": self._bounded(result.stderr),
        }

    def _allows(self, value: str) -> bool:
        path = PurePosixPath(value)
        within_allowed = any(path == root or root in path.parents for root in self.allowed_paths)
        return within_allowed and not self._is_disallowed(value)

    def _is_disallowed(self, value: str) -> bool:
        path = PurePosixPath(value)
        return any(path == root or root in path.parents for root in self.disallowed_paths)

    @staticmethod
    def _safe_path(value: Any, *, allow_dot: bool = False) -> str:
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise ValueError("path must be a non-empty string")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("path must stay inside the repository")
        normalized = path.as_posix()
        if normalized == "." and not allow_dot:
            raise ValueError("path must identify a file")
        return normalized

    @staticmethod
    def _tool_call(raw_call: Any) -> tuple[str, str, dict[str, Any]]:
        if not isinstance(raw_call, dict):
            raise ValueError("tool call must be an object")
        call_id = raw_call.get("id")
        function = raw_call.get("function")
        if not isinstance(call_id, str) or not isinstance(function, dict):
            raise ValueError("tool call is missing id or function")
        name = function.get("name")
        raw_arguments = function.get("arguments", "{}")
        if not isinstance(name, str) or not isinstance(raw_arguments, str):
            raise ValueError("tool call has invalid name or arguments")
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as error:
            raise ValueError(f"tool arguments are invalid JSON: {error}") from error
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must decode to an object")
        return call_id, name, arguments

    @staticmethod
    def _assistant_message(message: dict[str, Any]) -> dict[str, Any]:
        selected: dict[str, Any] = {"role": "assistant"}
        if "content" in message:
            selected["content"] = message["content"]
        if "tool_calls" in message:
            selected["tool_calls"] = message["tool_calls"]
        return selected

    @staticmethod
    def _text_content(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts = [item.get("text") for item in value if isinstance(item, dict)]
            return "\n".join(part for part in parts if isinstance(part, str))
        return ""

    @staticmethod
    def _bounded(value: str) -> str:
        if len(value) <= MAX_TOOL_OUTPUT_CHARS:
            return value
        half = MAX_TOOL_OUTPUT_CHARS // 2
        return value[:half] + "\n... output truncated ...\n" + value[-half:]

    def _outcome(
        self,
        *,
        started: float,
        log: list[str],
        summary: str,
        actual_models: list[str],
        usage: dict[str, int],
        steps: int,
        timed_out: bool,
        exit_code: int | None,
        stderr: str,
    ) -> SolverOutcome:
        model_list = list(dict.fromkeys(actual_models))
        stdout = "\n".join([*log, "", summary]).rstrip() + "\n"
        return SolverOutcome(
            adapter=self.name,
            process=ProcessResult(
                argv=("agent", "openrouter", self.settings.model),
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                duration_seconds=time.monotonic() - started,
                timed_out=timed_out,
            ),
            details={
                "provider": self.settings.provider,
                "requested_model": self.settings.model,
                "actual_models": model_list,
                "steps": steps,
                "usage": usage,
            },
        )
