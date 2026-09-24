"""Entity helpers for the DG-LAB integration."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.entity import DeviceInfo, Entity

from .api import DGLabClient, DGLabDevice, channel_name, device_supports_channel
from .const import DOMAIN, MANUFACTURER


class DGLabBaseEntity(Entity):
    """Base entity for DG-LAB entities."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, client: DGLabClient) -> None:
        """Initialize the entity."""
        self.client = client

    async def async_added_to_hass(self) -> None:
        """Subscribe to websocket updates."""
        self.async_on_remove(self.client.async_add_listener(self._handle_update))

    @callback
    def _handle_update(self) -> None:
        """Write current state to Home Assistant."""
        self.async_write_ha_state()


class DGLabHubEntity(DGLabBaseEntity):
    """Base entity for controller-level entities."""

    def __init__(self, client: DGLabClient, key: str) -> None:
        """Initialize the entity."""
        super().__init__(client)
        self._attr_unique_id = f"{client.entry.entry_id}_{key}"
        self._attr_device_info = hub_device_info(client)


class DGLabAppEntity(DGLabBaseEntity):
    """Base entity for app-level entities."""

    def __init__(self, client: DGLabClient, client_id: str, key: str) -> None:
        """Initialize the entity."""
        super().__init__(client)
        self.client_id = client_id
        self._attr_unique_id = f"{client.entry.entry_id}_{client_id}_{key}"
        self._attr_device_info = app_device_info(client, client_id)

    @property
    def available(self) -> bool:
        """Return if the app is currently attached."""
        app = self.client.apps.get(self.client_id)
        return bool(app and app.connected)


class DGLabDeviceEntity(DGLabBaseEntity):
    """Base entity for device-level entities."""

    def __init__(
        self, client: DGLabClient, client_id: str, slot_id: str, key: str
    ) -> None:
        """Initialize the entity."""
        super().__init__(client)
        self.client_id = client_id
        self.slot_id = slot_id
        self._attr_unique_id = (
            f"{client.entry.entry_id}_{client_id}_{slot_id}_{key}"
        )

    @property
    def device(self) -> DGLabDevice | None:
        """Return the device state."""
        return self.client.device(self.client_id, self.slot_id)

    @property
    def device_info(self) -> DeviceInfo:
        """Return device registry info."""
        return device_info(self.client, self.client_id, self.slot_id)

    @property
    def available(self) -> bool:
        """Return if the device is currently available."""
        device = self.device
        app = self.client.apps.get(self.client_id)
        return bool(device and app and app.connected and not device.removed)


class DGLabChannelEntity(DGLabDeviceEntity):
    """Base entity for channel-level entities."""

    def __init__(
        self,
        client: DGLabClient,
        client_id: str,
        slot_id: str,
        channel: int,
        key: str,
    ) -> None:
        """Initialize the entity."""
        super().__init__(
            client, client_id, slot_id, f"channel_{channel_name(channel).lower()}_{key}"
        )
        self.channel = channel

    @property
    def available(self) -> bool:
        """Return if channel controls can be used."""
        if not super().available:
            return False
        device = self.device
        if device is None or not device_supports_channel(device, self.channel):
            return False
        has_device = device.slot_state.get("hasDevice")
        return has_device is not False


def hub_device_info(client: DGLabClient) -> DeviceInfo:
    """Return the controller device info."""
    info = DeviceInfo(
        identifiers={(DOMAIN, client.entry.entry_id)},
        manufacturer=MANUFACTURER,
        name=client.name,
        model="WebSocket controller",
    )
    if client.pairing_url:
        info["configuration_url"] = client.pairing_url
    return info


def app_device_info(client: DGLabClient, client_id: str) -> DeviceInfo:
    """Return an app pseudo-device info object."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"{client.entry.entry_id}_app_{client_id}")},
        manufacturer=MANUFACTURER,
        name=f"DG-LAB App {short_id(client_id)}",
        model="DG-LAB app",
        via_device=(DOMAIN, client.entry.entry_id),
    )


def device_info(client: DGLabClient, client_id: str, slot_id: str) -> DeviceInfo:
    """Return a DG-LAB device info object."""
    device = client.device(client_id, slot_id)
    return DeviceInfo(
        identifiers={(DOMAIN, f"{client.entry.entry_id}_device_{client_id}_{slot_id}")},
        manufacturer=MANUFACTURER,
        name=device.display_name if device else slot_id,
        model=device.type if device and device.type else "DG-LAB device",
        via_device=(DOMAIN, f"{client.entry.entry_id}_app_{client_id}"),
    )


def short_id(value: str) -> str:
    """Return a short display ID."""
    return value[:8] if len(value) > 8 else value


def iter_device_channels(device: DGLabDevice) -> Iterator[int]:
    """Yield supported channel indexes for a device."""
    for channel in (0, 1):
        if device_supports_channel(device, channel):
            yield channel


def iter_leaf_fields(
    data: dict[str, Any], path: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], Any]]:
    """Yield primitive leaves from a nested dictionary."""
    for key, value in data.items():
        next_path = (*path, key)
        if isinstance(value, dict):
            yield from iter_leaf_fields(value, next_path)
        else:
            yield next_path, value


def field_unique_key(source: str, path: tuple[str, ...]) -> str:
    """Return a unique key for a source/path field."""
    return f"{source}_{'_'.join(path)}"


def field_name(source: str, path: tuple[str, ...]) -> str:
    """Return a readable field name."""
    label = " ".join(_humanize(part) for part in path)
    if source == "slot_state":
        return f"Slot {label}"
    return label


def _humanize(value: str) -> str:
    """Convert camel-ish field names to a readable label."""
    chars: list[str] = []
    previous_lower = False
    for char in value.replace("_", " "):
        if previous_lower and char.isupper():
            chars.append(" ")
        chars.append(char)
        previous_lower = char.islower() or char.isdigit()
    return "".join(chars).strip().title()
