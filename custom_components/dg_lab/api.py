"""DG-LAB V4 websocket client."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import secrets
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

from aiohttp import ClientError, ClientWebSocketResponse, WSMsgType, web
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .const import (
    ADD_INTENSITY,
    APPEND_PULSE_DATA,
    ATTR_CHANNEL,
    CHANNEL_NAMES,
    CHANNELS,
    CONF_AUTO_RECONNECT,
    CONF_COMMAND_STEP,
    CONF_CONNECT_TIMEOUT,
    CONF_CONNECTION_ATTEMPTS_PER_MINUTE,
    CONF_CONNECTION_MODE,
    CONF_EMULATED_OPOSSUM,
    CONF_HA_URL,
    CONF_MAX_APP_CONNECTIONS,
    CONF_MAX_INTENSITY,
    CONF_MESSAGES_PER_SECOND,
    CONF_RECONNECT_DELAY,
    CONF_RESPONSE_TIMEOUT,
    CONF_URL,
    CONTROL_CAPABLE_DEVICE_TYPES,
    DEFAULT_AUTO_RECONNECT,
    DEFAULT_COMMAND_STEP,
    DEFAULT_CONNECTION_ATTEMPTS_PER_MINUTE,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_EMULATED_OPOSSUM,
    DEFAULT_MAX_APP_CONNECTIONS,
    DEFAULT_MAX_INTENSITY,
    DEFAULT_MESSAGES_PER_SECOND,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_RESPONSE_TIMEOUT,
    DEFAULT_WS_URL,
    DEVICE_TYPE_BMTR,
    DEVICE_TYPE_OVC,
    EVENT_CLIENT_ATTACHED,
    EVENT_CLIENT_DISCONNECTED,
    EVENT_CUSTOM_ACTION,
    EVENT_DEVICE_DISCOVERED,
    EVENT_ERROR,
    LOCAL_WS_PATH,
    MODE_LOCAL,
    MODE_RELAY,
    PAIRING_PAGE_URL,
    SET_INTENSITY,
    SET_TEMP_INTENSITY,
    friendly_device_name,
)
from .emulator import DGLabOpossumEmulator

_LOGGER = logging.getLogger(__name__)

SOCKET_IDLE = "idle"
SOCKET_CONNECTING = "connecting"
SOCKET_WAITING_FOR_PEER = "waiting_for_peer"
SOCKET_PAIRED = "paired"
SOCKET_DISCONNECTED = "disconnected"

SERVER_PING_INTERVAL = 2
MAX_MISSED_SERVER_PONGS = 3
MESSAGE_SIZE_WARNING = 1950
LOCAL_RATE_LIMIT_WINDOW = 1.0
LOCAL_CONNECTION_LIMIT_WINDOW = 60.0
MAX_TRACKED_CONNECTION_SOURCES = 1024


class DGLabError(HomeAssistantError):
    """Base DG-LAB error."""


class DGLabNotConnectedError(DGLabError):
    """Raised when a command requires an open websocket."""


class DGLabResponseError(DGLabError):
    """Raised when the app rejects an RPC request."""


class DGLabTimeoutError(DGLabError):
    """Raised when a command response times out."""


@dataclass(slots=True)
class DGLabLastError:
    """Last websocket or protocol error."""

    code: str
    message: str
    received_at: datetime
    client_id: str | None = None


@dataclass(slots=True)
class DGLabAction:
    """Custom action emitted by the DG-LAB app."""

    action: int
    client_id: str
    received_at: datetime


@dataclass(slots=True)
class DGLabApp:
    """State for a paired DG-LAB app connection."""

    client_id: str
    connected: bool = False
    last_seen: datetime | None = None
    last_rtt_ms: float | None = None
    last_app_timestamp: int | float | None = None


@dataclass(slots=True)
class DGLabDevice:
    """State for a device exposed by a DG-LAB app."""

    client_id: str
    slot_id: str
    name: str | None = None
    type: str | None = None
    props: dict[str, Any] = field(default_factory=dict)
    slot_state: dict[str, Any] = field(default_factory=dict)
    edge_count: int = 0
    last_edge_state: int | None = None
    removed: bool = False
    last_seen: datetime | None = None

    @property
    def key(self) -> tuple[str, str]:
        """Return the stable in-memory key."""
        return (self.client_id, self.slot_id)

    @property
    def display_name(self) -> str:
        """Return a friendly name."""
        return friendly_device_name(self.type, self.name) or self.type or self.slot_id


@dataclass(slots=True)
class DGLabChannelSettings:
    """User-facing helper values for channel entity controls."""

    step: int
    temp_intensity: float = 10
    temp_duration_ms: int = 3000
    pulse_duration_ms: int = 1000
    pulse_frames: str = ""


def entry_value(entry: ConfigEntry, key: str, default: Any) -> Any:
    """Return an option value with config-entry data fallback."""
    return entry.options.get(key, entry.data.get(key, default))


def normalize_channel(channel: str | int) -> int:
    """Convert channel names accepted by services/entities to protocol values."""
    if isinstance(channel, int) and channel in CHANNEL_NAMES:
        return channel
    if isinstance(channel, str):
        channel = channel.strip().upper()
        if channel in CHANNELS:
            return CHANNELS[channel]
        if channel.isdigit() and int(channel) in CHANNEL_NAMES:
            return int(channel)
    raise HomeAssistantError(f"Unsupported DG-LAB channel: {channel!r}")


def channel_name(channel: int) -> str:
    """Return the display name for a channel."""
    return CHANNEL_NAMES.get(channel, str(channel))


def channel_prop_name(channel: int) -> str:
    """Return the props field used for the channel intensity."""
    return f"intensity{channel_name(channel)}"


def channel_state_name(channel: int) -> str:
    """Return the slotState field used for the channel state."""
    return f"channel{channel_name(channel)}"


def device_supports_channel(device: DGLabDevice, channel: int) -> bool:
    """Return whether a device has protocol controls for a channel."""
    if device.type in CONTROL_CAPABLE_DEVICE_TYPES:
        return True
    prop = channel_prop_name(channel)
    if prop in device.props:
        return True
    state = device.slot_state.get(channel_state_name(channel))
    return isinstance(state, dict)


def device_is_connected(device: DGLabDevice) -> bool:
    """Return whether a discovered slot currently has a physical device."""
    return not device.removed and device.slot_state.get("hasDevice") is not False


def channel_is_available(device: DGLabDevice, channel: int) -> bool:
    """Return whether a device channel is present and ready for control."""
    if not device_is_connected(device) or not device_supports_channel(device, channel):
        return False

    # Opossum reports whether an accessory is plugged into each channel. Other
    # device families use similarly named values for different purposes, so the
    # check must remain model-specific and boolean-specific.
    if device.type == DEVICE_TYPE_OVC:
        status = device.props.get(f"channel{channel_name(channel)}Status")
        if status is False:
            return False
    return True


def nested_get(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    """Read a nested dictionary path."""
    current: Any = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def update_civet_edge_count(device: DGLabDevice) -> None:
    """Update the derived Civet session edge count from its state machine."""
    if device.type != DEVICE_TYPE_BMTR:
        device.edge_count = 0
        device.last_edge_state = None
        return

    edge_state = nested_get(device.slot_state, ("edge", "edgeState"))
    if type(edge_state) is not int or edge_state not in range(5):
        device.last_edge_state = None
        return

    if edge_state == 0:
        device.edge_count = 0
    elif device.last_edge_state == 1 and edge_state in (2, 3):
        # State 2 starts forced cooldown. Accept a direct transition to state 3
        # as well because the app may coalesce adjacent state updates.
        device.edge_count += 1

    device.last_edge_state = edge_state


def merge_patch(current: Any, patch: Any) -> Any:
    """Recursively merge DG-LAB patch objects."""
    if not isinstance(current, dict) or not isinstance(patch, dict):
        return patch

    merged = dict(current)
    for key, value in patch.items():
        merged[key] = merge_patch(merged.get(key), value)
    return merged


def _rate_limit_retry_after(
    events: deque[float], now: float, window: float, limit: int
) -> float | None:
    """Record one event or return the delay when its rolling window is full."""
    cutoff = now - window
    while events and events[0] <= cutoff:
        events.popleft()
    if len(events) >= limit:
        return max(events[0] + window - now, 0.001)
    events.append(now)
    return None


def normalize_frames(value: Any) -> list[Any]:
    """Normalize Home Assistant service/text pulse frame input."""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise HomeAssistantError("Pulse frames cannot be empty")
        if text.startswith("["):
            decoded = json.loads(text)
            if not isinstance(decoded, list):
                raise HomeAssistantError("Pulse frame JSON must be a list")
            return decoded
        return [
            item.strip() for item in text.replace("\n", ",").split(",") if item.strip()
        ]
    if isinstance(value, list):
        return value
    raise HomeAssistantError("Pulse frames must be a list, JSON list, or comma-separated text")


def normalize_intensity(value: float) -> int:
    """Validate a whole-number intensity in the protocol's absolute range."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HomeAssistantError("Intensity must be a number")
    numeric = float(value)
    if not math.isfinite(numeric) or not numeric.is_integer():
        raise HomeAssistantError("Intensity must be a whole number")
    intensity = int(numeric)
    if not -200 <= intensity <= 200:
        raise HomeAssistantError("Intensity must be between -200 and 200")
    return intensity


