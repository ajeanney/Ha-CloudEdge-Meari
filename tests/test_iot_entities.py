"""Offline entity regressions for vendor IoT scales and capability profiles."""

from __future__ import annotations

import importlib
import sys
import unittest
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from debug_tools.bootstrap import _bootstrap_integration_modules

MODULES = _bootstrap_integration_modules()
COORDINATOR = MODULES["coordinator"].CloudEdgeMeariCoordinator


def load_platforms() -> dict[str, ModuleType]:
    """Add only the platform types missing from the standalone HA stubs."""
    bindings = {
        "homeassistant.const": {
            "PERCENTAGE": "%",
            "UnitOfTemperature": SimpleNamespace(CELSIUS="C"),
            "UnitOfTime": SimpleNamespace(SECONDS="s"),
        },
        "homeassistant.components.sensor": {
            "SensorEntity": type("SensorEntity", (), {}),
            "SensorDeviceClass": SimpleNamespace(TEMPERATURE="temperature", HUMIDITY="humidity", BATTERY="battery"),
            "SensorStateClass": SimpleNamespace(MEASUREMENT="measurement"),
        },
        "homeassistant.components.number": {
            "NumberEntity": type("NumberEntity", (), {}),
            "NumberMode": SimpleNamespace(SLIDER="slider"),
        },
        "homeassistant.components.select": {
            "SelectEntity": type("SelectEntity", (), {}),
        },
    }
    stubs = {}
    for name, attributes in bindings.items():
        module = ModuleType(name)
        vars(module).update(attributes)
        stubs[name] = module
    with patch.dict(sys.modules, stubs):
        return {name: importlib.import_module(f"custom_components.cloudplus.{name}")
                for name in ("sensor", "number", "select")}


PLATFORMS = load_platforms()
SENSOR, NUMBER, SELECT = (PLATFORMS[name] for name in ("sensor", "number", "select"))


def coordinator(caps: dict | None = None, values: dict | None = None) -> Any:
    """Build entity-facing coordinator state without threads or cloud traffic."""
    coord = COORDINATOR.__new__(COORDINATOR)
    coord._capabilities = caps or {}
    coord._device = {}
    coord._iot_data = values or {}
    coord._sn_num = "synthetic-camera"
    coord._device_name = "Synthetic camera"
    coord._device_category = "ipc"
    coord._is_snap = False
    coord._available = True
    return coord


def spec_for(specs: tuple, code: int) -> Any:
    return next(spec for spec in specs if spec.code == code)


class CapabilityTests(unittest.TestCase):
    """Missing SDK flags must not become an explicit unsupported flag."""

    def test_unknown_capabilities(self):
        for caps in ({}, {"tmpr": -1}, {"tmpr": None}, {"tmpr": "invalid"}):
            with self.subTest(caps=caps):
                self.assertIsNone(coordinator(caps).iot_capability("tmpr"))

    def test_explicit_capabilities(self):
        for value in (0, 1, 127, "0", "1"):
            with self.subTest(value=value):
                self.assertEqual(coordinator({"tmpr": value}).iot_capability("tmpr"), int(value))

    def test_unspecified_capability_has_no_flag(self):
        self.assertIsNone(coordinator().iot_capability(None))


