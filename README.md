# DG-LAB for Home Assistant

A custom Home Assistant integration for pairing with and controlling DG-LAB V4 devices. Connect the DG-LAB app directly to Home Assistant or use a compatible V4 relay.

[![Open your Home Assistant instance and add this repository to HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=greatmastix&repository=DG-LAB-Homeassisstant&category=integration)

## Features

- Direct, local WebSocket pairing or remote V4 relay mode.
- Pairing URL and QR code generated inside Home Assistant.
- Automatic discovery of connected apps and physical devices.
- Automatic removal of app and device entities after disconnection.
- Friendly names for Coyote, Opossum, and Civet devices.
- Sensors for device properties, slot state, and raw diagnostic data.
- Per-channel intensity, temporary intensity, pulse, reset, and clear controls.
- Home Assistant actions for every supported V4 control path.

## Installation

### HACS

1. Select the **Open your Home Assistant** button above.
2. Download **DG-LAB** from HACS.
3. Restart Home Assistant.
4. Go to **Settings → Devices & services → Add integration** and select **DG-LAB**.

If the button does not open your instance, add this repository manually as a custom **Integration** repository in HACS:

```text
https://github.com/greatmastix/DG-LAB-Homeassisstant
```

### Manual installation

1. Copy `custom_components/dg_lab` into your Home Assistant `custom_components` directory.
2. Restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration** and select **DG-LAB**.

## Configuration

Choose one of two connection modes during setup:

- **Home Assistant direct** — recommended for local use. Enter a Home Assistant URL that the DG-LAB app can reach, such as `http://192.168.1.10:8123`.
- **V4 relay** — connects through `wss://trex.dungeon-lab.cn/v4` by default. A self-hosted compatible relay can be used instead.

In direct mode, the integration generates an app WebSocket URL similar to:

```text
ws://192.168.1.10:8123/api/dg_lab/v4/<entry-id>?tid=<pairing-id>
```

HTTPS Home Assistant addresses use `wss://`. Your Home Assistant HTTP server or reverse proxy must allow WebSocket upgrades.

## Pairing

Open the **Pairing QR code** image entity and scan it with the DG-LAB app. The integration creates entities when the app and a physical device connect, then removes them when they disconnect.

The **Pairing ID** sensor also provides these attributes:

- `app_websocket_url` — the exact WebSocket destination used by the app.
- `pairing_url` — the DG-LAB app handoff link encoded in the QR code.

The pairing ID is a secret access token for the unauthenticated WebSocket endpoint. Keep the URL private and use HTTPS when crossing an untrusted network. Pairing details can change after a reconnect.

## Actions

The integration registers these actions under the `dg_lab` domain:

- `dg_lab.reconnect`
- `dg_lab.request_devices`
- `dg_lab.ping`
- `dg_lab.add_intensity`
- `dg_lab.set_intensity`
- `dg_lab.set_temp_intensity`
- `dg_lab.send_pulse`
- `dg_lab.clear_operations`
- `dg_lab.send_rpc`

For V4 protocol actions, `client_id` identifies the paired app and `slot_id` identifies its exposed device. Both values are available in the raw device sensor attributes.

## Safety and protocol notes

- Intensity controls use whole-number steps of `1`.
- The default maximum-intensity safety limit is `100` and applies to controls and action calls.
- Opossum reports a native channel-strength range up to `200`; the integration does not rescale those values and will not send above the configured safety limit.
- V4 only supports setting absolute intensity directly to `0`. Other changes are sent as relative deltas from the last reported channel intensity.

Use automations carefully: the integration exposes live intensity and pulse controls.
