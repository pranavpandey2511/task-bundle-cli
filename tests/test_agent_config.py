from __future__ import annotations

from pathlib import Path

import pytest

from taskbundle.agent_config import DEFAULT_OPENROUTER_MODEL, resolve_agent_settings
from taskbundle.errors import ConfigurationError


def test_agent_settings_use_cli_then_environment_then_defaults(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENROUTER_API_KEY=file-key\nOPENROUTER_MODEL=file-model\n",
        encoding="utf-8",
    )

    file_settings = resolve_agent_settings(
        model=None,
        api_key_env="OPENROUTER_API_KEY",
        env_file=env_file,
        max_steps=24,
        environment={},
    )
    assert file_settings.api_key == "file-key"
    assert file_settings.model == "file-model"

    overridden = resolve_agent_settings(
        model="cli-model",
        api_key_env="OPENROUTER_API_KEY",
        env_file=env_file,
        max_steps=12,
        environment={
            "OPENROUTER_API_KEY": "shell-key",
            "OPENROUTER_MODEL": "shell-model",
        },
    )
    assert overridden.api_key == "shell-key"
    assert overridden.model == "cli-model"
    assert "shell-key" not in repr(overridden)


def test_agent_settings_use_builtin_model_when_no_model_is_configured(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENROUTER_API_KEY=test-key\n", encoding="utf-8")

    settings = resolve_agent_settings(
        model=None,
        api_key_env="OPENROUTER_API_KEY",
        env_file=env_file,
        max_steps=24,
        environment={},
    )

    assert settings.model == DEFAULT_OPENROUTER_MODEL


def test_missing_agent_key_has_actionable_error(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"

    with pytest.raises(ConfigurationError, match="OpenRouter API key was not found") as error:
        resolve_agent_settings(
            model=None,
            api_key_env="OPENROUTER_API_KEY",
            env_file=env_file,
            max_steps=24,
            environment={},
        )

    assert "OPENROUTER_API_KEY" in (error.value.hint or "")
    assert str(env_file) in (error.value.hint or "")
