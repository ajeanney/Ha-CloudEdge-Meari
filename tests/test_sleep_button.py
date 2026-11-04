"""Sleep Camera: end the live session and hold off motion wake-ups until Wake."""

from __future__ import annotations

import importlib
import threading
import time
import unittest
from unittest.mock import Mock

from debug_tools.bootstrap import _bootstrap_integration_modules

_bootstrap_integration_modules()
COORDINATOR = importlib.import_module("custom_components.cloudplus.coordinator")


def make_coordinator():
    coord = object.__new__(COORDINATOR.CloudEdgeMeariCoordinator)
    coord.hass = Mock()
    coord.hass.loop.is_closed.return_value = False
    coord._update_callbacks = []
    coord._motion_callbacks = []
    coord._wake_event = threading.Event()
    coord._is_snap = True
    coord._motion_wake_enabled = True
    coord._motion_detected = False
    coord._motion_type = ""
    coord._motion_timeout = 120
    coord._last_motion_time = 0.0
    coord._live_deadline = 0.0
    coord._camera_awake = False
    return coord


class SleepButtonTests(unittest.TestCase):
    def test_sleep_ends_the_live_deadline(self):
        coord = make_coordinator()
        coord.wake_camera()
        self.assertGreater(coord._live_deadline, time.monotonic())
        coord.sleep_camera()
        self.assertEqual(coord._live_deadline, 0.0)
        self.assertFalse(coord._wake_event.is_set())

    def test_motion_does_not_wake_after_sleep(self):
        coord = make_coordinator()
        coord.wake_camera()
        coord.sleep_camera()
        coord._note_motion("pir")
        self.assertTrue(coord._motion_detected)
        self.assertFalse(coord._wake_event.is_set())
        self.assertEqual(coord._live_deadline, 0.0)

    def test_motion_clears_after_timeout_while_held(self):
        coord = make_coordinator()
        coord.sleep_camera()
        coord._note_motion("pir")
        coord._track_motion(time.monotonic())
        self.assertTrue(coord._motion_detected)
        coord._track_motion(time.monotonic() + coord._motion_timeout)
        self.assertFalse(coord._motion_detected)
        self.assertEqual(coord._live_deadline, 0.0)

    def test_wake_clears_the_hold(self):
        coord = make_coordinator()
        coord.sleep_camera()
        coord.wake_camera()
        coord._wake_event.clear()
        coord._note_motion("pir")
        self.assertTrue(coord._wake_event.is_set())

    def test_motion_wakes_when_never_put_to_sleep(self):
        coord = make_coordinator()
        coord._note_motion("pir")
        self.assertTrue(coord._wake_event.is_set())
        self.assertGreater(coord._live_deadline, time.monotonic())


if __name__ == "__main__":
    unittest.main()
