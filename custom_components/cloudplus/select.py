"""Select platform for CloudEdge / Meari — stream host mode."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_STREAM_QUALITY, DOMAIN
from .coordinator import CloudEdgeMeariCoordinator
from .entity import CloudEdgeMeariEntity, CloudEdgeMeariIotEntity
from .meari_commands import (
    ALARM_FREQUENCY,
    DAY_NIGHT_MODE,
    FULL_COLOR_MODE,
    NO_FLK,
    SD_RECORD_TYPE,
    SOUND_LIGHT_TYPE,
)

STREAM_HOST_OPTIONS: dict[str, str] = {
    "ip": "IP Address",
    "docker": "Docker Hostname",
}
_OPTION_TO_KEY = {v: k for k, v in STREAM_HOST_OPTIONS.items()}

_NIGHT_VISION_LABELS = {
    0: "Intelligent Vision",
    1: "Full Color Night Vision",
    2: "Black and White Night Vision",
    3: "Shimmer Full Color",
    5: "White Light Off",
    6: "Intelligent Full Color",
}
_NIGHT_VISION_PROFILES = {
    2: (0, 1, 2),
    3: (0, 1, 2, 3),
    4: (0, 2),
    5: (1, 5),
}


@dataclass(frozen=True)
class IotSelectSpec:
    """Declarative spec for an IoT-backed select entity."""

    feature: str | None
    code: int
    name: str
    options: dict[int, str]
    icon: str | None = None


IOT_SELECTS: tuple[IotSelectSpec, ...] = (
    IotSelectSpec(
        "sd_card",
        SD_RECORD_TYPE,
        "SD Record Type",
        {0: "Continuous", 1: "Event"},
        "mdi:sd",
    ),
    IotSelectSpec(
        None,
        DAY_NIGHT_MODE,
        "Day/Night Mode",
        # R.array.day_night_mode paired with day_night_mode_value = [0, 1, 2].
        {0: "Automatic", 1: "Day", 2: "Night"},
        "mdi:theme-light-dark",
    ),
    IotSelectSpec(
        "alarm_frequency",
        ALARM_FREQUENCY,
        "Alarm Interval",
        # R.array.alarm_frequency_name paired with alarm_frequency_value.
        # This is an alarm re-trigger interval, not a sensitivity level.
        {
            0: "Off",
            1: "1 Minute",
            2: "2 Minutes",
            3: "3 Minutes",
            4: "5 Minutes",
            5: "10 Minutes",
            6: "30 Seconds",
        },
        "mdi:bell-ring",
    ),
    IotSelectSpec(
        "siren_alarm",
        SOUND_LIGHT_TYPE,
        "Sound/Light Alarm Type",
        # R.array.alarm_type_name.
        {0: "Audio Warning", 1: "White Light Warning", 2: "Audio and Strobe"},
        "mdi:alarm-light",
    ),
    IotSelectSpec(
        None,
        NO_FLK,
        "Anti-Flicker",
        {0: "50Hz", 1: "60Hz"},
        "mdi:sine-wave",
    ),
    IotSelectSpec(
        "full_color",
        FULL_COLOR_MODE,
        "Full Color Mode",
        {value: _NIGHT_VISION_LABELS[value] for value in _NIGHT_VISION_PROFILES[2]},
        "mdi:invert-colors",
    ),
)


def _night_vision_options(
    coordinator: CloudEdgeMeariCoordinator, spec: IotSelectSpec
) -> dict[int, str]:
    mode = coordinator.iot_capability("dnm")
    flags = coordinator.iot_capability("dnm2") or 0
    # Match the SDK's ordered overrides when multiple profile bits are set.
    for bit, profile in ((1, 1), (2, 2), (4, 3), (16, 5)):
        if flags & bit:
            mode = profile

    if mode is None:
        options = dict(spec.options)
    elif spec.code == DAY_NIGHT_MODE:
        options = dict(spec.options) if mode == 1 else {}
    else:
        values = _NIGHT_VISION_PROFILES.get(mode, ())
        if mode == 5 and flags & 32:
            values = (1, 6, 5)
        options = {value: _NIGHT_VISION_LABELS[value] for value in values}

    if options:
        if flags & 8:
            options[4] = "Scheduled"
        if flags & 32 and mode != 5:
            options[6] = _NIGHT_VISION_LABELS[6]
    return options


def _select_options(
    coordinator: CloudEdgeMeariCoordinator, spec: IotSelectSpec
) -> dict[int, str]:
    if spec.code == ALARM_FREQUENCY:
        mask = coordinator.iot_capability("afq")
        version = coordinator.iot_capability("ver") or 0
        if version >= 22 and mask is not None:
            return {value: label for value, label in spec.options.items() if mask & (1 << value)}
        # Older or unadvertised capabilities keep the original three choices.
        return {value: label for value, label in spec.options.items() if value <= 2}
    if spec.code in {DAY_NIGHT_MODE, FULL_COLOR_MODE}:
        return _night_vision_options(coordinator, spec)
    return dict(spec.options)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up CloudEdge / Meari select entities from a config entry."""
    coord: CloudEdgeMeariCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SelectEntity] = [CloudEdgeMeariStreamHostSelect(coord, entry)]
    if coord.quality_profiles:
        entities.append(CloudEdgeMeariStreamQualitySelect(coord, entry))
    for spec in IOT_SELECTS:
        if not (coord.supports_iot(spec.feature) or coord.has_iot_code(spec.code)):
            continue
        if options := _select_options(coord, spec):
            entities.append(CloudEdgeMeariIotSelect(coord, entry, spec, options))
    async_add_entities(entities)


