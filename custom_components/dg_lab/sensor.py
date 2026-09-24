"""Sensor platform for DG-LAB."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory

from .api import DGLabClient, nested_get
from .const import DOMAIN
from .entity import (
    DGLabAppEntity,
    DGLabDeviceEntity,
    DGLabHubEntity,
    field_name,
    field_unique_key,
    iter_leaf_fields,
    short_id,
)

MAX_STATE_LENGTH = 255


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: Callable[[list[SensorEntity]], None],
) -> None:
    """Set up DG-LAB sensors."""
    client: DGLabClient = hass.data[DOMAIN][entry.entry_id]
    seen: set[tuple[Any, ...]] = set()

    @callback
    def discover_entities() -> None:
        entities: list[SensorEntity] = []

        for cls in (
            DGLabPairingSensor,
            DGLabConnectionStateSensor,
            DGLabConnectedAppsSensor,
            DGLabLastActionSensor,
            DGLabLastErrorSensor,
        ):
            key = ("hub", cls.__name__)
            if key not in seen:
                seen.add(key)
                entities.append(cls(client))

        for client_id in client.apps:
            for cls in (DGLabAppRttSensor, DGLabAppLastSeenSensor):
                key = ("app", client_id, cls.__name__)
                if key not in seen:
                    seen.add(key)
                    entities.append(cls(client, client_id))

        for device in client.devices.values():
            raw_key = ("device", device.client_id, device.slot_id, "raw")
            if raw_key not in seen:
                seen.add(raw_key)
                entities.append(
                    DGLabRawDeviceSensor(client, device.client_id, device.slot_id)
                )

            for source, source_data in (
                ("props", device.props),
                ("slot_state", device.slot_state),
            ):
                for path, value in iter_leaf_fields(source_data):
                    if value is None or isinstance(value, bool):
                        continue
                    if not isinstance(value, (int, float, str)):
                        continue
                    key = (device.client_id, device.slot_id, source, path)
                    if key in seen:
                        continue
                    seen.add(key)
                    entities.append(
                        DGLabDeviceFieldSensor(
                            client,
                            device.client_id,
                            device.slot_id,
                            source,
                            path,
                        )
                    )

        if entities:
            async_add_entities(entities)

    discover_entities()
    entry.async_on_unload(client.async_add_listener(discover_entities))


class DGLabPairingSensor(DGLabHubEntity, SensorEntity):
    """Expose the current app pairing ID and URLs."""

    _attr_name = "Pairing ID"
    _attr_icon = "mdi:qrcode"

    def __init__(self, client: DGLabClient) -> None:
        """Initialize the sensor."""
        super().__init__(client, "pairing_id")

    @property
    def native_value(self) -> str | None:
        """Return the current target ID."""
        return self.client.target_id

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return pairing details."""
        return {
            "websocket_url": self.client.url,
            "app_websocket_url": self.client.app_websocket_url,
            "pairing_url": self.client.pairing_url,
        }


class DGLabConnectionStateSensor(DGLabHubEntity, SensorEntity):
    """Expose websocket state."""

    _attr_name = "Connection state"
    _attr_icon = "mdi:websocket"

    def __init__(self, client: DGLabClient) -> None:
        """Initialize the sensor."""
        super().__init__(client, "connection_state")

    @property
    def native_value(self) -> str:
        """Return the websocket state."""
        return self.client.state


class DGLabConnectedAppsSensor(DGLabHubEntity, SensorEntity):
    """Expose the attached app count."""

    _attr_name = "Connected apps"
    _attr_icon = "mdi:cellphone-link"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, client: DGLabClient) -> None:
        """Initialize the sensor."""
        super().__init__(client, "connected_apps")

    @property
    def native_value(self) -> int:
        """Return the attached app count."""
        return len(self.client.connected_client_ids)


class DGLabLastActionSensor(DGLabHubEntity, SensorEntity):
    """Expose the last custom app action."""

    _attr_name = "Last app action"
    _attr_icon = "mdi:gesture-tap-button"

    def __init__(self, client: DGLabClient) -> None:
        """Initialize the sensor."""
        super().__init__(client, "last_action")

    @property
    def native_value(self) -> int | None:
        """Return the last action ID."""
        return self.client.last_action.action if self.client.last_action else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return last action metadata."""
        if not self.client.last_action:
            return None
        return {
            "client_id": self.client.last_action.client_id,
            "received_at": self.client.last_action.received_at.isoformat(),
        }


class DGLabLastErrorSensor(DGLabHubEntity, SensorEntity):
    """Expose the last connection/protocol error."""

    _attr_name = "Last error"
    _attr_icon = "mdi:alert-circle-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, client: DGLabClient) -> None:
        """Initialize the sensor."""
        super().__init__(client, "last_error")

    @property
    def native_value(self) -> str | None:
        """Return the last error code."""
        return self.client.last_error.code if self.client.last_error else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return error metadata."""
        if not self.client.last_error:
            return None
        return {
            "message": self.client.last_error.message,
            "client_id": self.client.last_error.client_id,
            "received_at": self.client.last_error.received_at.isoformat(),
        }


