"""Controlled-side DG-LAB Opossum emulator."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError, ClientWebSocketResponse, WSMsgType
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    ADD_INTENSITY,
    APPEND_PULSE_DATA,
    DEVICE_TYPE_OVC,
    SET_INTENSITY,
    SET_TEMP_INTENSITY,
)

if TYPE_CHECKING:
    from .api import DGLabClient

_LOGGER = logging.getLogger(__name__)

EMULATED_SLOT_ID = "emulated-opossum"
EMULATED_MAX_INTENSITY = 200
RELAY_PING_INTERVAL = 2


@dataclass(slots=True)
class _TimedOperation:
    """A running virtual-device operation."""

    request_id: str
    operation_type: int
    channel: int
    task: asyncio.Task[None]
    cancel_reason: str | None = None


class DGLabOpossumEmulator:
    """Emulate one Opossum as a V4 controlled client."""

    def __init__(self, client: DGLabClient) -> None:
        """Initialize virtual state."""
        self.client = client
        self.client_id: str | None = None
        self._known_client_ids: set[str] = set()
        self._relay_task: asyncio.Task[None] | None = None
        self._relay_ws: ClientWebSocketResponse | None = None
        self._relay_target_id: str | None = None
        self._local_client_id = f"emulator-{client.entry.entry_id[:12]}"
        self._stopping = False
        self._intensity = [0, 0]
        self._operations: dict[str, _TimedOperation] = {}
        self._sender: Callable[[dict[str, Any]], Awaitable[None]] | None = None

    def owns_client(self, client_id: str) -> bool:
        """Return whether a V4 client ID belongs to this emulator."""
        return client_id in self._known_client_ids

    async def async_start_local(self) -> None:
        """Attach directly to Home Assistant's in-process V4 controller."""
        await self.async_stop_transport()
        self._stopping = False
        self.client_id = self._local_client_id
        self._known_client_ids.add(self.client_id)
        self._sender = self._send_local
        await self.client.async_attach_emulated_peer(
            self.client_id, self.async_receive_controller_message
        )
        await self._send_snapshot()

    async def async_start_relay(self, target_id: str, websocket_url: str) -> None:
        """Connect the virtual controlled client through the configured relay."""
        if (
            self._relay_task is not None
            and not self._relay_task.done()
            and self._relay_target_id == target_id
        ):
            return
        await self.async_stop_transport()
        self._stopping = False
        self._relay_target_id = target_id
        self._relay_task = self.client.hass.async_create_task(
            self._run_relay(websocket_url, target_id),
            name=f"dg_lab_opossum_emulator_{self.client.entry.entry_id}",
        )

    async def async_stop(self) -> None:
        """Stop the emulator and all virtual operations."""
        self._stopping = True
        await self._cancel_operations()
        await self.async_stop_transport()

    async def async_stop_transport(self) -> None:
        """Disconnect the current local or relay transport."""
        await self._cancel_operations()
        local_client_id = (
            self.client_id if self.client_id == self._local_client_id else None
        )
        if local_client_id is not None:
            await self.client.async_detach_emulated_peer(local_client_id)

        ws = self._relay_ws
        if ws is not None and not ws.closed:
            await ws.close(code=1000, message=b"emulator_shutdown")

        task = self._relay_task
        if task is not None and not task.done():
            task.cancel()
            if task is not asyncio.current_task():
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._relay_task = None
        self._relay_ws = None
        self._relay_target_id = None
        self._sender = None
        self.client_id = None

    async def async_receive_controller_message(self, data: dict[str, Any]) -> None:
        """Receive a normal V4 RPC payload from the controller."""
        if not isinstance(data, dict) or data.get("t") != "req":
            return
        request_id = data.get("reqId") or data.get("requestId")
        if request_id is None:
            return
        request_id = str(request_id)
        method = data.get("m")

        if method == "devices.get":
            await self._respond(request_id, {"devices": [self.device_snapshot()]})
            return
        if method == "ping":
            await self._respond(request_id, int(time.time() * 1000))
            return
        if method == "device.op.clear":
            error = await self._clear_operations(data.get("data"))
            await self._respond(request_id, {}, error=error)
            return
        if method == "device.op":
            await self._operate(request_id, data.get("data"))
            return
        await self._respond(request_id, error=f"Unsupported method: {method}")

    def device_snapshot(self) -> dict[str, Any]:
        """Return the current virtual Opossum descriptor."""
        return {
            "id": 0,
            "slotId": EMULATED_SLOT_ID,
            "name": "Emulated Opossum",
            "type": DEVICE_TYPE_OVC,
            "props": {
                "power": 100,
                "version": 1,
                "label": 0,
                "intensityA": self._intensity[0],
                "intensityB": self._intensity[1],
                "connectState": "connected",
                "channelAStatus": True,
                "channelBStatus": True,
                "mode": 0,
                "updateTime": "",
                "updateValue": "",
            },
            "slotState": {
                "markLight": "green",
                "hasDevice": True,
                "channelA": {
                    "isMuted": False,
                    "intensityMax": EMULATED_MAX_INTENSITY,
                },
                "channelB": {
                    "isMuted": False,
                    "intensityMax": EMULATED_MAX_INTENSITY,
                },
            },
        }

    async def _run_relay(self, websocket_url: str, target_id: str) -> None:
        """Keep the relay-side virtual app attached while its controller exists."""
        session = async_get_clientsession(self.client.hass)
        try:
            while (
                not self._stopping
                and self.client.target_id == target_id
                and self.client.connected
            ):
                try:
                    await self._relay_once(session, websocket_url)
                except asyncio.CancelledError:
                    raise
                except (ClientError, OSError, asyncio.TimeoutError) as err:
                    _LOGGER.warning("Opossum emulator relay connection failed: %s", err)
                finally:
                    self._relay_ws = None
                    self._sender = None
                    self.client_id = None
                if (
                    not self._stopping
                    and self.client.target_id == target_id
                    and self.client.connected
                ):
                    await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            self._relay_task = None

    async def _relay_once(self, session: Any, websocket_url: str) -> None:
        """Run one relay-side virtual app connection."""
        ws = await session.ws_connect(websocket_url)
        self._relay_ws = ws
        self._sender = self._send_relay
        ping_task = self.client.hass.async_create_task(
            self._relay_ping_loop(ws),
            name=f"dg_lab_opossum_emulator_ping_{self.client.entry.entry_id}",
        )
        try:
            async for message in ws:
                if message.type in (WSMsgType.TEXT, WSMsgType.BINARY):
                    frame = message.json()
                elif message.type in (
                    WSMsgType.CLOSE,
                    WSMsgType.CLOSED,
                    WSMsgType.CLOSING,
                    WSMsgType.ERROR,
                ):
                    break
                else:
                    continue
                if not isinstance(frame, dict):
                    continue
                frame_type = frame.get("type")
                if frame_type == "hello" and isinstance(frame.get("clientId"), str):
                    self.client_id = frame["clientId"]
                    self._known_client_ids.add(self.client_id)
                elif frame_type == "controller_attached":
                    await self._send_snapshot()
                elif frame_type == "message":
                    await self.async_receive_controller_message(frame.get("data"))
                elif frame_type == "ping":
                    await ws.send_json(
                        {"type": "pong", "ts": int(time.time() * 1000)}
                    )
                elif frame_type == "controller_disconnected":
                    break
        finally:
            ping_task.cancel()
            try:
                await ping_task
            except asyncio.CancelledError:
                pass

    async def _relay_ping_loop(self, ws: ClientWebSocketResponse) -> None:
        """Keep the relay-side controlled-client connection alive."""
        while not self._stopping and not ws.closed:
            await asyncio.sleep(RELAY_PING_INTERVAL)
            await ws.send_json({"type": "ping"})

    async def _operate(self, request_id: str, data: Any) -> None:
        """Apply one supported Opossum operation."""
        if not isinstance(data, dict):
            await self._respond(request_id, error="invalid_params")
            return
        if data.get("s") != EMULATED_SLOT_ID:
            await self._respond(request_id, error="slot_not_found")
            return
        channel = data.get("c")
        if channel not in (0, 1):
            await self._respond(request_id, error="invalid_operate")
            return
        operation_type = data.get("t")

        if request_id in self._operations:
            await self._respond(request_id, error="duplicate_request_id")
            return

        if data.get("im"):
            await self._cancel_matching(channel, operation_type, "replaced")

        if operation_type == ADD_INTENSITY:
            delta = self._whole_number(data.get("v"))
            if delta is None:
                await self._respond(request_id, error="invalid_operate")
                return
            await self._set_intensity(
                channel,
                max(0, min(EMULATED_MAX_INTENSITY, self._intensity[channel] + delta)),
            )
            await self._respond(
                request_id, self._operation_result(operation_type, channel, "completed")
            )
            return

        if operation_type == SET_INTENSITY:
            if data.get("v") != 0:
                await self._respond(request_id, error="invalid_operate")
                return
            await self._set_intensity(channel, 0)
            await self._respond(
                request_id, self._operation_result(operation_type, channel, "completed")
            )
            return

        if operation_type in (SET_TEMP_INTENSITY, APPEND_PULSE_DATA):
            duration = self._whole_number(data.get("d", 0))
            if duration is None or duration < 0:
                await self._respond(request_id, error="invalid_operate")
                return
            if operation_type == SET_TEMP_INTENSITY:
                value = self._whole_number(data.get("v"))
                if value is None or not 0 <= value <= EMULATED_MAX_INTENSITY:
                    await self._respond(request_id, error="invalid_operate")
                    return
                await self._set_intensity(channel, value)
            elif not isinstance(data.get("v"), list):
                await self._respond(request_id, error="invalid_operate")
                return

            if operation_type == APPEND_PULSE_DATA:
                frame_duration = len(data["v"]) * 100
                duration = frame_duration if duration == 0 else min(duration, frame_duration)
                if duration == 0:
                    await self._respond(
                        request_id,
                        self._operation_result(operation_type, channel, "completed"),
                    )
                    return

            task = self.client.hass.async_create_task(
                self._finish_timed_operation(
                    request_id, operation_type, channel, duration
                ),
                name=f"dg_lab_emulator_operation_{request_id}",
            )
            self._operations[request_id] = _TimedOperation(
                request_id, operation_type, channel, task
            )
            return

        await self._respond(request_id, error=f"Unsupported operation type: {operation_type}")

    async def _finish_timed_operation(
        self, request_id: str, operation_type: int, channel: int, duration_ms: int
    ) -> None:
        """Complete a timed virtual operation and report its result."""
        try:
            if duration_ms == 0:
                await asyncio.Future()
            else:
                await asyncio.sleep(duration_ms / 1000)
        except asyncio.CancelledError:
            return
        finally:
            if operation_type == SET_TEMP_INTENSITY:
                await self._set_intensity(channel, 0)

        self._operations.pop(request_id, None)
        await self._respond(
            request_id, self._operation_result(operation_type, channel, "completed")
        )

    async def _clear_operations(self, data: Any) -> str | None:
        """Cancel operations matching an optional slot/channel filter."""
        if data is not None and not isinstance(data, dict):
            return "invalid_params"
        if isinstance(data, dict):
            slot_id = data.get("s")
            channel = data.get("c")
            if channel is not None and slot_id is None:
                return "invalid_params"
            if slot_id not in (None, EMULATED_SLOT_ID):
                return "slot_not_found"
            if channel not in (None, 0, 1):
                return "invalid_params"
        channel = data.get("c") if isinstance(data, dict) else None
        cancelled: list[_TimedOperation] = []
        for operation in list(self._operations.values()):
            if channel is None or operation.channel == channel:
                operation.cancel_reason = "cleared"
                operation.task.cancel()
                cancelled.append(operation)
        await self._finish_cancelled_operations(cancelled)
        return None

    async def _cancel_matching(
        self, channel: int, operation_type: int, reason: str
    ) -> None:
        """Cancel operations of the same channel and operation type."""
        cancelled: list[_TimedOperation] = []
        for operation in list(self._operations.values()):
            if operation.channel == channel and operation.operation_type == operation_type:
                operation.cancel_reason = reason
                operation.task.cancel()
                cancelled.append(operation)
        await self._finish_cancelled_operations(cancelled)

    async def _cancel_operations(self) -> None:
        """Cancel every operation without sending shutdown responses."""
        operations = list(self._operations.values())
        tasks = [operation.task for operation in operations]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for channel in {
            operation.channel
            for operation in operations
            if operation.operation_type == SET_TEMP_INTENSITY
        }:
            if self._intensity[channel] != 0:
                await self._set_intensity(channel, 0)
        self._operations.clear()

    async def _finish_cancelled_operations(
        self, operations: list[_TimedOperation]
    ) -> None:
        """Wait for cancellation cleanup and publish terminal responses."""
        if not operations:
            return
        await asyncio.gather(
            *(operation.task for operation in operations), return_exceptions=True
        )
        for operation in operations:
            if (
                operation.operation_type == SET_TEMP_INTENSITY
                and self._intensity[operation.channel] != 0
            ):
                await self._set_intensity(operation.channel, 0)
            self._operations.pop(operation.request_id, None)
            await self._respond(
                operation.request_id,
                self._operation_result(
                    operation.operation_type,
                    operation.channel,
                    operation.cancel_reason or "cleared",
                ),
            )

    async def _set_intensity(self, channel: int, value: int) -> None:
        """Update virtual state and publish a standard slot patch."""
        self._intensity[channel] = value
        suffix = "A" if channel == 0 else "B"
        await self._send(
            {
                "t": "ev",
                "ev": "slots.patch",
                "slots": [
                    {
                        "slotId": EMULATED_SLOT_ID,
                        "props": {f"intensity{suffix}": value},
                    }
                ],
            }
        )

    async def _send_snapshot(self) -> None:
        """Publish the virtual device inventory."""
        await self._send(
            {"t": "ev", "ev": "devices.snapshot", "devices": [self.device_snapshot()]}
        )

    async def _respond(
        self, request_id: str, result: Any = None, *, error: str | None = None
    ) -> None:
        """Send a V4 RPC response."""
        response: dict[str, Any] = {"t": "resp", "reqId": request_id}
        if error is not None:
            response["error"] = error
        else:
            response["result"] = result
        await self._send(response)

    async def _send(self, data: dict[str, Any]) -> None:
        """Send a controlled-side V4 payload over the active transport."""
        sender = self._sender
        if sender is not None:
            await sender(data)

    async def _send_local(self, data: dict[str, Any]) -> None:
        """Send a payload to the in-process controller."""
        if self.client_id is not None:
            await self.client.async_receive_emulated_peer(self.client_id, data)

    async def _send_relay(self, data: dict[str, Any]) -> None:
        """Send a payload to the relay controller."""
        if self._relay_ws is not None and not self._relay_ws.closed:
            await self._relay_ws.send_json({"type": "message", "data": data})

    @staticmethod
    def _whole_number(value: Any) -> int | None:
        """Return a whole-number value without accepting booleans."""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        numeric = float(value)
        return int(numeric) if numeric.is_integer() else None

    @staticmethod
    def _operation_result(
        operation_type: int, channel: int, reason: str
    ) -> dict[str, Any]:
        """Build the standard terminal operation result."""
        return {
            "type": operation_type,
            "reason": reason,
            "slotId": EMULATED_SLOT_ID,
            "channel": channel,
        }
