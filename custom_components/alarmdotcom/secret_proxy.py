"""Alarm.com credential helper client."""

from __future__ import annotations

import asyncio
import json
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from homeassistant.core import HomeAssistant

from .const import (
    CONF_SECRET_PROFILE,
    DEFAULT_CREDENTIAL_HELPER_SOCKET,
    DEFAULT_CREDENTIAL_HELPER_TOKEN_FILE,
    DEFAULT_SECRET_PROFILE,
)

MAX_PROXY_MESSAGE_BYTES = 16 * 1024
PROXY_TIMEOUT_SECONDS = 5


class SecretProxyError(Exception):
    """Base error for credential helper failures."""


class SecretProxyUnavailable(SecretProxyError):
    """Credential helper could not be reached."""


class SecretProxyAuthFailed(SecretProxyError):
    """Credential helper rejected the caller."""


class SecretProxySecurityError(SecretProxyError):
    """Credential helper path failed local safety checks."""


class ConfigDataEntry(Protocol):
    """Minimal config-entry surface needed by credential resolution."""

    data: Any


@dataclass(frozen=True)
class AlarmComCredentials:
    """Alarm.com credentials resolved from the host-side helper."""

    username: str
    password: str
    mfa_token: str | None = None


def _read_token_file(path: str) -> str:
    token_path = Path(path)
    _assert_private_regular_file(token_path, "Credential helper token file")
    token = token_path.read_text(encoding="utf-8").strip()
    if not token:
        raise SecretProxyAuthFailed("Credential helper token file is empty.")
    return token


def _mode_string(mode: int) -> str:
    return f"0{stat.S_IMODE(mode):o}"


def _assert_owner_only(mode: int, label: str) -> None:
    if stat.S_IMODE(mode) & 0o077:
        raise SecretProxySecurityError(f"{label} must be owner-only; got {_mode_string(mode)}.")


def _assert_private_regular_file(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as err:
        raise SecretProxyUnavailable(f"{label} is unavailable.") from err

    if stat.S_ISLNK(info.st_mode):
        raise SecretProxySecurityError(f"{label} must not be a symbolic link.")
    if not stat.S_ISREG(info.st_mode):
        raise SecretProxySecurityError(f"{label} must be a regular file.")
    _assert_owner_only(info.st_mode, label)


def _assert_private_socket(path: str) -> None:
    socket_path = Path(path)
    try:
        info = socket_path.lstat()
    except OSError as err:
        raise SecretProxyUnavailable("Credential helper socket is unavailable.") from err

    if stat.S_ISLNK(info.st_mode):
        raise SecretProxySecurityError("Credential helper socket must not be a symbolic link.")
    if not stat.S_ISSOCK(info.st_mode):
        raise SecretProxySecurityError("Credential helper socket path must be a socket.")
    _assert_owner_only(info.st_mode, "Credential helper socket")


def _validate_helper_paths() -> None:
    _assert_private_regular_file(
        Path(DEFAULT_CREDENTIAL_HELPER_TOKEN_FILE),
        "Credential helper token file",
    )
    _assert_private_socket(DEFAULT_CREDENTIAL_HELPER_SOCKET)


def _read_string(mapping: dict[str, Any], key: str, *, required: bool = True) -> str | None:
    value = mapping.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value:
        raise SecretProxyError(f"Credential helper response is missing {key}.")
    return value


async def _proxy_request(socket_path: str, payload: dict[str, Any]) -> dict[str, Any]:
    _assert_private_socket(socket_path)

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(socket_path),
            timeout=PROXY_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, ConnectionError, OSError, TimeoutError) as err:
        raise SecretProxyUnavailable("Credential helper is unavailable.") from err

    try:
        writer.write((json.dumps(payload, separators=(",", ":")) + "\n").encode())
        await asyncio.wait_for(writer.drain(), timeout=PROXY_TIMEOUT_SECONDS)
        raw = await asyncio.wait_for(
            reader.readline(),
            timeout=PROXY_TIMEOUT_SECONDS,
        )
    except (ConnectionError, OSError, TimeoutError) as err:
        raise SecretProxyUnavailable("Credential helper request failed.") from err
    finally:
        writer.close()
        await writer.wait_closed()

    if not raw:
        raise SecretProxyUnavailable("Credential helper closed without a response.")
    if len(raw) > MAX_PROXY_MESSAGE_BYTES:
        raise SecretProxyError("Credential helper response is too large.")

    try:
        response = json.loads(raw.decode())
    except json.JSONDecodeError as err:
        raise SecretProxyError("Credential helper returned invalid JSON.") from err

    if not isinstance(response, dict):
        raise SecretProxyError("Credential helper returned an invalid response.")
    if response.get("ok") is not True:
        error = response.get("error")
        if error == "unauthorized":
            raise SecretProxyAuthFailed("Credential helper rejected the request.")
        raise SecretProxyUnavailable("Credential helper could not return credentials.")

    return response


async def async_resolve_credentials(
    hass: HomeAssistant, config_entry: ConfigDataEntry
) -> AlarmComCredentials:
    """Resolve Alarm.com credentials from the host-side credential helper."""

    profile = config_entry.data.get(CONF_SECRET_PROFILE, DEFAULT_SECRET_PROFILE)
    socket_path = DEFAULT_CREDENTIAL_HELPER_SOCKET
    token_file = DEFAULT_CREDENTIAL_HELPER_TOKEN_FILE

    if not isinstance(profile, str) or not profile:
        raise SecretProxyError("Credential helper profile is invalid.")

    try:
        token = await hass.async_add_executor_job(_read_token_file, token_file)
    except OSError as err:
        raise SecretProxyUnavailable("Credential helper token file is unavailable.") from err

    response = await _proxy_request(
        socket_path,
        {
            "op": "get_alarmdotcom_credentials",
            "profile": profile,
            "token": token,
        },
    )

    return AlarmComCredentials(
        username=_read_string(response, "username") or "",
        password=_read_string(response, "password") or "",
        mfa_token=_read_string(response, "mfa_token", required=False),
    )


async def async_validate_credential_helper_paths(hass: HomeAssistant) -> None:
    """Validate credential helper socket and token paths without sending credentials."""

    await hass.async_add_executor_job(_validate_helper_paths)
