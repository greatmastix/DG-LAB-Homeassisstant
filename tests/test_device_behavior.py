"""Regression tests for DG-LAB device naming and lifecycle behavior."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

from homeassistant.components.sensor import SensorStateClass
from homeassistant.exceptions import HomeAssistantError

from custom_components.dg_lab import _reconcile_device_registry
from custom_components.dg_lab.api import (
    DGLabApp,
    DGLabClient,
    DGLabDevice,
    channel_is_available,
    device_is_connected,
)
from custom_components.dg_lab.const import (
    CONF_MAX_INTENSITY,
    DEVICE_TYPE_BMTR,
    DEVICE_TYPE_COYOTE_020,
    DEVICE_TYPE_COYOTE_030,
    DEVICE_TYPE_OVC,
)
from custom_components.dg_lab.entity import device_info
from custom_components.dg_lab.number import (
    DGLabIntensityNumber,
    DGLabStepNumber,
    DGLabTempIntensityNumber,
)
from custom_components.dg_lab.sensor import DGLabCivetEdgeCountSensor


def make_client(*, max_intensity: int = 100) -> DGLabClient:
    """Create a client with the small Home Assistant surface these tests need."""
    hass = SimpleNamespace(
        bus=SimpleNamespace(async_fire=lambda *_args, **_kwargs: None)
    )
    entry = SimpleNamespace(
        entry_id="test-entry",
        title="DG-LAB",
        data={},
        options={CONF_MAX_INTENSITY: max_intensity},
    )
    return DGLabClient(hass, entry)


class DeviceNamingTest(unittest.TestCase):
    """Check that protocol identifiers are not exposed as product names."""

    def test_cryptic_or_missing_names_use_friendly_product_names(self) -> None:
        """Known model identifiers and aliases have readable fallbacks."""
        cases = (
            (DEVICE_TYPE_OVC, "OVC", "Opossum Vibrate Controller"),
            (DEVICE_TYPE_BMTR, "BMTR", "Civet Edging Sensor"),
            (DEVICE_TYPE_COYOTE_020, None, "Coyote 2.0"),
            (DEVICE_TYPE_COYOTE_030, None, "Coyote 3.0"),
        )

        for device_type, reported_name, expected in cases:
            with self.subTest(device_type=device_type, reported_name=reported_name):
                device = DGLabDevice(
                    client_id="app",
                    slot_id="slot",
                    type=device_type,
                    name=reported_name,
                )
                self.assertEqual(device.display_name, expected)

    def test_meaningful_reported_name_is_preserved(self) -> None:
        """A name chosen by the user takes precedence over a model fallback."""
        device = DGLabDevice(
            client_id="app",
            slot_id="slot",
            type=DEVICE_TYPE_OVC,
            name="Bedroom Opossum",
        )

        self.assertEqual(device.display_name, "Bedroom Opossum")

    def test_device_info_uses_friendly_name_and_model(self) -> None:
        """The Home Assistant device registry receives readable metadata."""
        client = make_client()
        cases = (
            (DEVICE_TYPE_OVC, "OVC", "Opossum Vibrate Controller", "Opossum 1.0"),
            (DEVICE_TYPE_BMTR, "BMTR", "Civet Edging Sensor", "Civet 1.0"),
            (DEVICE_TYPE_COYOTE_020, None, "Coyote 2.0", "Coyote 2.0"),
            (DEVICE_TYPE_COYOTE_030, None, "Coyote 3.0", "Coyote 3.0"),
        )

        for index, (
            device_type,
            reported_name,
            expected_name,
            expected_model,
        ) in enumerate(cases):
            slot_id = f"slot-{index}"
            client.devices[("app", slot_id)] = DGLabDevice(
                client_id="app",
                slot_id=slot_id,
                type=device_type,
                name=reported_name,
            )

            with self.subTest(device_type=device_type):
                info = device_info(client, "app", slot_id)
                self.assertEqual(info["name"], expected_name)
                self.assertEqual(info["model"], expected_model)


class ChannelBehaviorTest(unittest.TestCase):
    """Check model-specific availability and safe intensity limits."""

    def test_reported_maximum_is_capped_by_configured_limit(self) -> None:
        """Opossum's raw 0..200 range does not leak past the UI safety cap."""
        client = make_client(max_intensity=100)
        device = DGLabDevice(
            client_id="app",
            slot_id="opossum",
            type=DEVICE_TYPE_OVC,
            slot_state={"channelA": {"intensityMax": 200}},
        )

        self.assertEqual(client.channel_max_intensity(device, 0), 100)

    def test_intensity_controls_use_unit_steps_and_effective_maximum(self) -> None:
        """All intensity-related number controls expose the same safe range."""
        client = make_client(max_intensity=100)
        client.devices[("app", "opossum")] = DGLabDevice(
            client_id="app",
            slot_id="opossum",
            type=DEVICE_TYPE_OVC,
            slot_state={"channelA": {"intensityMax": 200}},
        )

        intensity = DGLabIntensityNumber(client, "app", "opossum", 0)
        step = DGLabStepNumber(client, "app", "opossum", 0)
        temporary = DGLabTempIntensityNumber(client, "app", "opossum", 0)

        self.assertEqual(intensity.native_min_value, 0)
        self.assertEqual(intensity.native_max_value, 100)
        self.assertEqual(intensity.native_step, 1)
        self.assertEqual(step.native_min_value, 1)
        self.assertEqual(step.native_max_value, 100)
        self.assertEqual(step.native_step, 1)
        self.assertEqual(temporary.native_min_value, 0)
        self.assertEqual(temporary.native_max_value, 100)
        self.assertEqual(temporary.native_step, 1)

    def test_maximum_uses_first_valid_protocol_candidate(self) -> None:
        """The most authoritative reported maximum wins before applying caps."""
        client = make_client(max_intensity=200)
        cases = (
            (
                {
                    "intensityMax": 160,
                    "comfortLimit": {"comfortMax": 70, "absoluteMax": 180},
                },
                160,
            ),
            ({"comfortLimit": {"comfortMax": 70, "absoluteMax": 180}}, 70),
            ({"comfortLimit": {"absoluteMax": 180}}, 180),
            ({}, 200),
        )

        for channel_state, expected in cases:
            with self.subTest(channel_state=channel_state):
                device = DGLabDevice(
                    client_id="app",
                    slot_id="slot",
                    type=DEVICE_TYPE_OVC,
                    slot_state={"channelA": channel_state},
                )
                self.assertEqual(client.channel_max_intensity(device, 0), expected)

    def test_maximum_honors_device_and_protocol_caps(self) -> None:
        """A lower device maximum wins and the protocol maximum remains 200."""
        device = DGLabDevice(
            client_id="app",
            slot_id="slot",
            type=DEVICE_TYPE_OVC,
            slot_state={"channelA": {"intensityMax": 60}},
        )
        self.assertEqual(
            make_client(max_intensity=100).channel_max_intensity(device, 0), 60
        )

        device.slot_state["channelA"]["intensityMax"] = 500
        self.assertEqual(
            make_client(max_intensity=500).channel_max_intensity(device, 0), 200
        )

    def test_opossum_accessory_status_controls_channel_availability(self) -> None:
        """Each Opossum channel is available only when its accessory is present."""
        device = DGLabDevice(
            client_id="app",
            slot_id="opossum",
            type=DEVICE_TYPE_OVC,
            props={"channelAStatus": True, "channelBStatus": False},
            slot_state={"hasDevice": True},
        )

        self.assertTrue(channel_is_available(device, 0))
        self.assertFalse(channel_is_available(device, 1))

    def test_channel_status_is_interpreted_only_for_opossum(self) -> None:
        """A similarly named Coyote value must not hide a valid channel."""
        device = DGLabDevice(
            client_id="app",
            slot_id="coyote",
            type=DEVICE_TYPE_COYOTE_030,
            props={"channelAStatus": False},
            slot_state={"hasDevice": True},
        )

        self.assertTrue(channel_is_available(device, 0))

    def test_disconnected_or_removed_device_has_no_available_channels(self) -> None:
        """Physical device presence is shared by device and channel availability."""
        device = DGLabDevice(
            client_id="app",
            slot_id="slot",
            type=DEVICE_TYPE_OVC,
            slot_state={"hasDevice": False},
        )
        self.assertFalse(device_is_connected(device))
        self.assertFalse(channel_is_available(device, 0))

        device.slot_state["hasDevice"] = True
        device.removed = True
        self.assertFalse(device_is_connected(device))
        self.assertFalse(channel_is_available(device, 0))


