"""Stream Host Mode: saved in the camera entry's options, read back on start."""

from __future__ import annotations

import asyncio
import importlib
import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

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
        entity.hass = SimpleNamespace(config_entries=Mock())
        entity.async_write_ha_state = Mock()
        asyncio.run(entity.async_select_option("Docker Hostname"))
        entity.hass.config_entries.async_update_entry.assert_called_once_with(
            entry, options={"lens": 3, "stream_host_mode": "docker"}
        )
        self.assertEqual(coord.stream_host_mode, "docker")


if __name__ == "__main__":
    unittest.main()
