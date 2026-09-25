"""Tests for the DG-LAB app pairing QR image."""

from __future__ import annotations

import asyncio
import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

import segno
from PIL import Image

from custom_components.dg_lab.api import DGLabClient
from custom_components.dg_lab.const import MODE_LOCAL
from custom_components.dg_lab.image import DGLabPairingQRImage


class PairingQRImageTest(unittest.IsolatedAsyncioTestCase):
    """Exercise QR content and pairing ID changes."""

    async def asyncSetUp(self) -> None:
        """Create a local-mode client and image entity."""
        self.hass = SimpleNamespace(async_add_executor_job=asyncio.to_thread)
        entry = SimpleNamespace(
            entry_id="test-entry",
            title="DG-LAB",
            data={"connection_mode": MODE_LOCAL, "ha_url": "http://127.0.0.1:8123"},
            options={},
        )
        self.client = DGLabClient(self.hass, entry)
        with patch("homeassistant.components.image.get_async_client"):
            self.image = DGLabPairingQRImage(self.client)
        self.image.hass = self.hass
        self.image.async_write_ha_state = Mock()
        self.remove_listener = self.client.async_add_listener(self.image._handle_update)

    async def asyncTearDown(self) -> None:
        """Close the local client."""
        self.remove_listener()
        await self.client.async_stop()

    async def test_qr_contains_pairing_url_and_rotates(self) -> None:
        """The image follows the live pairing link and disappears on stop."""
        self.assertFalse(self.image.available)
        self.assertIsNone(await self.image.async_image())
        await self.client.async_start()
        old_url = self.client.pairing_url
        self.assertTrue(self.image.available)
        self.assertEqual(self.image.content_type, "image/png")
        old_updated = self.image.image_last_updated
        old_bytes = await self.image.async_image()
        self.assertIsNotNone(old_bytes)
        self.assertEqual(old_bytes[:8], b"\x89PNG\r\n\x1a\n")
        self._assert_qr_content(old_bytes, old_url)
        self.assertEqual(await self.image.async_image(), old_bytes)

        await self.client.async_stop()
        self.assertFalse(self.image.available)
        self.assertIsNone(self.image.image_last_updated)
        self.assertIsNone(await self.image.async_image())

        await self.client.async_start()
        new_url = self.client.pairing_url
        self.assertNotEqual(new_url, old_url)
        self.assertTrue(self.image.available)
        self.assertIsNotNone(self.image.image_last_updated)
        self.assertNotEqual(self.image.image_last_updated, old_updated)
        new_bytes = await self.image.async_image()
        self.assertNotEqual(new_bytes, old_bytes)
        self._assert_qr_content(new_bytes, new_url)

    def _assert_qr_content(self, data: bytes, url: str) -> None:
        """Compare PNG modules with the expected QR matrix."""
        matrix = segno.make(url, micro=False).matrix
        with Image.open(BytesIO(data)) as png:
            self.assertEqual(
                png.size, ((len(matrix[0]) + 8) * 6, (len(matrix) + 8) * 6)
            )
            pixels = png.convert("RGB")
            for row_number, row in enumerate(matrix):
                for column_number, dark in enumerate(row):
                    pixel = pixels.getpixel(
                        ((column_number + 4) * 6 + 3, (row_number + 4) * 6 + 3)
                    )
                    self.assertEqual(pixel, (0, 0, 0) if dark else (255, 255, 255))