class DGLabAppRttSensor(DGLabAppEntity, SensorEntity):
    """Expose app ping RTT."""

    _attr_name = "Round-trip time"
    _attr_icon = "mdi:timer-outline"
    _attr_native_unit_of_measurement = UnitOfTime.MILLISECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, client: DGLabClient, client_id: str) -> None:
        """Initialize the sensor."""
        super().__init__(client, client_id, "rtt")

    @property
    def native_value(self) -> float | None:
        """Return last measured RTT."""
        app = self.client.apps.get(self.client_id)
        return round(app.last_rtt_ms, 1) if app and app.last_rtt_ms is not None else None


class DGLabAppLastSeenSensor(DGLabAppEntity, SensorEntity):
    """Expose app last seen timestamp."""

    _attr_name = "Last seen"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, client: DGLabClient, client_id: str) -> None:
        """Initialize the sensor."""
        super().__init__(client, client_id, "last_seen")

    @property
    def native_value(self) -> datetime | None:
        """Return app last-seen time."""
        app = self.client.apps.get(self.client_id)
        return app.last_seen if app else None


class DGLabRawDeviceSensor(DGLabDeviceEntity, SensorEntity):
    """Expose raw device data as attributes."""

    _attr_name = "Raw data"
    _attr_icon = "mdi:code-json"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, client: DGLabClient, client_id: str, slot_id: str) -> None:
        """Initialize the sensor."""
        super().__init__(client, client_id, slot_id, "raw_data")

    @property
    def native_value(self) -> str | None:
        """Return a compact state for raw data."""
        device = self.device
        if device is None:
            return None
        if device.removed:
            return "removed"
        connect_state = device.props.get("connectState")
        if isinstance(connect_state, str):
            return connect_state
        has_device = device.slot_state.get("hasDevice")
        if has_device is False:
            return "not_connected"
        return "connected" if self.available else "unavailable"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the raw device dictionaries."""
        device = self.device
        if device is None:
            return None
        return {
            "client_id": device.client_id,
            "slot_id": device.slot_id,
            "name": device.name,
            "type": device.type,
            "removed": device.removed,
            "props": device.props,
            "slot_state": device.slot_state,
        }


class DGLabDeviceFieldSensor(DGLabDeviceEntity, SensorEntity):
    """Expose one primitive device field."""

    def __init__(
        self,
        client: DGLabClient,
        client_id: str,
        slot_id: str,
        source: str,
        path: tuple[str, ...],
    ) -> None:
        """Initialize the sensor."""
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
        self._attr_native_unit_of_measurement = _field_unit(path)
        self._attr_device_class = _field_device_class(path)
        if _is_measurement_field(path):
            self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> int | float | str | None:
        """Return the field value."""
        device = self.device
        if device is None:
            return None
        source_data = device.props if self.source == "props" else device.slot_state
        value = nested_get(source_data, self.path)
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, str) and len(value) > MAX_STATE_LENGTH:
            return f"{value[: MAX_STATE_LENGTH - 3]}..."
        return value if isinstance(value, (int, float, str)) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return field metadata."""
        device = self.device
        source_data = {}
        if device is not None:
            source_data = device.props if self.source == "props" else device.slot_state
        raw_value = nested_get(source_data, self.path)
        attrs: dict[str, Any] = {
            "client_id": self.client_id,
            "slot_id": self.slot_id,
            "source": self.source,
            "path": ".".join(self.path),
        }
        if isinstance(raw_value, str) and len(raw_value) > MAX_STATE_LENGTH:
            attrs["raw_value"] = raw_value
        return attrs


def _field_icon(path: tuple[str, ...]) -> str | None:
    """Return an icon for a DG-LAB field."""
    last = path[-1]
    if last == "power":
        return "mdi:battery"
    if last.startswith("intensity"):
        return "mdi:flash"
    if last == "pressure":
        return "mdi:gauge"
    if last == "connectState":
        return "mdi:connection"
    if last == "version":
        return "mdi:source-branch"
    if last == "label":
        return "mdi:tag-outline"
    if "Status" in last or last == "edgeState":
        return "mdi:list-status"
    if last == "mode":
        return "mdi:tune"
    return None


def _field_unit(path: tuple[str, ...]) -> str | None:
    """Return a unit for a DG-LAB field."""
    last = path[-1]
    if last == "power" or last == "overheatPercent":
        return PERCENTAGE
    return None


def _field_device_class(path: tuple[str, ...]) -> SensorDeviceClass | None:
    """Return a device class for a DG-LAB field."""
    if path[-1] == "power":
        return SensorDeviceClass.BATTERY
    return None


def _is_measurement_field(path: tuple[str, ...]) -> bool:
    """Return if a field should be long-term-statistics capable."""
    last = path[-1]
    return last in {
        "power",
        "pressure",
        "intensityA",
        "intensityB",
        "intensityMax",
        "comfortMax",
        "absoluteMax",
        "overheatPercent",
        "warmUpScale",
        "totalIncr",
        "autoIncrMax",
    }
