"""Switch platform for CloudEdge / Meari — wake on motion and charging mode toggles."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_CHARGING_MODE, CONF_CHARGING_RESTORE, DOMAIN
from .coordinator import CloudEdgeMeariCoordinator
from .coordinator.state import CHARGING_OFF_ONLY, CHARGING_OFF_SETTINGS
from .entity import CloudEdgeMeariEntity, CloudEdgeMeariIotEntity
from .meari_commands import (
    ABNORMAL_NOISE_ENABLE,
    ANTI_JAMMING,
    AUTO_UPDATE,
    CRY_DET_ENABLE,
    FACE_RECOGNITION_SWITCH,
    FLIGHT_LINK_LIGHTING_ENABLE,
    FLIGHT_LINK_SIREN_ENABLE,
    HOMEKIT_ENABLE,
    HUMAN_DET_ENABLE,
    HUMAN_TRACK_ENABLE,
    LASER_SWITCH,
    LED_ENABLE,
    LOGO_SWITCH,
    MOTION_DET_ENABLE,
    ONVIF_ENABLE,
    OSD_ENABLE,
    PET_ALARM_ENABLE,
    PET_THROW_WARNING,
    PIR_DET_ENABLE,
    PTZ_PATROL,
    RECORD_SWITCH,
    RGB_LIGHT_SWITCH,
    SLEEP_MODE,
    SOUND_DET_ENABLE,
    SOUND_SWITCH,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class IotSwitchSpec:
    """Declarative spec for an IoT-backed switch entity."""

    feature: str | None
    code: int
    name: str
    icon: str | None = None


IOT_SWITCHES: tuple[IotSwitchSpec, ...] = (
    IotSwitchSpec("status_led", LED_ENABLE, "Status LED", "mdi:led-on"),
    IotSwitchSpec(
        "motion_det",
        MOTION_DET_ENABLE,
        "Motion Detection",
        "mdi:motion-sensor",
    ),
    IotSwitchSpec(
        "person_det",
        HUMAN_DET_ENABLE,
        "Person Detection",
        "mdi:account-search",
    ),
    IotSwitchSpec(
        "human_track",
        HUMAN_TRACK_ENABLE,
        "Human Tracking",
        "mdi:target-account",
    ),
    IotSwitchSpec("noise_det", SOUND_DET_ENABLE, "Sound Detection", "mdi:ear-hearing"),
    IotSwitchSpec(
        "cry_det",
        CRY_DET_ENABLE,
        "Crying Detection",
        "mdi:baby-face-outline",
    ),
    IotSwitchSpec("onvif", ONVIF_ENABLE, "ONVIF", "mdi:network-outline"),
    IotSwitchSpec("sd_card", RECORD_SWITCH, "SD Recording", "mdi:sd"),
    IotSwitchSpec("pir", PIR_DET_ENABLE, "PIR Sensor", "mdi:motion-sensor"),
    IotSwitchSpec(
        "floodlight",
        FLIGHT_LINK_LIGHTING_ENABLE,
        "Floodlight Linkage",
        "mdi:link-variant",
    ),
    IotSwitchSpec(
        "siren",
        FLIGHT_LINK_SIREN_ENABLE,
        "Siren Linkage",
        "mdi:alarm-light",
    ),
    IotSwitchSpec(
        "face_det",
        FACE_RECOGNITION_SWITCH,
        "Face Recognition",
        "mdi:face-recognition",
    ),
    IotSwitchSpec("sleep_mode", SLEEP_MODE, "Sleep Mode", "mdi:sleep"),
    IotSwitchSpec("rgb_light", RGB_LIGHT_SWITCH, "RGB Light", "mdi:palette"),
    IotSwitchSpec("anti_jamming", ANTI_JAMMING, "Anti-Jamming", "mdi:shield-check"),
    IotSwitchSpec(
        "abnormal_noise",
        ABNORMAL_NOISE_ENABLE,
        "Abnormal Noise",
        "mdi:alert-decagram",
    ),
    IotSwitchSpec("ptz_patrol", PTZ_PATROL, "PTZ Patrol", "mdi:pan-horizontal"),
    IotSwitchSpec("laser", LASER_SWITCH, "Laser Toy", "mdi:laser-pointer"),
    IotSwitchSpec("pet_alarm", PET_ALARM_ENABLE, "Pet Alarm", "mdi:bell-alert"),
    IotSwitchSpec(
        "pet_alarm",
        PET_THROW_WARNING,
        "Pet Throw Warning",
        "mdi:bell-alert",
    ),
    IotSwitchSpec("homekit", HOMEKIT_ENABLE, "HomeKit", "mdi:home-automation"),
    IotSwitchSpec("auto_update", AUTO_UPDATE, "Auto Update", "mdi:update"),
    IotSwitchSpec(None, OSD_ENABLE, "OSD Watermark", "mdi:watermark"),
    IotSwitchSpec(None, LOGO_SWITCH, "Brand Logo", "mdi:label-outline"),
    IotSwitchSpec(None, SOUND_SWITCH, "Sound", "mdi:volume-high"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up CloudEdge / Meari switches from a config entry."""
    coord: CloudEdgeMeariCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SwitchEntity] = []
    if coord.is_battery_camera:
        entities.append(CloudEdgeMeariMotionWakeSwitch(coord, entry))
        entities.append(CloudEdgeMeariChargingModeSwitch(coord, entry))
    if coord.has_lamp:
        entities.append(CloudEdgeMeariLampSwitch(coord, entry))
    else:
        _add_lamp_switch_when_reported(coord, entry, async_add_entities)
    entities.extend(
        CloudEdgeMeariIotSwitch(coord, entry, spec)
        for spec in IOT_SWITCHES
        if (spec.feature and coord.supports_iot(spec.feature))
        or coord.has_iot_code(spec.code)
    )
    async_add_entities(entities)


