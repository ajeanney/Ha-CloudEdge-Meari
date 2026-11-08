"""Charging Mode: keep a battery camera asleep (no wake-ups, lights and detection off) so it can charge."""

from __future__ import annotations

import asyncio
import importlib
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

from debug_tools.bootstrap import _bootstrap_integration_modules

MODULES = _bootstrap_integration_modules()
COORDINATOR = MODULES["coordinator"].CloudEdgeMeariCoordinator
CONST = importlib.import_module("custom_components.cloudplus.const")
PIR = str(importlib.import_module("custom_components.cloudplus.meari_commands").PIR_DET_ENABLE)
CHARGING_CHECK_IN_S = importlib.import_module("custom_components.cloudplus.coordinator.state").CHARGING_CHECK_IN_S


def load_switch() -> ModuleType:
    """Import the switch platform with the one HA stub it needs beyond the bootstrap."""
    switch_stub = ModuleType("homeassistant.components.switch")
    switch_stub.SwitchEntity = type("SwitchEntity", (), {})
    with patch.dict(sys.modules, {"homeassistant.components.switch": switch_stub}):
        return importlib.import_module("custom_components.cloudplus.switch")


SWITCH = load_switch()


def load_button() -> ModuleType:
    """Import the button platform with the HA stubs it needs beyond the bootstrap."""
    button_stub = ModuleType("homeassistant.components.button")
    button_stub.ButtonEntity = type("ButtonEntity", (), {})
    exceptions_stub = ModuleType("homeassistant.exceptions")
    exceptions_stub.ServiceValidationError = type("ServiceValidationError", (Exception,), {})
    with patch.dict(sys.modules, {"homeassistant.components.button": button_stub,
                                  "homeassistant.exceptions": exceptions_stub}):
        module = importlib.import_module("custom_components.cloudplus.button")
    return module


def make_coordinator(iot: dict[str, Any] | None = None, options: dict[str, Any] | None = None,
                     write_ok: bool = True) -> Any:
    """A battery camera's coordinator state, without threads or cloud."""
    coord = COORDINATOR.__new__(COORDINATOR)
    coord.hass = Mock()
    coord.hass.loop.is_closed.return_value = False
    coord._entry = SimpleNamespace(entry_id="test", options=dict(options or {}), async_on_unload=Mock())
    coord._capabilities = {}
    coord._device = {}
    coord._iot_data = dict(iot or {})
    coord._api = Mock()
    coord._api.set_device_iot_value.return_value = write_ok
    coord._sn_num = "synthetic-camera"
    coord._device_name = "Synthetic battery camera"
    coord._device_category = "snap"
    coord._available = True
    coord._has_lamp = False
    coord._lamp_on = False
    coord._update_callbacks = []
    coord._motion_callbacks = []
    coord._wake_event = threading.Event()
    coord._idle_frame_refresh = threading.Event()
    coord._is_snap = True
    coord._motion_wake_enabled = True
    coord._motion_detected = False
    coord._motion_type = ""
    coord._motion_timeout = 120
    coord._last_motion_time = 0.0
    coord._live_deadline = 0.0
    coord._camera_awake = False
    return coord


MC = importlib.import_module("custom_components.cloudplus.meari_commands")
LED, MOTION, PERSON, SOUND, SD = (str(c) for c in (MC.LED_ENABLE, MC.MOTION_DET_ENABLE, MC.HUMAN_DET_ENABLE,
                                                    MC.SOUND_DET_ENABLE, MC.RECORD_SWITCH))
FLOOD, OSD, LAMP = str(MC.FLIGHT_LINK_LIGHTING_ENABLE), str(MC.OSD_ENABLE), str(MC.FLIGHT_LIGHT_SWITCH)
# A solar floodlight camera's settings: battery spenders on and off, plus the OSD watermark, which costs nothing.
CAMERA_IOT = {LAMP: 1, LED: 1, PIR: 1, MOTION: 1, PERSON: 1, SOUND: 0, SD: 1, FLOOD: 0, OSD: 1}
SPENDERS_ON = sorted([LAMP, LED, PIR, MOTION, PERSON, SD], key=int)
RESTORABLE = sorted([LED, PIR, MOTION, PERSON, SD], key=int)  # the lamp goes off but does not come back on
CAMERA_AFTER = {**CAMERA_IOT, LAMP: 0}  # the settings after a Charging Mode on/off cycle


def writes(coord: Any) -> list[tuple[str, Any]]:
    return [(c.args[1], c.args[2]) for c in coord._api.set_device_iot_value.call_args_list]