class CloudEdgeMeariIotSelect(CloudEdgeMeariIotEntity, SelectEntity):
    """Select entity backed by a Meari IoT value."""

    def __init__(
        self,
        coordinator: CloudEdgeMeariCoordinator,
        entry: ConfigEntry,
        spec: IotSelectSpec,
        options: dict[int, str],
    ) -> None:
        super().__init__(coordinator, entry, spec)
        self._options = options
        self._label_to_value = {label: value for value, label in options.items()}
        self._attr_options = list(options.values())
        self._attr_unique_id = f"{coordinator.device_uuid}_iot_select_{spec.code}"

    @property
    def current_option(self) -> str | None:
        value = self._iot_value
        try:
            return self._options.get(int(value))
        except (TypeError, ValueError):
            return None

    async def async_select_option(self, option: str) -> None:
        """Update the camera IoT value."""
        value = self._label_to_value.get(option)
        if value is None:
            raise ValueError(f"Unsupported {self._spec.name} option: {option}")
        await self.hass.async_add_executor_job(
            self._coordinator.set_iot_value,
            self._spec.code,
            value,
        )
        self.async_write_ha_state()


class CloudEdgeMeariStreamHostSelect(CloudEdgeMeariEntity, SelectEntity):
    """Select entity to choose between IP address or Docker hostname for stream URL."""

    _attr_name = "Stream Host Mode"
    _attr_icon = "mdi:ip-network"
    _attr_options = list(STREAM_HOST_OPTIONS.values())

    def __init__(
        self, coordinator: CloudEdgeMeariCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.device_uuid}_stream_host_mode"

    @property
    def current_option(self) -> str:
        mode = self._coordinator.stream_host_mode
        return STREAM_HOST_OPTIONS.get(mode, STREAM_HOST_OPTIONS["ip"])

    async def async_select_option(self, option: str) -> None:
        """Change stream host mode."""
        key = _OPTION_TO_KEY.get(option)
        if key:
            self._coordinator.set_stream_host_mode(key)
        self.async_write_ha_state()


# ---------------------------------------------------------------------------
# Stream quality profile selector
# ---------------------------------------------------------------------------

_AUTO_LABEL = "AUTO"


class CloudEdgeMeariStreamQualitySelect(CloudEdgeMeariEntity, SelectEntity):
    """Select entity to choose the camera stream quality profile."""

    _attr_name = "Stream Quality"
    _attr_icon = "mdi:video-high-definition"

    def __init__(
        self, coordinator: CloudEdgeMeariCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.device_uuid}_stream_quality"
        # Build option list from device capabilities
        self._profiles = coordinator.quality_profiles  # {int: str}
        self._label_to_id: dict[str, int | None] = {}
        if coordinator.supports_auto_quality:
            self._label_to_id[_AUTO_LABEL] = None
        for pid, label in sorted(self._profiles.items()):
            self._label_to_id[label] = pid
        self._attr_options = list(self._label_to_id.keys())

    @property
    def current_option(self) -> str:
        quality = self._coordinator.vvp_quality
        if quality is None:
            if self._coordinator.supports_auto_quality:
                return _AUTO_LABEL
            if self._profiles:
                return self._profiles[max(self._profiles)]
            return None
        return self._profiles.get(quality, _AUTO_LABEL)

    async def async_select_option(self, option: str) -> None:
        """Change stream quality profile."""
        if option not in self._label_to_id:
            return
        quality_id = self._label_to_id.get(option)
        self._coordinator.set_vvp_quality(quality_id)
        options = dict(self._entry.options)
        options[CONF_STREAM_QUALITY] = _AUTO_LABEL if quality_id is None else quality_id
        self.hass.config_entries.async_update_entry(self._entry, options=options)
        self.async_write_ha_state()