class CivetEdgeCountTest(unittest.TestCase):
    """Check derived Civet edge-count behavior."""

    def test_initial_cooldown_snapshot_does_not_invent_an_edge(self) -> None:
        """Attaching during cooldown starts at zero without a known transition."""
        for edge_state in (2, 3):
            with self.subTest(edge_state=edge_state):
                client = make_client()
                device = client._upsert_device(
                    "app",
                    {
                        "slotId": "civet",
                        "type": DEVICE_TYPE_BMTR,
                        "slotState": {"edge": {"edgeState": edge_state}},
                    },
                    replace=True,
                )

                self.assertEqual(device.edge_count, 0)

    def test_sensor_exposes_the_derived_total(self) -> None:
        """The Civet entity has a stable ID and total-increasing semantics."""
        client = make_client()
        device = client._upsert_device(
            "app",
            {
                "slotId": "civet",
                "type": DEVICE_TYPE_BMTR,
                "slotState": {"edge": {"edgeState": 1}},
            },
            replace=True,
        )
        sensor = DGLabCivetEdgeCountSensor(client, "app", "civet")

        self.assertEqual(sensor.unique_id, "test-entry_app_civet_derived_edge_count")
        self.assertEqual(sensor.native_value, 0)
        self.assertEqual(sensor.state_class, SensorStateClass.TOTAL_INCREASING)
        self.assertEqual(
            sensor.extra_state_attributes["derived_from"],
            "slot_state.edge.edgeState",
        )

        client._patch_slot(
            "app",
            {"slotId": "civet", "slotState": {"edge": {"edgeState": 2}}},
        )

        self.assertEqual(device.edge_count, 1)
        self.assertEqual(sensor.native_value, 1)

    def test_counts_each_stimulation_to_cooldown_transition_once(self) -> None:
        """Duplicate and continued cooldown updates do not double-count an edge."""
        client = make_client()
        device = client._upsert_device(
            "app",
            {
                "slotId": "civet",
                "type": DEVICE_TYPE_BMTR,
                "slotState": {"edge": {"edgeState": 1}},
            },
            replace=True,
        )

        for edge_state, expected_count in (
            (2, 1),
            (2, 1),
            (3, 1),
            (1, 1),
            (3, 2),
            (1, 2),
            (2, 3),
        ):
            with self.subTest(edge_state=edge_state, expected_count=expected_count):
                client._patch_slot(
                    "app",
                    {
                        "slotId": "civet",
                        "slotState": {"edge": {"edgeState": edge_state}},
                    },
                )
                self.assertEqual(device.edge_count, expected_count)

    def test_repeated_full_snapshot_does_not_double_count(self) -> None:
        """A refreshed inventory preserves the counter and transition baseline."""
        client = make_client()
        device = client._upsert_device(
            "app",
            {
                "slotId": "civet",
                "type": DEVICE_TYPE_BMTR,
                "slotState": {"edge": {"edgeState": 1}},
            },
            replace=True,
        )

        for _ in range(2):
            refreshed = client._upsert_device(
                "app",
                {
                    "slotId": "civet",
                    "type": DEVICE_TYPE_BMTR,
                    "slotState": {"edge": {"edgeState": 2}},
                },
                replace=True,
            )
            self.assertIs(refreshed, device)

        self.assertEqual(device.edge_count, 1)

    def test_stopped_state_resets_the_session_count(self) -> None:
        """Stopping edge control resets the count and transition baseline."""
        client = make_client()
        device = client._upsert_device(
            "app",
            {
                "slotId": "civet",
                "type": DEVICE_TYPE_BMTR,
                "slotState": {"edge": {"edgeState": 1}},
            },
            replace=True,
        )
        client._patch_slot(
            "app",
            {"slotId": "civet", "slotState": {"edge": {"edgeState": 2}}},
        )
        self.assertEqual(device.edge_count, 1)

        client._patch_slot(
            "app",
            {"slotId": "civet", "slotState": {"edge": {"edgeState": 0}}},
        )
        self.assertEqual(device.edge_count, 0)

        client._patch_slot(
            "app",
            {"slotId": "civet", "slotState": {"edge": {"edgeState": 2}}},
        )
        self.assertEqual(device.edge_count, 0)

    def test_other_device_types_do_not_count_edge_state_transitions(self) -> None:
        """Only Civet devices interpret the edge state as an edge counter."""
        client = make_client()
        device = client._upsert_device(
            "app",
            {
                "slotId": "other",
                "type": DEVICE_TYPE_COYOTE_030,
                "slotState": {"edge": {"edgeState": 1}},
            },
            replace=True,
        )

        client._patch_slot(
            "app",
            {"slotId": "other", "slotState": {"edge": {"edgeState": 2}}},
        )

        self.assertEqual(device.edge_count, 0)


