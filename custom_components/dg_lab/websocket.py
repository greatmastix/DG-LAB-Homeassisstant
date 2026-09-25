"""V4 WebSocket endpoint hosted by Home Assistant."""

from __future__ import annotations

import asyncio
import secrets

from aiohttp import WSMsgType, web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .api import DGLabClient
from .const import DOMAIN, LOCAL_WS_PATH, MODE_LOCAL


class DGLabWebSocketView(HomeAssistantView):
    """Accept DG-LAB app connections without a Home Assistant login."""

    name = "api:dg_lab:v4"
    url = LOCAL_WS_PATH
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        """Keep access to active integration entries."""
        self.hass = hass

    async def get(self, request: web.Request, entry_id: str) -> web.WebSocketResponse:
        """Pair an app with the selected local controller."""
        client: DGLabClient | None = self.hass.data.get(DOMAIN, {}).get(entry_id)
        token = request.query.get("tid", "")
        if (
            client is None
            or client.mode != MODE_LOCAL
            or not client.connected
            or not token
            or not secrets.compare_digest(token, client.target_id or "")
        ):
            raise web.HTTPNotFound()

        peer = web.WebSocketResponse(heartbeat=30, max_msg_size=1_048_576)
        await peer.prepare(request)
        client_id: str | None = None
        heartbeat_task: asyncio.Task[None] | None = None
        try:
            client_id = await client.async_attach_local_peer(peer)
            heartbeat_task = asyncio.create_task(self._heartbeat(peer))
            async for message in peer:
                if message.type == WSMsgType.TEXT:
                    await client.async_receive_local_peer(client_id, message.data)
                elif message.type == WSMsgType.BINARY:
                    try:
                        await client.async_receive_local_peer(
                            client_id, message.data.decode("utf-8")
                        )
                    except UnicodeDecodeError:
                        pass
                elif message.type == WSMsgType.ERROR:
                    break
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
            if client_id is not None:
                await client.async_detach_local_peer(client_id, peer)
            if not peer.closed:
                await peer.close()
        return peer

    @staticmethod
    async def _heartbeat(peer: web.WebSocketResponse) -> None:
        """Send the V4 application-level heartbeat."""
        try:
            while not peer.closed:
                await asyncio.sleep(30)
                if not peer.closed:
                    await peer.send_json({"type": "heartbeat"})
        except (asyncio.CancelledError, ConnectionResetError):
            pass