class SensorTests(unittest.IsolatedAsyncioTestCase):
    """Preserve Arenti filtering without dropping legacy camera telemetry."""

    async def sensors(self, caps: dict, values: dict) -> dict:
        coord = coordinator(caps, values)
        entry = SimpleNamespace(entry_id="test")
        hass = SimpleNamespace(data={SENSOR.DOMAIN: {entry.entry_id: coord}})
        add_entities = Mock()
        await SENSOR.async_setup_entry(hass, entry, add_entities)
        return {entity._spec.code: entity for entity in add_entities.call_args.args[0]}

    async def test_legacy_readings_survive_missing_flags(self):
        entities = await self.sensors({}, {1008: 21560, 1009: 60})
        self.assertAlmostEqual(entities[1008].native_value, 21.56)
        self.assertEqual(entities[1009].native_value, 60)

    async def test_explicit_absence_hides_stale_channel(self):
        entities = await self.sensors({"tmpr": 1, "hmd": 0}, {1008: 21560, 1009: 60})
        self.assertEqual(set(entities), {1008})
        entities = await self.sensors({"tmpr": "0", "hmd": "1"}, {1008: 21560, 1009: 60})
        self.assertEqual(set(entities), {1009})

    async def test_invalid_and_sdk_default_flags_use_legacy_fallback(self):
        for flags in ({"tmpr": -1, "hmd": -1}, {"tmpr": "invalid", "hmd": None}):
            with self.subTest(flags=flags):
                self.assertEqual(set(await self.sensors(flags, {1008: 0, 1009: 0})), {1008, 1009})

    async def test_sensor_sentinels_are_individually_unavailable(self):
        for values, unavailable in (({1008: 255000, 1009: 60}, 1008),
                                    ({1008: 21560, 1009: 255}, 1009)):
            with self.subTest(values=values):
                entities = await self.sensors({"tmpr": 1, "hmd": 1}, values)
                self.assertIsNone(entities[unavailable].native_value)
                self.assertFalse(entities[unavailable].available)
                other = 1009 if unavailable == 1008 else 1008
                self.assertTrue(entities[other].available)

    async def test_numeric_edge_cases(self):
        for raw, expected in (("21560", 21.56), (-5000, -5), (0, 0), (None, None), ("bad", None)):
            with self.subTest(raw=raw):
                entities = await self.sensors({"tmpr": 1}, {1008: raw})
                self.assertEqual(entities[1008].native_value, expected)

    async def test_absent_readings_do_not_create_legacy_sensors(self):
        self.assertEqual(await self.sensors({}, {}), {})

    async def test_wifi_is_unscaled(self):
        entities = await self.sensors({}, {1007: 75})
        self.assertEqual(entities[1007].native_value, 75)


