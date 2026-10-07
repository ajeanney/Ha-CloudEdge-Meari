"""Event platform for CloudEdge / Meari — doorbell presses."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import CloudEdgeMeariCoordinator
from .entity import CloudEdgeMeariEntity

EVENT_TYPE_RING = "ring"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the doorbell event entity for doorbell-category devices."""
    coord: CloudEdgeMeariCoordinator = hass.data[DOMAIN][entry.entry_id]
    if coord.is_doorbell:
        async_add_entities([CloudEdgeMeariDoorbellEvent(coord, entry)])


class CloudEdgeMeariDoorbellEvent(CloudEdgeMeariEntity, EventEntity):
    """Event entity fired when the doorbell button is pressed."""

    _attr_name = "Doorbell"
    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_event_types = [EVENT_TYPE_RING]

    def __init__(
        self, coordinator: CloudEdgeMeariCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.device_uuid}_doorbell"
        self._unsub_doorbell: Any = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._unsub_doorbell = self._coordinator.register_doorbell_callback(
            self._handle_ring
        )

    async def async_will_remove_from_hass(self) -> None:
        await super().async_will_remove_from_hass()
        if self._unsub_doorbell:
            self._unsub_doorbell()

    @callback
    def _handle_ring(self, alarm_name: str) -> None:
        self._trigger_event(EVENT_TYPE_RING, {"alarm_type": alarm_name})
        self.async_write_ha_state()
