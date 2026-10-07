"""Offline doorbell-press regressions with synthetic payloads."""

from __future__ import annotations

import importlib
import json
import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from debug_tools.bootstrap import _bootstrap_integration_modules

MODULES = _bootstrap_integration_modules()
CONST = MODULES["const"]
MOTION = importlib.import_module("custom_components.cloudplus.coordinator.motion")
PARSER = importlib.import_module("custom_components.cloudplus.motion_event")
COORDINATOR = MODULES["coordinator"].CloudEdgeMeariCoordinator
# Synthetic identifiers only; no camera captures or account data.
SERIAL = "synthetic-doorbell"
VISITOR = 3


def load_event_platform() -> ModuleType:
    """Import event.py against a minimal stub of HA's event component."""
    class EventEntity:
        """Records triggers the way HA's EventEntity would."""

        def _trigger_event(self, event_type, attributes=None):
            self.triggered = (event_type, attributes)

    stub = ModuleType("homeassistant.components.event")
    stub.EventEntity = EventEntity
    stub.EventDeviceClass = SimpleNamespace(DOORBELL="doorbell")
    with patch.dict(sys.modules, {"homeassistant.components.event": stub}):
        return importlib.import_module("custom_components.cloudplus.event")


EVENT = load_event_platform()


def payload(**fields) -> bytes:
    event = {"deviceID": "42", "eventType": VISITOR, "msgID": "m1"}
    event.update(fields)
    return json.dumps({"params": event}).encode()


class ParserTests(unittest.TestCase):
    """Doorbell and motion alarm types stay separate."""

    def test_type_sets_do_not_overlap(self):
        self.assertFalse(CONST.DOORBELL_ALARM_TYPES & CONST.MOTION_ALARM_TYPES)

    def test_visitor_is_doorbell_not_motion(self):
        event = PARSER.parse_motion_event(payload())
        self.assertTrue(event["is_doorbell"])
        self.assertFalse(event["is_motion"])
        self.assertEqual(event["evt_name"], "Visitor")
        self.assertEqual(event["msg_id"], "m1")

    def test_motion_is_not_doorbell(self):
        event = PARSER.parse_motion_event(payload(eventType=2))
        self.assertTrue(event["is_motion"])
        self.assertFalse(event["is_doorbell"])

    def test_doorbell_entry_preferred_over_unrelated_first_entry(self):
        raw = {"items": [{"deviceID": "42", "eventType": 21},
                         {"deviceID": "42", "eventType": VISITOR}]}
        event = PARSER.parse_motion_event(json.dumps(raw).encode())
        self.assertTrue(event["is_doorbell"])


class ListenerTests(unittest.TestCase):
    """Routing and de-duplication across MQTT, event log and summary."""

    def setUp(self):
        self.api = Mock()
        self.api.get_new_device_events.return_value = []
        self.listener = MOTION.MotionEventListener(self.api)
        self.motion, self.ring = Mock(), Mock()
        self.other_motion, self.other_ring = Mock(), Mock()
        self.listener.register(42, SERIAL, self.motion, self.ring)
        self.listener.register(43, "other", self.other_motion, self.other_ring)

    def poll(self, events, dispatch=True):
        self.api.get_device_events.side_effect = (
            lambda device, _day: list(events) if device == "42" else []
        )
        self.listener._poll_device_events(dispatch=dispatch)

    def test_mqtt_ring_reaches_owning_camera_only(self):
        self.listener._handle_payload(payload())
        self.ring.assert_called_once_with("Visitor")
        self.motion.assert_not_called()
        self.other_ring.assert_not_called()
        self.api.download_snapshot.assert_not_called()

    def test_serial_only_routing(self):
        self.listener._handle_payload(payload(deviceID="", licenseID=SERIAL))
        self.ring.assert_called_once_with("Visitor")

    def test_motion_does_not_ring(self):
        self.api.download_snapshot.return_value = None
        self.listener._handle_payload(payload(eventType=2))
        self.motion.assert_called_once_with("Motion", None)
        self.ring.assert_not_called()

    def test_camera_without_doorbell_callback_ignores_ring(self):
        listener = MOTION.MotionEventListener(self.api)
        motion = Mock()
        listener.register(42, SERIAL, motion)
        listener._handle_payload(payload())
        motion.assert_not_called()

    def test_poll_seeds_then_rings_once_per_msg_id(self):
        old = {"deviceID": "42", "eventType": VISITOR, "msgID": "old"}
        self.poll([old], dispatch=False)
        self.ring.assert_not_called()
        new = dict(old, msgID="new")
        self.poll([old, new])
        self.poll([old, new])
        self.ring.assert_called_once_with("Visitor")

    def test_mqtt_then_poll_rings_once(self):
        self.poll([], dispatch=False)
        self.listener._handle_payload(payload(msgID="m7"))
        self.poll([{"deviceID": "42", "eventType": VISITOR, "msgID": "m7"}])
        self.ring.assert_called_once_with("Visitor")

    def test_mqtt_redelivery_rings_once(self):
        self.listener._handle_payload(payload(msgID="m8"))
        self.listener._handle_payload(payload(msgID="m8"))
        self.ring.assert_called_once()

    def test_summary_row_skipped_when_event_log_answered(self):
        self.poll([], dispatch=False)
        summary = {"deviceID": "42", "imageAlertType": VISITOR,
                   "devLocalTime": "t1"}
        self.api.get_new_device_events.return_value = [summary]
        self.poll([{"deviceID": "42", "eventType": VISITOR, "msgID": "m9"}])
        self.ring.assert_called_once_with("Visitor")

    def test_summary_row_rings_when_event_log_unavailable(self):
        self.poll([], dispatch=False)
        self.api.get_device_events.side_effect = OSError("event log down")
        self.api.get_new_device_events.return_value = [
            {"deviceID": "42", "imageAlertType": VISITOR, "devLocalTime": "t2"}
        ]
        self.listener._poll_device_events(dispatch=True)
        self.ring.assert_called_once_with("Visitor")


