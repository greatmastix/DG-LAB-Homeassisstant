# DG-LAB Home Assistant Integration

Custom Home Assistant integration for DG-LAB V4 websocket control.

Home Assistant acts as the websocket controller. The DG-LAB app pairs to the generated URL, then apps, slots, device fields, and controls are discovered dynamically from V4 `devices.snapshot`, `devices.patch`, and `slots.patch` messages.

## Features

- UI config flow with configurable websocket relay URL.
- V4 websocket pairing URL sensor for the DG-LAB app.
- Automatic app and device discovery after pairing.
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

The default relay is:

```text
wss://trex.dungeon-lab.cn/v4
```

You can also point the integration at a self-hosted V4 websocket relay.

## Pairing

After setup, open the `Pairing ID` sensor attributes and use the `pairing_url` value with the DG-LAB app. When the app connects, Home Assistant will create app/device entities automatically.

The pairing ID changes after reconnects because it is assigned by the relay.

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

Use automations carefully. The integration exposes live device controls, including intensity and pulse commands.
