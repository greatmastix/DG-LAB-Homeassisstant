"""Button platform for DG-LAB controls."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .api import DGLabClient, channel_name, normalize_frames
from .const import DOMAIN
from .entity import (
    DGLabAppEntity,
    DGLabChannelEntity,
    DGLabHubEntity,
    iter_device_channels,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: Callable[[list[ButtonEntity]], None],
) -> None:
    """Set up DG-LAB buttons."""
    client: DGLabClient = hass.data[DOMAIN][entry.entry_id]
    seen: set[tuple[Any, ...]] = set()

    @callback
    def discover_entities() -> None:
        entities: list[ButtonEntity] = []

        key = ("hub", "reconnect")
        if key not in seen:
            seen.add(key)
            entities.append(DGLabReconnectButton(client))

        for client_id in client.apps:
            for cls in (
                DGLabRequestDevicesButton,
                DGLabPingAppButton,
                DGLabClearAppButton,
            ):
                key = ("app", client_id, cls.__name__)
                if key in seen:
                    continue
                seen.add(key)
                entities.append(cls(client, client_id))

        for device in client.devices.values():
            for channel in iter_device_channels(device):
                for cls in (
                    DGLabIncreaseIntensityButton,
                    DGLabDecreaseIntensityButton,
                    DGLabResetIntensityButton,
                    DGLabClearChannelButton,
                    DGLabApplyTempIntensityButton,
                    DGLabSendPulseButton,
                ):
                    key = (device.client_id, device.slot_id, channel, cls.__name__)
                    if key in seen:
                        continue
                    seen.add(key)
                    entities.append(
                        cls(client, device.client_id, device.slot_id, channel)
                    )

        if entities:
            async_add_entities(entities)

    discover_entities()
    entry.async_on_unload(client.async_add_listener(discover_entities))


class DGLabReconnectButton(DGLabHubEntity, ButtonEntity):
    """Reconnect the relay websocket."""

    _attr_name = "Reconnect"
    _attr_icon = "mdi:reload"

    def __init__(self, client: DGLabClient) -> None:
        """Initialize the button."""
        super().__init__(client, "reconnect")

    async def async_press(self) -> None:
        """Press the button."""
        await self.client.async_reconnect()


class DGLabRequestDevicesButton(DGLabAppEntity, ButtonEntity):
    """Request device list from an app."""

    _attr_name = "Request devices"
    _attr_icon = "mdi:database-refresh-outline"

    def __init__(self, client: DGLabClient, client_id: str) -> None:
        """Initialize the button."""
        super().__init__(client, client_id, "request_devices")

    async def async_press(self) -> None:
        """Press the button."""
        await self.client.request_devices(self.client_id)


class DGLabPingAppButton(DGLabAppEntity, ButtonEntity):
    """Ping an app."""

    _attr_name = "Ping"
    _attr_icon = "mdi:access-point-network"

    def __init__(self, client: DGLabClient, client_id: str) -> None:
        """Initialize the button."""
        super().__init__(client, client_id, "ping")

    async def async_press(self) -> None:
        """Press the button."""
        await self.client.ping_app(self.client_id)


class DGLabClearAppButton(DGLabAppEntity, ButtonEntity):
    """Clear all app operations."""

    _attr_name = "Clear all operations"
    _attr_icon = "mdi:stop-circle-outline"

    def __init__(self, client: DGLabClient, client_id: str) -> None:
        """Initialize the button."""
        super().__init__(client, client_id, "clear_all")

    async def async_press(self) -> None:
        """Press the button."""
        await self.client.clear_operations(self.client_id)


class DGLabChannelButton(DGLabChannelEntity, ButtonEntity):
    """Base channel button."""

    def __init__(
        self,
        client: DGLabClient,
        client_id: str,
        slot_id: str,
        channel: int,
        key: str,
        name: str,
    ) -> None:
        """Initialize the button."""
        super().__init__(client, client_id, slot_id, channel, key)
        self._attr_name = f"{channel_name(channel)} {name}"


class DGLabIncreaseIntensityButton(DGLabChannelButton):
    """Increase channel intensity by the configured step."""

    _attr_icon = "mdi:plus-circle-outline"

    def __init__(
        self, client: DGLabClient, client_id: str, slot_id: str, channel: int
    ) -> None:
        """Initialize the button."""
        super().__init__(client, client_id, slot_id, channel, "increase", "increase")

    async def async_press(self) -> None:
        """Press the button."""
        setting = self.client.channel_setting(self.client_id, self.slot_id, self.channel)
        await self.client.add_intensity(
            self.client_id, self.slot_id, self.channel, setting.step
        )


class DGLabDecreaseIntensityButton(DGLabChannelButton):
    """Decrease channel intensity by the configured step."""

    _attr_icon = "mdi:minus-circle-outline"

    def __init__(
        self, client: DGLabClient, client_id: str, slot_id: str, channel: int
    ) -> None:
        """Initialize the button."""
        super().__init__(client, client_id, slot_id, channel, "decrease", "decrease")

    async def async_press(self) -> None:
        """Press the button."""
        setting = self.client.channel_setting(self.client_id, self.slot_id, self.channel)
        await self.client.add_intensity(
            self.client_id, self.slot_id, self.channel, -setting.step
        )


class DGLabResetIntensityButton(DGLabChannelButton):
    """Reset channel intensity to zero."""

    _attr_icon = "mdi:numeric-0-circle-outline"

    def __init__(
        self, client: DGLabClient, client_id: str, slot_id: str, channel: int
    ) -> None:
        """Initialize the button."""
        super().__init__(client, client_id, slot_id, channel, "reset", "reset")

    async def async_press(self) -> None:
        """Press the button."""
        await self.client.reset_intensity(self.client_id, self.slot_id, self.channel)


class DGLabClearChannelButton(DGLabChannelButton):
    """Clear channel operations."""

    _attr_icon = "mdi:stop-circle-outline"

    def __init__(
        self, client: DGLabClient, client_id: str, slot_id: str, channel: int
    ) -> None:
        """Initialize the button."""
        super().__init__(
            client, client_id, slot_id, channel, "clear_channel", "clear operations"
        )

    async def async_press(self) -> None:
        """Press the button."""
        await self.client.clear_operations(
            self.client_id, slot_id=self.slot_id, channel=self.channel
        )


class DGLabApplyTempIntensityButton(DGLabChannelButton):
    """Apply temporary intensity using stored helper settings."""

    _attr_icon = "mdi:timer-flash-outline"

    def __init__(
        self, client: DGLabClient, client_id: str, slot_id: str, channel: int
    ) -> None:
        """Initialize the button."""
        super().__init__(
            client,
            client_id,
            slot_id,
            channel,
            "apply_temp_intensity",
            "apply temporary intensity",
        )

    async def async_press(self) -> None:
        """Press the button."""
        setting = self.client.channel_setting(self.client_id, self.slot_id, self.channel)
        await self.client.set_temp_intensity(
            self.client_id,
            self.slot_id,
            self.channel,
            setting.temp_intensity,
            setting.temp_duration_ms,
        )


class DGLabSendPulseButton(DGLabChannelButton):
    """Send raw pulse frames using stored helper settings."""

    _attr_icon = "mdi:sine-wave"

    def __init__(
        self, client: DGLabClient, client_id: str, slot_id: str, channel: int
    ) -> None:
        """Initialize the button."""
        super().__init__(client, client_id, slot_id, channel, "send_pulse", "send pulse")

    async def async_press(self) -> None:
        """Press the button."""
        setting = self.client.channel_setting(self.client_id, self.slot_id, self.channel)
        await self.client.send_pulse(
            self.client_id,
            self.slot_id,
            self.channel,
            normalize_frames(setting.pulse_frames),
            setting.pulse_duration_ms,
        )