class DGLabClient:
    """Async DG-LAB V4 websocket controller."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the client."""
        self.hass = hass
        self.entry = entry
        self.name: str = entry_value(entry, CONF_NAME, entry.title)
        self.url: str = entry_value(entry, CONF_URL, DEFAULT_WS_URL)
        self.mode: str = entry_value(entry, CONF_CONNECTION_MODE, MODE_RELAY)
        self.ha_url: str = entry_value(entry, CONF_HA_URL, "")
        self.connect_timeout: int = entry_value(
            entry, CONF_CONNECT_TIMEOUT, DEFAULT_CONNECT_TIMEOUT
        )
        self.response_timeout: int = entry_value(
            entry, CONF_RESPONSE_TIMEOUT, DEFAULT_RESPONSE_TIMEOUT
        )
        self.reconnect_delay: int = entry_value(
            entry, CONF_RECONNECT_DELAY, DEFAULT_RECONNECT_DELAY
        )
        self.command_step: int = entry_value(
            entry, CONF_COMMAND_STEP, DEFAULT_COMMAND_STEP
        )
        self.default_max_intensity: int = entry_value(
            entry, CONF_MAX_INTENSITY, DEFAULT_MAX_INTENSITY
        )
        self.connection_attempts_per_minute: int = entry_value(
            entry,
            CONF_CONNECTION_ATTEMPTS_PER_MINUTE,
            DEFAULT_CONNECTION_ATTEMPTS_PER_MINUTE,
        )
        self.messages_per_second: int = entry_value(
            entry, CONF_MESSAGES_PER_SECOND, DEFAULT_MESSAGES_PER_SECOND
        )
        self.max_app_connections: int = entry_value(
            entry, CONF_MAX_APP_CONNECTIONS, DEFAULT_MAX_APP_CONNECTIONS
        )
        self.auto_reconnect: bool = entry_value(
            entry, CONF_AUTO_RECONNECT, DEFAULT_AUTO_RECONNECT
        )
        self.emulated_opossum_enabled: bool = entry_value(
            entry, CONF_EMULATED_OPOSSUM, DEFAULT_EMULATED_OPOSSUM
        )

        self.state = SOCKET_IDLE
        self.target_id: str | None = None
        self.last_error: DGLabLastError | None = None
        self.last_action: DGLabAction | None = None
        self.last_server_heartbeat: datetime | None = None
        self.last_server_pong: datetime | None = None

        self.apps: dict[str, DGLabApp] = {}
        self.devices: dict[tuple[str, str], DGLabDevice] = {}
        self.channel_settings: dict[tuple[str, str, int], DGLabChannelSettings] = {}

        self._listeners: set[Callable[[], None]] = set()
        self._runner_task: asyncio.Task[None] | None = None
        self._ping_task: asyncio.Task[None] | None = None
        self._closing = False
        self._ws: ClientWebSocketResponse | None = None
        self._local_peers: dict[str, web.WebSocketResponse] = {}
        self._pending_local_peers = 0
        self._local_connection_attempts: OrderedDict[str, deque[float]] = (
            OrderedDict()
        )
        self._local_message_events: dict[str, deque[float]] = {}
        self._emulated_peers: dict[
            str, Callable[[dict[str, Any]], Awaitable[None]]
        ] = {}
        self._emulator = (
            DGLabOpossumEmulator(self) if self.emulated_opossum_enabled else None
        )
        self._missed_server_pongs = 0
        self._request_id_counter = 0
        self._pending: dict[tuple[str, str], asyncio.Future[Any]] = {}

    @property
    def connected(self) -> bool:
        """Return whether the selected V4 transport is ready."""
        if self.mode == MODE_LOCAL:
            return bool(self.target_id and not self._closing)
        return (
            self._ws is not None and not self._ws.closed and self.target_id is not None
        )

    @property
    def connected_client_ids(self) -> set[str]:
        """Return currently attached app client IDs."""
        return {client_id for client_id, app in self.apps.items() if app.connected}

    def is_emulated_client(self, client_id: str) -> bool:
        """Return whether a client ID belongs to the built-in emulator."""
        return bool(self._emulator and self._emulator.owns_client(client_id))

    @property
    def app_websocket_url(self) -> str | None:
        """Return the app-facing websocket URL containing the current target ID."""
        websocket_url = self.websocket_url
        if not self.target_id or not websocket_url:
            return None

        parsed = urlparse(websocket_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["tid"] = self.target_id
        return urlunparse(parsed._replace(query=urlencode(query)))

    @property
    def websocket_url(self) -> str | None:
        """Return the V4 endpoint used by the app."""
        if self.mode == MODE_RELAY:
            return self.url
        if not self.ha_url:
            return None
        parsed = urlparse(self.ha_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        path = LOCAL_WS_PATH.format(entry_id=self.entry.entry_id)
        return urlunparse(parsed._replace(scheme=scheme, path=path))

    @property
    def pairing_url(self) -> str | None:
        """Return the DG-LAB app pairing URL."""
        app_url = self.app_websocket_url
        if not app_url:
            return None
        return f"{PAIRING_PAGE_URL}?v=1&action=socket&url={quote(app_url, safe='')}"

    def device(self, client_id: str, slot_id: str) -> DGLabDevice | None:
        """Return a known device."""
        return self.devices.get((client_id, slot_id))

    def channel_setting(
        self, client_id: str, slot_id: str, channel: int
    ) -> DGLabChannelSettings:
        """Return mutable channel helper settings."""
        key = (client_id, slot_id, channel)
        if key not in self.channel_settings:
            self.channel_settings[key] = DGLabChannelSettings(step=self.command_step)
        return self.channel_settings[key]

    def local_connection_retry_after(self, remote: str | None) -> float | None:
        """Rate-limit failed local endpoint authentication by source address."""
        source = remote or "unknown"
        events = self._local_connection_attempts.get(source)
        if events is None:
            if len(self._local_connection_attempts) >= MAX_TRACKED_CONNECTION_SOURCES:
                self._local_connection_attempts.popitem(last=False)
            events = deque()
            self._local_connection_attempts[source] = events
        else:
            self._local_connection_attempts.move_to_end(source)
        return _rate_limit_retry_after(
            events,
            time.monotonic(),
            LOCAL_CONNECTION_LIMIT_WINDOW,
            self.connection_attempts_per_minute,
        )

    def try_reserve_local_peer(self) -> bool:
        """Reserve capacity for one authenticated local app connection."""
        if (
            len(self._local_peers) + self._pending_local_peers
            >= self.max_app_connections
        ):
            return False
        self._pending_local_peers += 1
        return True

    def release_local_peer_reservation(self) -> None:
        """Release a pending local app connection reservation."""
        self._pending_local_peers = max(0, self._pending_local_peers - 1)

    def local_message_rate_limited(self, client_id: str) -> bool:
        """Return whether an attached app exceeded its inbound message limit."""
        events = self._local_message_events.setdefault(client_id, deque())
        return (
            _rate_limit_retry_after(
                events,
                time.monotonic(),
                LOCAL_RATE_LIMIT_WINDOW,
                self.messages_per_second,
            )
            is not None
        )

    def channel_intensity(self, device: DGLabDevice, channel: int) -> float | None:
        """Return the current channel intensity if the app exposes it."""
        value = device.props.get(channel_prop_name(channel))
        if (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value)
        ):
            return float(value)
        return None

    def channel_max_intensity(self, device: DGLabDevice, channel: int) -> float:
        """Return the effective channel maximum within the configured safety cap."""
        channel_state = device.slot_state.get(channel_state_name(channel))
        candidates: list[Any] = []
        if isinstance(channel_state, dict):
            candidates.append(channel_state.get("intensityMax"))
            comfort = channel_state.get("comfortLimit")
            if isinstance(comfort, dict):
                candidates.extend(
                    [
                        comfort.get("comfortMax"),
                        comfort.get("absoluteMax"),
                    ]
                )

        for candidate in candidates:
            if (
                not isinstance(candidate, bool)
                and isinstance(candidate, (int, float))
                and candidate > 0
            ):
                return min(float(candidate), float(self.default_max_intensity), 200.0)
        return min(float(self.default_max_intensity), 200.0)

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Add a state listener."""
        self._listeners.add(listener)

        @callback
        def remove_listener() -> None:
            self._listeners.discard(listener)

        return remove_listener

    @callback
    def _notify(self) -> None:
        """Notify listeners."""
        for listener in list(self._listeners):
            listener()

    async def async_start(self) -> None:
        """Start the websocket client."""
        if self.mode == MODE_LOCAL:
            self._closing = False
            self.target_id = secrets.token_urlsafe(24)
            self.last_error = None
            self._set_state(SOCKET_WAITING_FOR_PEER)
            self._notify()
            if self._emulator is not None:
                await self._emulator.async_start_local()
            return
        if self._runner_task and not self._runner_task.done():
            return
        self._closing = False
        self._runner_task = self.hass.async_create_task(
            self._run(), name=f"dg_lab_ws_{self.entry.entry_id}"
        )

    async def async_stop(self) -> None:
        """Stop the websocket client."""
        self._closing = True
        self._stop_server_ping()

        if self._emulator is not None:
            await self._emulator.async_stop()

        for peer in list(self._local_peers.values()):
            if not peer.closed:
                try:
                    await peer.send_json(
                        {"type": "controller_disconnected", "clientId": self.target_id}
                    )
                except ConnectionResetError:
                    pass
                await peer.close(code=4000, message=b"controller_disconnected")
        self._local_peers.clear()
        self._pending_local_peers = 0
        self._local_connection_attempts.clear()
        self._local_message_events.clear()

        if self._ws and not self._ws.closed:
            await self._ws.close(code=1000, message=b"shutdown")

        if self._runner_task and not self._runner_task.done():
            self._runner_task.cancel()
            try:
                await self._runner_task
            except asyncio.CancelledError:
                pass

        self._ws = None
        self._reject_all_pending(DGLabNotConnectedError("DG-LAB websocket stopped"))
        self._mark_disconnected()

    async def async_reconnect(self) -> None:
        """Reconnect the websocket."""
        if self.mode == MODE_LOCAL:
            await self.async_stop()
            await self.async_start()
            return
        if self._ws and not self._ws.closed:
            await self._ws.close(code=1000, message=b"reconnect")
        if not self._runner_task or self._runner_task.done():
            await self.async_start()

    async def _run(self) -> None:
        """Run the reconnect loop."""
        try:
            while not self._closing:
                await self._connect_once()
                if self._closing:
                    break
                self._mark_disconnected()
                if not self.auto_reconnect:
                    break
                await asyncio.sleep(self.reconnect_delay)
        finally:
            self._runner_task = None

    async def _connect_once(self) -> None:
        """Open one websocket connection and read until it closes."""
        self._set_state(SOCKET_CONNECTING)
        session = async_get_clientsession(self.hass)
        ws: ClientWebSocketResponse | None = None

        try:
            ws = await asyncio.wait_for(
                session.ws_connect(self.url), timeout=self.connect_timeout
            )
            self._ws = ws
            self.last_error = None
            self._notify()

            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._handle_text(msg.data)
                elif msg.type == WSMsgType.BINARY:
                    await self._handle_text(msg.data.decode())
                elif msg.type == WSMsgType.ERROR:
                    error = ws.exception()
                    if error is not None:
                        raise error
                    break
                elif msg.type in (WSMsgType.CLOSED, WSMsgType.CLOSE, WSMsgType.CLOSING):
                    break
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, ClientError, OSError) as err:
            self._record_error("connection_failed", str(err) or err.__class__.__name__)
        except Exception as err:
            _LOGGER.exception("DG-LAB websocket crashed")
            self._record_error("connection_error", str(err) or err.__class__.__name__)
        finally:
            self._stop_server_ping()
            if self._emulator is not None:
                await self._emulator.async_stop_transport()
            if ws is not None and not ws.closed:
                await ws.close()
            if self._ws is ws:
                self._ws = None

    async def _handle_text(self, text: str) -> None:
        """Handle a websocket text frame."""
        try:
            frame = json.loads(text)
        except json.JSONDecodeError:
            _LOGGER.debug("Ignoring non-JSON DG-LAB websocket message: %s", text)
            return

        if not isinstance(frame, dict):
            return

        frame_type = frame.get("type")
        if frame_type == "hello":
            client_id = frame.get("clientId")
            if isinstance(client_id, str):
                self.target_id = client_id
                self._missed_server_pongs = 0
                self._set_state(SOCKET_WAITING_FOR_PEER)
                self._start_server_ping()
                if self._emulator is not None and self.app_websocket_url is not None:
                    await self._emulator.async_start_relay(
                        client_id, self.app_websocket_url
                    )
            return

        if frame_type == "client_attached":
            client_id = frame.get("clientId")
            if isinstance(client_id, str):
                self._set_app_connected(client_id, True)
                self._set_state(SOCKET_PAIRED)
                self.hass.bus.async_fire(
                    EVENT_CLIENT_ATTACHED,
                    {"entry_id": self.entry.entry_id, "client_id": client_id},
                )
                self.hass.async_create_task(self.request_devices(client_id))
            return

        if frame_type == "client_disconnected":
            client_id = frame.get("clientId")
            if isinstance(client_id, str):
                self._remove_app(client_id)
                self._reject_client_pending(client_id)
                self._set_state(
                    SOCKET_PAIRED
                    if self.connected_client_ids
                    else SOCKET_WAITING_FOR_PEER
                )
                self.hass.bus.async_fire(
                    EVENT_CLIENT_DISCONNECTED,
                    {"entry_id": self.entry.entry_id, "client_id": client_id},
                )
            return

        if frame_type == "message":
            await self._handle_message_frame(frame)
            return

        if frame_type == "pong":
            self._missed_server_pongs = 0
            self.last_server_pong = dt_util.utcnow()
            self._notify()
            return

        if frame_type == "heartbeat":
            self.last_server_heartbeat = dt_util.utcnow()
            self._notify()
            return

        if frame_type == "idle_timeout":
            self._record_error(
                "idle_timeout",
                "The DG-LAB relay closed the controller because no app was paired.",
            )
            if self._ws:
                await self._ws.close(code=1000, message=b"idle_timeout")
            return

        if frame_type == "error":
            self._record_error(
                str(frame.get("code", "server_error")),
                str(frame.get("message") or frame.get("code") or "DG-LAB server error"),
                frame.get("clientId") if isinstance(frame.get("clientId"), str) else None,
            )

    async def _handle_message_frame(self, frame: dict[str, Any]) -> None:
        """Handle a V4 message frame."""
        client_id = frame.get("clientId")
        data = frame.get("data")
        if not isinstance(client_id, str):
            return

        self._touch_app(client_id)

        if isinstance(data, dict):
            if data.get("t") == "ev":
                self._handle_event(client_id, data)
            elif data.get("t") == "resp":
                self._handle_response_devices(client_id, data.get("result"))
                self._resolve_response(client_id, data)
        self._notify()

    def _handle_event(self, client_id: str, data: dict[str, Any]) -> None:
        """Handle an app event payload."""
        event_type = data.get("ev")
        if event_type == "devices.snapshot":
            devices = data.get("devices")
            if isinstance(devices, list):
                self._replace_devices(client_id, devices)
            return

        if event_type == "devices.patch":
            added = data.get("added")
            removed = data.get("removed")
            if isinstance(added, list):
                for item in added:
                    if isinstance(item, dict):
                        self._upsert_device(client_id, item, replace=True)
            if isinstance(removed, list):
                for slot_id in removed:
                    if isinstance(slot_id, str):
                        self._remove_device(client_id, slot_id)
            return

        if event_type == "slots.patch":
            slots = data.get("slots")
            if isinstance(slots, list):
                for slot in slots:
                    if isinstance(slot, dict) and isinstance(slot.get("slotId"), str):
                        self._patch_slot(client_id, slot)
            return

        if event_type == "custom.action":
            action = data.get("action")
            if isinstance(action, int):
                self.last_action = DGLabAction(
                    action=action,
                    client_id=client_id,
                    received_at=dt_util.utcnow(),
                )
                self.hass.bus.async_fire(
                    EVENT_CUSTOM_ACTION,
                    {
                        "entry_id": self.entry.entry_id,
                        "client_id": client_id,
                        "action": action,
                    },
                )

    def _handle_response_devices(self, client_id: str, result: Any) -> None:
        """Update device state from a devices.get response."""
        if not isinstance(result, dict):
            return
        devices = result.get("devices")
        if isinstance(devices, list):
            self._replace_devices(client_id, devices)

    def _replace_devices(self, client_id: str, devices: list[Any]) -> None:
        """Replace the known devices for one app."""
        next_slot_ids: set[str] = set()
        for item in devices:
            if isinstance(item, dict) and isinstance(item.get("slotId"), str):
                next_slot_ids.add(item["slotId"])
                self._upsert_device(client_id, item, replace=True)

        for app_client_id, slot_id in list(self.devices):
            if app_client_id == client_id and slot_id not in next_slot_ids:
                self._remove_device(client_id, slot_id)

    def _upsert_device(
        self, client_id: str, payload: dict[str, Any], *, replace: bool
    ) -> DGLabDevice:
        """Create or update a device from a full payload."""
        slot_id = str(payload["slotId"])
        key = (client_id, slot_id)
        now = dt_util.utcnow()
        existing = self.devices.get(key)
        discovered = existing is None

        props = payload.get("props")
        slot_state = payload.get("slotState")
        if not isinstance(props, dict):
            props = {}
        if not isinstance(slot_state, dict):
            slot_state = {}

        if existing is None:
            existing = DGLabDevice(client_id=client_id, slot_id=slot_id)
            self.devices[key] = existing

        existing.name = (
            payload.get("name") if isinstance(payload.get("name"), str) else existing.name
        )
        existing.type = (
            payload.get("type") if isinstance(payload.get("type"), str) else existing.type
        )
        existing.props = dict(props) if replace else merge_patch(existing.props, props)
        existing.slot_state = (
            dict(slot_state) if replace else merge_patch(existing.slot_state, slot_state)
        )
        update_civet_edge_count(existing)
        existing.removed = False
        existing.last_seen = now

        if discovered:
            self.hass.bus.async_fire(
                EVENT_DEVICE_DISCOVERED,
                {
                    "entry_id": self.entry.entry_id,
                    "client_id": client_id,
                    "slot_id": slot_id,
                    "type": existing.type,
                    "name": existing.name,
                },
            )
        return existing

    def _patch_slot(self, client_id: str, payload: dict[str, Any]) -> None:
        """Patch a device slot."""
        slot_id = str(payload["slotId"])
        device = self.devices.get((client_id, slot_id))
        if device is None:
            _LOGGER.debug(
                "Ignoring slot patch for unknown DG-LAB device %s/%s",
                client_id,
                slot_id,
            )
            return

        props = payload.get("props")
        slot_state = payload.get("slotState")
        if isinstance(props, dict):
            device.props = merge_patch(device.props, props)
        if isinstance(slot_state, dict):
            device.slot_state = merge_patch(device.slot_state, slot_state)
        update_civet_edge_count(device)
        device.removed = False
        device.last_seen = dt_util.utcnow()

    def _remove_device(self, client_id: str, slot_id: str) -> None:
        """Forget a device and all transient channel helper state."""
        self.devices.pop((client_id, slot_id), None)
        for key in list(self.channel_settings):
            if key[:2] == (client_id, slot_id):
                self.channel_settings.pop(key)

    def _remove_app(self, client_id: str) -> None:
        """Forget an app and every device it exposed."""
        self.apps.pop(client_id, None)
        for app_client_id, slot_id in list(self.devices):
            if app_client_id == client_id:
                self._remove_device(client_id, slot_id)
        self._notify()

    def _set_app_connected(self, client_id: str, connected: bool) -> None:
        """Set app connection state."""
        if not connected:
            self._remove_app(client_id)
            return
        app = self.apps.get(client_id)
        if app is None:
            app = DGLabApp(client_id=client_id)
            self.apps[client_id] = app
        app.connected = connected
        app.last_seen = dt_util.utcnow()
        self._notify()

    def _touch_app(self, client_id: str) -> None:
        """Update app last-seen state."""
        app = self.apps.get(client_id)
        if app is None:
            app = DGLabApp(client_id=client_id, connected=True)
            self.apps[client_id] = app
        app.last_seen = dt_util.utcnow()

    def _set_state(self, state: str) -> None:
        """Set socket state."""
        if self.state == state:
            return
        self.state = state
        self._notify()

    def _mark_disconnected(self) -> None:
        """Forget disconnected apps and all of their transient inventory."""
        changed = bool(self.target_id or self.apps or self.devices or self.channel_settings)
        self.target_id = None
        self.apps.clear()
        self.devices.clear()
        self.channel_settings.clear()
        self._emulated_peers.clear()
        self._pending_local_peers = 0
        self._local_connection_attempts.clear()
        self._local_message_events.clear()
        self._reject_all_pending(DGLabNotConnectedError("DG-LAB websocket disconnected"))
        if self.state != SOCKET_DISCONNECTED:
            self.state = SOCKET_DISCONNECTED
            changed = True
        if changed:
            self._notify()

    def _record_error(
        self, code: str, message: str, client_id: str | None = None
    ) -> None:
        """Store and fire an integration error."""
        self.last_error = DGLabLastError(
            code=code,
            message=message,
            client_id=client_id,
            received_at=dt_util.utcnow(),
        )
        self.hass.bus.async_fire(
            EVENT_ERROR,
            {
                "entry_id": self.entry.entry_id,
                "code": code,
                "message": message,
                "client_id": client_id,
            },
        )
        self._notify()

    def _start_server_ping(self) -> None:
        """Start relay-level ping."""
        self._stop_server_ping()
        self._ping_task = self.hass.async_create_task(
            self._server_ping_loop(), name=f"dg_lab_ping_{self.entry.entry_id}"
        )

    def _stop_server_ping(self) -> None:
        """Stop relay-level ping."""
        if self._ping_task and not self._ping_task.done():
            self._ping_task.cancel()
        self._ping_task = None
        self._missed_server_pongs = 0

    async def _server_ping_loop(self) -> None:
        """Send relay-level pings like the official SDK."""
        try:
            while not self._closing and self._ws and not self._ws.closed:
                await asyncio.sleep(SERVER_PING_INTERVAL)
                if self._missed_server_pongs >= MAX_MISSED_SERVER_PONGS:
                    self._record_error("ping_timeout", "DG-LAB relay ping timed out")
                    await self._ws.close(code=1000, message=b"ping_timeout")
                    return
                await self._send_frame({"type": "ping"})
                self._missed_server_pongs += 1
        except asyncio.CancelledError:
            pass
        except DGLabError as err:
            self._record_error("ping_failed", str(err))

    async def _send_frame(self, frame: dict[str, Any]) -> None:
        """Send a raw protocol frame."""
        if self.mode == MODE_LOCAL:
            client_id = frame.get("clientId")
            emulator = self._emulated_peers.get(client_id)
            if frame.get("type") == "message" and emulator is not None:
                data = frame.get("data")
                if not isinstance(data, dict):
                    raise DGLabNotConnectedError("Invalid emulator message")
                await emulator(data)
                return
            peer = self._local_peers.get(client_id)
            if frame.get("type") != "message" or peer is None or peer.closed:
                raise DGLabNotConnectedError("DG-LAB app is not connected")
            try:
                await peer.send_json({"type": "message", "data": frame.get("data")})
            except ConnectionResetError as err:
                raise DGLabNotConnectedError("DG-LAB app disconnected") from err
            return
        ws = self._ws
        if ws is None or ws.closed:
            raise DGLabNotConnectedError("DG-LAB websocket is not connected")

        payload = json.dumps(frame, separators=(",", ":"), ensure_ascii=False)
        if len(payload) > MESSAGE_SIZE_WARNING:
            _LOGGER.warning(
                "DG-LAB message is %s characters; some app versions discard messages above %s",
                len(payload),
                MESSAGE_SIZE_WARNING,
            )
        await ws.send_str(payload)

    async def async_attach_emulated_peer(
        self,
        client_id: str,
        receiver: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Attach an in-process controlled client through the normal V4 path."""
        self._emulated_peers[client_id] = receiver
        await self._handle_text(
            json.dumps({"type": "client_attached", "clientId": client_id})
        )

    async def async_receive_emulated_peer(
        self, client_id: str, data: dict[str, Any]
    ) -> None:
        """Process an in-process controlled-client payload."""
        if client_id not in self._emulated_peers:
            return
        await self._handle_message_frame(
            {"type": "message", "clientId": client_id, "data": data}
        )

    async def async_detach_emulated_peer(self, client_id: str) -> None:
        """Detach an in-process controlled client."""
        if self._emulated_peers.pop(client_id, None) is None:
            return
        await self._handle_text(
            json.dumps({"type": "client_disconnected", "clientId": client_id})
        )

    async def async_attach_local_peer(self, peer: web.WebSocketResponse) -> str:
        """Attach an app to the Home Assistant hosted V4 endpoint."""
        client_id = secrets.token_hex(4)
        while client_id in self._local_peers:
            client_id = secrets.token_hex(4)
        try:
            await peer.send_json({"type": "hello", "clientId": client_id})
            await peer.send_json(
                {"type": "controller_attached", "clientId": self.target_id}
            )
            self._local_peers[client_id] = peer
            self._local_message_events[client_id] = deque()
            await self._handle_text(
                json.dumps({"type": "client_attached", "clientId": client_id})
            )
        except Exception:
            self._local_peers.pop(client_id, None)
            self._local_message_events.pop(client_id, None)
            raise
        return client_id

    async def async_receive_local_peer(self, client_id: str, raw: str) -> None:
        """Process an app frame through the normal controller message handler."""
        peer = self._local_peers.get(client_id)
        if peer is None or peer.closed:
            return
        if self.local_message_rate_limited(client_id):
            await peer.close(code=1008, message=b"rate_limit")
            return
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(frame, dict):
            return
        if frame.get("type") == "ping":
            await peer.send_json(
                {"type": "pong", "ts": int(dt_util.utcnow().timestamp() * 1000)}
            )
        elif frame.get("type") == "message":
            await self._handle_message_frame(
                {"type": "message", "clientId": client_id, "data": frame.get("data")}
            )

    async def async_detach_local_peer(
        self, client_id: str, peer: web.WebSocketResponse
    ) -> None:
        """Remove an app connection and update discovered entity availability."""
        if self._local_peers.get(client_id) is not peer:
            return
        self._local_peers.pop(client_id)
        self._local_message_events.pop(client_id, None)
        await self._handle_text(
            json.dumps({"type": "client_disconnected", "clientId": client_id})
        )

    def _next_request_id(self) -> str:
        """Return the next RPC request ID."""
        self._request_id_counter += 1
        return str(self._request_id_counter)

    async def send_rpc(
        self,
        client_id: str,
        payload: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> Any:
        """Send a V4 RPC request and wait for the response."""
        if not client_id:
            raise HomeAssistantError("client_id is required")

        request = dict(payload)
        request_id = (
            request.get("reqId") or request.get("requestId") or self._next_request_id()
        )
        request["reqId"] = str(request_id)
        request.pop("requestId", None)

        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        key = (client_id, str(request_id))
        self._pending[key] = future
        try:
            await self._send_frame(
                {
                    "type": "message",
                    "clientId": client_id,
                    "data": request,
                }
            )
            try:
                return await asyncio.wait_for(
                    future, timeout=timeout or self.response_timeout
                )
            except asyncio.TimeoutError as err:
                raise DGLabTimeoutError(
                    f"Timed out waiting for DG-LAB response to {request.get('m')}"
                ) from err
        finally:
            self._pending.pop(key, None)

    def _resolve_response(self, client_id: str, response: dict[str, Any]) -> None:
        """Resolve a pending RPC response."""
        request_id = response.get("reqId") or response.get("requestId")
        if not isinstance(request_id, str):
            return

        future = self._pending.get((client_id, request_id))
        if future is None or future.done():
            return

        if response.get("error"):
            future.set_exception(DGLabResponseError(str(response["error"])))
        else:
            future.set_result(response.get("result"))

    def _reject_client_pending(self, client_id: str) -> None:
        """Reject pending requests for a disconnected app."""
        error = DGLabNotConnectedError(f"DG-LAB app {client_id} disconnected")
        for key, future in list(self._pending.items()):
            if key[0] != client_id:
                continue
            if not future.done():
                future.set_exception(error)
            self._pending.pop(key, None)

    def _reject_all_pending(self, error: Exception) -> None:
        """Reject all pending requests."""
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    def _operate_timeout(
        self, duration_ms: int | None = None, timeout: float | None = None
    ) -> float:
        """Return a timeout for device.op commands."""
        if timeout is not None:
            return timeout
        if duration_ms is not None and duration_ms / 1000 > self.response_timeout:
            return duration_ms / 1000 + 1
        return float(self.response_timeout)

    async def request_devices(self, client_id: str) -> Any:
        """Request the current app device list."""
        result = await self.send_rpc(client_id, {"t": "req", "m": "devices.get"})
        self._handle_response_devices(client_id, result)
        self._notify()
        return result

    async def ping_app(self, client_id: str, *, timeout: float | None = None) -> float:
        """Ping an app and return round-trip time in milliseconds."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        result = await self.send_rpc(
            client_id, {"t": "req", "m": "ping"}, timeout=timeout
        )
        rtt_ms = (loop.time() - started) * 1000
        app = self.apps.setdefault(client_id, DGLabApp(client_id=client_id))
        app.last_rtt_ms = rtt_ms
        if isinstance(result, (int, float)):
            app.last_app_timestamp = result
        self._notify()
        return rtt_ms

    async def add_intensity(
        self,
        client_id: str,
        slot_id: str,
        channel: str | int,
        value: float,
        *,
        priority: int | None = None,
        immediate: bool | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Add or reduce channel intensity."""
        channel_index = normalize_channel(channel)
        delta = normalize_intensity(value)
        device = self.device(client_id, slot_id)
        if device is None:
            raise HomeAssistantError(f"Unknown DG-LAB device: {slot_id}")

        current = self.channel_intensity(device, channel_index)
        if current is None:
            if delta > 0:
                raise HomeAssistantError(
                    "Cannot safely increase intensity until the app reports its current value"
                )
        elif delta > 0:
            remaining = max(
                0,
                int(self.channel_max_intensity(device, channel_index) - current),
            )
            delta = min(delta, remaining)
        elif delta < 0:
            delta = max(delta, -int(current))

        if delta == 0:
            return {}
        data: dict[str, Any] = {
            "s": slot_id,
            "c": channel_index,
            "t": ADD_INTENSITY,
            "v": delta,
        }
        if priority is not None:
            data["p"] = priority
        if immediate is not None:
            data["im"] = immediate
        return await self.send_rpc(
            client_id,
            {"t": "req", "m": "device.op", "data": data},
            timeout=self._operate_timeout(timeout=timeout),
        )

    async def set_intensity(
        self,
        client_id: str,
        slot_id: str,
        channel: str | int,
        value: float,
        *,
        priority: int | None = None,
        immediate: bool | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Set channel intensity using the V4 operations available to controllers."""
        channel_index = normalize_channel(channel)
        target = normalize_intensity(value)
        if target < 0:
            raise HomeAssistantError("Absolute intensity cannot be negative")
        if target == 0:
            return await self.reset_intensity(
                client_id,
                slot_id,
                channel_index,
                priority=priority,
                immediate=immediate,
                timeout=timeout,
            )

        device = self.device(client_id, slot_id)
        if device is None:
            raise HomeAssistantError(f"Unknown DG-LAB device: {slot_id}")
        maximum = self.channel_max_intensity(device, channel_index)
        if target > maximum:
            raise HomeAssistantError(
                f"Intensity {target} exceeds the configured channel maximum of {maximum:g}"
            )
        current = self.channel_intensity(device, channel_index) if device else None
        if current is None:
            raise HomeAssistantError(
                "Cannot set a non-zero absolute intensity until the app reports current intensity"
            )
        delta = target - current
        if delta == 0:
            return {}
        return await self.add_intensity(
            client_id,
            slot_id,
            channel_index,
            delta,
            priority=priority,
            immediate=immediate,
            timeout=timeout,
        )

    async def reset_intensity(
        self,
        client_id: str,
        slot_id: str,
        channel: str | int,
        *,
        priority: int | None = None,
        immediate: bool | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Set channel intensity to zero."""
        channel_index = normalize_channel(channel)
        data: dict[str, Any] = {
            "s": slot_id,
            "c": channel_index,
            "t": SET_INTENSITY,
            "v": 0,
        }
        if priority is not None:
            data["p"] = priority
        if immediate is not None:
            data["im"] = immediate
        return await self.send_rpc(
            client_id,
            {"t": "req", "m": "device.op", "data": data},
            timeout=self._operate_timeout(timeout=timeout),
        )

    async def set_temp_intensity(
        self,
        client_id: str,
        slot_id: str,
        channel: str | int,
        value: float,
        duration_ms: int,
        *,
        priority: int | None = None,
        immediate: bool | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Set a temporary channel intensity."""
        channel_index = normalize_channel(channel)
        target = normalize_intensity(value)
        if target < 0:
            raise HomeAssistantError("Temporary intensity cannot be negative")
        device = self.device(client_id, slot_id)
        if device is None:
            raise HomeAssistantError(f"Unknown DG-LAB device: {slot_id}")
        maximum = self.channel_max_intensity(device, channel_index)
        if target > maximum:
            raise HomeAssistantError(
                f"Intensity {target} exceeds the configured channel maximum of {maximum:g}"
            )
        data: dict[str, Any] = {
            "s": slot_id,
            "c": channel_index,
            "t": SET_TEMP_INTENSITY,
            "v": target,
            "d": duration_ms,
        }
        if priority is not None:
            data["p"] = priority
        if immediate is not None:
            data["im"] = immediate
        return await self.send_rpc(
            client_id,
            {"t": "req", "m": "device.op", "data": data},
            timeout=self._operate_timeout(duration_ms, timeout),
        )

    async def send_pulse(
        self,
        client_id: str,
        slot_id: str,
        channel: str | int,
        frames: list[Any],
        duration_ms: int,
        *,
        priority: int | None = None,
        immediate: bool | None = None,
        version: int | None = None,
        seq: int | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Send raw pulse frames."""
        channel_index = normalize_channel(channel)
        data: dict[str, Any] = {
            "s": slot_id,
            "c": channel_index,
            "t": APPEND_PULSE_DATA,
            "v": frames,
            "d": duration_ms,
        }
        if priority is not None:
            data["p"] = priority
        if immediate is not None:
            data["im"] = immediate
        if version is not None:
            data["ver"] = version
        if seq is not None:
            data["seq"] = seq
        return await self.send_rpc(
            client_id,
            {"t": "req", "m": "device.op", "data": data},
            timeout=self._operate_timeout(duration_ms, timeout),
        )

    async def clear_operations(
        self,
        client_id: str,
        *,
        slot_id: str | None = None,
        channel: str | int | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Clear queued/running operations."""
        data: dict[str, Any] | None = None
        if slot_id is not None:
            data = {"s": slot_id}
            if channel is not None:
                data["c"] = normalize_channel(channel)
        elif channel is not None:
            raise HomeAssistantError(f"{ATTR_CHANNEL} requires slot_id")

        request: dict[str, Any] = {"t": "req", "m": "device.op.clear"}
        if data:
            request["data"] = data
        return await self.send_rpc(client_id, request, timeout=timeout)
