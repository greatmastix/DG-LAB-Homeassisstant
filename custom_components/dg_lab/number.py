"""Number platform for DG-LAB controls."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant, callback

from .api import DGLabClient, channel_name
from .const import DOMAIN
from .entity import DGLabChannelEntity, iter_device_channels


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: Callable[[list[NumberEntity]], None],
) -> None:
    """Set up DG-LAB number entities."""
    client: DGLabClient = hass.data[DOMAIN][entry.entry_id]
    seen: set[tuple[Any, ...]] = set()

    @callback
    def discover_entities() -> None:
        entities: list[NumberEntity] = []
        for device in client.devices.values():
            for channel in iter_device_channels(device):
                for cls in (
                    DGLabIntensityNumber,
                    DGLabStepNumber,
                    DGLabTempIntensityNumber,
                    DGLabTempDurationNumber,
                    DGLabPulseDurationNumber,
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


class DGLabIntensityNumber(DGLabChannelEntity, NumberEntity):
    """Control current channel intensity using protocol deltas."""

    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0
    _attr_native_step = 1
    _attr_icon = "mdi:flash"

    def __init__(
        self, client: DGLabClient, client_id: str, slot_id: str, channel: int
    ) -> None:
        """Initialize the number."""
        super().__init__(client, client_id, slot_id, channel, "intensity")
        self._attr_name = f"{channel_name(channel)} intensity"

    @property
    def native_value(self) -> float | None:
        """Return current channel intensity."""
        device = self.device
        if device is None:
            return None
        return self.client.channel_intensity(device, self.channel)

    @property
    def native_max_value(self) -> float:
        """Return current channel maximum."""
        device = self.device
        return (
            self.client.channel_max_intensity(device, self.channel)
            if device
            else float(self.client.default_max_intensity)
        )

    async def async_set_native_value(self, value: float) -> None:
        """Set channel intensity."""
        await self.client.set_intensity(
            self.client_id,
            self.slot_id,
            self.channel,
            value,
        )


class DGLabStoredChannelNumber(DGLabChannelEntity, RestoreNumber):
    """Base class for stored channel helper numbers."""

    _attr_mode = NumberMode.BOX
    _attr_native_step = 1

    setting_name: str

    async def async_added_to_hass(self) -> None:
        """Restore stored helper state."""
        await super().async_added_to_hass()
        last_data = await self.async_get_last_number_data()
        if last_data is None or last_data.native_value is None:
            return
        self._write_setting(float(last_data.native_value))

    @property
    def native_value(self) -> float:
        """Return the stored value."""
        setting = self.client.channel_setting(self.client_id, self.slot_id, self.channel)
        return float(getattr(setting, self.setting_name))

    async def async_set_native_value(self, value: float) -> None:
        """Store the helper value."""
        self._write_setting(value)
        self.async_write_ha_state()

    def _write_setting(self, value: float) -> None:
        """Write the helper value."""
        setattr(
            self.client.channel_setting(self.client_id, self.slot_id, self.channel),
            self.setting_name,
            self._coerce_setting(value),
        )

    def _coerce_setting(self, value: float) -> int | float:
        """Coerce a stored setting value."""
        return value


class DGLabStepNumber(DGLabStoredChannelNumber):
    """Set the button intensity step for a channel."""

    setting_name = "step"
    _attr_native_min_value = 1
    _attr_icon = "mdi:plus-minus"

    def __init__(
        self, client: DGLabClient, client_id: str, slot_id: str, channel: int
    ) -> None:
        """Initialize the number."""
        super().__init__(client, client_id, slot_id, channel, "step")
        self._attr_name = f"{channel_name(channel)} intensity step"

    @property
    def native_max_value(self) -> float:
        """Return a useful max step."""
        device = self.device
        return (
            self.client.channel_max_intensity(device, self.channel)
            if device
            else float(self.client.default_max_intensity)
        )

    def _coerce_setting(self, value: float) -> int:
        """Coerce step to an integer."""
        return max(1, int(round(value)))


class DGLabTempIntensityNumber(DGLabStoredChannelNumber):
    """Set the temporary intensity value for a channel."""

    setting_name = "temp_intensity"
    _attr_native_min_value = 0
    _attr_mode = NumberMode.SLIDER
    _attr_icon = "mdi:timer-flash-outline"

    def __init__(
        self, client: DGLabClient, client_id: str, slot_id: str, channel: int
    ) -> None:
        """Initialize the number."""
        super().__init__(client, client_id, slot_id, channel, "temp_intensity")
        self._attr_name = f"{channel_name(channel)} temporary intensity"

    @property
    def native_max_value(self) -> float:
        """Return channel max intensity."""
        device = self.device
        return (
            self.client.channel_max_intensity(device, self.channel)
            if device
            else float(self.client.default_max_intensity)
        )


class DGLabTempDurationNumber(DGLabStoredChannelNumber):
    """Set the temporary intensity duration."""

    setting_name = "temp_duration_ms"
    _attr_native_min_value = 0.1
    _attr_native_max_value = 3600
    _attr_native_step = 0.1
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_icon = "mdi:timer-outline"

    def __init__(
        self, client: DGLabClient, client_id: str, slot_id: str, channel: int
    ) -> None:
        """Initialize the number."""
        super().__init__(client, client_id, slot_id, channel, "temp_duration")
        self._attr_name = f"{channel_name(channel)} temporary duration"

    @property
    def native_value(self) -> float:
        """Return duration in seconds."""
        setting = self.client.channel_setting(self.client_id, self.slot_id, self.channel)
        return setting.temp_duration_ms / 1000

    def _coerce_setting(self, value: float) -> int:
        """Coerce seconds to milliseconds."""
        return max(1, int(round(value * 1000)))


class DGLabPulseDurationNumber(DGLabStoredChannelNumber):
    """Set the send-pulse duration."""

    setting_name = "pulse_duration_ms"
    _attr_native_min_value = 0.1
    _attr_native_max_value = 3600
    _attr_native_step = 0.1
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_icon = "mdi:sine-wave"

    def __init__(
        self, client: DGLabClient, client_id: str, slot_id: str, channel: int
    ) -> None:
        """Initialize the number."""
        super().__init__(client, client_id, slot_id, channel, "pulse_duration")
        self._attr_name = f"{channel_name(channel)} pulse duration"

    @property
    def native_value(self) -> float:
        """Return duration in seconds."""
        setting = self.client.channel_setting(self.client_id, self.slot_id, self.channel)
        return setting.pulse_duration_ms / 1000

    def _coerce_setting(self, value: float) -> int:
        """Coerce seconds to milliseconds."""
        return max(1, int(round(value * 1000)))
