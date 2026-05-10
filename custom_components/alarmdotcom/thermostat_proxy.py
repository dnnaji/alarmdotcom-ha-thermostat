"""Thermostat-only client for the host-side Alarm.com proxy."""

from __future__ import annotations

import asyncio
import json
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol

import pyalarmdotcomajax as pyadc
from homeassistant.core import CALLBACK_TYPE, HomeAssistant

from .const import (
    CONF_SECRET_PROFILE,
    DEFAULT_SECRET_PROFILE,
    DEFAULT_THERMOSTAT_PROXY_SOCKET,
    DEFAULT_THERMOSTAT_PROXY_TOKEN_FILE,
    DOMAIN,
)

MAX_PROXY_MESSAGE_BYTES = 256 * 1024
PROXY_TIMEOUT_SECONDS = 60
REFRESH_CACHE_SECONDS = 5
SETPOINT_RANGE_OP = "set_setpoint_range"


class ThermostatProxyError(Exception):
    """Base error for thermostat proxy failures."""


class ThermostatProxyUnavailable(ThermostatProxyError):
    """Thermostat proxy could not be reached."""


class ThermostatProxyAuthFailed(ThermostatProxyError):
    """Thermostat proxy or upstream Alarm.com authentication failed."""


class ThermostatProxyMfaRequired(ThermostatProxyAuthFailed):
    """Alarm.com requires a refreshed MFA token."""


class ThermostatProxySecurityError(ThermostatProxyError):
    """Thermostat proxy path failed local safety checks."""


class ThermostatProxyUnsupportedOperation(ThermostatProxyError):
    """Thermostat proxy does not support the requested operation."""


class ThermostatProxyPartialFailure(ThermostatProxyError):
    """Thermostat proxy accepted only part of a compound update."""


class ConfigDataEntry(Protocol):
    """Minimal config-entry surface needed by thermostat proxy resolution."""

    data: Any


@dataclass(frozen=True)
class _ProxyConfig:
    """Resolved proxy connection details."""

    profile: str
    socket_path: str
    token_file: str


class ProxyPartitions:
    """Minimal partition adapter for thermostat-only device info."""

    def get_device_partition(self, resource_id: str) -> None:
        """Return no partition for thermostat-only proxy resources."""


@dataclass
class ProxyThermostatAttributes:
    """Thermostat attributes shaped like the pyalarmdotcomajax resource."""

    state: pyadc.thermostat.ThermostatState
    inferred_state: pyadc.thermostat.ThermostatState
    schedule_mode: pyadc.thermostat.ThermostatScheduleMode
    ambient_temp: float | None = None
    humidity_level: int | None = None
    heat_setpoint: float | None = None
    cool_setpoint: float | None = None
    fan_mode: pyadc.thermostat.ThermostatFanMode = pyadc.thermostat.ThermostatFanMode.AUTO
    uses_celsius: bool = False
    setpoint_offset: float | None = 1.0
    supports_heat_mode: bool = False
    supports_cool_mode: bool = False
    supports_auto_mode: bool = False
    supports_off_mode: bool = False
    supports_fan_mode: bool = False
    supports_circulate_fan_mode_always: bool = False
    supports_circulate_fan_mode_when_off: bool = False
    supports_schedules: bool = False
    supports_setpoints: bool = False
    min_heat_setpoint: float | None = None
    max_heat_setpoint: float | None = None
    min_cool_setpoint: float | None = None
    max_cool_setpoint: float | None = None
    manufacturer: str | None = "Alarm.com"
    device_model: str | None = "Thermostat"
    loading: bool = False


@dataclass
class ProxyThermostatResource:
    """Thermostat resource shaped like the pyalarmdotcomajax resource."""

    id: str
    name: str
    system_id: str | None
    attributes: ProxyThermostatAttributes

    @property
    def fan_mode(self) -> pyadc.thermostat.ThermostatFanMode:
        """Return the currently requested fan mode."""
        return self.attributes.fan_mode


def _mode_string(mode: int) -> str:
    return f"0{stat.S_IMODE(mode):o}"


