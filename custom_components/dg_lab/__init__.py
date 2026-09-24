"""DG-LAB Home Assistant integration."""

from __future__ import annotations

from collections.abc import Iterable
import json
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
import homeassistant.helpers.config_validation as cv

from .api import DGLabClient, normalize_frames
from .const import (
    ATTR_CHANNEL,
    ATTR_CLIENT_ID,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_DURATION_MS,
    ATTR_FRAMES,
    ATTR_IMMEDIATE,
    ATTR_PAYLOAD,
    ATTR_PRIORITY,
    ATTR_SEQ,
    ATTR_SLOT_ID,
    ATTR_TIMEOUT,
    ATTR_VALUE,
    ATTR_VERSION,
    DOMAIN,
    PLATFORMS,
    SERVICE_ADD_INTENSITY,
    SERVICE_CLEAR_OPERATIONS,
    SERVICE_PING,
    SERVICE_RECONNECT,
    SERVICE_REQUEST_DEVICES,
    SERVICE_SEND_PULSE,
    SERVICE_SEND_RPC,
    SERVICE_SET_INTENSITY,
    SERVICE_SET_TEMP_INTENSITY,
)
from .websocket import DGLabWebSocketView

_CHANNEL_SCHEMA = vol.Any(
    vol.In(["A", "a", "B", "b", "0", "1"]),
    vol.In([0, 1]),
)
_PRIORITY_SCHEMA = vol.All(vol.Coerce(int), vol.Range(min=0, max=2))
_TIMEOUT_SCHEMA = vol.All(vol.Coerce(float), vol.Range(min=0.1, max=86400))

