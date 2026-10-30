"""Offline regressions for IoT config reads that fail while another session logs in."""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import Mock, patch

from debug_tools.bootstrap import _bootstrap_integration_modules

MODULES = _bootstrap_integration_modules()
COORDINATOR = MODULES["coordinator"].CloudEdgeMeariCoordinator
STATE = "custom_components.cloudplus.coordinator.state"

AUTH_FAILED = RuntimeError("Device IoT config failed: {'errid': 401, 'errstr': 'Authorization Failed'}")
SETTINGS = {"167": {"value": 0}, "1022": {"value": 76}, "103": {"value": 0}}


def coordinator() -> Any:
    """Coordinator state for a mains-powered floodlight camera, without threads or cloud."""
    coord = COORDINATOR.__new__(COORDINATOR)
    coord._capabilities = {}
    coord._device = {}
    coord._iot_data = {}
    coord._sn_num = "synthetic-camera"
    coord._device_name = "Synthetic floodlight"
    coord._device_category = "ipc"
    coord._is_snap = False
    coord._available = True
    coord._has_lamp = False
    coord._lamp_on = False
    coord._iot_read_failures = 0
    coord._battery_percent = None
    coord._battery_charging = False
    coord._video_e2ee_enabled = False
    coord._configured_video_password = ""
    coord._video_password = ""
    coord._update_callbacks = []
    coord._fire_update = Mock()   # the HA event-loop hop is not under test
    return coord


def api(*results: Any) -> Mock:
    """An OpenAPI client whose IoT config reads return or raise *results* in order."""
    client = Mock()
    client.openapi_server = "https://openapi.example"
    client.get_device_iot_config.side_effect = list(results)
    return client


@patch(f"{STATE}.time.sleep", lambda _seconds: None)
class IotReadRetryTests(unittest.TestCase):
    def test_prefetch_retries_after_another_session_logged_in(self):
        coord, client = coordinator(), api(AUTH_FAILED, SETTINGS)
        coord.prefetch_lamp(client)
        self.assertEqual(client.get_device_iot_config.call_count, 2)
        client.login.assert_called_once()
        self.assertEqual(coord.get_iot_value("1022"), 76)
        self.assertTrue(coord.has_lamp)
        coord._fire_update.assert_called()

    def test_status_poll_survives_two_lost_logins(self):
        coord, client = coordinator(), api(AUTH_FAILED, AUTH_FAILED, SETTINGS)
        coord._api = client
        coord._poll_status()
        self.assertEqual(client.login.call_count, 2)
        self.assertEqual(coord.get_iot_value("1022"), 76)
        self.assertEqual(coord._iot_read_failures, 0)

    def test_backoff_happens_before_login_so_the_read_follows_it(self):
        coord, client = coordinator(), api()
        calls = []
        results = [AUTH_FAILED, AUTH_FAILED, SETTINGS]

        def read(_sn):
            calls.append("read")
            result = results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        client.get_device_iot_config.side_effect = read
        client.login.side_effect = lambda: calls.append("login")
        with patch(f"{STATE}.time.sleep", lambda _s: calls.append("sleep")):
            coord.prefetch_lamp(client)
        self.assertEqual(calls, ["read", "sleep", "login", "read", "sleep", "login", "read"])

    def test_repeated_failures_are_logged_once_as_a_warning(self):
        coord = coordinator()
        coord._api = api(*([AUTH_FAILED] * 9))
        with self.assertLogs(STATE, level="WARNING") as logs:
            for _ in range(3):
                coord._poll_status()
        self.assertEqual(coord._iot_read_failures, 3)
        self.assertEqual(len(logs.records), 1)
        self.assertIn("could not be read 3 times in a row", logs.output[0])
        self.assertIsNone(coord.get_iot_value("1022"))

    def test_a_successful_read_resets_the_failure_count(self):
        coord = coordinator()
        coord._iot_read_failures = 3
        coord._api = api(SETTINGS)
        with self.assertLogs(STATE, level="INFO") as logs:
            coord._poll_status()
        self.assertEqual(coord._iot_read_failures, 0)
        self.assertIn("readable again", logs.output[0])
        self.assertEqual(coord.get_iot_value("1022"), 76)


if __name__ == "__main__":
    unittest.main()