def _assert_owner_only(mode: int, label: str) -> None:
    if stat.S_IMODE(mode) & 0o077:
        raise ThermostatProxySecurityError(
            f"{label} must be owner-only; got {_mode_string(mode)}."
        )


def _assert_private_regular_file(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as err:
        raise ThermostatProxyUnavailable(f"{label} is unavailable.") from err

    if stat.S_ISLNK(info.st_mode):
        raise ThermostatProxySecurityError(f"{label} must not be a symbolic link.")
    if not stat.S_ISREG(info.st_mode):
        raise ThermostatProxySecurityError(f"{label} must be a regular file.")
    _assert_owner_only(info.st_mode, label)


def _assert_private_socket(path: str) -> None:
    socket_path = Path(path)
    try:
        info = socket_path.lstat()
    except OSError as err:
        raise ThermostatProxyUnavailable("Thermostat proxy socket is unavailable.") from err

    if stat.S_ISLNK(info.st_mode):
        raise ThermostatProxySecurityError("Thermostat proxy socket must not be a symbolic link.")
    if not stat.S_ISSOCK(info.st_mode):
        raise ThermostatProxySecurityError("Thermostat proxy socket path must be a socket.")
    _assert_owner_only(info.st_mode, "Thermostat proxy socket")


def _validate_proxy_paths() -> None:
    _assert_private_regular_file(
        Path(DEFAULT_THERMOSTAT_PROXY_TOKEN_FILE),
        "Thermostat proxy token file",
    )
    _assert_private_socket(DEFAULT_THERMOSTAT_PROXY_SOCKET)


def _read_token_file(path: str) -> str:
    token_path = Path(path)
    _assert_private_regular_file(token_path, "Thermostat proxy token file")
    token = token_path.read_text(encoding="utf-8").strip()
    if not token:
        raise ThermostatProxyAuthFailed("Thermostat proxy token file is empty.")
    return token


def _resolve_proxy_config(config_entry: ConfigDataEntry) -> _ProxyConfig:
    profile = config_entry.data.get(CONF_SECRET_PROFILE, DEFAULT_SECRET_PROFILE)
    if not isinstance(profile, str) or not profile:
        raise ThermostatProxyError("Thermostat proxy profile is invalid.")
    return _ProxyConfig(
        profile=profile,
        socket_path=DEFAULT_THERMOSTAT_PROXY_SOCKET,
        token_file=DEFAULT_THERMOSTAT_PROXY_TOKEN_FILE,
    )


async def _proxy_request(
    hass: HomeAssistant,
    proxy_config: _ProxyConfig,
    payload: dict[str, Any],
) -> dict[str, Any]:
    _assert_private_socket(proxy_config.socket_path)
    token = await hass.async_add_executor_job(_read_token_file, proxy_config.token_file)
    request = {
        **payload,
        "profile": proxy_config.profile,
        "token": token,
    }

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(
                proxy_config.socket_path,
                limit=MAX_PROXY_MESSAGE_BYTES + 1,
            ),
            timeout=PROXY_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, ConnectionError, OSError, TimeoutError) as err:
        raise ThermostatProxyUnavailable("Thermostat proxy is unavailable.") from err

    try:
        writer.write((json.dumps(request, separators=(",", ":")) + "\n").encode())
        await asyncio.wait_for(writer.drain(), timeout=PROXY_TIMEOUT_SECONDS)
        raw = await asyncio.wait_for(
            reader.readline(),
            timeout=PROXY_TIMEOUT_SECONDS,
        )
    except asyncio.LimitOverrunError as err:
        raise ThermostatProxyError("Thermostat proxy response is too large.") from err
    except (ConnectionError, OSError, TimeoutError) as err:
        raise ThermostatProxyUnavailable("Thermostat proxy request failed.") from err
    finally:
        writer.close()
        await writer.wait_closed()

    if not raw:
        raise ThermostatProxyUnavailable("Thermostat proxy closed without a response.")
    if len(raw) > MAX_PROXY_MESSAGE_BYTES:
        raise ThermostatProxyError("Thermostat proxy response is too large.")

    try:
        response = json.loads(raw.decode())
    except json.JSONDecodeError as err:
        raise ThermostatProxyError("Thermostat proxy returned invalid JSON.") from err

    if not isinstance(response, dict):
        raise ThermostatProxyError("Thermostat proxy returned an invalid response.")
    if response.get("ok") is True:
        return response

    error = response.get("error")
    if error in {"unauthorized", "auth_failed"}:
        raise ThermostatProxyAuthFailed("Thermostat proxy rejected the request.")
    if error == "mfa_required":
        raise ThermostatProxyMfaRequired("Alarm.com MFA token must be refreshed.")
    if error == "not_found":
        raise ThermostatProxyError("Thermostat proxy could not find the resource.")
    if error == "unsupported_operation":
        raise ThermostatProxyUnsupportedOperation(
            "Thermostat proxy refused unsupported operation."
        )
    raise ThermostatProxyUnavailable("Thermostat proxy could not return thermostat data.")