class ChargingModeCoordinatorTests(unittest.TestCase):
    def test_motion_does_not_wake_in_charging_mode(self):
        coord = make_coordinator(options={CONST.CONF_CHARGING_MODE: True})
        coord._note_motion("pir")
        self.assertTrue(coord._motion_detected)  # the Motion sensor still updates
        self.assertFalse(coord._wake_event.is_set())
        self.assertEqual(coord._live_deadline, 0.0)

    def test_motion_clears_after_timeout_in_charging_mode(self):
        coord = make_coordinator(options={CONST.CONF_CHARGING_MODE: True})
        coord._note_motion("pir")
        coord._track_motion(time.monotonic() + coord._motion_timeout)
        self.assertFalse(coord._motion_detected)
        self.assertEqual(coord._live_deadline, 0.0)

    def test_motion_wakes_again_once_charging_mode_is_off(self):
        coord = make_coordinator(options={CONST.CONF_CHARGING_MODE: False})
        coord._note_motion("pir")
        self.assertTrue(coord._wake_event.is_set())

    def test_wake_on_motion_does_not_override_charging_mode(self):
        coord = make_coordinator(options={CONST.CONF_CHARGING_MODE: True})
        coord._motion_wake_enabled = False
        coord._note_motion("pir")
        coord.set_motion_wake_enabled(True)  # turned on while motion is still detected
        self.assertFalse(coord._wake_event.is_set())
        self.assertEqual(coord._live_deadline, 0.0)
        coord._note_motion("pir")
        self.assertFalse(coord._wake_event.is_set())

    def test_wake_button_does_nothing_in_charging_mode(self):
        coord = make_coordinator(options={CONST.CONF_CHARGING_MODE: True})
        with self.assertLogs(level="INFO"):
            coord.wake_camera()
        self.assertFalse(coord._wake_event.is_set())
        self.assertEqual(coord._live_deadline, 0.0)
        self.assertFalse(coord._camera_awake)

    def test_wake_button_wakes_once_charging_mode_is_off(self):
        coord = make_coordinator(options={CONST.CONF_CHARGING_MODE: False})
        coord.wake_camera()
        self.assertTrue(coord._wake_event.is_set())
        self.assertGreater(coord._live_deadline, time.monotonic())

    def test_end_live_session_does_not_hold_motion(self):
        coord = make_coordinator()
        coord.wake_camera()
        coord.end_live_session()
        self.assertEqual(coord._live_deadline, 0.0)
        self.assertFalse(coord._wake_event.is_set())
        coord._note_motion("pir")
        self.assertTrue(coord._wake_event.is_set())

    def test_settings_that_spend_battery_go_off_and_back_on(self):
        coord = make_coordinator(iot=CAMERA_IOT)
        coord._has_lamp = coord._lamp_on = True
        now_on = coord.charging_settings_on()
        self.assertEqual(sorted(now_on, key=int), SPENDERS_ON)
        self.assertEqual(coord.charging_settings_off(now_on), [])
        self.assertTrue(all(coord.get_iot_value(code) == 0 for code in SPENDERS_ON))
        self.assertFalse(coord.lamp_on)  # the Lamp switch follows
        self.assertEqual(coord.get_iot_value(OSD), 1)  # costs nothing, left alone
        self.assertEqual(sorted(code for code, _ in writes(coord)), sorted(SPENDERS_ON))  # off ones untouched
        self.assertEqual(coord.charging_settings_restore(now_on), [])
        self.assertTrue(all(coord.get_iot_value(code) == 1 for code in SPENDERS_ON))
        self.assertTrue(coord.lamp_on)
        self.assertEqual((coord.get_iot_value(SOUND), coord.get_iot_value(FLOOD)), (0, 0))  # were off, stay off

    def test_unknown_values_are_left_alone(self):
        coord = make_coordinator(iot={PIR: None})
        self.assertEqual(coord.charging_settings_on(), [])

    def test_failed_write_is_returned(self):
        coord = make_coordinator(iot={LED: 1, PIR: 1}, write_ok=False)
        with self.assertLogs(level="WARNING"):
            self.assertEqual(coord.charging_settings_off([LED, PIR]), [LED, PIR])

    def test_write_that_raises_is_a_failure_not_a_crash(self):
        coord = make_coordinator(iot={LED: 1, PIR: 1})
        coord._api.set_device_iot_value.side_effect = lambda sn, code, value: (_ for _ in ()).throw(
            OSError("read timeout")) if code == LED else True
        with self.assertLogs(level="WARNING"):
            self.assertEqual(coord.charging_settings_off([LED, PIR]), [LED])
        self.assertEqual(coord.get_iot_value(PIR), 0)

    def test_failed_restore_is_returned(self):
        coord = make_coordinator(iot={LED: 1, PIR: 1})
        coord.charging_settings_off([LED, PIR])
        coord._api.set_device_iot_value.side_effect = lambda sn, code, value: code != PIR
        with self.assertLogs(level="WARNING"):
            self.assertEqual(coord.charging_settings_restore([LED, PIR]), [PIR])
        self.assertEqual((coord.get_iot_value(LED), coord.get_iot_value(PIR)), (1, 0))

    def test_motion_timeout_change_does_not_start_a_live_session(self):
        coord = make_coordinator(options={CONST.CONF_CHARGING_MODE: True})
        coord._camera_awake = True  # during a check-in still grab
        coord.set_motion_timeout(600)
        self.assertEqual(coord._live_deadline, 0.0)

    def test_stray_wake_request_is_dropped(self):
        coord = make_coordinator(options={CONST.CONF_CHARGING_MODE: True})
        coord._wake_event.set()  # e.g. motion that passed its check just before Charging Mode went on
        self.assertFalse(coord._wake_requested())
        self.assertFalse(coord._wake_event.is_set())
        coord._entry.options[CONST.CONF_CHARGING_MODE] = False
        coord._wake_event.set()
        self.assertTrue(coord._wake_requested())

    def test_stray_wake_event_sends_no_wake(self):
        coord = make_coordinator(options={CONST.CONF_CHARGING_MODE: True})
        coord._send_wake = Mock()
        coord._wake_event.set()
        coord._consume_wake_event()  # the coordinator's own path, run every snap-loop pass
        coord._send_wake.assert_not_called()
        coord._entry.options[CONST.CONF_CHARGING_MODE] = False
        coord._wake_event.set()
        coord._consume_wake_event()
        coord._send_wake.assert_called_once()

    def test_pan_tilt_does_not_wake_it(self):
        coord = make_coordinator(options={CONST.CONF_CHARGING_MODE: True})
        self.assertFalse(coord.ptz_move("left"))
        self.assertFalse(coord.ptz_stop())
        coord._api.ptz_start.assert_not_called()
        coord._api.ptz_stop.assert_not_called()


