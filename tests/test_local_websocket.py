"""End-to-end checks for the Home Assistant hosted V4 endpoint."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import ClientSession, WSServerHandshakeError, web

from custom_components.dg_lab.api import DGLabClient
from custom_components.dg_lab.const import (
    CONF_CONNECTION_ATTEMPTS_PER_MINUTE,
    CONF_MAX_APP_CONNECTIONS,
    CONF_MESSAGES_PER_SECOND,
    DOMAIN,
    MODE_LOCAL,
)
from custom_components.dg_lab.websocket import DGLabWebSocketView


class LocalWebSocketTest(unittest.IsolatedAsyncioTestCase):
    """Exercise the app-facing V4 frames over a real local socket."""

    async def asyncSetUp(self) -> None:
        """Start a minimal HTTP server with the integration view."""
        self.tasks: list[asyncio.Task] = []

        def create_task(coro, **kwargs):
            task = asyncio.create_task(coro, **kwargs)
            self.tasks.append(task)
            return task

        self.hass = SimpleNamespace(
            data={DOMAIN: {}},
            bus=SimpleNamespace(async_fire=lambda *_: None),
            async_create_task=create_task,
        )
        entry = SimpleNamespace(
            entry_id="test-entry",
            title="DG-LAB",
            data={"connection_mode": MODE_LOCAL, "ha_url": "http://127.0.0.1"},
            options={},
        )
        self.client = DGLabClient(self.hass, entry)
        self.hass.data[DOMAIN][entry.entry_id] = self.client
        view = DGLabWebSocketView(self.hass)
        app = web.Application()

        async def handle(request):
            return await view.get(request, request.match_info["entry_id"])

        app.router.add_get(view.url, handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.client.ha_url = f"http://127.0.0.1:{port}"
        await self.client.async_start()
        self.session = ClientSession()

    async def asyncTearDown(self) -> None:
        """Close sockets and outstanding request tasks."""
        await self.client.async_stop()
        await self.session.close()
        await self.runner.cleanup()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def test_pair_discover_and_command(self) -> None:
        """A local app can report devices and answer controller RPCs."""
        with self.assertRaises(WSServerHandshakeError) as rejected:
            await self.session.ws_connect(
                self.client.websocket_url + "?tid=wrong"
            )
        self.assertEqual(rejected.exception.status, 404)

        async with self.session.ws_connect(self.client.app_websocket_url) as app:
            hello = await app.receive_json(timeout=5)
            attached = await app.receive_json(timeout=5)
            self.assertEqual(hello["type"], "hello")
            self.assertEqual(attached, {
                "type": "controller_attached",
                "clientId": self.client.target_id,
            })
            client_id = hello["clientId"]
            self.assertTrue(self.client.apps[client_id].connected)

            await app.send_json({
                "type": "message",
                "data": {
                    "t": "ev",
                    "ev": "devices.snapshot",
                    "devices": [{
                        "slotId": "slot-a",
                        "name": "Test device",
                        "type": "COYOTE_030",
                        "props": {"intensityA": 12},
                    }],
                },
            })
            request = await app.receive_json(timeout=5)
            self.assertEqual(request["type"], "message")
            self.assertEqual(request["data"]["m"], "devices.get")
            await app.send_json({
                "type": "message",
                "data": {
                    "t": "resp",
                    "reqId": request["data"]["reqId"],
                    "result": {"devices": [{
                        "slotId": "slot-a",
                        "name": "Test device",
                        "type": "COYOTE_030",
                        "props": {"intensityA": 12},
                    }]},
                },
            })
            await asyncio.sleep(0)
            self.assertIsNotNone(self.client.device(client_id, "slot-a"))

            command = asyncio.create_task(
                self.client.add_intensity(client_id, "slot-a", "A", 3)
            )
            operation = await app.receive_json(timeout=5)
            self.assertEqual(operation["data"]["m"], "device.op")
            self.assertEqual(operation["data"]["data"]["v"], 3)
            await app.send_json({
                "type": "message",
                "data": {
                    "t": "resp",
                    "reqId": operation["data"]["reqId"],
                    "result": {"ok": True},
                },
            })
            self.assertEqual(await asyncio.wait_for(command, 5), {"ok": True})

        await asyncio.sleep(0)
        self.assertNotIn(client_id, self.client.apps)
        self.assertFalse(self.client.devices)
        self.assertFalse(self.client.channel_settings)

        old_url = self.client.app_websocket_url
        await self.client.async_reconnect()
        self.assertNotEqual(old_url, self.client.app_websocket_url)
        with self.assertRaises(WSServerHandshakeError) as expired:
            await self.session.ws_connect(old_url)
        self.assertEqual(expired.exception.status, 404)


class LocalRateLimitTest(unittest.TestCase):
    """Check direct-mode rolling limits without opening network sockets."""

    def make_client(self, **options: int) -> DGLabClient:
        """Create a direct-mode client with selected limiter settings."""
        hass = SimpleNamespace(
            bus=SimpleNamespace(async_fire=lambda *_args, **_kwargs: None)
        )
        entry = SimpleNamespace(
            entry_id="rate-limit-test",
            title="DG-LAB",
            data={"connection_mode": MODE_LOCAL, "ha_url": "https://ha.example"},
            options=options,
        )
        return DGLabClient(hass, entry)

    def test_failed_connection_attempts_use_a_rolling_window(self) -> None:
        """A source is allowed again after its oldest attempt expires."""
        client = self.make_client(**{CONF_CONNECTION_ATTEMPTS_PER_MINUTE: 2})

        with patch(
            "custom_components.dg_lab.api.time.monotonic",
            side_effect=(0.0, 1.0, 2.0, 60.1),
        ):
            self.assertIsNone(client.local_connection_retry_after("192.0.2.1"))
            self.assertIsNone(client.local_connection_retry_after("192.0.2.1"))
            self.assertGreater(
                client.local_connection_retry_after("192.0.2.1") or 0,
                0,
            )
            self.assertIsNone(client.local_connection_retry_after("192.0.2.1"))

    def test_message_limit_and_connection_capacity_are_independent(self) -> None:
        """Each peer has its own message window and capacity is reserved atomically."""
        client = self.make_client(
            **{
                CONF_MESSAGES_PER_SECOND: 2,
                CONF_MAX_APP_CONNECTIONS: 1,
            }
        )

        with patch(
            "custom_components.dg_lab.api.time.monotonic",
            side_effect=(0.0, 0.1, 0.2, 1.1),
        ):
            self.assertFalse(client.local_message_rate_limited("app"))
            self.assertFalse(client.local_message_rate_limited("app"))
            self.assertTrue(client.local_message_rate_limited("app"))
            self.assertFalse(client.local_message_rate_limited("app"))

        self.assertTrue(client.try_reserve_local_peer())
        self.assertFalse(client.try_reserve_local_peer())
        client.release_local_peer_reservation()
        self.assertTrue(client.try_reserve_local_peer())
