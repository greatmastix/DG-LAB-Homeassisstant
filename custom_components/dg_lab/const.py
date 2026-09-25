"""Constants for the DG-LAB integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "dg_lab"
MANUFACTURER = "DG-LAB"
DEFAULT_NAME = "DG-LAB WebSocket"
DEFAULT_WS_URL = "wss://trex.dungeon-lab.cn/v4"
PAIRING_PAGE_URL = "https://dungeon-lab.cn/s/"
LOCAL_WS_PATH = "/api/dg_lab/v4/{entry_id}"

CONF_CONNECTION_MODE = "connection_mode"
CONF_HA_URL = "ha_url"
MODE_LOCAL = "local"
MODE_RELAY = "relay"

CONF_AUTO_RECONNECT = "auto_reconnect"
CONF_COMMAND_STEP = "command_step"
CONF_CONNECT_TIMEOUT = "connect_timeout"
CONF_MAX_INTENSITY = "max_intensity"
CONF_RECONNECT_DELAY = "reconnect_delay"
CONF_RESPONSE_TIMEOUT = "response_timeout"
CONF_URL = "url"

DEFAULT_AUTO_RECONNECT = True
DEFAULT_COMMAND_STEP = 1
DEFAULT_CONNECT_TIMEOUT = 10
DEFAULT_MAX_INTENSITY = 100
DEFAULT_RECONNECT_DELAY = 5
DEFAULT_RESPONSE_TIMEOUT = 8

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.IMAGE,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.TEXT,
]

CHANNELS = {"A": 0, "B": 1}
CHANNEL_NAMES = {0: "A", 1: "B"}

EVENT_CUSTOM_ACTION = f"{DOMAIN}_custom_action"
EVENT_CLIENT_ATTACHED = f"{DOMAIN}_client_attached"
EVENT_CLIENT_DISCONNECTED = f"{DOMAIN}_client_disconnected"
EVENT_DEVICE_DISCOVERED = f"{DOMAIN}_device_discovered"
EVENT_ERROR = f"{DOMAIN}_error"

SERVICE_ADD_INTENSITY = "add_intensity"
SERVICE_CLEAR_OPERATIONS = "clear_operations"
SERVICE_PING = "ping"
SERVICE_RECONNECT = "reconnect"
SERVICE_REQUEST_DEVICES = "request_devices"
SERVICE_SEND_PULSE = "send_pulse"
SERVICE_SEND_RPC = "send_rpc"
SERVICE_SET_INTENSITY = "set_intensity"
SERVICE_SET_TEMP_INTENSITY = "set_temp_intensity"

ATTR_CHANNEL = "channel"
ATTR_CLIENT_ID = "client_id"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_DURATION_MS = "duration_ms"
ATTR_FRAMES = "frames"
ATTR_IMMEDIATE = "immediate"
ATTR_PAYLOAD = "payload"
ATTR_PRIORITY = "priority"
ATTR_SEQ = "seq"
ATTR_SLOT_ID = "slot_id"
ATTR_TIMEOUT = "timeout"
ATTR_VALUE = "value"
ATTR_VERSION = "version"

APPEND_PULSE_DATA = 0
ADD_INTENSITY = 3
SET_TEMP_INTENSITY = 4
SET_INTENSITY = 7

DEVICE_TYPE_BMTR = "BMTR_1"
DEVICE_TYPE_COYOTE_020 = "COYOTE_020"
DEVICE_TYPE_COYOTE_030 = "COYOTE_030"
DEVICE_TYPE_OVC = "OVC_1"

DEVICE_TYPE_FRIENDLY_NAMES: dict[str, str] = {
    DEVICE_TYPE_BMTR: "Civet Edging Sensor",
    DEVICE_TYPE_COYOTE_020: "Coyote 2.0",
    DEVICE_TYPE_COYOTE_030: "Coyote 3.0",
    DEVICE_TYPE_OVC: "Opossum Vibrate Controller",
}

DEVICE_TYPE_FRIENDLY_MODELS: dict[str, str] = {
    DEVICE_TYPE_BMTR: "Civet 1.0",
    DEVICE_TYPE_COYOTE_020: "Coyote 2.0",
    DEVICE_TYPE_COYOTE_030: "Coyote 3.0",
    DEVICE_TYPE_OVC: "Opossum 1.0",
}

# Normalized aliases used by the app in place of meaningful display names.
DEVICE_TYPE_NAME_ALIASES: dict[str, frozenset[str]] = {
    DEVICE_TYPE_BMTR: frozenset({"bmtr", "bmtr1", "bmtrv1"}),
    DEVICE_TYPE_COYOTE_020: frozenset(
        {"coyote", "coyote2", "coyote20", "coyote020"}
    ),
    DEVICE_TYPE_COYOTE_030: frozenset(
        {"coyote", "coyote3", "coyote30", "coyote030"}
    ),
    DEVICE_TYPE_OVC: frozenset({"ovc", "ovc1", "ovcv1"}),
}

CONTROL_CAPABLE_DEVICE_TYPES = {
    DEVICE_TYPE_COYOTE_020,
    DEVICE_TYPE_COYOTE_030,
    DEVICE_TYPE_OVC,
}


def friendly_device_name(
    device_type: str | None, reported_name: str | None
) -> str | None:
    """Return a friendly device name while preserving meaningful app names."""
    name = reported_name.strip() if reported_name else ""
    if device_type not in DEVICE_TYPE_FRIENDLY_NAMES:
        return name or None
    if (
        not name
        or _normalize_device_alias(name) in DEVICE_TYPE_NAME_ALIASES[device_type]
    ):
        return DEVICE_TYPE_FRIENDLY_NAMES[device_type]
    return name


def friendly_device_model(device_type: str | None) -> str | None:
    """Return a friendly model, retaining unknown protocol type identifiers."""
    if device_type is None:
        return None
    return DEVICE_TYPE_FRIENDLY_MODELS.get(device_type, device_type)


def _normalize_device_alias(value: str) -> str:
    """Normalize punctuation and capitalization in a reported device name."""
    return "".join(char for char in value.casefold() if char.isalnum())
