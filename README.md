# DG-LAB for Home Assistant

A custom Home Assistant integration for pairing with and controlling DG-LAB V4 devices. Connect the DG-LAB app directly to Home Assistant or use a compatible V4 relay.

[![Open your Home Assistant instance and add this repository to HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=greatmastix&repository=DG-LAB-Homeassisstant&category=integration)

## Features

- Direct, local WebSocket pairing or remote V4 relay mode.
- Pairing URL and QR code generated inside Home Assistant.
- Automatic discovery of connected apps and physical devices.
- Automatic removal of app and device entities after disconnection.
- Friendly names for Coyote, Opossum, and Civet devices.
- A derived per-session edge count for the Civet edging sensor.
- Optional two-channel Opossum emulator for hardware-free testing.
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

### Virtual Opossum

Enable **Create an emulated Opossum** in the integration's setup or options to add a virtual two-channel Opossum. It appears as **Emulated Opossum**, reports channel state, and supports the same intensity, temporary-intensity, pulse, reset, and stop-operation actions as a connected device. Both channels use the native Opossum `0`–`200` range with steps of `1`.

The emulator attaches as a controlled V4 client: directly inside Home Assistant in direct mode, or through the configured V4 relay in relay mode. It is disabled by default. Turning the option off reloads the integration and removes the emulator's entities and device entries.

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

Actions provide Home Assistant entity or connection dropdowns. Channel actions accept one or more number entities, so the same intensity or pulse command can target several channels at once. **Stop operations** accepts hub, app, device, or channel entities and uses the selected entity's scope.

Raw V4 targeting remains available under each action's collapsed **Advanced targeting** section. `client_id` identifies the paired app and `slot_id` identifies its exposed device; both values are available in the raw device sensor attributes. Do not combine entity targets with raw identifiers in the same action call.

## Safety and protocol notes

- Intensity controls use whole-number steps of `1`.
- The default maximum-intensity limit is `200`, matching the Opossum's native channel-strength range. You can lower the configured safety limit for controls and action calls.
- Opossum values are not rescaled; one Home Assistant intensity step is one native device step.
- V4 only supports setting absolute intensity directly to `0`. Other changes are sent as relative deltas from the last reported channel intensity.

Use automations carefully: the integration exposes live intensity and pulse controls.
