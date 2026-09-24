"""Config flow for the DG-LAB integration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
import homeassistant.helpers.config_validation as cv

from .const import (
    CONF_AUTO_RECONNECT,
    CONF_COMMAND_STEP,
    CONF_CONNECTION_MODE,
    CONF_CONNECT_TIMEOUT,
    CONF_HA_URL,
    CONF_MAX_INTENSITY,
    CONF_RECONNECT_DELAY,
    CONF_RESPONSE_TIMEOUT,
    CONF_URL,
    DEFAULT_AUTO_RECONNECT,
    DEFAULT_COMMAND_STEP,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_MAX_INTENSITY,
    DEFAULT_NAME,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_RESPONSE_TIMEOUT,
    DEFAULT_WS_URL,
    DOMAIN,
    MODE_LOCAL,
    MODE_RELAY,
)


def _validate_websocket_url(value: str) -> str:
    """Validate a ws/wss URL."""
    value = value.strip()
    try:
        parsed = urlparse(value)
        valid = (
            parsed.scheme in {"ws", "wss"}
            and parsed.hostname
            and parsed.port != 0
        )
    except ValueError:
        valid = False
    if not valid:
        raise vol.Invalid("websocket_url")
    return value


def _validate_ha_url(value: str) -> str:
    """Validate a Home Assistant base URL reachable by the app."""
    value = value.strip().rstrip("/")
    try:
        parsed = urlparse(value)
        valid = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and parsed.port != 0
        )
    except ValueError:
        valid = False
    if (
        not valid
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise vol.Invalid("home_assistant_url")
    return value


def _schema(defaults: Mapping[str, Any] | None = None) -> vol.Schema:
    """Return the flow schema."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_NAME, default=defaults.get(CONF_NAME, DEFAULT_NAME)
            ): cv.string,
            vol.Required(
                CONF_CONNECTION_MODE,
                default=defaults.get(CONF_CONNECTION_MODE, MODE_LOCAL),
            ): vol.In({MODE_LOCAL: "Home Assistant direct", MODE_RELAY: "V4 relay"}),
            vol.Optional(CONF_HA_URL, default=defaults.get(CONF_HA_URL, "")): cv.string,
            vol.Required(
                CONF_URL, default=defaults.get(CONF_URL, DEFAULT_WS_URL)
            ): cv.string,
            vol.Required(
                CONF_CONNECT_TIMEOUT,
                default=defaults.get(CONF_CONNECT_TIMEOUT, DEFAULT_CONNECT_TIMEOUT),
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=120)),
            vol.Required(
                CONF_RESPONSE_TIMEOUT,
                default=defaults.get(CONF_RESPONSE_TIMEOUT, DEFAULT_RESPONSE_TIMEOUT),
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=3600)),
            vol.Required(
                CONF_RECONNECT_DELAY,
                default=defaults.get(CONF_RECONNECT_DELAY, DEFAULT_RECONNECT_DELAY),
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=300)),
            vol.Required(
                CONF_COMMAND_STEP,
                default=defaults.get(CONF_COMMAND_STEP, DEFAULT_COMMAND_STEP),
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
            vol.Required(
                CONF_MAX_INTENSITY,
                default=defaults.get(CONF_MAX_INTENSITY, DEFAULT_MAX_INTENSITY),
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=1000)),
            vol.Required(
                CONF_AUTO_RECONNECT,
                default=defaults.get(CONF_AUTO_RECONNECT, DEFAULT_AUTO_RECONNECT),
            ): cv.boolean,
        }
    )


class DGLabConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a DG-LAB config flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._default_ha_url: str = ""

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Create the options flow."""
        return DGLabOptionsFlowHandler(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            errors = _validate_input(user_input)
            if not errors:
                return self.async_create_entry(
                    title=user_input[CONF_NAME],
                    data=user_input,
                )

        if not self._default_ha_url:
            self._default_ha_url = (
                self.hass.config.internal_url or self.hass.config.external_url or ""
            )
        return self.async_show_form(
            step_id="user",
            data_schema=_schema(
                {CONF_HA_URL: self._default_ha_url, **(user_input or {})}
            ),
            errors=errors,
        )


class DGLabOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle DG-LAB options."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize the options flow."""
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage integration options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            errors = _validate_input(user_input)
            if not errors:
                return self.async_create_entry(data=user_input)

        defaults = {
            CONF_CONNECTION_MODE: MODE_RELAY,
            **self._config_entry.data,
            **self._config_entry.options,
        }
        return self.async_show_form(
            step_id="init",
            data_schema=_schema(defaults),
            errors=errors,
        )


def _validate_input(user_input: dict[str, Any]) -> dict[str, str]:
    """Validate fields used by the selected connection mode."""
    errors: dict[str, str] = {}
    if user_input[CONF_CONNECTION_MODE] == MODE_LOCAL:
        try:
            user_input[CONF_HA_URL] = _validate_ha_url(user_input.get(CONF_HA_URL, ""))
        except vol.Invalid:
            errors[CONF_HA_URL] = "invalid_home_assistant_url"
    else:
        try:
            user_input[CONF_URL] = _validate_websocket_url(user_input[CONF_URL])
        except vol.Invalid:
            errors[CONF_URL] = "invalid_websocket_url"
    return errors
