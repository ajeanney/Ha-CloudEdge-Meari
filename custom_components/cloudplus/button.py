"""Button platform for CloudEdge / Meari — wake camera."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import CloudEdgeMeariCoordinator
from .entity import CloudEdgeMeariEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up CloudEdge / Meari buttons from a config entry."""
    coord: CloudEdgeMeariCoordinator = hass.data[DOMAIN][entry.entry_id]
    if coord.is_battery_camera:
        async_add_entities(
            [CloudEdgeMeariWakeButton(coord, entry), CloudEdgeMeariSleepButton(coord, entry)]
        )


class CloudEdgeMeariWakeButton(CloudEdgeMeariEntity, ButtonEntity):
    """Button to wake the camera."""

    _attr_name = "Wake Camera"
    _attr_icon = "mdi:alarm-light-outline"

    def __init__(
        self, coordinator: CloudEdgeMeariCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.device_uuid}_wake"

    async def async_press(self) -> None:
        """Wake the camera."""
        if self._coordinator.charging_mode:
            raise ServiceValidationError(
                f"{self._coordinator.device_name} is in Charging Mode; turn it off to wake the camera"
            )
        _LOGGER.info("Wake button pressed for %s", self._coordinator.device_name)
        self._coordinator.wake_camera()


class CloudEdgeMeariSleepButton(CloudEdgeMeariEntity, ButtonEntity):
    """Button to end the live session and let the camera sleep."""

    _attr_name = "Sleep Camera"
    _attr_icon = "mdi:sleep"

    def __init__(
        self, coordinator: CloudEdgeMeariCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.device_uuid}_sleep"

    async def async_press(self) -> None:
        """End the live session now."""
        _LOGGER.info("Sleep button pressed for %s", self._coordinator.device_name)
        self._coordinator.sleep_camera()
