"""Stream Host Mode: saved in the entry options; Local only binds loopback."""

from __future__ import annotations

import asyncio
import importlib
import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from debug_tools.bootstrap import _bootstrap_integration_modules

MODULES = _bootstrap_integration_modules()
COORDINATOR = MODULES["coordinator"].CloudEdgeMeariCoordinator
_SELECT_STUB = ModuleType("homeassistant.components.select")
_SELECT_STUB.SelectEntity = type("SelectEntity", (), {})
with patch.dict(sys.modules, {_SELECT_STUB.__name__: _SELECT_STUB}):
    SELECT = importlib.import_module("custom_components.cloudplus.select")


def _coordinator(options: dict) -> COORDINATOR:
    hass = Mock()
    hass.loop.is_closed.return_value = False
    return COORDINATOR(
        hass, "e", "p", {"snNum": "SN1"}, entry=SimpleNamespace(options=options)
    )


class StreamHostModeTests(unittest.TestCase):
    def test_saved_mode_is_restored(self) -> None:
        self.assertEqual(_coordinator({"stream_host_mode": "docker"}).stream_host_mode, "docker")

    def test_unknown_or_missing_mode_falls_back_to_ip(self) -> None:
        self.assertEqual(_coordinator({"stream_host_mode": "bogus"}).stream_host_mode, "ip")
        self.assertEqual(_coordinator({}).stream_host_mode, "ip")

    def test_select_saves_the_choice(self) -> None:
        entry = SimpleNamespace(entry_id="test", options={"lens": 3})
        coord = _coordinator(entry.options)
        entity = SELECT.CloudEdgeMeariStreamHostSelect(coord, entry)
        entity.hass = SimpleNamespace(
            config_entries=Mock(),
            async_add_executor_job=AsyncMock(side_effect=lambda func, *args: func(*args)),
        )
        entity.async_write_ha_state = Mock()
        asyncio.run(entity.async_select_option("Docker Hostname"))
        entity.hass.config_entries.async_update_entry.assert_called_once_with(
            entry, options={"lens": 3, "stream_host_mode": "docker"}
        )
        self.assertEqual(coord.stream_host_mode, "docker")

    def test_local_only_binds_and_advertises_loopback(self) -> None:
        coord = _coordinator({"stream_host_mode": "local"})
        self.assertEqual(coord.stream_host, "127.0.0.1")
        coord._stream_server.start()
        self.addCleanup(coord._stream_server.stop)
        self.assertEqual(coord._stream_server._sock.getsockname()[0], "127.0.0.1")

    def test_switching_to_or_from_local_rebinds_a_running_server(self) -> None:
        coord = _coordinator({})
        coord._running = True
        coord._stream_server.start()
        self.addCleanup(coord._stream_server.stop)
        self.assertEqual(coord._stream_server._sock.getsockname()[0], "0.0.0.0")
        coord.set_stream_host_mode("local")
        self.assertEqual(coord._stream_server._sock.getsockname()[0], "127.0.0.1")
        self.assertTrue(coord.current_stream_port)
        sock = coord._stream_server._sock
        coord.set_stream_host_mode("local")  # same bind address: no rebind
        self.assertIs(coord._stream_server._sock, sock)
        coord.set_stream_host_mode("docker")
        self.assertEqual(coord._stream_server._sock.getsockname()[0], "0.0.0.0")

    def test_switch_does_not_start_a_stopped_server(self) -> None:
        coord = _coordinator({})
        coord.set_stream_host_mode("local")
        self.assertEqual(coord.current_stream_port, 0)
        self.assertEqual(coord._stream_server.host, "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
