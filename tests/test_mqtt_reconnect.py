"""Offline tests for MQTT credential refresh and keepalive in the motion listener."""

from __future__ import annotations

import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from debug_tools.bootstrap import _bootstrap_integration_modules

_bootstrap_integration_modules()
MOTION = importlib.import_module("custom_components.cloudplus.coordinator.motion")


def make_api():
    api = Mock()
    api.mqtt_host = "mqtt.example.invalid"
    api.mqtt_port = 8883
    api.user_id = "1000"
    api.client_id = ""
    api.access_id = "access"
    api.mqtt_signature = "old-signature"
    return api


class BadCredentialCodeTests(unittest.TestCase):
    """Classify CONNACK codes from Paho v1 (int) and v2 (ReasonCode)."""

    def test_bad_credentials(self):
        for code in (4, 5, 134, 135, SimpleNamespace(value=134)):
            with self.subTest(code=code):
                self.assertTrue(MOTION._mqtt_bad_credentials(code))

    def test_other_codes(self):
        for code in (0, 3, 128, None, "x", SimpleNamespace(value=0)):
            with self.subTest(code=code):
                self.assertFalse(MOTION._mqtt_bad_credentials(code))


class MqttReconnectTests(unittest.TestCase):
    """A rejected login marks the credentials stale; the poll thread renews them."""

    def setUp(self):
        self.api = make_api()
        self.listener = MOTION.MotionEventListener(self.api)
        self.listener._start_alarm_poll = Mock()
        self.client = Mock()
        with patch.object(MOTION.mqtt, "Client", return_value=self.client):
            self.listener.start()

    def test_keepalive(self):
        args, kwargs = self.client.connect_async.call_args
        self.assertEqual(args[:2], ("mqtt.example.invalid", 8883))
        self.assertEqual(kwargs["keepalive"], MOTION.MQTT_KEEPALIVE_S)

    def test_bad_credentials_mark_stale_and_refresh_once(self):
        on_connect = self.client.on_connect
        on_connect(self.client, None, {}, SimpleNamespace(value=134), None)
        self.assertTrue(self.listener._mqtt_credentials_stale.is_set())

        def relogin():
            self.api.mqtt_signature = "new-signature"
        self.api.login.side_effect = relogin
        self.assertTrue(self.listener._refresh_stale_mqtt_credentials())
        self.client.username_pw_set.assert_called_with("access", "new-signature")
        self.assertFalse(self.listener._mqtt_credentials_stale.is_set())

        # A second rejection right away waits for the refresh interval.
        on_connect(self.client, None, {}, SimpleNamespace(value=134), None)
        self.assertFalse(self.listener._refresh_stale_mqtt_credentials())
        self.assertEqual(self.api.login.call_count, 1)
        self.assertTrue(self.listener._mqtt_credentials_stale.is_set())

    def test_other_failures_do_not_log_in(self):
        self.client.on_connect(self.client, None, {}, SimpleNamespace(value=3), None)
        self.assertFalse(self.listener._refresh_stale_mqtt_credentials())
        self.api.login.assert_not_called()

    def test_success_does_not_log_in(self):
        self.client.on_connect(self.client, None, {}, SimpleNamespace(value=0), None)
        self.assertFalse(self.listener._refresh_stale_mqtt_credentials())
        self.api.login.assert_not_called()


if __name__ == "__main__":
    unittest.main()
