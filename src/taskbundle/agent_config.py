"""OpenRouter agent settings with explicit, secret-safe precedence."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from taskbundle.errors import ConfigurationError

DEFAULT_OPENROUTER_MODEL = "openrouter/auto"
DEFAULT_OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_AGENT_MAX_STEPS = 24
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class AgentSettings:
    provider: str
    model: str
    api_key_env: str
    api_key: str = field(repr=False)
    endpoint: str
    max_steps: int
    env_file: Path


def read_env_file(path: Path) -> dict[str, str]:
    """Read the small dotenv subset needed for local credentials and defaults."""

    try:
        source = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as error:
        raise ConfigurationError(
            f"Could not read agent environment file: {path}",
            details={"reason": str(error)},
        ) from error

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(source.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        name, separator, raw_value = line.partition("=")
        name = name.strip()
        if not separator or not _ENV_NAME.fullmatch(name):
            raise ConfigurationError(
                f"Invalid assignment in {path} at line {line_number}.",
                hint="Use NAME=value entries; comments must start with #.",
            )
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[name] = value
    return values


def resolve_agent_settings(
    *,
    model: str | None,
    api_key_env: str,
    env_file: Path,
    max_steps: int,
    environment: Mapping[str, str] | None = None,
) -> AgentSettings:
    if not _ENV_NAME.fullmatch(api_key_env):
        raise ConfigurationError(
            f"Invalid OpenRouter API-key environment name: {api_key_env}",
            hint="Use a name such as OPENROUTER_API_KEY.",
        )
    if not 1 <= max_steps <= 100:
        raise ConfigurationError("Agent maximum steps must be between 1 and 100.")

    resolved_env_file = env_file.expanduser().resolve()
    file_values = read_env_file(resolved_env_file)
    process_values = os.environ if environment is None else environment

    def selected(name: str) -> str | None:
        process_value = process_values.get(name)
        if process_value is not None:
            return process_value.strip()
        file_value = file_values.get(name)
        return file_value.strip() if file_value is not None else None

    api_key = selected(api_key_env)
    if not api_key:
        raise ConfigurationError(
            f"OpenRouter API key was not found in {api_key_env}.",
            hint=(
                f"Add `{api_key_env}=...` to {resolved_env_file} or export it in the shell, "
                "then retry."
            ),
            details={
                "api_key_env": api_key_env,
                "env_file": str(resolved_env_file),
            },
        )

    selected_model = (model or "").strip() or selected("OPENROUTER_MODEL")
    selected_model = selected_model or DEFAULT_OPENROUTER_MODEL
    endpoint = selected("OPENROUTER_BASE_URL") or DEFAULT_OPENROUTER_BASE_URL
    return AgentSettings(
        provider="openrouter",
        model=selected_model,
        api_key_env=api_key_env,
        api_key=api_key,
        endpoint=endpoint,
        max_steps=max_steps,
        env_file=resolved_env_file,
    )
