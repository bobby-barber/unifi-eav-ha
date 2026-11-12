"""Config flow: console address and a local account."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import EavAuthError, EavConnectionError, EavConsoleClient
from .const import (
    CONF_CEC,
    CONF_CONTROL_MODE,
    CONF_SCAN_INTERVAL,
    CONF_SITE,
    CONF_SNAPSHOT_INTERVAL,
    CONF_SSH_PASSWORD,
    CONF_SSH_USERNAME,
    CONF_VERIFY_SSL,
    CONTROL_MODES,
    CONTROL_OFF,
    DEFAULT_CEC,
    DEFAULT_CONTROL_MODE,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SITE,
    DEFAULT_SNAPSHOT_INTERVAL,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
    MIN_SNAPSHOT_INTERVAL,
)


def _user_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "")): str,
            vol.Required(CONF_PASSWORD): str,
            vol.Optional(CONF_SITE, default=defaults.get(CONF_SITE, DEFAULT_SITE)): str,
            vol.Optional(
                CONF_VERIFY_SSL, default=defaults.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)
            ): bool,
        }
    )


class UnifiEavConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle adding the integration."""

    VERSION = 1

    async def _validate(self, data: dict[str, Any]) -> tuple[dict[str, str], int]:
        """Return (errors, number_of_bridges_found)."""
        client = EavConsoleClient(
            async_create_clientsession(self.hass, verify_ssl=data[CONF_VERIFY_SSL]),
            data[CONF_HOST],
            data[CONF_USERNAME],
            data[CONF_PASSWORD],
            site=data[CONF_SITE],
            verify_ssl=data[CONF_VERIFY_SSL],
        )
        try:
            bridges = await client.async_get_bridges()
        except EavAuthError:
            return {"base": "invalid_auth"}, 0
        except EavConnectionError:
            return {"base": "cannot_connect"}, 0
        if not bridges:
            return {"base": "no_bridges"}, 0
        return {}, len(bridges)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors, _ = await self._validate(user_input)
            if not errors:
                await self.async_set_unique_id(user_input[CONF_HOST])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"UniFi EAV ({user_input[CONF_HOST]})", data=user_input
                )
        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(user_input or {}),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            errors, _ = await self._validate(data)
            if not errors:
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return UnifiEavOptionsFlow()


class UnifiEavOptionsFlow(OptionsFlow):
    """Polling, preview refresh, whether this integration may change routes, and CEC.

    CEC forwarding needs control (Live; Dry run only logs) and the bridges' SSH login. The
    SSH password field is left empty when shown; leaving it empty keeps the saved one.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        opts = self.config_entry.options
        errors: dict[str, str] = {}
        if user_input is not None:
            data = dict(user_input)
            if not data.get(CONF_SSH_PASSWORD):
                data[CONF_SSH_PASSWORD] = opts.get(CONF_SSH_PASSWORD, "")
            if data.get(CONF_CEC):
                if data.get(CONF_CONTROL_MODE) == CONTROL_OFF:
                    errors["base"] = "cec_needs_control"
                elif not data.get(CONF_SSH_USERNAME) or not data.get(CONF_SSH_PASSWORD):
                    errors["base"] = "cec_needs_ssh"
            if not errors:
                return self.async_create_entry(data=data)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=opts.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                    ): vol.All(int, vol.Range(min=MIN_SCAN_INTERVAL, max=600)),
                    vol.Required(
                        CONF_SNAPSHOT_INTERVAL,
                        default=opts.get(CONF_SNAPSHOT_INTERVAL, DEFAULT_SNAPSHOT_INTERVAL),
                    ): vol.All(int, vol.Range(min=MIN_SNAPSHOT_INTERVAL, max=300)),
                    vol.Required(
                        CONF_CONTROL_MODE,
                        default=opts.get(CONF_CONTROL_MODE, DEFAULT_CONTROL_MODE),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=CONTROL_MODES,
                            translation_key="control_mode",
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(CONF_CEC, default=opts.get(CONF_CEC, DEFAULT_CEC)): bool,
                    vol.Optional(
                        CONF_SSH_USERNAME, default=opts.get(CONF_SSH_USERNAME, "")
                    ): str,
                    vol.Optional(CONF_SSH_PASSWORD, default=""): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
        )