class IntensityCommandTest(unittest.IsolatedAsyncioTestCase):
    """Check command granularity and enforcement below the entity layer."""

    def setUp(self) -> None:
        """Create an Opossum with a reported raw maximum of 200."""
        self.client = make_client(max_intensity=100)
        self.client.devices[("app", "opossum")] = DGLabDevice(
            client_id="app",
            slot_id="opossum",
            type=DEVICE_TYPE_OVC,
            props={"intensityA": 10},
            slot_state={"channelA": {"intensityMax": 200}},
        )
        self.client.send_rpc = AsyncMock(return_value={"ok": True})

    async def test_single_unit_adjustment_is_sent_unchanged(self) -> None:
        """Opossum intensity commands have native unit granularity, not steps of ten."""
        await self.client.add_intensity("app", "opossum", 0, 1)

        request = self.client.send_rpc.await_args.args[1]
        self.assertEqual(request["data"]["v"], 1)

    async def test_absolute_change_uses_a_single_unit_delta(self) -> None:
        """Setting 10 to 11 emits the V4 relative delta of one."""
        await self.client.set_intensity("app", "opossum", 0, 11)

        request = self.client.send_rpc.await_args.args[1]
        self.assertEqual(request["data"]["v"], 1)

    async def test_absolute_and_temporary_values_obey_safety_cap(self) -> None:
        """Direct API callers cannot bypass the configured maximum."""
        with self.assertRaises(HomeAssistantError):
            await self.client.set_intensity("app", "opossum", 0, 101)
        with self.assertRaises(HomeAssistantError):
            await self.client.set_temp_intensity("app", "opossum", 0, 101, 1000)


