"""Config flow to configure Alarmdotcom."""

import logging
from typing import Any

import aiohttp
import async_timeout
import pyalarmdotcomajax as pyadc
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_OPTIONS_DEFAULT,
    CONF_SECRET_PROFILE,
    DEFAULT_SECRET_PROFILE,
    DOMAIN,
)
from .secret_proxy import (
    SecretProxyAuthFailed,
    SecretProxyError,
    SecretProxyUnavailable,
    async_resolve_credentials,
)

LOGGER = logging.getLogger(__name__)


class ADCFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a Alarmdotcom config flow."""

    VERSION = 6

    def __init__(self) -> None:
        """Initialize the Alarmdotcom flow."""
        self.config: dict = {}
        self.system_id: str | None = None
        self.sensor_data: dict | None = {}
        self._config_title: str | None = None
        self._existing_entry: config_entries.ConfigEntry | None = None

        self._force_generic_name: bool = False

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "ADCOptionsFlowHandler":
        """Tell Home Assistant that this integration supports configuration options."""

        return ADCOptionsFlowHandler(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Gather configuration data when flow is initiated via the user interface."""
        errors = {}

        if user_input is not None:
            self.config = {
                CONF_SECRET_PROFILE: user_input[CONF_SECRET_PROFILE],
            }

            async with async_timeout.timeout(60):
                try:
                    credentials = await async_resolve_credentials(
                        self.hass, _FlowEntry(self.config)
                    )
                    self.bridge = pyadc.AlarmBridge(
                        username=credentials.username,
                        password=credentials.password,
                        mfa_token=credentials.mfa_token,
                    )
                    await self.bridge.login()
                except pyadc.OtpRequired:
                    LOGGER.debug("OTP code required; host-side MFA cookie refresh needed.")
                    errors["base"] = "mfa_cookie_required"
                except pyadc.MustConfigureMfa:
                    return self.async_abort(reason="must_enable_2fa")
                except SecretProxyAuthFailed:
                    errors["base"] = "credential_helper_auth_failed"
                except SecretProxyUnavailable:
                    errors["base"] = "credential_helper_unavailable"
                except SecretProxyError:
                    errors["base"] = "credential_helper_invalid"
                except (
                    TimeoutError,
                    aiohttp.ClientError,
                    pyadc.UnexpectedResponse,
                    pyadc.NotAuthorized,
                ):
                    LOGGER.exception(
                        "%s: user login failed to contact Alarm.com.",
                        __name__,
                    )
                    errors["base"] = "cannot_connect"
                except pyadc.AuthenticationFailed:
                    LOGGER.exception(
                        "%s: user login failed with AuthenticationFailed exception.",
                        __name__,
                    )
                    errors["base"] = "invalid_auth"
                except Exception:
                    LOGGER.exception("Got error while initializing Alarm.com.")
                    errors["base"] = "unknown"
                else:
                    return await self.async_step_final()

        creds_schema = vol.Schema(
            {
                vol.Required(
                    CONF_SECRET_PROFILE,
                    default=DEFAULT_SECRET_PROFILE,
                ): TextSelector(
                    TextSelectorConfig(
                        type=TextSelectorType.TEXT,
                        autocomplete="off",
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="user", data_schema=creds_schema, errors=errors, last_step=False
        )

    async def async_step_final(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create configuration entry using entered data."""

        # Fetch full state as a lazy way to get active_system_name. (Controller must be initialized.)
        await self.bridge.fetch_full_state()

        self._config_title = f"{self.bridge.active_system.name} ({self.bridge.auth_controller.dealer}):{self.bridge.auth_controller.user_email}"

        if self._existing_entry:
            LOGGER.debug(
                "Existing config entry found. Updating entry, then aborting config flow."
            )
            self.hass.config_entries.async_update_entry(
                self._existing_entry, data=self.config
            )
            await self.hass.config_entries.async_reload(self._existing_entry.entry_id)

            return self.async_abort(reason="reauth_successful")

        # Named async_ but doesn't require await!
        return self.async_create_entry(
            title=self._config_title, data=self.config, options=CONF_OPTIONS_DEFAULT
        )

    # #
    # Reauthentication Steps
    # #

    async def async_step_reauth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Perform reauth upon an API authentication error."""
        LOGGER.debug("Reauthenticating.")
        self._existing_entry = await self.async_set_unique_id(self._config_title)
        return await self.async_step_reauth_confirm(user_input)

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Dialog that informs the user that reauth is required."""
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=vol.Schema({}),
            )
        return await self.async_step_user()


class ADCOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle option configuration via Integrations page."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self.options = dict(config_entry.options)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """First screen for configuration options. Sets arming code."""
        return self.async_create_entry(title="", data={})

    async def async_step_modes(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """First screen for configuration options. Sets arming mode profiles."""
        return self.async_create_entry(title="", data={})


class _FlowEntry:
    """Minimal config-entry shape for credential resolution during setup."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