def _add_lamp_switch_when_reported(
    coord: CloudEdgeMeariCoordinator,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the Lamp switch once the camera first reports its lamp value.

    The IoT config read during setup can come back without the lamp code (or
    fail), even on a floodlight camera; the coordinator's status poll picks it
    up later, but by then the platform has been set up. Without this the switch
    would stay missing until the config entry is reloaded.
    """
    unsub: Callable[[], None] | None = None

    @callback
    def _check_lamp() -> None:
        nonlocal unsub
        if unsub is None or not coord.has_lamp:
            return
        unsub()
        unsub = None
        _LOGGER.debug(
            "Lamp reported for %s after setup; adding the Lamp switch",
            coord.device_name,
        )
        async_add_entities([CloudEdgeMeariLampSwitch(coord, entry)])

    @callback
    def _stop_waiting() -> None:
        nonlocal unsub
        if unsub is not None:
            unsub()
            unsub = None

    unsub = coord.register_update_callback(_check_lamp)
    entry.async_on_unload(_stop_waiting)


class CloudEdgeMeariIotSwitch(CloudEdgeMeariIotEntity, SwitchEntity):
    """Switch entity backed by a Meari IoT value."""

    def __init__(
        self,
        coordinator: CloudEdgeMeariCoordinator,
        entry: ConfigEntry,
        spec: IotSwitchSpec,
    ) -> None:
        super().__init__(coordinator, entry, spec)
        self._attr_unique_id = f"{coordinator.device_uuid}_iot_switch_{spec.code}"

    @property
    def is_on(self) -> bool:
        value = self._iot_value
        try:
            return int(value) == 1
        except (TypeError, ValueError):
            return False

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Turn the camera IoT value on."""
        await self.hass.async_add_executor_job(
            self._coordinator.set_iot_value,
            self._spec.code,
            1,
        )
        self.async_write_ha_state()

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Turn the camera IoT value off."""
        await self.hass.async_add_executor_job(
            self._coordinator.set_iot_value,
            self._spec.code,
            0,
        )
        self.async_write_ha_state()


class CloudEdgeMeariMotionWakeSwitch(CloudEdgeMeariEntity, SwitchEntity):
    """Switch to enable / disable automatic wake on motion."""

    _attr_name = "Wake on Motion"
    _attr_icon = "mdi:motion-sensor"

    def __init__(
        self, coordinator: CloudEdgeMeariCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.device_uuid}_motion_wake"

    @property
    def is_on(self) -> bool:
        return self._coordinator.motion_wake_enabled

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Enable wake on motion."""
        self._coordinator.set_motion_wake_enabled(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Disable wake on motion."""
        self._coordinator.set_motion_wake_enabled(False)
        self.async_write_ha_state()


class CloudEdgeMeariChargingModeSwitch(CloudEdgeMeariEntity, SwitchEntity):
    """Switch that keeps a battery camera asleep so it can charge.

    On: ends any live session, stops motion and Wake Camera from waking the
    camera and turns off its lights, sensors, detection, recording and motion
    linkage (``CHARGING_OFF_SETTINGS``), leaving the Wi-Fi link that carries a
    wake-up. Off: turns back on what it turned off, except lights and toys.
    Saved in the entry options with that list, so it survives restarts. Each
    change ends with a short check-in (a still grab), so the camera picks the
    settings up now.
    """

    _attr_name = "Charging Mode"
    _attr_icon = "mdi:battery-charging"

    def __init__(
        self, coordinator: CloudEdgeMeariCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.device_uuid}_charging_mode"
        # One change at a time: an overlapping on/off could save the wrong list.
        self._lock = asyncio.Lock()

    @property
    def is_on(self) -> bool:
        return self._coordinator.charging_mode

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        codes = set(self._restore_codes())
        names = [name for code, name in CHARGING_OFF_SETTINGS.items() if str(code) in codes]
        names += sorted(codes - {str(code) for code in CHARGING_OFF_SETTINGS})  # saved by another version
        if self.is_on:
            return {"will_restore": names}
        return {"restore_failed": names} if names else {}

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Hold off wake-ups, end the live session, turn off what spends battery.

        Also when already on: anything turned back on meanwhile goes off again.
        The list is saved before any write, so a failed write cannot lose a setting.
        Lights and toys (``CHARGING_OFF_ONLY``) go off but are not listed to come back.
        """
        async with self._lock:
            was_on = self._coordinator.charging_mode
            now_on = self._coordinator.charging_settings_on()
            restore = set(self._restore_codes()) | {c for c in now_on if int(c) not in CHARGING_OFF_ONLY}
            self._save(True, sorted(restore, key=int))
            self._coordinator.end_live_session()
            if now_on:
                await self.hass.async_add_executor_job(self._coordinator.charging_settings_off, now_on)
            if now_on or not was_on:
                self._coordinator.request_check_in()  # the camera takes the new settings now
            self.async_write_ha_state()

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Let the camera wake again and turn back on what Charging Mode turned off.

        Settings that fail to come back on stay listed, so the next cycle retries them.
        """
        async with self._lock:
            was_on = self._coordinator.charging_mode
            codes = self._restore_codes()
            failed = []
            if codes:
                failed = await self.hass.async_add_executor_job(
                    self._coordinator.charging_settings_restore, codes
                )
            self._save(False, failed)
            if was_on or codes:
                self._coordinator.request_check_in()
            self.async_write_ha_state()

    def _restore_codes(self) -> list[str]:
        return [str(code) for code in self._entry.options.get(CONF_CHARGING_RESTORE, [])]

    def _save(self, on: bool, restore: list[str]) -> None:
        options = dict(self._entry.options)
        options[CONF_CHARGING_MODE] = on
        if restore:
            options[CONF_CHARGING_RESTORE] = restore
        else:
            options.pop(CONF_CHARGING_RESTORE, None)
        self.hass.config_entries.async_update_entry(self._entry, options=options)


class CloudEdgeMeariLampSwitch(CloudEdgeMeariEntity, SwitchEntity):
    """Switch to control the camera's built-in lamp / LED light."""

    _attr_name = "Lamp"
    _attr_icon = "mdi:lightbulb"

    def __init__(
        self, coordinator: CloudEdgeMeariCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.device_uuid}_lamp"

    @property
    def is_on(self) -> bool:
        return self._coordinator.lamp_on

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Turn on the lamp."""
        await self.hass.async_add_executor_job(self._coordinator.set_lamp, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Turn off the lamp."""
        await self.hass.async_add_executor_job(self._coordinator.set_lamp, False)
        self.async_write_ha_state()