class ChargingModeIdleGrabTests(unittest.TestCase):
    """The idle still grab wakes the camera, so Charging Mode skips it, at start and on refresh."""

    @staticmethod
    def grabs(charging: bool, refresh: bool = False, check_in_due: bool = False) -> int:
        coord = make_coordinator(options={CONST.CONF_CHARGING_MODE: charging})
        coord._initial_frame_grab = True
        coord._idle_frame_ready = threading.Event()
        coord._idle_frame_refresh = threading.Event()
        coord._stream_restart = threading.Event()
        if check_in_due:
            coord._idle_frame_ready.set()  # it has a still; only the check-in may wake it
            coord._check_in_at = 1.0  # an hour since the last wake has passed
        if refresh:
            coord._idle_frame_ready.set()  # start-up grab done; a quality change asks for a new still
            coord._idle_frame_refresh.set()
        coord._poll_battery = Mock()
        coord._poll_status = Mock()
        coord._run_initial_frame_grab = Mock(return_value=True)
        coord._running = True

        def stop_after_one_pass(_seconds):
            coord._running = False

        with patch("time.sleep", side_effect=stop_after_one_pass):
            coord._snap_session_loop()
        return coord._run_initial_frame_grab.call_count

    def test_start_grabs_a_still_normally(self):
        self.assertEqual(self.grabs(charging=False), 1)

    def test_start_does_not_wake_the_camera_in_charging_mode(self):
        self.assertEqual(self.grabs(charging=True), 0)

    def test_refresh_does_not_wake_the_camera_in_charging_mode(self):
        self.assertEqual(self.grabs(charging=False, refresh=True), 1)
        self.assertEqual(self.grabs(charging=True, refresh=True), 0)

    def test_hourly_check_in_wakes_it_briefly_in_charging_mode(self):
        self.assertEqual(self.grabs(charging=True, check_in_due=True), 1)

    def test_no_check_in_outside_charging_mode(self):
        self.assertEqual(self.grabs(charging=False, check_in_due=True), 0)  # motion wakes it anyway


