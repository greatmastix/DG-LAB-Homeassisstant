"""Text platform for DG-LAB controls."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.text import RestoreText, TextEntity, TextMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback

from .api import DGLabClient, channel_name
from .const import DOMAIN
from .entity import (
    DGLabChannelEntity,
    iter_connected_devices,
    iter_device_channels,
    remove_stale_entities,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: Callable[[list[TextEntity]], None],
) -> None:
    """Set up DG-LAB text entities."""
    client: DGLabClient = hass.data[DOMAIN][entry.entry_id]
    seen: dict[tuple[Any, ...], str] = {}

    @callback
    def discover_entities() -> None:
        entities: list[TextEntity] = []
        current: set[tuple[Any, ...]] = set()
        for device in iter_connected_devices(client):
            for channel in iter_device_channels(device):
                key = (device.client_id, device.slot_id, channel, "pulse_frames")
                current.add(key)
                if key in seen:
                    continue
                entity = DGLabPulseFramesText(
                    client, device.client_id, device.slot_id, channel
                )
                assert entity.unique_id is not None
                seen[key] = entity.unique_id
                entities.append(entity)
        remove_stale_entities(hass, Platform.TEXT, seen, current)
        if entities:
            async_add_entities(entities)

    discover_entities()
    entry.async_on_unload(client.async_add_listener(discover_entities))


class DGLabPulseFramesText(DGLabChannelEntity, RestoreText):
    """Store raw pulse frames for the channel send-pulse button."""

    _attr_icon = "mdi:code-json"
    _attr_mode = TextMode.TEXT
    _attr_native_min = 0
    _attr_native_max = 1800

    def __init__(
        self, client: DGLabClient, client_id: str, slot_id: str, channel: int
    ) -> None:
        """Initialize the text entity."""
        super().__init__(client, client_id, slot_id, channel, "pulse_frames")
        self._attr_name = f"{channel_name(channel)} pulse frames"

    async def async_added_to_hass(self) -> None:
        """Restore stored text."""
        await super().async_added_to_hass()
        last_data = await self.async_get_last_text_data()
        if last_data is not None and last_data.native_value is not None:
            self.client.channel_setting(
                self.client_id, self.slot_id, self.channel
            ).pulse_frames = last_data.native_value

    @property
    def native_value(self) -> str:
        """Return stored frame text."""
        return self.client.channel_setting(
            self.client_id, self.slot_id, self.channel
        ).pulse_frames

    async def async_set_value(self, value: str) -> None:
        """Store frame text."""
        self.client.channel_setting(
            self.client_id, self.slot_id, self.channel
        ).pulse_frames = value
        self.async_write_ha_state()
