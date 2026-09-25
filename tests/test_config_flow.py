"""Tests for mode-specific DG-LAB config flows and pairing links."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import FlowResultType

from custom_components.dg_lab.api import DGLabClient
from custom_components.dg_lab.config_flow import (
    DGLabConfigFlow,
    DGLabOptionsFlowHandler,
)
from custom_components.dg_lab.const import (
    CONF_CONNECTION_MODE,
    CONF_EMULATED_OPOSSUM,
    CONF_HA_URL,
    CONF_URL,
    MODE_LOCAL,
    MODE_RELAY,
)


class ConfigFlowTest(unittest.IsolatedAsyncioTestCase):
    """The selected transport controls the fields and saved data."""

    def setUp(self) -> None:
        """Provide the flow with a configured Home Assistant address."""
        self.hass = SimpleNamespace(
            config=SimpleNamespace(
                internal_url="http://ha.example:8123", external_url=None
            )
        )

    async def test_direct_setup_uses_home_assistant_in_qr(self) -> None:
        """Direct setup needs no relay URL and points the app at HA."""
        flow = DGLabConfigFlow()
        flow.hass = self.hass
        mode_form = await flow.async_step_user()
        self.assertEqual(mode_form["step_id"], "user")

        details = await flow.async_step_user({CONF_CONNECTION_MODE: MODE_LOCAL})
        self.assertEqual(details["step_id"], "details")
        self.assertNotIn(CONF_URL, _field_names(details["data_schema"]))
        self.assertIn(CONF_HA_URL, _field_names(details["data_schema"]))
        self.assertIn(CONF_EMULATED_OPOSSUM, _field_names(details["data_schema"]))

        submitted = details["data_schema"]({
            CONF_NAME: "Test DG-LAB",
            CONF_HA_URL: "http://ha.example:8123",
        })
        result = await flow.async_step_details(submitted)
        self.assertEqual(result["type"], FlowResultType.CREATE_ENTRY)
        self.assertNotIn(CONF_URL, result["data"])
        self.assertEqual(result["data"][CONF_CONNECTION_MODE], MODE_LOCAL)
        self.assertFalse(result["data"][CONF_EMULATED_OPOSSUM])

        invalid = await flow.async_step_details(
            {CONF_NAME: "Test DG-LAB", CONF_HA_URL: "not-a-url"}
        )
        self.assertEqual(invalid["errors"][CONF_HA_URL], "invalid_home_assistant_url")

        entry = SimpleNamespace(
            entry_id="test-entry",
            title=result["title"],
            data=result["data"],
            options={},
        )
        client = DGLabClient(self.hass, entry)
        await client.async_start()
        try:
            self.assertEqual(client.mode, MODE_LOCAL)
            self.assertTrue(
                client.app_websocket_url.startswith(
                    "ws://ha.example:8123/api/dg_lab/v4/test-entry?tid="
                )
            )
            qr_link = urlparse(client.pairing_url)
            self.assertEqual(qr_link.netloc, "dungeon-lab.cn")
            self.assertEqual(
                parse_qs(qr_link.query)["url"], [client.app_websocket_url]
            )
        finally:
            await client.async_stop()

    async def test_legacy_relay_entry_can_switch_to_direct(self) -> None:
        """Old relay data does not make its URL a direct-mode form field."""
        entry = SimpleNamespace(
            entry_id="old-entry",
            title="Old DG-LAB",
            data={CONF_NAME: "Old DG-LAB", CONF_URL: "wss://relay.example/v4"},
            options={},
        )
        flow = DGLabOptionsFlowHandler(entry)
        flow.hass = self.hass
        mode_form = await flow.async_step_init()
        self.assertEqual(mode_form["step_id"], "init")

        details = await flow.async_step_init({CONF_CONNECTION_MODE: MODE_LOCAL})
        self.assertEqual(details["step_id"], "details")
        self.assertNotIn(CONF_URL, _field_names(details["data_schema"]))
        submitted = details["data_schema"]({
            CONF_NAME: "Old DG-LAB",
            CONF_HA_URL: "http://ha.example:8123",
        })
        result = await flow.async_step_details(submitted)
        self.assertEqual(result["type"], FlowResultType.CREATE_ENTRY)
        self.assertNotIn(CONF_URL, result["data"])
        entry.options = result["data"]
        client = DGLabClient(self.hass, entry)
        await client.async_start()
        try:
            self.assertEqual(client.mode, MODE_LOCAL)
            self.assertTrue(client.app_websocket_url.startswith("ws://ha.example:8123/"))
        finally:
            await client.async_stop()

    async def test_relay_setup_requires_relay_url_only(self) -> None:
        """Relay setup exposes its websocket URL, not the HA URL."""
        flow = DGLabConfigFlow()
        flow.hass = self.hass
        details = await flow.async_step_user({CONF_CONNECTION_MODE: MODE_RELAY})
        self.assertIn(CONF_URL, _field_names(details["data_schema"]))
        self.assertNotIn(CONF_HA_URL, _field_names(details["data_schema"]))


def _field_names(schema) -> set[str]:
    """Return field names from a Voluptuous form schema."""
    return {field.schema for field in schema.schema}
