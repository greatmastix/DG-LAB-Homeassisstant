"""DG-LAB Home Assistant integration."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import voluptuous as vol
from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceValidationError,
    Unauthorized,
    UnknownUser,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .api import (
    DGLabClient,
    DGLabDevice,
    channel_name,
    device_is_connected,
    normalize_channel,
    normalize_frames,
)
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
    vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
    vol.Optional(ATTR_CLIENT_ID): cv.string,
}
_CHANNEL_COMMAND_SCHEMA = {
    **_APP_SCHEMA,
    vol.Optional(ATTR_SLOT_ID): cv.string,
    vol.Optional(ATTR_CHANNEL): _CHANNEL_SCHEMA,
    vol.Optional(ATTR_PRIORITY): _PRIORITY_SCHEMA,
    vol.Optional(ATTR_IMMEDIATE): cv.boolean,
    vol.Optional(ATTR_TIMEOUT): _TIMEOUT_SCHEMA,
}


@dataclass(frozen=True, slots=True)
class _ActionTarget:
    """Resolved Home Assistant action target."""

    client: DGLabClient
    client_id: str | None = None
    slot_id: str | None = None
    channel: int | None = None


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up global DG-LAB services."""
    hass.data.setdefault(DOMAIN, {})
    hass.http.register_view(DGLabWebSocketView(hass))

    async def handle_reconnect(call: ServiceCall) -> None:
        for client in _clients_from_call(hass, call):
            await client.async_reconnect()

    async def handle_request_devices(call: ServiceCall) -> None:
        await _async_check_entity_permissions(hass, call)
        for target in _app_targets_from_call(hass, call):
            assert target.client_id is not None
            await target.client.request_devices(target.client_id)

    async def handle_ping(call: ServiceCall) -> None:
        await _async_check_entity_permissions(hass, call)
        for target in _app_targets_from_call(hass, call):
            assert target.client_id is not None
            await target.client.ping_app(
                target.client_id, timeout=call.data.get(ATTR_TIMEOUT)
            )

    async def handle_add_intensity(call: ServiceCall) -> None:
        await _async_check_entity_permissions(hass, call)
        for target in _channel_targets_from_call(hass, call):
            assert target.client_id is not None
            assert target.slot_id is not None
            assert target.channel is not None
            await target.client.add_intensity(
                target.client_id,
                target.slot_id,
                target.channel,
                call.data[ATTR_VALUE],
                priority=call.data.get(ATTR_PRIORITY),
                immediate=call.data.get(ATTR_IMMEDIATE),
                timeout=call.data.get(ATTR_TIMEOUT),
            )

    async def handle_set_intensity(call: ServiceCall) -> None:
        await _async_check_entity_permissions(hass, call)
        for target in _channel_targets_from_call(hass, call):
            assert target.client_id is not None
            assert target.slot_id is not None
            assert target.channel is not None
            await target.client.set_intensity(
                target.client_id,
                target.slot_id,
                target.channel,
                call.data[ATTR_VALUE],
                priority=call.data.get(ATTR_PRIORITY),
                immediate=call.data.get(ATTR_IMMEDIATE),
                timeout=call.data.get(ATTR_TIMEOUT),
            )

    async def handle_set_temp_intensity(call: ServiceCall) -> None:
        await _async_check_entity_permissions(hass, call)
        for target in _channel_targets_from_call(hass, call):
            assert target.client_id is not None
            assert target.slot_id is not None
            assert target.channel is not None
            await target.client.set_temp_intensity(
                target.client_id,
                target.slot_id,
                target.channel,
                call.data[ATTR_VALUE],
                call.data[ATTR_DURATION_MS],
                priority=call.data.get(ATTR_PRIORITY),
                immediate=call.data.get(ATTR_IMMEDIATE),
                timeout=call.data.get(ATTR_TIMEOUT),
            )

    async def handle_send_pulse(call: ServiceCall) -> None:
        await _async_check_entity_permissions(hass, call)
        frames = normalize_frames(call.data[ATTR_FRAMES])
        for target in _channel_targets_from_call(hass, call):
            assert target.client_id is not None
            assert target.slot_id is not None
            assert target.channel is not None
            await target.client.send_pulse(
                target.client_id,
                target.slot_id,
                target.channel,
                frames,
                call.data[ATTR_DURATION_MS],
                priority=call.data.get(ATTR_PRIORITY),
                immediate=call.data.get(ATTR_IMMEDIATE),
                version=call.data.get(ATTR_VERSION),
                seq=call.data.get(ATTR_SEQ),
                timeout=call.data.get(ATTR_TIMEOUT),
            )

    async def handle_clear_operations(call: ServiceCall) -> None:
        await _async_check_entity_permissions(hass, call)
        for target in _clear_targets_from_call(hass, call):
            assert target.client_id is not None
            await target.client.clear_operations(
                target.client_id,
                slot_id=target.slot_id,
                channel=target.channel,
                timeout=call.data.get(ATTR_TIMEOUT),
            )

    async def handle_send_rpc(call: ServiceCall) -> None:
        await _async_check_entity_permissions(hass, call)
        payload = call.data[ATTR_PAYLOAD]
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            raise HomeAssistantError("payload must be a dictionary or JSON object")
        for target in _app_targets_from_call(hass, call, require_explicit=True):
            assert target.client_id is not None
            await target.client.send_rpc(
                target.client_id,
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
                vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
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
                vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
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
                    vol.Coerce(int), vol.Range(min=-200, max=200)
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
                    vol.Coerce(int), vol.Range(min=0, max=200)
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
                    vol.Coerce(int), vol.Range(min=0, max=200)
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
                vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
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
    entry.async_on_unload(
        client.async_add_listener(
            lambda: _reconcile_device_registry(hass, entry, client)
        )
    )

    _reconcile_device_registry(hass, entry, client)

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


def _reconcile_device_registry(
    hass: HomeAssistant, entry: ConfigEntry, client: DGLabClient
) -> None:
    """Remove app and physical devices that are no longer connected."""
    connected_client_ids = client.connected_client_ids
    active_identifiers = {(DOMAIN, entry.entry_id)}
    active_identifiers.update(
        (DOMAIN, f"{entry.entry_id}_app_{client_id}")
        for client_id in connected_client_ids
    )
    active_identifiers.update(
        (
            DOMAIN,
            f"{entry.entry_id}_device_{device.client_id}_{device.slot_id}",
        )
        for device in client.devices.values()
        if device.client_id in connected_client_ids and device_is_connected(device)
    )

    registry = dr.async_get(hass)
    stale_devices = [
        device
        for device in dr.async_entries_for_config_entry(registry, entry.entry_id)
        if device.identifiers.isdisjoint(active_identifiers)
    ]

    device_prefix = f"{entry.entry_id}_device_"
    app_prefix = f"{entry.entry_id}_app_"

    def removal_order(device: dr.DeviceEntry) -> int:
        """Remove physical devices before their app parent devices."""
        identifiers = {
            identifier
            for domain, identifier in device.identifiers
            if domain == DOMAIN
        }
        if any(identifier.startswith(device_prefix) for identifier in identifiers):
            return 0
        if any(identifier.startswith(app_prefix) for identifier in identifiers):
            return 2
        return 1

    for device in sorted(stale_devices, key=removal_order):
        registry.async_remove_device(device.id)


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


async def _async_check_entity_permissions(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    """Check control permission for every explicitly selected entity."""
    entity_ids = _selected_entity_ids(call)
    user_id = call.context.user_id
    if not entity_ids or user_id is None:
        return

    user = await hass.auth.async_get_user(user_id)
    if user is None:
        raise UnknownUser(context=call.context)
    for entity_id in entity_ids:
        if not user.permissions.check_entity(entity_id, POLICY_CONTROL):
            raise Unauthorized(
                context=call.context,
                entity_id=entity_id,
                permission=POLICY_CONTROL,
            )


def _app_targets_from_call(
    hass: HomeAssistant,
    call: ServiceCall,
    *,
    require_explicit: bool = False,
) -> list[_ActionTarget]:
    """Resolve one or more app targets from entities or legacy protocol IDs."""
    if _selected_entity_ids(call):
        _reject_mixed_targeting(call, (ATTR_CLIENT_ID,))
        targets = _entity_targets_from_call(hass, call)
        expanded: list[_ActionTarget] = []
        for target in targets:
            if target.client_id is not None:
                expanded.append(_ActionTarget(target.client, target.client_id))
                continue
            expanded.extend(
                _ActionTarget(target.client, client_id)
                for client_id in sorted(target.client.connected_client_ids)
            )
        if not expanded:
            raise ServiceValidationError("No DG-LAB apps are currently connected")
        return _deduplicate_targets(expanded)

    client = _client_from_call(hass, call)
    client_id = call.data.get(ATTR_CLIENT_ID)
    if require_explicit and client_id is None:
        raise ServiceValidationError(
            "Select an app or device entity, or provide an app client ID"
        )
    client_ids = [client_id] if client_id else sorted(client.connected_client_ids)
    if not client_ids:
        raise ServiceValidationError("No DG-LAB apps are currently connected")
    return [_ActionTarget(client, target_id) for target_id in client_ids]


def _channel_targets_from_call(
    hass: HomeAssistant, call: ServiceCall
) -> list[_ActionTarget]:
    """Resolve one or more channel targets from entities or protocol IDs."""
    if _selected_entity_ids(call):
        _reject_mixed_targeting(
            call, (ATTR_CLIENT_ID, ATTR_SLOT_ID, ATTR_CHANNEL)
        )
        targets = _entity_targets_from_call(hass, call)
        if any(
            target.client_id is None
            or target.slot_id is None
            or target.channel is None
            for target in targets
        ):
            raise ServiceValidationError(
                "Select a DG-LAB channel entity, such as an intensity number"
            )
        return _deduplicate_targets(targets)

    missing = [
        field
        for field in (ATTR_CLIENT_ID, ATTR_SLOT_ID, ATTR_CHANNEL)
        if field not in call.data
    ]
    if missing:
        raise ServiceValidationError(
            "Select a channel entity or provide advanced target fields: "
            + ", ".join(missing)
        )
    client = _client_from_call(hass, call)
    return [
        _ActionTarget(
            client,
            call.data[ATTR_CLIENT_ID],
            call.data[ATTR_SLOT_ID],
            normalize_channel(call.data[ATTR_CHANNEL]),
        )
    ]


def _clear_targets_from_call(
    hass: HomeAssistant, call: ServiceCall
) -> list[_ActionTarget]:
    """Resolve clear-operation targets at hub, app, device, or channel level."""
    if _selected_entity_ids(call):
        _reject_mixed_targeting(
            call, (ATTR_CLIENT_ID, ATTR_SLOT_ID, ATTR_CHANNEL)
        )
        expanded: list[_ActionTarget] = []
        for target in _entity_targets_from_call(hass, call):
            if target.client_id is not None:
                expanded.append(target)
                continue
            expanded.extend(
                _ActionTarget(target.client, client_id)
                for client_id in sorted(target.client.connected_client_ids)
            )
        if not expanded:
            raise ServiceValidationError("No DG-LAB apps are currently connected")
        return _deduplicate_targets(expanded)

    client = _client_from_call(hass, call)
    client_id = call.data.get(ATTR_CLIENT_ID)
    client_ids = [client_id] if client_id else sorted(client.connected_client_ids)
    if not client_ids:
        raise ServiceValidationError("No DG-LAB apps are currently connected")

    slot_id = call.data.get(ATTR_SLOT_ID)
    channel = call.data.get(ATTR_CHANNEL)
    if channel is not None and slot_id is None:
        raise ServiceValidationError("slot_id is required when channel is provided")
    normalized_channel = normalize_channel(channel) if channel is not None else None
    return [
        _ActionTarget(client, target_id, slot_id, normalized_channel)
        for target_id in client_ids
    ]


def _entity_targets_from_call(
    hass: HomeAssistant, call: ServiceCall
) -> list[_ActionTarget]:
    """Resolve registry entities to their DG-LAB hierarchy coordinates."""
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    clients: dict[str, DGLabClient] = hass.data.get(DOMAIN, {})
    requested_entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
    targets: list[_ActionTarget] = []

    for entity_id in _selected_entity_ids(call):
        entity_entry = entity_registry.async_get(entity_id)
        if entity_entry is None or entity_entry.platform != DOMAIN:
            raise ServiceValidationError(
                f"{entity_id} is not a registered DG-LAB entity"
            )
        if (
            requested_entry_id is not None
            and entity_entry.config_entry_id != requested_entry_id
        ):
            raise ServiceValidationError(
                f"{entity_id} does not belong to config entry {requested_entry_id}"
            )

        client = clients.get(entity_entry.config_entry_id)
        if client is None:
            raise ServiceValidationError(
                f"The DG-LAB config entry for {entity_id} is not loaded"
            )
        if entity_entry.device_id is None:
            raise ServiceValidationError(f"{entity_id} has no DG-LAB device")
        device_entry = device_registry.async_get(entity_entry.device_id)
        if device_entry is None:
            raise ServiceValidationError(f"The device for {entity_id} no longer exists")

        targets.append(_target_from_registry_entry(client, entity_entry, device_entry))

    return targets


def _target_from_registry_entry(
    client: DGLabClient,
    entity_entry: er.RegistryEntry,
    device_entry: dr.DeviceEntry,
) -> _ActionTarget:
    """Resolve one registry entity to a hub, app, device, or channel target."""
    entry_id = client.entry.entry_id
    identifiers = device_entry.identifiers

    for device in client.devices.values():
        identifier = (
            DOMAIN,
            f"{entry_id}_device_{device.client_id}_{device.slot_id}",
        )
        if identifier not in identifiers:
            continue
        channel = _channel_from_unique_id(entity_entry.unique_id, client, device)
        return _ActionTarget(
            client,
            device.client_id,
            device.slot_id,
            channel,
        )

    for client_id in client.apps:
        if (DOMAIN, f"{entry_id}_app_{client_id}") in identifiers:
            return _ActionTarget(client, client_id)

    if (DOMAIN, entry_id) in identifiers:
        return _ActionTarget(client)

    raise ServiceValidationError(
        f"{entity_entry.entity_id} is not attached to a live DG-LAB target"
    )


def _channel_from_unique_id(
    unique_id: str, client: DGLabClient, device: DGLabDevice
) -> int | None:
    """Extract a channel from a known channel entity unique ID."""
    for channel in (0, 1):
        prefix = (
            f"{client.entry.entry_id}_{device.client_id}_{device.slot_id}_"
            f"channel_{channel_name(channel).lower()}_"
        )
        if unique_id.startswith(prefix):
            return channel
    return None


def _selected_entity_ids(call: ServiceCall) -> list[str]:
    """Return normalized entity IDs selected in an action call."""
    value = call.data.get(ATTR_ENTITY_ID, [])
    if isinstance(value, str):
        return [value]
    return list(value)


def _reject_mixed_targeting(call: ServiceCall, raw_fields: tuple[str, ...]) -> None:
    """Reject combining entity targets with raw protocol coordinates."""
    mixed = [field for field in raw_fields if field in call.data]
    if mixed:
        raise ServiceValidationError(
            "Do not combine entity targets with advanced target fields: "
            + ", ".join(mixed)
        )


def _deduplicate_targets(targets: Iterable[_ActionTarget]) -> list[_ActionTarget]:
    """Preserve target order while removing repeated hierarchy coordinates."""
    result: list[_ActionTarget] = []
    seen: set[tuple[str, str | None, str | None, int | None]] = set()
    for target in targets:
        key = (
            target.client.entry.entry_id,
            target.client_id,
            target.slot_id,
            target.channel,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(target)
    return result
