"""Binary sensor platform for DG-LAB."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback

from .api import DGLabClient, nested_get
from .const import DOMAIN
from .entity import (
    DGLabAppEntity,
    DGLabDeviceEntity,
    DGLabHubEntity,
    field_name,
    field_unique_key,
    iter_connected_devices,
    iter_leaf_fields,
    remove_stale_entities,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: Callable[[list[BinarySensorEntity]], None],
) -> None:
    """Set up DG-LAB binary sensors."""
    client: DGLabClient = hass.data[DOMAIN][entry.entry_id]
    seen: dict[tuple[Any, ...], str] = {}

    @callback
    def discover_entities() -> None:
        entities: list[BinarySensorEntity] = []
        current: set[tuple[Any, ...]] = set()

        for cls in (DGLabSocketConnectedBinarySensor, DGLabPairedBinarySensor):
            key = ("hub", cls.__name__)
            current.add(key)
            if key not in seen:
                entity = cls(client)
                assert entity.unique_id is not None
                seen[key] = entity.unique_id
                entities.append(entity)

        for client_id in client.apps:
            key = ("app", client_id, "connected")
            current.add(key)
            if key not in seen:
                entity = DGLabAppConnectedBinarySensor(client, client_id)
                assert entity.unique_id is not None
                seen[key] = entity.unique_id
                entities.append(entity)

        for device in iter_connected_devices(client):
            for source, source_data in (
                ("props", device.props),
                ("slot_state", device.slot_state),
            ):
                for path, value in iter_leaf_fields(source_data):
                    if not isinstance(value, bool):
                        continue
                    key = (device.client_id, device.slot_id, source, path)
                    current.add(key)
                    if key in seen:
                        continue
                    entity = DGLabDeviceBoolBinarySensor(
                        client,
                        device.client_id,
                        device.slot_id,
                        source,
                        path,
                    )
                    assert entity.unique_id is not None
                    seen[key] = entity.unique_id
                    entities.append(entity)

        remove_stale_entities(hass, Platform.BINARY_SENSOR, seen, current)
        if entities:
            async_add_entities(entities)

    discover_entities()
    entry.async_on_unload(client.async_add_listener(discover_entities))


class DGLabSocketConnectedBinarySensor(DGLabHubEntity, BinarySensorEntity):
    """Expose relay websocket connection."""

    _attr_name = "Socket connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, client: DGLabClient) -> None:
        """Initialize the binary sensor."""
        super().__init__(client, "socket_connected")

    @property
    def is_on(self) -> bool:
        """Return if the websocket is connected."""
        return self.client.connected


class DGLabPairedBinarySensor(DGLabHubEntity, BinarySensorEntity):
    """Expose whether at least one app is attached."""

    _attr_name = "Paired"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, client: DGLabClient) -> None:
        """Initialize the binary sensor."""
        super().__init__(client, "paired")

    @property
    def is_on(self) -> bool:
        """Return if an app is attached."""
        return bool(self.client.connected_client_ids)


class DGLabAppConnectedBinarySensor(DGLabAppEntity, BinarySensorEntity):
    """Expose one app connection."""

    _attr_name = "Connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, client: DGLabClient, client_id: str) -> None:
        """Initialize the binary sensor."""
        super().__init__(client, client_id, "connected")

    @property
    def is_on(self) -> bool:
        """Return app connected state."""
        app = self.client.apps.get(self.client_id)
        return bool(app and app.connected)


class DGLabDeviceBoolBinarySensor(DGLabDeviceEntity, BinarySensorEntity):
    """Expose one boolean device field."""

    def __init__(
        self,
        client: DGLabClient,
        client_id: str,
        slot_id: str,
        source: str,
        path: tuple[str, ...],
    ) -> None:
        """Initialize the binary sensor."""
        self.source = source
        self.path = path
        super().__init__(
            client,
            client_id,
            slot_id,
            field_unique_key(source, path),
        )
        self._attr_name = field_name(source, path)
        self._attr_icon = _field_icon(path)
        self._attr_device_class = _field_device_class(path)

    @property
    def is_on(self) -> bool | None:
        """Return the boolean field value."""
        device = self.device
        if device is None:
            return None
        source_data = device.props if self.source == "props" else device.slot_state
        value = nested_get(source_data, self.path)
        return value if isinstance(value, bool) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return field metadata."""
        return {
            "client_id": self.client_id,
            "slot_id": self.slot_id,
            "source": self.source,
            "path": ".".join(self.path),
        }


def _field_icon(path: tuple[str, ...]) -> str | None:
    """Return an icon for a boolean field."""
    last = path[-1]
    if last == "isMuted":
        return "mdi:volume-off"
    if last == "autoIncr":
        return "mdi:trending-up"
    return None


def _field_device_class(path: tuple[str, ...]) -> BinarySensorDeviceClass | None:
    """Return a device class for a boolean field."""
    last = path[-1]
    if last == "hasDevice":
        return BinarySensorDeviceClass.CONNECTIVITY
    if last == "overheat":
        return BinarySensorDeviceClass.PROBLEM
    return None