class ChargingModeCheckInTests(unittest.TestCase):
    """While Charging Mode keeps it asleep, an hourly check-in lets the camera report its battery."""

    @staticmethod
    def coordinator(charging: bool = True) -> Any:
        coord = make_coordinator(options={CONST.CONF_CHARGING_MODE: charging})
        coord._initial_frame_grab = True
        coord._idle_frame_refresh = threading.Event()
        return coord

    def test_after_a_restart_the_check_in_follows_the_last_report(self):
        coord = self.coordinator()
        coord.restore_battery_reported(datetime.now(timezone.utc) - timedelta(minutes=20))
        coord._snap_housekeeping(False)
        self.assertAlmostEqual(coord._check_in_at - time.monotonic(), CHARGING_CHECK_IN_S - 20 * 60, delta=5)
        self.assertFalse(coord._check_in_requested)  # the restart itself does not wake it

    def test_after_a_restart_an_overdue_check_in_runs(self):
        coord = self.coordinator()
        coord.restore_battery_reported(datetime.now(timezone.utc) - timedelta(hours=3))
        coord._snap_housekeeping(False)  # schedules (due now)
        coord._snap_housekeeping(False)
        self.assertTrue(coord._check_in_requested)

    def test_restore_after_the_first_pass_reschedules(self):
        coord = self.coordinator()
        coord._snap_housekeeping(False)  # the loop started before the sensor restored its state
        coord.restore_battery_reported(datetime.now(timezone.utc) - timedelta(minutes=50))
        coord._snap_housekeeping(False)
        self.assertAlmostEqual(coord._check_in_at - time.monotonic(), CHARGING_CHECK_IN_S - 50 * 60, delta=5)

    def test_first_pass_schedules_an_hour_out(self):
        coord = self.coordinator()
        coord._snap_housekeeping(False)
        self.assertGreater(coord._check_in_at, time.monotonic() + CHARGING_CHECK_IN_S - 5)
        self.assertFalse(coord._check_in_requested)
        self.assertFalse(coord._idle_grab_allowed)  # a restart does not wake it

    def test_due_check_in_is_requested_once_then_rescheduled_by_the_wake(self):
        coord = self.coordinator()
        coord._check_in_at = 1.0
        coord._snap_housekeeping(False)
        self.assertTrue(coord._check_in_requested)
        self.assertTrue(coord._idle_frame_refresh.is_set())
        self.assertTrue(coord._idle_grab_allowed)
        coord._set_camera_awake(True)
        coord._set_camera_awake(False)
        self.assertFalse(coord._check_in_requested)
        self.assertFalse(coord._idle_grab_allowed)
        self.assertGreater(coord._check_in_at, time.monotonic() + CHARGING_CHECK_IN_S - 5)

    def test_any_wake_pushes_the_next_check_in_back(self):
        coord = self.coordinator(charging=False)
        coord._check_in_at = 1.0
        coord._snap_housekeeping(False)
        self.assertFalse(coord._check_in_requested)
        coord._set_camera_awake(True)
        coord._set_camera_awake(False)
        self.assertGreater(coord._check_in_at, time.monotonic() + CHARGING_CHECK_IN_S - 5)


