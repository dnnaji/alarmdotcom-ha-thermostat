"""Controller interfaces with the Alarm.com API via pyalarmdotcomajax."""

import asyncio
import logging
from typing import cast

import pyalarmdotcomajax as pyadc
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from pyalarmdotcomajax import AlarmBridge

from .const import (
    DATA_HUB,
    DOMAIN,
    PLATFORMS,
)
from .thermostat_proxy import (
    ThermostatProxyAuthFailed,
    ThermostatProxyBridge,
    ThermostatProxyError,
    ThermostatProxyMfaRequired,
    ThermostatProxyUnavailable,
)

log = logging.getLogger(__name__)


class AlarmHub:
    """Config-entry initiated Alarm Hub."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize the system."""

        self.hass: HomeAssistant = hass
        self.config_entry: ConfigEntry = config_entry

        self.api: AlarmBridge
        self._api_initialized = False

        self.close_jobs: list[CALLBACK_TYPE] = []

        hass.data.setdefault(DOMAIN, {})[self.config_entry.entry_id] = {DATA_HUB: self}

        self.available: bool = True

    # @property
    # def available(self) -> bool:
    #     """
    #     Whether the Alarm.com API is available.

    #     This will only be true if the websocket connection is established and has not been disconnected
    #     for more than 60 seconds. This is to prevent the system from being marked as unavailable if the
    #     connection is temporarily lost.
    #     """
    #     # If never connected, treat as unavailable.
    #     ws = self.api.ws_controller
    #     if ws.connected:
    #         # Update last connected time
    #         self._last_connected = asyncio.get_event_loop().time()
    #         return True

    #     # If never set, treat as unavailable
    #     last_connected = getattr(self, "_last_connected", None)
    #     if last_connected is None:
    #         return False

    #     # If disconnected for less than 60 seconds, still available
    #     return bool(asyncio.get_event_loop().time() - last_connected < 60)

    async def login(self) -> bool:
        """Create the thermostat-only proxy facade."""

        self.api = cast("AlarmBridge", ThermostatProxyBridge(self.hass, self.config_entry))
        return True

    async def initialize(self) -> bool:
        """Initialize connection to Alarm.com after user-driven authentication has already taken place."""

        setup_ok = False
        if not await self.login():
            return False

        try:
            async with asyncio.timeout(60):
                await self.api.initialize()
            setup_ok = True
            self._api_initialized = True
        except ThermostatProxyMfaRequired as err:
            raise ConfigEntryAuthFailed("Alarm.com MFA token must be refreshed.") from err
        except ThermostatProxyAuthFailed as err:
            raise ConfigEntryAuthFailed from err
        except ThermostatProxyUnavailable as err:
            raise ConfigEntryNotReady("Alarm.com thermostat proxy is unavailable.") from err
        except ThermostatProxyError as err:
            raise ConfigEntryNotReady("Alarm.com thermostat proxy returned invalid data.") from err
        except (
            TimeoutError,
            pyadc.UnexpectedResponse,
            pyadc.ServiceUnavailable,
        ) as err:
            raise ConfigEntryNotReady("Could not connect to Alarm.com.") from err
        except pyadc.AuthenticationException as err:
            raise ConfigEntryAuthFailed from err
        except Exception:
            log.exception("Unexpected error during Alarm.com initialization.")
            return False
        finally:
            if not setup_ok and self._api_initialized:
                await self.api.close()

        # Initialize WebSocket connection.
        await self.api.start_event_monitoring(_ws_state_handler)

        self.close_jobs.append(self.config_entry.add_update_listener(_update_listener))

        # Create system/hub device.
        device_registry = dr.async_get(self.hass)

        device_registry.async_get_or_create(
            config_entry_id=self.config_entry.entry_id,
            identifiers={(DOMAIN, self.api.active_system.id)},
            manufacturer="Alarm.com",
            name=self.api.active_system.name,
            entry_type=dr.DeviceEntryType.SERVICE,
        )

        return True

    async def close(self) -> bool:
        """
        Reset this bridge to default state.

        Will cancel any scheduled setup retry and will unload
        the config entry.
        """

        while self.close_jobs:
            self.close_jobs.pop()()

        if self._api_initialized:
            await self.api.close()

        unload_success: bool = await self.hass.config_entries.async_unload_platforms(
            self.config_entry, PLATFORMS
        )

        return unload_success


async def _ws_state_handler(message: pyadc.EventBrokerMessage) -> None:
    """Handle changes to websocket state for ConfigEntry and logging."""

    if not isinstance(message, pyadc.ConnectionEvent):
        return

    # WebSocket service handles reconnection on its own. Handle reporting for DEAD state here; do not attempt to
    # reconnect independently.

    if message.current_state == pyadc.WebSocketState.DEAD:
        log.error("Alarm.com websocket state message: %s", message)
        raise ConfigEntryNotReady("Alarm.com websocket connection died.")

    if message.current_state not in [
        pyadc.WebSocketState.CONNECTED,
        pyadc.WebSocketState.CONNECTING,
    ]:
        log.info("Alarm.com websocket state message: %s", message)
        return

    # Should only print CONNECTED events.
    log.debug("Alarm.com websocket state: %s", message.current_state)


async def _update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle ConfigEntry options update."""
    await hass.config_entries.async_reload(entry.entry_id)