class SelectTests(unittest.IsolatedAsyncioTestCase):
    """Resolve SDK option profiles and use those same maps for writes."""

    def options(self, code: int, caps: dict) -> dict[int, str]:
        return SELECT._select_options(coordinator(caps), spec_for(SELECT.IOT_SELECTS, code))

    def test_alarm_mask_filters_each_bit(self):
        for value in range(7):
            with self.subTest(value=value):
                self.assertEqual(set(self.options(178, {"ver": 22, "afq": 1 << value})), {value})
        self.assertEqual(set(self.options(178, {"ver": "22", "afq": "6"})), {1, 2})

    def test_zero_and_unknown_alarm_bits_offer_nothing(self):
        for mask in (0, 128):
            self.assertEqual(self.options(178, {"ver": 22, "afq": mask}), {})

    def test_legacy_alarm_options_remain_conservative(self):
        for caps in ({}, {"ver": 21, "afq": 127}, {"ver": 22}, {"ver": 22, "afq": -1}):
            with self.subTest(caps=caps):
                self.assertEqual(set(self.options(178, caps)), {0, 1, 2})

    def test_night_vision_profiles(self):
        for profile, values in ((2, {0, 1, 2}), (3, {0, 1, 2, 3}),
                                (4, {0, 2}), (5, {1, 5})):
            with self.subTest(profile=profile):
                self.assertEqual(set(self.options(209, {"dnm": profile})), values)
                self.assertEqual(self.options(113, {"dnm": profile}), {})
        self.assertEqual(set(self.options(113, {"dnm": 1})), {0, 1, 2})
        self.assertEqual(self.options(209, {"dnm": 1}), {})

    def test_night_vision_overrides_have_sdk_precedence(self):
        for flags, values in ((1, set()), (2, {0, 1, 2}), (4, {0, 1, 2, 3}),
                              (16, {1, 5}), (1 | 2 | 4 | 16, {1, 5})):
            with self.subTest(flags=flags):
                self.assertEqual(set(self.options(209, {"dnm": 4, "dnm2": flags})), values)

    def test_schedule_and_intelligent_color_require_modifier_bits(self):
        self.assertEqual(set(self.options(209, {"dnm": 5, "dnm2": 32})), {1, 5, 6})
        self.assertEqual(set(self.options(209, {"dnm": 2, "dnm2": 8 | 32})), {0, 1, 2, 4, 6})
        self.assertEqual(set(self.options(113, {"dnm": 1, "dnm2": 8})), {0, 1, 2, 4})

    def test_unadvertised_profile_retains_base_options(self):
        for code in (113, 209):
            self.assertEqual(set(self.options(code, {})), {0, 1, 2})
            self.assertEqual(set(self.options(code, {"dnm": -1})), {0, 1, 2})
            self.assertEqual(self.options(code, {"dnm": 0}), {})

    async def test_setup_omits_inactive_night_vision_and_empty_alarm(self):
        coord = coordinator({"dnm": 5, "ver": 22, "afq": 0}, {113: 0, 209: 5, 178: 1})
        entry = SimpleNamespace(entry_id="test")
        hass = SimpleNamespace(data={SELECT.DOMAIN: {entry.entry_id: coord}})
        add_entities = Mock()
        await SELECT.async_setup_entry(hass, entry, add_entities)
        entities = [entity for entity in add_entities.call_args.args[0] if hasattr(entity, "_spec")]
        self.assertEqual([entity._spec.code for entity in entities], [209])
        self.assertEqual(entities[0].current_option, "White Light Off")

    async def test_displayed_options_write_exact_vendor_values(self):
        for caps in ({"dnm": 5, "dnm2": 8 | 32}, {"dnm": 3}, {"ver": 22, "afq": 66}):
            code = 178 if "afq" in caps else 209
            spec = spec_for(SELECT.IOT_SELECTS, code)
            coord = coordinator(caps)
            options = SELECT._select_options(coord, spec)
            entity = SELECT.CloudEdgeMeariIotSelect(coord, SimpleNamespace(), spec, options)
            entity.hass = SimpleNamespace(async_add_executor_job=AsyncMock())
            entity.async_write_ha_state = Mock()
            for value, label in options.items():
                with self.subTest(caps=caps, value=value):
                    coord._iot_data[code] = value
                    self.assertEqual(entity.current_option, label)
                    await entity.async_select_option(label)
                    entity.hass.async_add_executor_job.assert_awaited_with(coord.set_iot_value, code, value)

    async def test_unsupported_option_does_not_write(self):
        coord = coordinator({"ver": 22, "afq": 6})
        spec = spec_for(SELECT.IOT_SELECTS, 178)
        entity = SELECT.CloudEdgeMeariIotSelect(coord, SimpleNamespace(), spec, SELECT._select_options(coord, spec))
        entity.hass = SimpleNamespace(async_add_executor_job=AsyncMock())
        with self.assertRaises(ValueError):
            await entity.async_select_option("30 Seconds")
        entity.hass.async_add_executor_job.assert_not_awaited()


class NumberTests(unittest.IsolatedAsyncioTestCase):
    """IoT sensitivity is not a union with the native P2P encoding."""

    async def test_motion_sensitivity_bounds_and_writes(self):
        coord = coordinator({}, {107: 1})
        spec = spec_for(NUMBER.IOT_NUMBERS, 107)
        self.assertEqual((spec.min_value, spec.max_value, spec.step), (0, 2, 1))
        entity = NUMBER.CloudEdgeMeariIotNumber(coord, SimpleNamespace(), spec)
        entity.hass = SimpleNamespace(async_add_executor_job=AsyncMock())
        entity.async_write_ha_state = Mock()
        for value in (0, 1, 2):
            await entity.async_set_native_value(value)
            entity.hass.async_add_executor_job.assert_awaited_with(coord.set_iot_value, 107, value)
        entity.hass.async_add_executor_job.reset_mock()
        for value in (-1, 0.5, 3, 4, 5, 6, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                await entity.async_set_native_value(value)
        entity.hass.async_add_executor_job.assert_not_awaited()

    async def test_pir_interval_still_requires_a_pir_sensor(self):
        coord = coordinator({"pir": 0, "tmpr": 1}, {242: 30000})
        entry = SimpleNamespace(entry_id="test")
        hass = SimpleNamespace(data={NUMBER.DOMAIN: {entry.entry_id: coord}})
        add_entities = Mock()
        await NUMBER.async_setup_entry(hass, entry, add_entities)
        self.assertEqual(add_entities.call_args.args[0], [])