class CoordinatorAndEntityTests(unittest.TestCase):
    """Category gating, thread hand-off and the event entity itself."""

    def coordinator(self, category):
        coord = COORDINATOR.__new__(COORDINATOR)
        coord._device_category = category
        coord._doorbell_callbacks = []
        coord._sn_num = SERIAL
        coord._device_name = "Synthetic doorbell"
        coord._device = {}
        coord.hass = SimpleNamespace(loop=Mock())
        coord.hass.loop.is_closed.return_value = False
        return coord

    def test_doorbell_categories(self):
        for category, expected in [("doorbell", True), ("picturedoorbell", True),
                                   ("voicebell", True), ("snap", False),
                                   ("ipc", False)]:
            with self.subTest(category=category):
                self.assertEqual(self.coordinator(category).is_doorbell, expected)

    def test_ring_is_handed_to_ha_loop(self):
        coord = self.coordinator("doorbell")
        callback = Mock()
        unsubscribe = coord.register_doorbell_callback(callback)
        coord._note_doorbell("Visitor")
        coord.hass.loop.call_soon_threadsafe.assert_called_once_with(
            callback, "Visitor")
        unsubscribe()
        self.assertEqual(coord._doorbell_callbacks, [])

    def test_entity_created_only_for_doorbells(self):
        for category, count in [("doorbell", 1), ("ipc", 0)]:
            with self.subTest(category=category):
                coord = self.coordinator(category)
                add = Mock()
                hass = SimpleNamespace(data={CONST.DOMAIN: {"entry": coord}})
                entry = SimpleNamespace(entry_id="entry")
                coro = EVENT.async_setup_entry(hass, entry, add)
                with self.assertRaises(StopIteration):
                    coro.send(None)
                self.assertEqual(add.call_count, count)

    def test_entity_fires_ring(self):
        coord = self.coordinator("doorbell")
        entity = EVENT.CloudEdgeMeariDoorbellEvent(coord, SimpleNamespace())
        entity.async_write_ha_state = Mock()
        self.assertEqual(entity._attr_event_types, ["ring"])
        self.assertEqual(entity._attr_device_class, "doorbell")
        entity._handle_ring("Visitor")
        self.assertEqual(entity.triggered, ("ring", {"alarm_type": "Visitor"}))
        entity.async_write_ha_state.assert_called_once()


class EventsCommandTests(unittest.TestCase):
    """``debug.py events`` mirrors HA routing without any cloud traffic."""

    def test_prints_rings_for_doorbells_only(self):
        events_cmd = importlib.import_module("debug_tools.events_cmd")
        listener = MOTION.MotionEventListener(Mock())
        devices = [
            {"deviceID": 42, "snNum": SERIAL, "deviceName": "Front door",
             "_category": "doorbell"},
            {"deviceID": 43, "snNum": "other", "deviceName": "Garden",
             "_category": "snap"},
        ]
        with patch("builtins.print") as printed:
            for dev in devices:
                events_cmd._register_camera(
                    listener, dev, CONST.DOORBELL_CATEGORIES)
            listener._handle_payload(payload())
            listener._handle_payload(payload(deviceID="43", msgID="m2"))
        lines = [call.args[0] for call in printed.call_args_list]
        rings = [line for line in lines if "RING" in line]
        self.assertEqual(len(rings), 1)
        self.assertIn("Front door: Visitor", rings[0])
        self.assertFalse(any(SERIAL in line for line in lines))


if __name__ == "__main__":
    unittest.main()