def _enum_by_name(enum_cls: Any, value: Any, default: Any) -> Any:
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls[value]
        except KeyError:
            return default
    return default


def _optional_float(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_bool(value: Any) -> bool:
    return bool(value) if isinstance(value, bool) else False


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _resource_from_snapshot(snapshot: dict[str, Any]) -> ProxyThermostatResource:
    attributes = ProxyThermostatAttributes(
        state=_enum_by_name(
            pyadc.thermostat.ThermostatState,
            snapshot.get("state"),
            pyadc.thermostat.ThermostatState.UNKNOWN,
        ),
        inferred_state=_enum_by_name(
            pyadc.thermostat.ThermostatState,
            snapshot.get("inferred_state"),
            pyadc.thermostat.ThermostatState.UNKNOWN,
        ),
        schedule_mode=_enum_by_name(
            pyadc.thermostat.ThermostatScheduleMode,
            snapshot.get("schedule_mode"),
            pyadc.thermostat.ThermostatScheduleMode.MANUAL_MODE,
        ),
        ambient_temp=_optional_float(snapshot.get("ambient_temp")),
        humidity_level=_optional_int(snapshot.get("humidity_level")),
        heat_setpoint=_optional_float(snapshot.get("heat_setpoint")),
        cool_setpoint=_optional_float(snapshot.get("cool_setpoint")),
        fan_mode=_enum_by_name(
            pyadc.thermostat.ThermostatFanMode,
            snapshot.get("fan_mode"),
            pyadc.thermostat.ThermostatFanMode.AUTO,
        ),
        uses_celsius=_optional_bool(snapshot.get("uses_celsius")),
        setpoint_offset=_optional_float(snapshot.get("setpoint_offset")) or 1.0,
        supports_heat_mode=_optional_bool(snapshot.get("supports_heat_mode")),
        supports_cool_mode=_optional_bool(snapshot.get("supports_cool_mode")),
        supports_auto_mode=_optional_bool(snapshot.get("supports_auto_mode")),
        supports_off_mode=_optional_bool(snapshot.get("supports_off_mode")),
        supports_fan_mode=_optional_bool(snapshot.get("supports_fan_mode")),
        supports_circulate_fan_mode_always=_optional_bool(
            snapshot.get("supports_circulate_fan_mode_always")
        ),
        supports_circulate_fan_mode_when_off=_optional_bool(
            snapshot.get("supports_circulate_fan_mode_when_off")
        ),
        supports_schedules=_optional_bool(snapshot.get("supports_schedules")),
        supports_setpoints=_optional_bool(snapshot.get("supports_setpoints")),
        min_heat_setpoint=_optional_float(snapshot.get("min_heat_setpoint")),
        max_heat_setpoint=_optional_float(snapshot.get("max_heat_setpoint")),
        min_cool_setpoint=_optional_float(snapshot.get("min_cool_setpoint")),
        max_cool_setpoint=_optional_float(snapshot.get("max_cool_setpoint")),
        manufacturer=_optional_string(snapshot.get("manufacturer")) or "Alarm.com",
        device_model=_optional_string(snapshot.get("device_model")) or "Thermostat",
    )
    return ProxyThermostatResource(
        id=str(snapshot["id"]),
        name=str(snapshot.get("name") or "Alarm.com Thermostat"),
        system_id=_optional_string(snapshot.get("system_id")) or DOMAIN,
        attributes=attributes,
    )


class ProxyThermostatController:
    """Thermostat-only controller backed by the local proxy."""

    def __init__(self, bridge: ThermostatProxyBridge) -> None:
        """Initialize controller."""
        self._bridge = bridge
        self._resources: dict[str, ProxyThermostatResource] = {}
        self._refresh_lock = asyncio.Lock()
        self._resource_refreshed_at: dict[str, float] = {}
        self._write_locks: dict[str, asyncio.Lock] = {}

    def __iter__(self) -> Any:
        """Iterate thermostat resources."""
        return iter(self._resources.values())

    def get(self, thermostat_id: str, default: Any = None) -> ProxyThermostatResource | Any:
        """Return thermostat resource by id."""
        return self._resources.get(thermostat_id, default)

    @property
    def resources(self) -> dict[str, ProxyThermostatResource]:
        """Return the live thermostat resource map."""
        return self._resources

    async def initialize(self) -> None:
        """Fetch initial thermostat list."""
        await self._refresh_all()

    async def _refresh_all(self) -> None:
        """Refresh all thermostat resources in one proxy request."""
        response = await self._bridge.request({"op": "list_thermostats"})
        snapshots = response.get("thermostats")
        if not isinstance(snapshots, list):
            raise ThermostatProxyError("Thermostat proxy returned invalid thermostat list.")
        resources = {
            resource.id: resource
            for resource in (
                _resource_from_snapshot(snapshot)
                for snapshot in snapshots
                if isinstance(snapshot, dict)
            )
        }
        self._resources.clear()
        self._resources.update(resources)
        refreshed_at = asyncio.get_running_loop().time()
        self._resource_refreshed_at = dict.fromkeys(self._resources, refreshed_at)

    def _is_fresh(self, thermostat_id: str) -> bool:
        refreshed_at = self._resource_refreshed_at.get(thermostat_id)
        if refreshed_at is None:
            return False
        return asyncio.get_running_loop().time() - refreshed_at < REFRESH_CACHE_SECONDS

    async def refresh(self, thermostat_id: str, *, force: bool = False) -> None:
        """Refresh a single thermostat resource."""
        if not force and self._is_fresh(thermostat_id):
            return

        async with self._refresh_lock:
            if not force and self._is_fresh(thermostat_id):
                return
            await self._refresh_all()
            if thermostat_id not in self._resources:
                raise ThermostatProxyError("Thermostat proxy could not find the resource.")

    def _write_lock(self, thermostat_id: str) -> asyncio.Lock:
        lock = self._write_locks.get(thermostat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._write_locks[thermostat_id] = lock
        return lock

    async def set_state(
        self,
        thermostat_id: str,
        state: pyadc.thermostat.ThermostatState | None = None,
        fan_mode: pyadc.thermostat.ThermostatFanMode | None = None,
        fan_mode_duration: int | None = None,
        cool_setpoint: float | None = None,
        heat_setpoint: float | None = None,
        schedule_mode: pyadc.thermostat.ThermostatScheduleMode | None = None,
    ) -> None:
        """Set allowed thermostat state via the proxy allowlist."""
        async with self._write_lock(thermostat_id):
            if state is not None:
                await self._request_and_store(
                    {
                        "op": "set_mode",
                        "thermostat_id": thermostat_id,
                        "mode": state.name.lower().replace("auto", "auto"),
                    }
                )
            if fan_mode is not None:
                await self._request_and_store(
                    {
                        "op": "set_fan_mode",
                        "thermostat_id": thermostat_id,
                        "fan_mode": fan_mode.name.lower(),
                    }
                )
            if heat_setpoint is not None and cool_setpoint is not None:
                await self._set_setpoint_range(
                    thermostat_id,
                    heat_setpoint=heat_setpoint,
                    cool_setpoint=cool_setpoint,
                )
                return
            if heat_setpoint is not None:
                await self._request_and_store(
                    {
                        "op": "set_heat_setpoint",
                        "thermostat_id": thermostat_id,
                        "temperature": heat_setpoint,
                    }
                )
            if cool_setpoint is not None:
                await self._request_and_store(
                    {
                        "op": "set_cool_setpoint",
                        "thermostat_id": thermostat_id,
                        "temperature": cool_setpoint,
                    }
                )
            if schedule_mode is not None:
                raise ThermostatProxySecurityError("Schedule writes are not implemented.")

    async def _set_setpoint_range(
        self,
        thermostat_id: str,
        *,
        heat_setpoint: float,
        cool_setpoint: float,
    ) -> None:
        """Set both setpoints atomically when the proxy supports it."""
        try:
            await self._request_and_store(
                {
                    "op": SETPOINT_RANGE_OP,
                    "thermostat_id": thermostat_id,
                    "heat_setpoint": heat_setpoint,
                    "cool_setpoint": cool_setpoint,
                }
            )
        except ThermostatProxyUnsupportedOperation:
            await self._set_setpoint_range_sequential(
                thermostat_id,
                heat_setpoint=heat_setpoint,
                cool_setpoint=cool_setpoint,
            )

    async def _set_setpoint_range_sequential(
        self,
        thermostat_id: str,
        *,
        heat_setpoint: float,
        cool_setpoint: float,
    ) -> None:
        """Set both setpoints on older proxies without hiding partial failure."""
        heat_applied = False
        try:
            await self._request_and_store(
                {
                    "op": "set_heat_setpoint",
                    "thermostat_id": thermostat_id,
                    "temperature": heat_setpoint,
                }
            )
            heat_applied = True
            await self._request_and_store(
                {
                    "op": "set_cool_setpoint",
                    "thermostat_id": thermostat_id,
                    "temperature": cool_setpoint,
                }
            )
        except ThermostatProxyError as err:
            if heat_applied:
                with suppress(ThermostatProxyError, TimeoutError):
                    await self.refresh(thermostat_id, force=True)
                raise ThermostatProxyPartialFailure(
                    "Thermostat proxy accepted the heat setpoint but failed to apply the cool setpoint."
                ) from err
            raise

    async def _request_and_store(self, payload: dict[str, Any]) -> None:
        response = await self._bridge.request(payload)
        snapshot = response.get("thermostat")
        if not isinstance(snapshot, dict):
            raise ThermostatProxyError("Thermostat proxy returned invalid thermostat state.")
        resource = _resource_from_snapshot(snapshot)
        self._resources[resource.id] = resource
        self._resource_refreshed_at[resource.id] = asyncio.get_running_loop().time()


class ThermostatProxyBridge:
    """Minimal AlarmBridge-shaped facade for thermostat-only Home Assistant entities."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigDataEntry) -> None:
        """Initialize thermostat proxy bridge."""
        self.hass = hass
        self.proxy_config = _resolve_proxy_config(config_entry)
        self.thermostats = ProxyThermostatController(self)
        self.managed_devices: dict[str, ProxyThermostatResource] = self.thermostats.resources
        self.partitions = ProxyPartitions()
        self.active_system = SimpleNamespace(id=DOMAIN, name="Alarm.com Thermostats")

    async def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send an allowed thermostat request to the host-side proxy."""
        return await _proxy_request(self.hass, self.proxy_config, payload)

    async def initialize(self) -> None:
        """Initialize thermostat-only state from the proxy."""
        await self.thermostats.initialize()

    async def close(self) -> None:
        """Close thermostat proxy bridge."""

    async def start_event_monitoring(self, handler: Any) -> None:
        """No-op event monitor; thermostat proxy uses polling."""

    def subscribe(self, callback: Any, resource_id: str) -> CALLBACK_TYPE:
        """Return a no-op unsubscribe callback; thermostat proxy uses polling."""
        return lambda: None


async def async_validate_thermostat_proxy_paths(hass: HomeAssistant) -> None:
    """Validate thermostat proxy socket and token paths without sending credentials."""

    await hass.async_add_executor_job(_validate_proxy_paths)
