"""Minimal OpenRouter chat-completions client for the built-in coding agent."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from taskbundle.errors import SolverError


@dataclass(frozen=True, slots=True)
class OpenRouterTurn:
    message: dict[str, Any]
    model: str
    usage: dict[str, Any]


class AgentClient(Protocol):
    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        timeout_seconds: float,
    ) -> OpenRouterTurn: ...


class OpenRouterClient:
    def __init__(self, *, api_key: str, model: str, endpoint: str) -> None:
        self._api_key = api_key
        self.model = model
        self.endpoint = endpoint

    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        timeout_seconds: float,
    ) -> OpenRouterTurn:
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "stream": False,
        }
        request = Request(
            self.endpoint,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "X-Title": "Task Bundle CLI",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=max(1.0, timeout_seconds)) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")[-4000:]
            error_message = self._error_message(body) or str(error.reason)
            hints = {
                401: "Check OPENROUTER_API_KEY in the selected environment file.",
                402: "Add OpenRouter credits or select a model available to this API key.",
                429: "Wait for the OpenRouter rate limit to reset, then retry.",
                503: "Retry later or select a different tool-capable model with --model.",
            }
            raise SolverError(
                f"OpenRouter rejected the agent request with HTTP {error.code}: {error_message}",
                hint=hints.get(error.code, "Check the model name and OpenRouter account settings."),
                details={"http_status": error.code, "provider": "openrouter"},
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise SolverError(
                "Could not reach OpenRouter for the agent request.",
                hint="Check host connectivity and OPENROUTER_BASE_URL, then retry.",
                details={"reason": str(error), "provider": "openrouter"},
            ) from error

        try:
            decoded = json.loads(raw)
            if not isinstance(decoded, dict):
                raise TypeError("response root is not an object")
            choices = decoded.get("choices")
            if not isinstance(choices, list) or not choices:
                raise TypeError("response has no choices")
            first = choices[0]
            if not isinstance(first, dict) or not isinstance(first.get("message"), dict):
                raise TypeError("response choice has no message")
            response_message = dict(first["message"])
            model = decoded.get("model")
            usage = decoded.get("usage")
            return OpenRouterTurn(
                message=response_message,
                model=model if isinstance(model, str) else self.model,
                usage=dict(usage) if isinstance(usage, dict) else {},
            )
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise SolverError(
                "OpenRouter returned an invalid agent response.",
                hint="Retry the request or select a different tool-capable model.",
                details={"reason": str(error), "response_excerpt": raw[-2000:]},
            ) from error

    @staticmethod
    def _error_message(body: str) -> str | None:
        try:
            decoded = json.loads(body)
        except json.JSONDecodeError:
            return None
        if not isinstance(decoded, dict):
            return None
        error = decoded.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return str(error["message"])
        return None