class ChargingModeSwitchTests(unittest.IsolatedAsyncioTestCase):
    async def setup(self, coord: Any) -> tuple[Any, list]:
        hass = SimpleNamespace(data={SWITCH.DOMAIN: {coord._entry.entry_id: coord}})
        add_entities = Mock()
        await SWITCH.async_setup_entry(hass, coord._entry, add_entities)
        found = [e for c in add_entities.call_args_list for e in c.args[0]
                 if isinstance(e, SWITCH.CloudEdgeMeariChargingModeSwitch)]
        for entity in found:
            entity.hass = SimpleNamespace(config_entries=Mock(), async_add_executor_job=self.run_job)
            entity.hass.config_entries.async_update_entry.side_effect = (
                lambda entry, options: setattr(entry, "options", options))
            entity.async_write_ha_state = Mock()
        return coord, found

    @staticmethod
    async def run_job(func, *args):
        return func(*args)

    async def test_only_battery_cameras_get_the_switch(self):
        coord = make_coordinator()
        _, found = await self.setup(coord)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]._attr_unique_id, "synthetic-camera_charging_mode")
        mains = make_coordinator()
        mains._is_snap = False
        _, found = await self.setup(mains)
        self.assertEqual(found, [])

    async def test_turn_on_then_off(self):
        coord = make_coordinator(iot=CAMERA_IOT)
        coord.wake_camera()
        _, (switch,) = await self.setup(coord)
        self.assertFalse(switch.is_on)

        await switch.async_turn_on()
        self.assertTrue(switch.is_on)
        self.assertEqual(coord._entry.options,
                         {CONST.CONF_CHARGING_MODE: True, CONST.CONF_CHARGING_RESTORE: RESTORABLE})
        self.assertEqual(coord._live_deadline, 0.0)  # the live session ends
        self.assertTrue(all(coord.get_iot_value(code) == 0 for code in SPENDERS_ON))
        coord._note_motion("pir")
        self.assertFalse(coord._wake_event.is_set())

        await switch.async_turn_off()
        self.assertFalse(switch.is_on)
        self.assertEqual(coord._entry.options, {CONST.CONF_CHARGING_MODE: False})
        self.assertEqual({code: coord.get_iot_value(code) for code in CAMERA_IOT}, CAMERA_AFTER)
        coord._note_motion("pir")
        self.assertTrue(coord._wake_event.is_set())

    async def test_lamp_goes_off_but_does_not_come_back_on(self):
        coord = make_coordinator(iot=CAMERA_IOT)
        coord._has_lamp = coord._lamp_on = True  # left on at night
        _, (switch,) = await self.setup(coord)
        await switch.async_turn_on()
        self.assertFalse(coord.lamp_on)
        await switch.async_turn_off()  # at noon
        self.assertFalse(coord.lamp_on)

    async def test_turning_on_again_catches_a_setting_turned_back_on(self):
        coord = make_coordinator(iot=CAMERA_IOT)
        _, (switch,) = await self.setup(coord)
        await switch.async_turn_on()
        coord.set_iot_value(LED, 1)  # someone turns the status LED back on meanwhile
        await switch.async_turn_on()
        self.assertEqual(coord.get_iot_value(LED), 0)
        self.assertEqual(coord._entry.options[CONST.CONF_CHARGING_RESTORE], RESTORABLE)
        await switch.async_turn_off()
        self.assertEqual({code: coord.get_iot_value(code) for code in CAMERA_IOT}, CAMERA_AFTER)

    async def test_failed_restore_is_retried_next_cycle(self):
        coord = make_coordinator(iot=CAMERA_IOT)
        _, (switch,) = await self.setup(coord)
        await switch.async_turn_on()
        coord._api.set_device_iot_value.side_effect = lambda sn, code, value: code != PIR
        with self.assertLogs(level="WARNING"):
            await switch.async_turn_off()
        self.assertEqual(coord._entry.options, {CONST.CONF_CHARGING_MODE: False, CONST.CONF_CHARGING_RESTORE: [PIR]})
        coord._api.set_device_iot_value.side_effect = None
        await switch.async_turn_on()
        await switch.async_turn_off()
        self.assertEqual(coord._entry.options, {CONST.CONF_CHARGING_MODE: False})
        self.assertEqual({code: coord.get_iot_value(code) for code in CAMERA_IOT}, CAMERA_AFTER)

    async def test_restart_in_charging_mode_keeps_motion_held(self):
        coord = make_coordinator(iot={**CAMERA_IOT, PIR: 0},
                                 options={CONST.CONF_CHARGING_MODE: True, CONST.CONF_CHARGING_RESTORE: [PIR]})
        _, (switch,) = await self.setup(coord)
        self.assertTrue(switch.is_on)
        coord._note_motion("pir")
        self.assertFalse(coord._wake_event.is_set())
        await switch.async_turn_off()
        self.assertEqual(writes(coord), [(PIR, 1)])  # only what Charging Mode turned off

    async def test_write_error_while_turning_on_loses_no_setting(self):
        coord = make_coordinator(iot=CAMERA_IOT)
        _, (switch,) = await self.setup(coord)
        calls = []

        def flaky(_sn, code, _value):
            calls.append(code)
            if len(calls) == 3:
                raise OSError("read timeout")  # the third cloud write fails
            return True

        coord._api.set_device_iot_value.side_effect = flaky
        with self.assertLogs(level="WARNING"):
            await switch.async_turn_on()
        self.assertEqual(coord._entry.options[CONST.CONF_CHARGING_RESTORE], RESTORABLE)  # saved before writing
        coord._api.set_device_iot_value.side_effect = None
        await switch.async_turn_off()
        self.assertEqual({code: coord.get_iot_value(code) for code in CAMERA_IOT}, CAMERA_AFTER)

    async def test_overlapping_on_and_off_end_as_the_last_action(self):
        coord = make_coordinator(iot=CAMERA_IOT)
        _, (switch,) = await self.setup(coord)

        async def slow_job(func, *args):
            await asyncio.sleep(0.01)  # cloud writes take a while; the other call can start meanwhile
            return func(*args)

        switch.hass.async_add_executor_job = slow_job
        await asyncio.gather(switch.async_turn_on(), switch.async_turn_off())
        self.assertFalse(switch.is_on)
        self.assertEqual(coord._entry.options, {CONST.CONF_CHARGING_MODE: False})
        self.assertEqual({code: coord.get_iot_value(code) for code in CAMERA_IOT}, CAMERA_AFTER)

        await switch.async_turn_on()
        await asyncio.gather(switch.async_turn_off(), switch.async_turn_on())  # off, then on again at once
        self.assertTrue(switch.is_on)
        self.assertEqual(coord._entry.options[CONST.CONF_CHARGING_RESTORE], RESTORABLE)
        self.assertTrue(all(coord.get_iot_value(code) == 0 for code in SPENDERS_ON))

    async def test_each_change_ends_with_a_check_in(self):
        coord = make_coordinator(iot=CAMERA_IOT)
        coord._initial_frame_grab = True
        _, (switch,) = await self.setup(coord)
        await switch.async_turn_on()
        self.assertTrue(coord._idle_grab_allowed)  # the camera picks the new settings up now
        self.assertTrue(coord._idle_frame_refresh.is_set())
        coord._set_camera_awake(True)
        coord._set_camera_awake(False)
        coord._idle_frame_refresh.clear()
        self.assertFalse(coord._idle_grab_allowed)  # then asleep until the hourly check-in
        await switch.async_turn_off()
        self.assertTrue(coord._idle_frame_refresh.is_set())  # PIR and the rest go back on now, too

    async def test_calls_that_change_nothing_do_not_wake_it(self):
        coord = make_coordinator(iot=CAMERA_IOT)
        coord._initial_frame_grab = True
        _, (switch,) = await self.setup(coord)
        await switch.async_turn_off()  # already off, nothing to restore (an automation repeating itself)
        self.assertFalse(coord._check_in_requested)
        await switch.async_turn_on()
        coord._set_camera_awake(True)
        coord._set_camera_awake(False)  # the check-in after turning on
        await switch.async_turn_on()  # already on, nothing left on
        self.assertFalse(coord._check_in_requested)
        self.assertEqual(sorted(writes(coord)), sorted((code, 0) for code in SPENDERS_ON))  # no second round

    async def test_check_in_asked_for_during_a_live_session_still_runs(self):
        coord = make_coordinator(iot=CAMERA_IOT)
        coord._initial_frame_grab = True
        _, (switch,) = await self.setup(coord)
        coord._set_camera_awake(True)  # a live session is running
        await switch.async_turn_on()
        coord._set_camera_awake(False)  # the session ends after the request
        self.assertTrue(coord._check_in_requested)
        self.assertTrue(coord._idle_grab_allowed)

    async def test_details_list_what_will_come_back_and_what_failed(self):
        coord = make_coordinator(iot=CAMERA_IOT)
        _, (switch,) = await self.setup(coord)
        self.assertEqual(switch.extra_state_attributes, {})
        await switch.async_turn_on()
        self.assertEqual(switch.extra_state_attributes["will_restore"],
                         ["Status LED", "PIR Sensor", "Motion Detection", "Person Detection", "SD Recording"])
        coord._api.set_device_iot_value.side_effect = lambda sn, code, value: code != PIR
        with self.assertLogs(level="WARNING"):
            await switch.async_turn_off()
        self.assertEqual(switch.extra_state_attributes, {"restore_failed": ["PIR Sensor"]})


class ChargingModeWakeButtonTests(unittest.IsolatedAsyncioTestCase):
    async def test_wake_button_says_why_it_will_not_wake(self):
        button = load_button()
        coord = make_coordinator(options={CONST.CONF_CHARGING_MODE: True})
        wake = button.CloudEdgeMeariWakeButton(coord, coord._entry)
        with self.assertRaisesRegex(Exception, "Charging Mode"):
            await wake.async_press()
        self.assertFalse(coord._wake_event.is_set())
        coord._entry.options[CONST.CONF_CHARGING_MODE] = False
        await wake.async_press()
        self.assertTrue(coord._wake_event.is_set())


if __name__ == "__main__":
    unittest.main()