_BASE_SCHEMA = {
    vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
}
_APP_SCHEMA = {
    **_BASE_SCHEMA,
    vol.Required(ATTR_CLIENT_ID): cv.string,
}
_CHANNEL_COMMAND_SCHEMA = {
    **_APP_SCHEMA,
    vol.Required(ATTR_SLOT_ID): cv.string,
    vol.Required(ATTR_CHANNEL): _CHANNEL_SCHEMA,
    vol.Optional(ATTR_PRIORITY): _PRIORITY_SCHEMA,
    vol.Optional(ATTR_IMMEDIATE): cv.boolean,
    vol.Optional(ATTR_TIMEOUT): _TIMEOUT_SCHEMA,
}


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up global DG-LAB services."""
    hass.data.setdefault(DOMAIN, {})
    hass.http.register_view(DGLabWebSocketView(hass))

    async def handle_reconnect(call: ServiceCall) -> None:
        for client in _clients_from_call(hass, call):
            await client.async_reconnect()

    async def handle_request_devices(call: ServiceCall) -> None:
        client = _client_from_call(hass, call)
        for client_id in _target_client_ids(client, call):
            await client.request_devices(client_id)

    async def handle_ping(call: ServiceCall) -> None:
        client = _client_from_call(hass, call)
        for client_id in _target_client_ids(client, call):
            await client.ping_app(client_id, timeout=call.data.get(ATTR_TIMEOUT))

    async def handle_add_intensity(call: ServiceCall) -> None:
        client = _client_from_call(hass, call)
        await client.add_intensity(
            call.data[ATTR_CLIENT_ID],
            call.data[ATTR_SLOT_ID],
            call.data[ATTR_CHANNEL],
            call.data[ATTR_VALUE],
            priority=call.data.get(ATTR_PRIORITY),
            immediate=call.data.get(ATTR_IMMEDIATE),
            timeout=call.data.get(ATTR_TIMEOUT),
        )

    async def handle_set_intensity(call: ServiceCall) -> None:
        client = _client_from_call(hass, call)
        await client.set_intensity(
            call.data[ATTR_CLIENT_ID],
            call.data[ATTR_SLOT_ID],
            call.data[ATTR_CHANNEL],
            call.data[ATTR_VALUE],
            priority=call.data.get(ATTR_PRIORITY),
            immediate=call.data.get(ATTR_IMMEDIATE),
            timeout=call.data.get(ATTR_TIMEOUT),
        )

    async def handle_set_temp_intensity(call: ServiceCall) -> None:
        client = _client_from_call(hass, call)
        await client.set_temp_intensity(
            call.data[ATTR_CLIENT_ID],
            call.data[ATTR_SLOT_ID],
            call.data[ATTR_CHANNEL],
            call.data[ATTR_VALUE],
            call.data[ATTR_DURATION_MS],
            priority=call.data.get(ATTR_PRIORITY),
            immediate=call.data.get(ATTR_IMMEDIATE),
            timeout=call.data.get(ATTR_TIMEOUT),
        )

    async def handle_send_pulse(call: ServiceCall) -> None:
        client = _client_from_call(hass, call)
        await client.send_pulse(
            call.data[ATTR_CLIENT_ID],
            call.data[ATTR_SLOT_ID],
            call.data[ATTR_CHANNEL],
            normalize_frames(call.data[ATTR_FRAMES]),
            call.data[ATTR_DURATION_MS],
            priority=call.data.get(ATTR_PRIORITY),
            immediate=call.data.get(ATTR_IMMEDIATE),
            version=call.data.get(ATTR_VERSION),
            seq=call.data.get(ATTR_SEQ),
            timeout=call.data.get(ATTR_TIMEOUT),
        )

    async def handle_clear_operations(call: ServiceCall) -> None:
        client = _client_from_call(hass, call)
        for client_id in _target_client_ids(client, call):
            await client.clear_operations(
                client_id,
                slot_id=call.data.get(ATTR_SLOT_ID),
                channel=call.data.get(ATTR_CHANNEL),
                timeout=call.data.get(ATTR_TIMEOUT),
            )

    async def handle_send_rpc(call: ServiceCall) -> None:
        client = _client_from_call(hass, call)
        payload = call.data[ATTR_PAYLOAD]
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            raise HomeAssistantError("payload must be a dictionary or JSON object")
        await client.send_rpc(
            call.data[ATTR_CLIENT_ID],
            payload,
            timeout=call.data.get(ATTR_TIMEOUT),
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_RECONNECT,
        handle_reconnect,
        schema=vol.Schema(_BASE_SCHEMA),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REQUEST_DEVICES,
        handle_request_devices,
        schema=vol.Schema(
            {
                **_BASE_SCHEMA,
                vol.Optional(ATTR_CLIENT_ID): cv.string,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_PING,
        handle_ping,
        schema=vol.Schema(
            {
                **_BASE_SCHEMA,
                vol.Optional(ATTR_CLIENT_ID): cv.string,
                vol.Optional(ATTR_TIMEOUT): _TIMEOUT_SCHEMA,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_INTENSITY,
        handle_add_intensity,
        schema=vol.Schema(
            {
                **_CHANNEL_COMMAND_SCHEMA,
                vol.Required(ATTR_VALUE): vol.All(
                    vol.Coerce(float), vol.Range(min=-1000, max=1000)
                ),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_INTENSITY,
        handle_set_intensity,
        schema=vol.Schema(
            {
                **_CHANNEL_COMMAND_SCHEMA,
                vol.Required(ATTR_VALUE): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=1000)
                ),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_TEMP_INTENSITY,
        handle_set_temp_intensity,
        schema=vol.Schema(
            {
                **_CHANNEL_COMMAND_SCHEMA,
                vol.Required(ATTR_VALUE): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=1000)
                ),
                vol.Required(ATTR_DURATION_MS): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=86_400_000)
                ),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_PULSE,
        handle_send_pulse,
        schema=vol.Schema(
            {
                **_CHANNEL_COMMAND_SCHEMA,
                vol.Required(ATTR_FRAMES): vol.Any(cv.string, list),
                vol.Required(ATTR_DURATION_MS): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=86_400_000)
                ),
                vol.Optional(ATTR_VERSION): vol.All(vol.Coerce(int), vol.Range(min=1)),
                vol.Optional(ATTR_SEQ): vol.All(vol.Coerce(int), vol.Range(min=0)),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_OPERATIONS,
        handle_clear_operations,
        schema=vol.Schema(
            {
                **_BASE_SCHEMA,
                vol.Optional(ATTR_CLIENT_ID): cv.string,
                vol.Optional(ATTR_SLOT_ID): cv.string,
                vol.Optional(ATTR_CHANNEL): _CHANNEL_SCHEMA,
                vol.Optional(ATTR_TIMEOUT): _TIMEOUT_SCHEMA,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_RPC,
        handle_send_rpc,
        schema=vol.Schema(
            {
                **_APP_SCHEMA,
                vol.Required(ATTR_PAYLOAD): vol.Any(dict, cv.string),
                vol.Optional(ATTR_TIMEOUT): _TIMEOUT_SCHEMA,
            }
        ),
    )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up DG-LAB from a config entry."""
    client = DGLabClient(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = client
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await client.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a DG-LAB config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        client: DGLabClient | None = hass.data[DOMAIN].pop(entry.entry_id, None)
        if client is not None:
            await client.async_stop()
    return unload_ok


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload DG-LAB after its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


def _clients_from_call(hass: HomeAssistant, call: ServiceCall) -> Iterable[DGLabClient]:
    """Return one or all clients for a service call."""
    entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
    clients: dict[str, DGLabClient] = hass.data.get(DOMAIN, {})
    if entry_id is not None:
        client = clients.get(entry_id)
        if client is None:
            raise HomeAssistantError(f"Unknown DG-LAB config entry: {entry_id}")
        return [client]
    if not clients:
        raise HomeAssistantError("No DG-LAB config entries are loaded")
    return list(clients.values())


def _client_from_call(hass: HomeAssistant, call: ServiceCall) -> DGLabClient:
    """Return exactly one client for a service call."""
    clients = list(_clients_from_call(hass, call))
    if len(clients) != 1:
        raise HomeAssistantError(
            f"{ATTR_CONFIG_ENTRY_ID} is required when multiple DG-LAB entries are loaded"
        )
    return clients[0]


def _target_client_ids(client: DGLabClient, call: ServiceCall) -> list[str]:
    """Return target app client IDs for app-level service calls."""
    client_id = call.data.get(ATTR_CLIENT_ID)
    if client_id:
        return [client_id]
    client_ids = sorted(client.connected_client_ids)
    if not client_ids:
        raise HomeAssistantError("No DG-LAB app clients are currently attached")
    return client_ids
