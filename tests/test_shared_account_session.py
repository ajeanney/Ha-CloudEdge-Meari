"""Offline regressions for the login shared by every entry of one account."""

from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from debug_tools.bootstrap import _bootstrap_integration_modules

MODULES = _bootstrap_integration_modules()
API = MODULES["api"]
COORDINATOR = MODULES["coordinator"].CloudEdgeMeariCoordinator
COORDINATOR_MODULE = "custom_components.cloudplus.coordinator"


def client() -> object:
    """A client whose login steps are mocked out, so login() does no I/O."""
    api = API.MeariApiClient("user@example.invalid", "secret")
    for step in ("_redirect", "_do_login", "_get_iot_config", "_get_devices"):
        setattr(api, step, Mock())
    return api


class ReloginTests(unittest.TestCase):
    def test_first_relogin_logs_in(self):
        api = client()
        api.relogin()
        api._do_login.assert_called_once()

    def test_callers_failing_together_share_one_login(self):
        api = client()
        with patch.object(API.time, "monotonic", return_value=100.0):
            api.relogin()
        with patch.object(API.time, "monotonic", return_value=100.0 + API.RELOGIN_COALESCE_S - 1):
            api.relogin()
            api.relogin()
        api._do_login.assert_called_once()

    def test_relogin_after_the_window_logs_in_again(self):
        api = client()
        with patch.object(API.time, "monotonic", return_value=100.0):
            api.relogin()
        with patch.object(API.time, "monotonic", return_value=100.0 + API.RELOGIN_COALESCE_S):
            api.relogin()
        self.assertEqual(api._do_login.call_count, 2)

    def test_failed_login_is_not_coalesced(self):
        api = client()
        api._do_login.side_effect = [RuntimeError("1023"), None]
        with self.assertRaises(RuntimeError):
            api.relogin()
        api.relogin()
        self.assertEqual(api._do_login.call_count, 2)


class SharedMotionListenerTests(unittest.TestCase):
    def test_cameras_starting_together_share_one_listener(self):
        hass, api = SimpleNamespace(data={}), object()
        created = []

        def slow_listener(*_args):
            time.sleep(0.05)  # widen the get-or-create window
            listener = Mock()
            created.append(listener)
            return listener

        coords = []
        for index in range(3):
            coord = COORDINATOR.__new__(COORDINATOR)
            coord.hass, coord._account_api = hass, api
            coord._device_id, coord._sn_num = index, f"SN{index}"
            coord._motion_listener = coord._motion_listener_unsub = None
            coords.append(coord)
        with patch(f"{COORDINATOR_MODULE}.MotionEventListener", side_effect=slow_listener):
            threads = [
                threading.Thread(target=coord._start_motion_listener, args=(api,))
                for coord in coords
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].register.call_count, 3)


if __name__ == "__main__":
    unittest.main()
