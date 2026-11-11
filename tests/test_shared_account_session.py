"""Offline regressions for the login shared by every entry of one account."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from debug_tools.bootstrap import _bootstrap_integration_modules

MODULES = _bootstrap_integration_modules()
API = MODULES["api"]


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


if __name__ == "__main__":
    unittest.main()