class DeviceLifecycleTest(unittest.TestCase):
    """Check that transient inventory cannot leave ghost devices behind."""

    def test_unknown_patch_and_removal_do_not_create_placeholders(self) -> None:
        """Out-of-order events for unknown slots are safely ignored."""
        client = make_client()

        client._patch_slot(
            "app", {"slotId": "unknown", "props": {"intensityA": 10}}
        )
        client._remove_device("app", "unknown")

        self.assertEqual(client.devices, {})
        self.assertEqual(client.channel_settings, {})

    def test_removing_device_clears_all_of_its_channel_settings(self) -> None:
        """Device removal also clears transient helpers for both channels."""
        client = make_client()
        client._upsert_device(
            "app", {"slotId": "removed", "type": DEVICE_TYPE_OVC}, replace=True
        )
        client._upsert_device(
            "app", {"slotId": "kept", "type": DEVICE_TYPE_OVC}, replace=True
        )
        client.channel_setting("app", "removed", 0)
        client.channel_setting("app", "removed", 1)
        client.channel_setting("app", "kept", 0)

        client._remove_device("app", "removed")

        self.assertNotIn(("app", "removed"), client.devices)
        self.assertNotIn(("app", "removed", 0), client.channel_settings)
        self.assertNotIn(("app", "removed", 1), client.channel_settings)
        self.assertIn(("app", "kept"), client.devices)
        self.assertIn(("app", "kept", 0), client.channel_settings)

    def test_complete_snapshot_removes_omitted_devices(self) -> None:
        """A full inventory snapshot is authoritative for an app."""
        client = make_client()
        client._replace_devices(
            "app",
            [
                {"slotId": "kept", "type": DEVICE_TYPE_OVC},
                {"slotId": "gone", "type": DEVICE_TYPE_COYOTE_030},
            ],
        )
        client.channel_setting("app", "gone", 0)

        client._replace_devices(
            "app", [{"slotId": "kept", "type": DEVICE_TYPE_OVC}]
        )

        self.assertIn(("app", "kept"), client.devices)
        self.assertNotIn(("app", "gone"), client.devices)
        self.assertNotIn(("app", "gone", 0), client.channel_settings)

    def test_removing_app_clears_only_its_inventory(self) -> None:
        """Disconnect cleanup leaves another connected app untouched."""
        client = make_client()
        client.apps = {
            "gone": DGLabApp(client_id="gone", connected=True),
            "kept": DGLabApp(client_id="kept", connected=True),
        }
        client._upsert_device(
            "gone", {"slotId": "slot", "type": DEVICE_TYPE_OVC}, replace=True
        )
        client._upsert_device(
            "kept", {"slotId": "slot", "type": DEVICE_TYPE_OVC}, replace=True
        )
        client.channel_setting("gone", "slot", 0)
        client.channel_setting("kept", "slot", 0)

        client._remove_app("gone")

        self.assertNotIn("gone", client.apps)
        self.assertNotIn(("gone", "slot"), client.devices)
        self.assertNotIn(("gone", "slot", 0), client.channel_settings)
        self.assertIn("kept", client.apps)
        self.assertIn(("kept", "slot"), client.devices)
        self.assertIn(("kept", "slot", 0), client.channel_settings)

    @patch("custom_components.dg_lab.dr.async_entries_for_config_entry")
    @patch("custom_components.dg_lab.dr.async_get")
    def test_registry_reconciliation_removes_children_before_app(
        self, async_get: Mock, entries_for_config_entry: Mock
    ) -> None:
        """Registry cleanup preserves live objects and removes stale children first."""
        client = make_client()
        client.apps["live"] = DGLabApp(client_id="live", connected=True)
        client.devices[("live", "slot")] = DGLabDevice(
            client_id="live",
            slot_id="slot",
            type=DEVICE_TYPE_OVC,
            slot_state={"hasDevice": True},
        )
        domain = "dg_lab"
        entry_id = client.entry.entry_id
        registry = SimpleNamespace(async_remove_device=Mock())
        async_get.return_value = registry
        entries_for_config_entry.return_value = [
            SimpleNamespace(
                id="old-app",
                identifiers={(domain, f"{entry_id}_app_old")},
            ),
            SimpleNamespace(
                id="hub",
                identifiers={(domain, entry_id)},
            ),
            SimpleNamespace(
                id="old-device",
                identifiers={(domain, f"{entry_id}_device_old_slot")},
            ),
            SimpleNamespace(
                id="live-app",
                identifiers={(domain, f"{entry_id}_app_live")},
            ),
            SimpleNamespace(
                id="live-device",
                identifiers={(domain, f"{entry_id}_device_live_slot")},
            ),
        ]

        _reconcile_device_registry(SimpleNamespace(), client.entry, client)

        self.assertEqual(
            registry.async_remove_device.call_args_list,
            [call("old-device"), call("old-app")],
        )


if __name__ == "__main__":
    unittest.main()
