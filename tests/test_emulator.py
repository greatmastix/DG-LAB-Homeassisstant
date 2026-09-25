"""Tests for the built-in Opossum emulator."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from aiohttp import ClientSession, WSMsgType, web

from custom_components.dg_lab.api import SOCKET_PAIRED, DGLabClient
from custom_components.dg_lab.const import (
    CONF_CONNECTION_MODE,
    CONF_EMULATED_OPOSSUM,
    CONF_HA_URL,
    CONF_URL,
    DEVICE_TYPE_OVC,
    MODE_LOCAL,
    MODE_RELAY,
)
from custom_components.dg_lab.emulator import EMULATED_SLOT_ID


class OpossumEmulatorTest(unittest.IsolatedAsyncioTestCase):
    """The emulator behaves like an attached V4 controlled client."""

    async def asyncSetUp(self) -> None:
        """Start an emulator on the deterministic in-process transport."""
        self.tasks: set[asyncio.Task] = set()

        def create_task(coro, *, name=None):
            task = asyncio.create_task(coro, name=name)
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            return task

        self.hass = SimpleNamespace(
            bus=SimpleNamespace(async_fire=Mock()),
            async_create_task=create_task,
        )
        self.entry = SimpleNamespace(
            entry_id="emulator-entry",
            title="DG-LAB Test",
            data={
                CONF_CONNECTION_MODE: MODE_LOCAL,
                CONF_HA_URL: "http://ha.example:8123",
                CONF_EMULATED_OPOSSUM: True,
            },
            options={},
        )
        self.client = DGLabClient(self.hass, self.entry)
        await self.client.async_start()
        await asyncio.sleep(0)

    async def asyncTearDown(self) -> None:
        """Stop background tasks."""
        await self.client.async_stop()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)

    async def test_emulator_is_discovered_as_connected_opossum(self) -> None:
        """Enabling the option creates one named Opossum with two channels."""
        self.assertEqual(self.client.state, SOCKET_PAIRED)
        self.assertEqual(len(self.client.connected_client_ids), 1)
        client_id = next(iter(self.client.connected_client_ids))
        self.assertTrue(self.client.is_emulated_client(client_id))

        device = self.client.device(client_id, EMULATED_SLOT_ID)
        self.assertIsNotNone(device)
        assert device is not None
        self.assertEqual(device.type, DEVICE_TYPE_OVC)
        self.assertEqual(device.display_name, "Emulated Opossum")
        self.assertEqual(device.props["intensityA"], 0)
        self.assertEqual(device.props["intensityB"], 0)
        self.assertEqual(self.client.channel_max_intensity(device, 0), 200)

    async def test_intensity_commands_update_reported_state(self) -> None:
        """Normal controller actions drive the emulated device state."""
        client_id = next(iter(self.client.connected_client_ids))

        result = await self.client.set_intensity(
            client_id, EMULATED_SLOT_ID, "A", 200
        )
        self.assertEqual(result["reason"], "completed")
        device = self.client.device(client_id, EMULATED_SLOT_ID)
        assert device is not None
        self.assertEqual(device.props["intensityA"], 200)

        await self.client.add_intensity(client_id, EMULATED_SLOT_ID, "A", -7)
        self.assertEqual(device.props["intensityA"], 193)

        await self.client.reset_intensity(client_id, EMULATED_SLOT_ID, "A")
        self.assertEqual(device.props["intensityA"], 0)

    async def test_temporary_intensity_resets_and_can_be_cleared(self) -> None:
        """Timed operations publish live state and honor operation clearing."""
        client_id = next(iter(self.client.connected_client_ids))
        operation = asyncio.create_task(
            self.client.set_temp_intensity(
                client_id, EMULATED_SLOT_ID, "B", 55, 5_000
            )
        )
        await asyncio.sleep(0)
        device = self.client.device(client_id, EMULATED_SLOT_ID)
        assert device is not None
        self.assertEqual(device.props["intensityB"], 55)

        await self.client.clear_operations(
            client_id, slot_id=EMULATED_SLOT_ID, channel="B"
        )
        result = await operation
        self.assertEqual(result["reason"], "cleared")
        self.assertEqual(device.props["intensityB"], 0)

    async def test_stop_removes_virtual_inventory(self) -> None:
        """Stopping or reloading leaves no virtual app or device behind."""
        await self.client.async_stop()
        self.assertFalse(self.client.apps)
        self.assertFalse(self.client.devices)


class OpossumEmulatorRelayTest(unittest.IsolatedAsyncioTestCase):
    """The emulator also uses the controlled side of a V4 relay."""

    async def test_relay_round_trip(self) -> None:
        """Discovery and commands cross two real WebSocket connections."""
        relay = _TestRelay()
        await relay.async_start()
        session = ClientSession()
        tasks: set[asyncio.Task] = set()

        def create_task(coro, *, name=None):
            task = asyncio.create_task(coro, name=name)
            tasks.add(task)
            task.add_done_callback(tasks.discard)
            return task

        hass = SimpleNamespace(
            bus=SimpleNamespace(async_fire=Mock()),
            async_create_task=create_task,
        )
        entry = SimpleNamespace(
            entry_id="relay-emulator-entry",
            title="DG-LAB Relay Test",
            data={
                CONF_CONNECTION_MODE: MODE_RELAY,
                CONF_URL: relay.url,
                CONF_EMULATED_OPOSSUM: True,
            },
            options={},
        )
        client = DGLabClient(hass, entry)
        try:
            with (
                patch(
                    "custom_components.dg_lab.api.async_get_clientsession",
                    return_value=session,
                ),
                patch(
                    "custom_components.dg_lab.emulator.async_get_clientsession",
                    return_value=session,
                ),
            ):
                await client.async_start()
                for _ in range(100):
                    if client.devices:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(client.state, SOCKET_PAIRED)
                client_id = next(iter(client.connected_client_ids))
                self.assertTrue(client.is_emulated_client(client_id))
                device = client.device(client_id, EMULATED_SLOT_ID)
                self.assertIsNotNone(device)

                await client.set_intensity(client_id, EMULATED_SLOT_ID, "A", 23)
                assert device is not None
                self.assertEqual(device.props["intensityA"], 23)
        finally:
            await client.async_stop()
            await session.close()
            await relay.async_stop()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)


class _TestRelay:
    """Minimal V4 relay used to exercise both WebSocket roles."""

    def __init__(self) -> None:
        self.controller: web.WebSocketResponse | None = None
        self.controlled: web.WebSocketResponse | None = None
        self.runner: web.AppRunner | None = None
        self.url = ""

    async def async_start(self) -> None:
        """Start on an ephemeral loopback port."""
        app = web.Application()
        app.router.add_get("/v4", self._handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        assert site._server is not None
        port = site._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/v4"

    async def async_stop(self) -> None:
        """Close the test relay."""
        if self.runner is not None:
            await self.runner.cleanup()

    async def _handle(self, request: web.Request) -> web.WebSocketResponse:
        """Attach a controller or controlled peer and relay message frames."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        if request.query.get("tid"):
            await self._handle_controlled(ws)
        else:
            await self._handle_controller(ws)
        return ws

    async def _handle_controller(self, ws: web.WebSocketResponse) -> None:
        """Run the controller side."""
        self.controller = ws
        await ws.send_json({"type": "hello", "clientId": "test-controller"})
        try:
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                frame = message.json()
                if frame.get("type") == "ping":
                    await ws.send_json({"type": "pong", "ts": 1})
                elif frame.get("type") == "message" and self.controlled is not None:
                    await self.controlled.send_json(
                        {"type": "message", "data": frame.get("data")}
                    )
        finally:
            self.controller = None
            if self.controlled is not None and not self.controlled.closed:
                await self.controlled.send_json(
                    {"type": "controller_disconnected", "clientId": "test-controller"}
                )
                await self.controlled.close()

    async def _handle_controlled(self, ws: web.WebSocketResponse) -> None:
        """Run the controlled side."""
        self.controlled = ws
        await ws.send_json({"type": "hello", "clientId": "test-emulator"})
        await ws.send_json(
            {"type": "controller_attached", "clientId": "test-controller"}
        )
        if self.controller is not None:
            await self.controller.send_json(
                {"type": "client_attached", "clientId": "test-emulator"}
            )
        try:
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                frame = message.json()
                if frame.get("type") == "ping":
                    await ws.send_json({"type": "pong", "ts": 1})
                elif frame.get("type") == "message" and self.controller is not None:
                    await self.controller.send_json(
                        {
                            "type": "message",
                            "clientId": "test-emulator",
                            "data": frame.get("data"),
                        }
                    )
        finally:
            self.controlled = None
            if self.controller is not None and not self.controller.closed:
                await self.controller.send_json(
                    {"type": "client_disconnected", "clientId": "test-emulator"}
                )
