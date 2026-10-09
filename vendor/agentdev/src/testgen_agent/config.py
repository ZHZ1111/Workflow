"""Validated settings: CLI overrides > environment > explicit .env > defaults."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator


class ConfigError(ValueError):
    """A configuration problem that may be displayed without exposing values."""


def _is_placeholder(value: str) -> bool:
    return value.casefold() in {
        "", "your-deepseek-model", "your-model", "your-deepseek-api-key",
        "your-api-key", "replace-me", "changeme",
    } or value.startswith("<")


class AppConfig(BaseModel):
    """A2 settings; task input/result contracts are defined in models.py."""

    model_config = ConfigDict(
        frozen=True, extra="forbid", validate_default=True, hide_input_in_errors=True,
    )

    provider: Literal["deepseek"] = "deepseek"
    model: str
    base_url: str = "https://api.deepseek.com"
    proxy_url: str | None = Field(default=None, exclude=True, repr=False)
    api_key_env: str = "DEEPSEEK_API_KEY"
    api_key: SecretStr = Field(exclude=True, repr=False)
    request_timeout_seconds: int = Field(default=60, ge=1, le=600)
    max_tokens: int = Field(default=8192, ge=1024, le=32_768)
    max_rounds: int = Field(default=3, ge=1, le=3)
    execution_timeout_seconds: int = Field(default=30, ge=1, le=300)

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        value = value.strip()
        if _is_placeholder(value) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", value):
            raise ValueError("a non-placeholder model identifier is required")
        return value

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or any(char.isspace() for char in value)
        ):
            raise ValueError("a service URL without credentials, query or fragment is required")
        # Accessing port also rejects a malformed/non-numeric port.
        _ = parsed.port
        return value

    @field_validator("proxy_url")
    @classmethod
    def validate_proxy_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip().rstrip("/")
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or any(char.isspace() for char in value)
        ):
            raise ValueError("an HTTP(S) proxy URL without credentials, query or fragment is required")
        _ = parsed.port
        return value

    @field_validator("api_key_env")
    @classmethod
    def validate_key_env(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError("a valid environment variable name is required")
        return value

    @field_validator("api_key")
    @classmethod
    def validate_key(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value().strip()
        if _is_placeholder(secret) or any(char.isspace() for char in secret):
            raise ValueError("a non-placeholder API key is required")
        return SecretStr(secret)

    @field_validator(
        "request_timeout_seconds", "max_tokens", "max_rounds", "execution_timeout_seconds", mode="before",
    )
    @classmethod
    def validate_integer(cls, value: Any) -> int:
        if type(value) is int:
            return value
        if isinstance(value, str) and re.fullmatch(r"[+-]?[0-9]+", value.strip()):
            return int(value)
        raise ValueError("an integer is required")

    def safe_summary(self) -> dict[str, Any]:
        """No secret or partial key is included in the public representation."""
        return {
            **self.model_dump(mode="json"),
            "api_key_configured": True,
            "proxy_configured": self.proxy_url is not None,
        }


_ENV_FIELDS = {
    "provider": "LLM_PROVIDER",
    "model": "LLM_MODEL",
    "base_url": "LLM_BASE_URL",
    "proxy_url": "LLM_PROXY_URL",
    "api_key_env": "LLM_API_KEY_ENV",
    "request_timeout_seconds": "LLM_REQUEST_TIMEOUT_SECONDS",
    "max_tokens": "LLM_MAX_TOKENS",
    "max_rounds": "AGENT_MAX_ROUNDS",
    "execution_timeout_seconds": "TEST_EXECUTION_TIMEOUT_SECONDS",
}

_ERROR_HINTS = {
    "provider": "LLM_PROVIDER 当前只支持 deepseek",
    "model": "LLM_MODEL 必须填写实际模型标识，不能使用占位符",
    "base_url": "LLM_BASE_URL 必须是有效 HTTP(S) 服务地址，不能包含认证信息、查询或片段",
    "proxy_url": "LLM_PROXY_URL 必须是有效 HTTP(S) 代理地址，不能包含认证信息、查询或片段",
    "api_key_env": "LLM_API_KEY_ENV 必须是有效环境变量名",
    "api_key": "指定的 API Key 环境变量必须已设置，且不能为空或占位符",
    "request_timeout_seconds": "LLM_REQUEST_TIMEOUT_SECONDS 必须是 1..600 的整数",
    "max_tokens": "LLM_MAX_TOKENS 必须是 1024..32768 的整数",
    "max_rounds": "AGENT_MAX_ROUNDS 必须是 1..3 的整数",
    "execution_timeout_seconds": "TEST_EXECUTION_TIMEOUT_SECONDS 必须是 1..300 的整数",
}


def load_config(
    *,
    env_file: Path | None = None,
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> AppConfig:
    """Read only the selected file; do not mutate os.environ or search ancestors."""
    selected_file = Path(".env") if env_file is None else Path(env_file)
    values: dict[str, Any] = {}
    if selected_file.exists():
        try:
            if not selected_file.is_file():
                raise OSError("not a file")
            values.update(dotenv_values(selected_file, encoding="utf-8-sig", interpolate=False))
        except (OSError, UnicodeError):
            raise ConfigError("无法读取配置文件，请检查文件权限及 UTF-8 编码。") from None
    elif env_file is not None:
        raise ConfigError("显式指定的配置文件不存在。")

    values.update(os.environ if environ is None else environ)
    data = {field: values[name] for field, name in _ENV_FIELDS.items() if name in values}
    if overrides:
        data.update({field: value for field, value in overrides.items() if value is not None})
    key_env = data.get("api_key_env", "DEEPSEEK_API_KEY")
    data["api_key"] = values.get(key_env, "")
    try:
        return AppConfig.model_validate(data)
    except ValidationError as error:
        # Never interpolate the rejected value or Pydantic's full error into logs.
        hints = dict.fromkeys(
            _ERROR_HINTS.get(str(item["loc"][0]), "存在未知配置字段")
            for item in error.errors(include_input=False, include_context=False)
        )
        raise ConfigError("配置无效：" + "；".join(hints)) from None
