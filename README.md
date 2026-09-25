# DG-LAB Home Assistant Integration

Custom Home Assistant integration for DG-LAB V4 websocket control.

Home Assistant can accept the DG-LAB app directly over its own WebSocket endpoint or connect through a V4 relay. The app pairs to the generated URL, then apps, slots, device fields, and controls are discovered dynamically from V4 `devices.snapshot`, `devices.patch`, and `slots.patch` messages.

## Features

- UI config flow with Home Assistant direct and V4 relay connection modes.
- Pairing QR code image and V4 websocket pairing URL sensor for the DG-LAB app.
- Automatic app and device discovery after pairing.
- Automatic removal of app/device entities when an app or physical device disconnects.
- Friendly product names for Coyote, Opossum, and Civet devices.
- Dynamic sensors for every primitive field reported in `props` and `slotState`.
- Raw diagnostic device sensor with the full `props` and `slot_state` payloads.
- Binary sensors for websocket/app/device boolean state.
- Per-channel controls:
  - current intensity number
  - increase/decrease buttons
  - reset intensity button
  - clear channel operations button
  - temporary intensity value/duration plus apply button
  - pulse frame text input, pulse duration, and send pulse button
- Services for all V4 control paths: ping, devices.get, add/set/temp intensity, send pulse, clear operations, and raw RPC.

## Install

Copy `custom_components/dg_lab` into your Home Assistant `custom_components` directory and restart Home Assistant.

Then add the integration from:

`Settings` -> `Devices & services` -> `Add integration` -> `DG-LAB`

For Home Assistant direct mode, enter an HA URL reachable from the DG-LAB app, such as `http://192.168.1.10:8123`. The integration generates an app URL like:

```text
ws://192.168.1.10:8123/api/dg_lab/v4/<entry-id>?tid=<pairing-id>
```

For a remote HA address using HTTPS, the app URL uses `wss://`. The HA HTTP server or reverse proxy must allow WebSocket upgrades. The pairing ID is a secret access token for this unauthenticated endpoint; treat the generated app URL as private and use HTTPS when crossing an untrusted network.

The QR code uses DG-LAB's `https://dungeon-lab.cn/s/` pairing link format. That website address is only the app handoff; its `url=` parameter contains your Home Assistant `ws://` or `wss://` endpoint in direct mode. The app connects to that endpoint, not to the default relay. Check the `app_websocket_url` attribute on the `Pairing ID` sensor to see the exact destination.

If you already configured relay mode, open the DG-LAB integration's options and select `Home Assistant direct`. Enter your Home Assistant base URL there, then scan the `Pairing QR code` image entity with the DG-LAB app. The `Pairing ID` sensor also exposes the `app_websocket_url` and `pairing_url` attributes.

Relay mode defaults to:

```text
wss://trex.dungeon-lab.cn/v4
```

You can also point the integration at a self-hosted V4 websocket relay.

## Pairing

After setup, open the `Pairing QR code` image entity and scan it with the DG-LAB app. When the app connects, Home Assistant will create app/device entities automatically. You can also use the `pairing_url` attribute of the `Pairing ID` sensor.

The pairing ID and QR code change after reconnects. In direct mode Home Assistant generates the ID; in relay mode the relay assigns it.

## Services

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

For V4 protocol commands, `client_id` identifies the paired app and `slot_id` identifies the app-exposed device. Both are available on the raw device sensor attributes.

## Notes

DG-LAB V4 only supports an absolute intensity set to `0`. Non-zero intensity changes are sent as relative deltas based on the last reported channel intensity.

The maximum-intensity option is a safety ceiling for every intensity control and
service call. It defaults to `100`, even when a device reports a higher hardware
limit. Intensity controls use whole-number steps of `1`; the Opossum's native
channel-strength range is not rescaled.

Use automations carefully. The integration exposes live device controls, including intensity and pulse commands.
