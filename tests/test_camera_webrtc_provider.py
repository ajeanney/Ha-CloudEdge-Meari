"""Offline regressions for HA's WebRTC (go2rtc) provider check on the camera entity.

HA picks a WebRTC provider when the camera entity is added, from ``stream_source``.
The entity is added before the coordinator starts its stream server, so that first
check sees no source and the camera is offered as HLS only until HA restarts a
provider. The entity must ask HA to check again once the stream port is known.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

from debug_tools.bootstrap import _bootstrap_integration_modules

MODULES = _bootstrap_integration_modules()
CAMERA = MODULES["camera"].CloudEdgeMeariCamera


def camera(port: int) -> tuple[Any, SimpleNamespace, Mock]:
    """A camera entity over a fake coordinator, with HA's hooks recorded."""
    coord = SimpleNamespace(current_stream_port=port)
    cam = CAMERA.__new__(CAMERA)
    cam._coordinator = coord
    cam._provider_port = None
    cam._refresh_task = None
    cam._unsub_update = None
    cam.async_write_ha_state = Mock()
    cam.async_refresh_providers = Mock(return_value="refresh")
    hass = Mock()
    cam.hass = hass
    return cam, coord, hass


class CameraWebRtcProviderTests(unittest.TestCase):
    def test_refreshes_providers_once_the_stream_server_has_a_port(self) -> None:
        cam, coord, hass = camera(port=0)
        cam._handle_update()
        hass.async_create_task.assert_not_called()

        coord.current_stream_port = 41234
        cam._handle_update()
        cam.async_refresh_providers.assert_called_once_with()
        hass.async_create_task.assert_called_once_with("refresh")

    def test_does_not_refresh_again_while_the_port_is_unchanged(self) -> None:
        cam, _, hass = camera(port=41234)
        cam._handle_update()
        cam._handle_update()
        cam._handle_update()
        self.assertEqual(hass.async_create_task.call_count, 1)

    def test_refreshes_when_the_stream_server_moves_to_a_new_port(self) -> None:
        cam, coord, hass = camera(port=41234)
        cam._handle_update()
        coord.current_stream_port = 41999
        cam._handle_update()
        self.assertEqual(hass.async_create_task.call_count, 2)

    def test_never_health_checks_the_listener_on_updates(self) -> None:
        class Coordinator:
            current_stream_port = 41234

            @property
            def stream_port(self) -> int:
                raise AssertionError("stream_port rebinds; not for updates")

        cam, _, hass = camera(port=0)
        cam._coordinator = Coordinator()
        cam._handle_update()
        hass.async_create_task.assert_called_once_with("refresh")

    def test_refresh_task_is_tracked_and_cancelled_on_removal(self) -> None:
        cam, coord, hass = camera(port=41234)
        first = Mock(done=Mock(return_value=False))
        second = Mock(done=Mock(return_value=False))
        hass.async_create_task.side_effect = [first, second]
        cam._handle_update()
        coord.current_stream_port = 41999
        cam._handle_update()  # a newer refresh supersedes the pending one
        first.cancel.assert_called_once_with()
        self.assertIs(cam._refresh_task, second)

        asyncio.run(cam.async_will_remove_from_hass())
        second.cancel.assert_called_once_with()
        self.assertIsNone(cam._refresh_task)

    def test_no_refresh_before_the_entity_has_hass(self) -> None:
        cam, _, _ = camera(port=41234)
        cam.hass = None
        cam._handle_update()
        self.assertIsNone(cam._refresh_task)

    def test_still_writes_state_on_every_update(self) -> None:
        cam, _, _ = camera(port=41234)
        cam._handle_update()
        cam._handle_update()
        self.assertEqual(cam.async_write_ha_state.call_count, 2)


if __name__ == "__main__":
    unittest.main()
