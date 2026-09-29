"""Pure validation and public DTO helpers for the platform domain.

This module deliberately has no Flask, SQLite, filesystem, or credential
dependencies so it can be reused by the Hub and future node tooling.
"""
from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import urlsplit

MAX_ID = 128
MAX_NAME = 120
MAX_PROVIDER = 80
MAX_MODEL = 160
MAX_SECRET_REF = 256
MAX_ROOT_PATH = 1024
MAX_ENDPOINT = 2048

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_FORBIDDEN_ID_MARKERS = ("/", "\\", "..", "token", "secret", "key", "password")


class PlatformValidationError(ValueError):
    """Stable validation error that never includes raw input values."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = str(detail)[:200]
        super().__init__(code)

    def __str__(self) -> str:
        return self.code


def validate_id(value: Any, field: str = "id") -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_ID:
        raise PlatformValidationError("invalid_id", f"{field} 不合法")
    lowered = value.lower()
    if not _ID_RE.fullmatch(value) or any(marker in lowered for marker in _FORBIDDEN_ID_MARKERS):
        raise PlatformValidationError("invalid_id", f"{field} 不合法")
    return value


def validate_owner_id(value: Any) -> str:
    """Validate an operator-derived owner identity (email is allowed)."""
    if not isinstance(value, str) or not value or len(value) > 320:
        raise PlatformValidationError("invalid_owner", "owner_id 不合法")
    if any(char in value for char in ("/", "\\", "\x00", "\r", "\n")):
        raise PlatformValidationError("invalid_owner", "owner_id 不合法")
    return value.strip()


def bounded_text(value: Any, field: str, limit: int, *, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise PlatformValidationError("invalid_value", f"{field} 不合法")
    if len(value) > limit:
        raise PlatformValidationError("value_too_large", f"{field} 超出限制")
    return value.strip()


def optional_id(value: Any, field: str) -> str | None:
    if value is None or value == "":
        return None
    return validate_id(value, field)


def validate_client_token(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise PlatformValidationError("invalid_client_token", "client_token 不合法")
    if any(marker in value.lower() for marker in ("/", "\\", "\x00", "secret", "password")):
        raise PlatformValidationError("invalid_client_token", "client_token 不合法")
    return value


def validate_message_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlatformValidationError("invalid_message", "消息不能为空")
    if len(value.encode("utf-8")) > 32 * 1024:
        raise PlatformValidationError("message_too_large", "消息超出限制")
    return value


def validate_model_profile(data: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise PlatformValidationError("invalid_model", "模型配置不合法")
    profile_id = validate_id(data.get("profile_id"), "profile_id")
    provider = bounded_text(data.get("provider"), "provider", MAX_PROVIDER)
    model = bounded_text(data.get("model"), "model", MAX_MODEL)
    secret_ref = bounded_text(data.get("secret_ref"), "secret_ref", MAX_SECRET_REF, required=False)
    capabilities = data.get("capabilities") or {}
    if not isinstance(capabilities, Mapping):
        raise PlatformValidationError("invalid_model", "模型能力不合法")
    provider_config = data.get("provider_config") or data.get("settings") or {}
    if not isinstance(provider_config, Mapping):
        raise PlatformValidationError("invalid_model", "provider 配置不合法")
    normalized_config: dict[str, Any] = {}
    allowed_config = {"endpoint", "base_url", "timeout_s", "max_retries", "stream", "headers"}
    if any(not isinstance(key, str) or key not in allowed_config for key in provider_config):
        raise PlatformValidationError("invalid_model", "provider 配置不合法")
    for key in ("endpoint", "base_url"):
        if key in provider_config:
            value = provider_config[key]
            if not isinstance(value, str) or not value or len(value) > MAX_ENDPOINT:
                raise PlatformValidationError("invalid_model", "provider endpoint 不合法")
            parsed = urlsplit(value)
            if (parsed.scheme not in ("http", "https") or not parsed.netloc
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise PlatformValidationError("invalid_model", "provider endpoint 不合法")
            normalized_config[key] = value
    if "timeout_s" in provider_config:
        try:
            timeout_s = float(provider_config["timeout_s"])
        except (TypeError, ValueError):
            raise PlatformValidationError("invalid_model", "provider timeout 不合法") from None
        if timeout_s != timeout_s or timeout_s in (float("inf"), float("-inf")):
            raise PlatformValidationError("invalid_model", "provider timeout 不合法")
        normalized_config["timeout_s"] = max(1.0, min(600.0, timeout_s))
    if "max_retries" in provider_config:
        try:
            retries = int(provider_config["max_retries"])
        except (TypeError, ValueError):
            raise PlatformValidationError("invalid_model", "provider retries 不合法") from None
        normalized_config["max_retries"] = max(0, min(5, retries))
    if "stream" in provider_config:
        if not isinstance(provider_config["stream"], bool):
            raise PlatformValidationError("invalid_model", "provider stream 不合法")
        normalized_config["stream"] = provider_config["stream"]
    if "headers" in provider_config:
        headers = provider_config["headers"]
        if not isinstance(headers, Mapping) or len(headers) > 32:
            raise PlatformValidationError("invalid_model", "provider headers 不合法")
        forbidden = ("authorization", "api-key", "token", "secret", "password", "credential")
        for key, value in headers.items():
            if (not isinstance(key, str) or not isinstance(value, str)
                    or len(key) > 80 or len(value) > 512
                    or any(marker in key.lower() for marker in forbidden)):
                raise PlatformValidationError("invalid_model", "provider headers 不合法")
        normalized_config["headers"] = dict(headers)
    return {
        "profile_id": profile_id,
        "provider": provider,
        "model": model,
        "secret_ref": secret_ref or None,
        "capabilities": {str(k): bool(v) for k, v in capabilities.items() if isinstance(k, str)},
        "provider_config": normalized_config,
        "enabled": bool(data.get("enabled", True)),
    }


def validate_workspace(data: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise PlatformValidationError("invalid_workspace", "工作区配置不合法")
    workspace_id = validate_id(data.get("workspace_id"), "workspace_id")
    name = bounded_text(data.get("name") or workspace_id, "name", MAX_NAME)
    backend = bounded_text(data.get("backend") or "directory", "backend", 40)
    root_path = bounded_text(data.get("root_path"), "root_path", MAX_ROOT_PATH)
    node_id = optional_id(data.get("default_node_id"), "default_node_id")
    return {
        "workspace_id": workspace_id, "name": name, "backend": backend,
        "root_path": root_path, "default_node_id": node_id,
        "enabled": bool(data.get("enabled", True)),
    }


def validate_node(data: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise PlatformValidationError("invalid_node", "执行节点配置不合法")
    node_id = validate_id(data.get("node_id"), "node_id")
    label = bounded_text(data.get("label") or node_id, "label", MAX_NAME)
    capabilities = data.get("capabilities") or {}
    if not isinstance(capabilities, Mapping):
        raise PlatformValidationError("invalid_node", "节点能力不合法")
    return {
        "node_id": node_id, "label": label,
        "capabilities": {str(k): bool(v) for k, v in capabilities.items() if isinstance(k, str)},
        "enabled": bool(data.get("enabled", True)),
    }


def public_model(row: Mapping[str, Any]) -> dict[str, Any]:
    """Return a model profile without secret values or secret-bearing fields."""
    capabilities = row.get("capabilities") or {}
    return {
        "profile_id": str(row.get("profile_id") or ""),
        "provider": str(row.get("provider") or ""),
        "model": str(row.get("model") or ""),
        "capabilities": dict(capabilities) if isinstance(capabilities, Mapping) else {},
        "secret_configured": bool(row.get("secret_ref")),
        "enabled": bool(row.get("enabled", True)),
    }


__all__ = [
    "MAX_ENDPOINT", "MAX_ID", "PlatformValidationError", "bounded_text",
    "optional_id", "public_model", "validate_client_token", "validate_id", "validate_message_text", "validate_owner_id",
    "validate_model_profile", "validate_node", "validate_workspace",
]
