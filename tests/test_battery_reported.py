"""Battery freshness: read the battery right after a real wake and date the reading by it.

Meari's battery data has no timestamp and the camera only reports while awake, so
without this an old figure looks current (a charging camera "stuck" for hours).
"""

from __future__ import annotations

import importlib
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from debug_tools.bootstrap import _bootstrap_integration_modules

MODULES = _bootstrap_integration_modules()
COORDINATOR = MODULES["coordinator"].CloudEdgeMeariCoordinator


def make_coordinator(battery: dict[str, Any] | None = None) -> Any:
    """A sleeping battery camera's coordinator state, without threads or cloud."""
    coord = COORDINATOR.__new__(COORDINATOR)
    coord.hass = Mock()
    coord.hass.loop.is_closed.return_value = False
    coord._update_callbacks = []
    coord._iot_data = {}
    coord._sn_num = "synthetic-camera"
    coord._is_snap = True
    coord._camera_awake = False
    coord._battery_percent = 77
    coord._battery_charging = True
    coord._idle_frame_ready = threading.Event()
    coord._api = Mock()
    coord._api.get_battery_info.return_value = {"154": 81, "156": 1} if battery is None else battery
    return coord


def wake(coord: Any, video: bool) -> float:
    """One awake period; returns the wall time it ended."""
    coord._set_camera_awake(True)
    if video:
        coord._note_camera_video()
    coord._set_camera_awake(False)
    return time.time()


class BatteryReportedTests(unittest.TestCase):
    def test_unknown_until_a_reading_follows_a_wake(self):
        coord = make_coordinator()
        coord._poll_battery()  # the start-up read: the camera has not been awake
        self.assertEqual(coord.battery_percent, 81)
        self.assertIsNone(coord.battery_reported_at)

    def test_real_wake_reads_the_battery_now_and_dates_it(self):
        coord = make_coordinator()
        self.assertFalse(coord._snap_housekeeping(False))
        ended = wake(coord, video=True)
        self.assertTrue(coord._snap_housekeeping(False))  # read now, not up to 5 minutes later
        coord._poll_battery()
        self.assertEqual(coord.battery_percent, 81)
        self.assertAlmostEqual(coord.battery_reported_at.timestamp(), ended, delta=1)
        self.assertFalse(coord._snap_housekeeping(False))  # one read per wake

    def test_failed_wake_does_not_date_the_reading(self):
        coord = make_coordinator()
        wake(coord, video=False)  # woke in name only: no video, so no battery report either
        self.assertFalse(coord._snap_housekeeping(False))
        coord._poll_battery()
        self.assertIsNone(coord.battery_reported_at)

    def test_failed_read_after_a_wake_is_not_retried_every_pass(self):
        coord = make_coordinator()
        coord._api.get_battery_info.side_effect = RuntimeError("Battery info failed: {'resultCode': '1023'}")
        coord._reauthenticate_api = Mock()
        wake(coord, video=True)
        coord._poll_battery()
        self.assertIsNone(coord.battery_reported_at)
        self.assertFalse(coord._snap_housekeeping(False))  # back to the 5-minute interval

    def test_reading_without_a_battery_value_is_not_dated(self):
        coord = make_coordinator(battery={"156": 1})
        coord._api.openapi_server = None
        wake(coord, video=True)
        coord._poll_battery()
        self.assertIsNone(coord.battery_reported_at)

    def test_out_of_range_value_is_not_dated(self):
        coord = make_coordinator(battery={"154": 255, "156": 1})
        wake(coord, video=True)
        coord._poll_battery()
        self.assertEqual(coord.battery_percent, 77)  # rejected, as before
        self.assertIsNone(coord.battery_reported_at)

    def test_empty_battery_is_dated(self):
        coord = make_coordinator(battery={"154": 0, "156": 0})
        wake(coord, video=True)
        coord._poll_battery()
        self.assertEqual(coord.battery_percent, 0)
        self.assertIsNotNone(coord.battery_reported_at)

    def test_keyframes_from_the_stream_mark_a_real_wake(self):
        coord = make_coordinator()
        coord._idle_frame_lock = threading.Lock()
        coord._snapshot_conversion_enabled = False
        coord._set_camera_awake(True)
        coord._remember_idle_frame("h264", b"\x00\x00\x00\x01keyframe")  # the coordinator's own keyframe path
        coord._set_camera_awake(False)
        self.assertTrue(coord._idle_frame_ready.is_set())
        self.assertTrue(coord._snap_housekeeping(False))

    def test_wall_clock_step_during_a_wake_still_counts(self):
        coord = make_coordinator()
        coord._set_camera_awake(True)
        with patch("time.time", return_value=0.0):  # wall clock stepped back an epoch
            coord._note_camera_video()
            coord._set_camera_awake(False)
        self.assertTrue(coord._snap_housekeeping(False))

    def test_interval_still_reads(self):
        coord = make_coordinator()
        self.assertTrue(coord._snap_housekeeping(True))

    def test_restored_time_is_used_until_a_newer_report(self):
        coord = make_coordinator()
        before_restart = datetime.now(timezone.utc) - timedelta(minutes=20)
        coord.restore_battery_reported(before_restart)
        self.assertAlmostEqual(coord.battery_reported_at.timestamp(), before_restart.timestamp(), delta=0.001)
        ended = wake(coord, video=True)
        coord._poll_battery()
        self.assertAlmostEqual(coord.battery_reported_at.timestamp(), ended, delta=1)

    def test_restored_time_never_replaces_a_newer_one(self):
        coord = make_coordinator()
        ended = wake(coord, video=True)
        coord._poll_battery()  # read and dated before the sensor restored its old state
        coord.restore_battery_reported(datetime.now(timezone.utc) - timedelta(hours=3))
        self.assertAlmostEqual(coord.battery_reported_at.timestamp(), ended, delta=1)


class BatteryReportedSensorTests(unittest.IsolatedAsyncioTestCase):
    async def test_sensor_restores_its_last_time_after_a_restart(self):
        sensor_platform = importlib.import_module("tests.test_iot_entities").PLATFORMS["sensor"]
        coord = make_coordinator()
        coord._device_name = "Synthetic camera"
        coord._device_category = "snap"
        sensor = sensor_platform.CloudEdgeMeariBatteryReportedSensor(coord, SimpleNamespace(entry_id="test"))
        before_restart = datetime(2026, 11, 8, 20, 17, 13, tzinfo=timezone.utc)
        sensor.async_get_last_sensor_data = AsyncMock(return_value=SimpleNamespace(native_value=before_restart))
        await sensor.async_added_to_hass()
        self.assertEqual(sensor.native_value, before_restart)

    async def test_nothing_to_restore_stays_unknown(self):
        sensor_platform = importlib.import_module("tests.test_iot_entities").PLATFORMS["sensor"]
        coord = make_coordinator()
        coord._device_name = "Synthetic camera"
        coord._device_category = "snap"
        sensor = sensor_platform.CloudEdgeMeariBatteryReportedSensor(coord, SimpleNamespace(entry_id="test"))
        sensor.async_get_last_sensor_data = AsyncMock(return_value=SimpleNamespace(native_value=None))
        await sensor.async_added_to_hass()
        self.assertIsNone(sensor.native_value)


if __name__ == "__main__":
    unittest.main()
