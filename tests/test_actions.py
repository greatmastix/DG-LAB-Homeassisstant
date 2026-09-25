"""Tests for user-friendly DG-LAB action targeting."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.exceptions import ServiceValidationError

from custom_components.dg_lab import (
    _app_targets_from_call,
    _channel_targets_from_call,
    _clear_targets_from_call,
)
from custom_components.dg_lab.api import DGLabApp, DGLabClient, DGLabDevice
from custom_components.dg_lab.const import (
    ATTR_CHANNEL,
    ATTR_CLIENT_ID,
    ATTR_SLOT_ID,
    CONF_MAX_INTENSITY,
    DEVICE_TYPE_OVC,
    DOMAIN,
)


def make_client() -> DGLabClient:
    """Create a client with the Home Assistant surface these tests need."""
    hass = SimpleNamespace(
        bus=SimpleNamespace(async_fire=lambda *_args, **_kwargs: None)
    )
    entry = SimpleNamespace(
        entry_id="test-entry",
        title="DG-LAB",
        data={},
        options={CONF_MAX_INTENSITY: 100},
    )
    return DGLabClient(hass, entry)


class ActionTargetingTest(unittest.TestCase):
    """Resolve action entity dropdown values to protocol coordinates."""

    def setUp(self) -> None:
        """Create one connected app, device, and representative entities."""
        self.client = make_client()
        self.client.apps["app"] = DGLabApp(client_id="app", connected=True)
        self.client.devices[("app", "slot")] = DGLabDevice(
            client_id="app",
            slot_id="slot",
            type=DEVICE_TYPE_OVC,
            slot_state={"hasDevice": True},
        )
        self.hass = SimpleNamespace(
            data={DOMAIN: {self.client.entry.entry_id: self.client}}
        )

        entry_id = self.client.entry.entry_id
        self.entities = {
            "number.opossum_a_intensity": self._entity(
                "number.opossum_a_intensity",
                f"{entry_id}_app_slot_channel_a_intensity",
                "physical",
            ),
            "number.opossum_a_step": self._entity(
                "number.opossum_a_step",
                f"{entry_id}_app_slot_channel_a_step",
                "physical",
            ),
            "number.opossum_b_intensity": self._entity(
                "number.opossum_b_intensity",
                f"{entry_id}_app_slot_channel_b_intensity",
                "physical",
            ),
            "sensor.opossum_raw": self._entity(
                "sensor.opossum_raw",
                f"{entry_id}_app_slot_raw",
                "physical",
            ),
            "button.dg_lab_app_ping": self._entity(
                "button.dg_lab_app_ping",
                f"{entry_id}_app_ping",
                "app",
            ),
            "sensor.dg_lab_status": self._entity(
                "sensor.dg_lab_status",
                f"{entry_id}_status",
                "hub",
            ),
        }
        self.devices = {
            "physical": SimpleNamespace(
                identifiers={(DOMAIN, f"{entry_id}_device_app_slot")}
            ),
            "app": SimpleNamespace(
                identifiers={(DOMAIN, f"{entry_id}_app_app")}
            ),
            "hub": SimpleNamespace(identifiers={(DOMAIN, entry_id)}),
        }

    def _entity(
        self, entity_id: str, unique_id: str, device_id: str
    ) -> SimpleNamespace:
        """Build the entity-registry surface used by target resolution."""
        return SimpleNamespace(
            entity_id=entity_id,
            unique_id=unique_id,
            platform=DOMAIN,
            config_entry_id=self.client.entry.entry_id,
            device_id=device_id,
        )

    def _call(self, **data: object) -> SimpleNamespace:
        """Build the ServiceCall data surface used by target resolution."""
        return SimpleNamespace(data=data)

    def _registry_patches(self):
        """Patch Home Assistant registries with the test hierarchy."""
        entity_registry = SimpleNamespace(async_get=self.entities.get)
        device_registry = SimpleNamespace(async_get=self.devices.get)
        return (
            patch(
                "custom_components.dg_lab.er.async_get",
                return_value=entity_registry,
            ),
            patch(
                "custom_components.dg_lab.dr.async_get",
                return_value=device_registry,
            ),
        )

    def test_channel_entity_multiselect_resolves_and_deduplicates(self) -> None:
        """Several selected number entities can target several channels once each."""
        call = self._call(
            **{
                ATTR_ENTITY_ID: [
                    "number.opossum_a_intensity",
                    "number.opossum_a_step",
                    "number.opossum_b_intensity",
                ]
            }
        )

        entity_patch, device_patch = self._registry_patches()
        with entity_patch, device_patch:
            targets = _channel_targets_from_call(self.hass, call)

        self.assertEqual(
            [(target.client_id, target.slot_id, target.channel) for target in targets],
            [("app", "slot", 0), ("app", "slot", 1)],
        )

    def test_app_action_accepts_app_or_physical_device_entities(self) -> None:
        """An app action derives its app from either level and deduplicates it."""
        call = self._call(
            **{
                ATTR_ENTITY_ID: [
                    "button.dg_lab_app_ping",
                    "sensor.opossum_raw",
                    "sensor.dg_lab_status",
                ]
            }
        )

        entity_patch, device_patch = self._registry_patches()
        with entity_patch, device_patch:
            targets = _app_targets_from_call(self.hass, call)

        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].client_id, "app")

    def test_clear_action_uses_selected_entity_scope(self) -> None:
        """Hub, device, and channel entities map to their natural clear scope."""
        cases = (
            ("sensor.dg_lab_status", ("app", None, None)),
            ("sensor.opossum_raw", ("app", "slot", None)),
            ("number.opossum_a_intensity", ("app", "slot", 0)),
        )

        for entity_id, expected in cases:
            with self.subTest(entity_id=entity_id):
                entity_patch, device_patch = self._registry_patches()
                with entity_patch, device_patch:
                    targets = _clear_targets_from_call(
                        self.hass,
                        self._call(**{ATTR_ENTITY_ID: [entity_id]}),
                    )
                self.assertEqual(len(targets), 1)
                self.assertEqual(
                    (targets[0].client_id, targets[0].slot_id, targets[0].channel),
                    expected,
                )

    def test_channel_action_rejects_non_channel_entity(self) -> None:
        """Selecting an app entity gives a useful validation error."""
        entity_patch, device_patch = self._registry_patches()
        with entity_patch, device_patch, self.assertRaises(ServiceValidationError):
            _channel_targets_from_call(
                self.hass,
                self._call(**{ATTR_ENTITY_ID: ["button.dg_lab_app_ping"]}),
            )

    def test_legacy_raw_targeting_remains_supported(self) -> None:
        """Existing YAML automations using protocol IDs keep working."""
        target = _channel_targets_from_call(
            self.hass,
            self._call(
                **{
                    ATTR_CLIENT_ID: "app",
                    ATTR_SLOT_ID: "slot",
                    ATTR_CHANNEL: "B",
                }
            ),
        )[0]

        self.assertEqual(
            (target.client_id, target.slot_id, target.channel),
            ("app", "slot", 1),
        )

    def test_raw_rpc_can_require_an_explicit_app_target(self) -> None:
        """Potentially broad raw commands never default to every connected app."""
        with self.assertRaises(ServiceValidationError):
            _app_targets_from_call(
                self.hass,
                self._call(),
                require_explicit=True,
            )

    def test_entity_and_raw_targeting_cannot_be_mixed(self) -> None:
        """Ambiguous mixed targets fail before any device command is sent."""
        with self.assertRaises(ServiceValidationError):
            _channel_targets_from_call(
                self.hass,
                self._call(
                    **{
                        ATTR_ENTITY_ID: ["number.opossum_a_intensity"],
                        ATTR_CLIENT_ID: "app",
                    }
                ),
            )


if __name__ == "__main__":
    unittest.main()
