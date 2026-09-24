"""Diagnostics support for DG-LAB."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import DGLabClient
from .const import DOMAIN

TO_REDACT = {
    "app_websocket_url",
    "pairing_url",
    "target_id",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    client: DGLabClient | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    data: dict[str, Any] = {
        "entry": {
            "title": entry.title,
            "data": dict(entry.data),
            "options": dict(entry.options),
        }
    }
    if client is not None:
        data["runtime"] = {
            "state": client.state,
            "connected": client.connected,
            "target_id": client.target_id,
            "app_websocket_url": client.app_websocket_url,
            "pairing_url": client.pairing_url,
            "connected_client_ids": sorted(client.connected_client_ids),
            "apps": {
                client_id: {
                    "connected": app.connected,
                    "last_seen": app.last_seen.isoformat() if app.last_seen else None,
                    "last_rtt_ms": app.last_rtt_ms,
                }
                for client_id, app in client.apps.items()
            },
            "devices": [
                {
                    "client_id": device.client_id,
                    "slot_id": device.slot_id,
                    "name": device.name,
                    "type": device.type,
                    "removed": device.removed,
                    "props": device.props,
                    "slot_state": device.slot_state,
                }
                for device in client.devices.values()
            ],
        }
    return async_redact_data(data, TO_REDACT)
