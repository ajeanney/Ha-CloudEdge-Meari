"""Binary sensor platform for CloudEdge / Meari — motion, sound & awake state."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN
from .coordinator import CloudEdgeMeariCoordinator
from .entity import CloudEdgeMeariEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up CloudEdge / Meari binary sensors from a config entry."""
    coord: CloudEdgeMeariCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [CloudEdgeMeariMotionSensor(coord, entry)]
    if any(
        coord.supports_iot(feature)
        for feature in ("noise_det", "cry_det", "abnormal_noise")
    ):
        entities.append(CloudEdgeMeariSoundSensor(coord, entry))
    if coord.is_battery_camera:
        entities.append(CloudEdgeMeariAwakeSensor(coord, entry))
        entities.append(CloudEdgeMeariChargingSensor(coord, entry))
    async_add_entities(entities)


class CloudEdgeMeariMotionSensor(CloudEdgeMeariEntity, BinarySensorEntity):
    """Binary sensor for motion detection."""

    _attr_name = "Motion"
    _attr_device_class = BinarySensorDeviceClass.MOTION

    def __init__(
        self, coordinator: CloudEdgeMeariCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.device_uuid}_motion"
        self._unsub_motion: Any = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._unsub_motion = self._coordinator.register_motion_callback(
            self._handle_update
        )

    async def async_will_remove_from_hass(self) -> None:
        await super().async_will_remove_from_hass()
        if self._unsub_motion:
            self._unsub_motion()

    @property
    def is_on(self) -> bool:
        return self._coordinator.motion_detected

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {}
        if self._coordinator.motion_type:
            attrs["motion_type"] = self._coordinator.motion_type
        return attrs


class CloudEdgeMeariSoundSensor(CloudEdgeMeariEntity, BinarySensorEntity):
    """Binary sensor for noise / baby-cry alerts, held on for the motion timeout."""

    _attr_name = "Sound"
    _attr_device_class = BinarySensorDeviceClass.SOUND
    _attr_is_on = False

    def __init__(
        self, coordinator: CloudEdgeMeariCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.device_uuid}_sound"
        self._unsub_sound: Any = None
        self._unsub_clear: Any = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._unsub_sound = self._coordinator.register_sound_callback(
            self._handle_sound
        )

    async def async_will_remove_from_hass(self) -> None:
        await super().async_will_remove_from_hass()
        if self._unsub_sound:
            self._unsub_sound()
        if self._unsub_clear:
            self._unsub_clear()

    @callback
    def _handle_sound(self, sound_type: str) -> None:
        # The poll paths can re-deliver an alert; each one just extends the hold.
        if self._unsub_clear:
            self._unsub_clear()
        self._unsub_clear = async_call_later(
            self.hass, self._coordinator.motion_timeout, self._clear
        )
        self._attr_is_on = True
        self._attr_extra_state_attributes = {"sound_type": sound_type}
        self.async_write_ha_state()

    @callback
    def _clear(self, _now: Any) -> None:
        self._unsub_clear = None
        self._attr_is_on = False
        self._attr_extra_state_attributes = {}
        self.async_write_ha_state()


class CloudEdgeMeariAwakeSensor(CloudEdgeMeariEntity, BinarySensorEntity):
    """Binary sensor for camera awake state."""

    _attr_name = "Camera Awake"
    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_icon = "mdi:eye"

    def __init__(
        self, coordinator: CloudEdgeMeariCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.device_uuid}_awake"

    @property
    def is_on(self) -> bool:
        return self._coordinator.camera_awake


class CloudEdgeMeariChargingSensor(CloudEdgeMeariEntity, BinarySensorEntity):
    """Binary sensor for camera charging state."""

    _attr_name = "Charging"
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING

    def __init__(
        self, coordinator: CloudEdgeMeariCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.device_uuid}_charging"

    @property
    def is_on(self) -> bool | None:
        if self._coordinator.battery_percent is None:
            return None
        return self._coordinator.battery_charging
