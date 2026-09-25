"""Pairing QR image for the DG-LAB app."""

from __future__ import annotations

from collections.abc import Callable
from io import BytesIO

import segno
from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .api import DGLabClient
from .const import DOMAIN
from .entity import DGLabHubEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: Callable[[list[ImageEntity]], None],
) -> None:
    """Add the app pairing QR image."""
    client: DGLabClient = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([DGLabPairingQRImage(client)])


class DGLabPairingQRImage(DGLabHubEntity, ImageEntity):
    """Show the current DG-LAB app pairing link as a scannable QR code."""

    _attr_name = "Pairing QR code"
    _attr_content_type = "image/png"

    def __init__(self, client: DGLabClient) -> None:
        """Initialize the image entity."""
        super().__init__(client, "pairing_qr_code")
        ImageEntity.__init__(self, client.hass)
        self._pairing_url = client.pairing_url
        self._qr_bytes: bytes | None = None
        self._attr_image_last_updated = (
            dt_util.utcnow() if self._pairing_url else None
        )

    @property
    def available(self) -> bool:
        """Return whether there is an active pairing link."""
        return self._pairing_url is not None

    @callback
    def _handle_update(self) -> None:
        """Invalidate the image only when the pairing link changes."""
        pairing_url = self.client.pairing_url
        if pairing_url != self._pairing_url:
            self._pairing_url = pairing_url
            self._qr_bytes = None
            self._attr_image_last_updated = (
                dt_util.utcnow() if pairing_url else None
            )
        super()._handle_update()

    async def async_image(self) -> bytes | None:
        """Render the current QR code without blocking the event loop."""
        while (pairing_url := self._pairing_url) is not None:
            if self._qr_bytes is not None:
                return self._qr_bytes
            qr_bytes = await self.hass.async_add_executor_job(_render_qr, pairing_url)
            if pairing_url == self._pairing_url:
                self._qr_bytes = qr_bytes
                return qr_bytes
        return None


def _render_qr(pairing_url: str) -> bytes:
    """Encode the official app pairing link in a PNG QR code."""
    buffer = BytesIO()
    segno.make(pairing_url, micro=False).save(buffer, kind="png", scale=6, border=4)
    return buffer.getvalue()
