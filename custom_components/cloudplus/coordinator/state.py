"""Coordinator mixin exposing camera availability and IoT-derived state."""

from __future__ import annotations

import logging
import random
import socket
import time
from datetime import datetime, timezone
from typing import Any, Callable

from ..api import MeariApiClient
from ..const import (
    CONF_CHARGING_MODE,
    CONF_LENS,
    IOT_CODE_BATTERY_PERCENT,
    IOT_CODE_CHARGE_STATUS,
    IOT_CODE_LAMP,
    IOT_CODE_VIDEO_ENCRYPTION,
    PTZ_DIRECTIONS,
    STREAM_HOST_MODES,
)
from ..meari_commands import (
    ABNORMAL_NOISE_ENABLE,
    CRY_DET_ENABLE,
    FACE_RECOGNITION_SWITCH,
    FLIGHT_LIGHT_SWITCH,
    FLIGHT_LINK_LIGHTING_ENABLE,
    FLIGHT_LINK_SIREN_ENABLE,
    HUMAN_DET_DAY_ENABLE,
    HUMAN_DET_ENABLE,
    HUMAN_DET_NIGHT_ENABLE,
    HUMAN_TRACK_ENABLE,
    LASER_SWITCH,
    LED_ENABLE,
    MOTION_DET_ENABLE,
    PET_ALARM_ENABLE,
    PIR_DET_ENABLE,
    RECORD_SWITCH,
    RGB_LIGHT_SWITCH,
    SMART_DET,
    SOUND_DET_ENABLE,
    SOUND_LIGHT_ENABLE,
)
from ..p2p_streamer import (
    lens_sources,
    quality_profile_labels,
    supports_adaptive_stream,
)
from .iot import capability_value, iot_value, normalize_iot_values, supports_feature

_LOGGER = logging.getLogger(__name__)

# Every config entry logs in on its own, and the Meari cloud keeps one session per
# account, so a read can fail with 401 because another entry (or the motion
# listener) has just logged in. Each failed attempt logs in again before the next.
IOT_READ_ATTEMPTS = 3
IOT_READ_RETRY_DELAY = 1.0
# Consecutive failed reads (setup prefetch, session start, then status polls 5
# minutes apart) before the failure is logged as a warning; until a read
# succeeds, the device's settings stay unavailable.
IOT_READ_WARN_AFTER = 3
# Settings Charging Mode turns off while it is on, with the names it reports them by:
# the camera's lights, sensors, detection, recording and motion linkage, which all
# spend battery. Only the ones that are on are touched, and only those go back on.
CHARGING_OFF_SETTINGS = {
    FLIGHT_LIGHT_SWITCH: "Lamp",
    LED_ENABLE: "Status LED",
    RGB_LIGHT_SWITCH: "RGB Light",
    LASER_SWITCH: "Laser Toy",
    PIR_DET_ENABLE: "PIR Sensor",
    MOTION_DET_ENABLE: "Motion Detection",
    SMART_DET: "Smart Detection",
    HUMAN_DET_ENABLE: "Person Detection",
    HUMAN_DET_DAY_ENABLE: "Day Person Detection",
    HUMAN_DET_NIGHT_ENABLE: "Night Person Detection",
    HUMAN_TRACK_ENABLE: "Human Tracking",
    FACE_RECOGNITION_SWITCH: "Face Recognition",
    SOUND_DET_ENABLE: "Sound Detection",
    CRY_DET_ENABLE: "Crying Detection",
    ABNORMAL_NOISE_ENABLE: "Abnormal Noise",
    PET_ALARM_ENABLE: "Pet Alarm",
    RECORD_SWITCH: "SD Recording",
    FLIGHT_LINK_LIGHTING_ENABLE: "Floodlight Linkage",
    FLIGHT_LINK_SIREN_ENABLE: "Siren Linkage",
    SOUND_LIGHT_ENABLE: "Siren and Flash",
}
# Lights and toys Charging Mode turns off but does not turn back on: a lamp left on
# at night should not come back on when Charging Mode ends at noon.
CHARGING_OFF_ONLY = frozenset({FLIGHT_LIGHT_SWITCH, RGB_LIGHT_SWITCH, LASER_SWITCH})
# The camera reports its battery to the cloud only while awake, so Charging Mode,
# which keeps it asleep, wakes it this often for a short check-in (an idle still grab).
CHARGING_CHECK_IN_S = 3600.0


class CoordinatorStateMixin:
    """Mixin exposing availability and IoT-derived state to HA entities."""

    # Set by the Sleep button, cleared by Wake or by turning Wake on Motion on:
    # motion still updates the sensor but does not wake the camera again.
    _sleep_hold = False

    # Battery freshness. Meari's battery data has no timestamp and the camera only
    # reports while awake, so a reading is dated by the real wake it followed.
    _awake_since = 0.0  # monotonic time the current awake period started
    _camera_video_at = 0.0  # monotonic time of the last keyframe from the camera
    _wake_ended_at: float | None = None  # a real wake ended; the next battery read is dated by it
    _battery_reported_at: float | None = None
    _check_in_at = 0.0  # monotonic time of the next Charging Mode check-in; 0 = not scheduled
    _check_in_requested = False
    _check_in_requested_at = 0.0  # monotonic; a request made during a wake outlives that wake

    _latest_image_source: str
    _latest_image_generation: int = 0
    _latest_image_updated_at: float

    @property
    def available(self) -> bool:
        return self._available

    @property
    def latest_image(self) -> bytes | None:
        return self._latest_image

    @property
    def latest_image_source(self) -> str:
        return self._latest_image_source

    @property
    def latest_image_generation(self) -> int:
        return self._latest_image_generation

    @property
    def latest_image_updated_at(self) -> float:
        return self._latest_image_updated_at

    @property
    def motion_type(self) -> str:
        return self._motion_type

    @property
    def motion_detected(self) -> bool:
        return self._motion_detected

    @property
    def device_uuid(self) -> str:
        return self._sn_num

    @property
    def device_name(self) -> str:
        return self._device_name

    @property
    def device_model(self) -> str:
        return f"Camera ({self._device_category or 'unknown'})"

    @property
    def device_id(self) -> int:
        return self._device_id

    @property
    def is_battery_camera(self) -> bool:
        return self._is_snap

    @property
    def camera_awake(self) -> bool:
        return self._camera_awake

    @property
    def battery_percent(self) -> int | None:
        return self._battery_percent

    @property
    def battery_charging(self) -> bool:
        return self._battery_charging

    @property
    def battery_reported_at(self) -> datetime | None:
        """End of the wake the battery reading comes from; None until one is known."""
        if self._battery_reported_at is None:
            return None
        return datetime.fromtimestamp(self._battery_reported_at, timezone.utc)

    def restore_battery_reported(self, reported_at: datetime) -> None:
        """Battery Reported's time from before an HA restart, unless a newer one is already known.

        If the camera woke while HA was down, the real report is newer, so the
        restored time can only understate how fresh the reading is.
        """
        if self._battery_reported_at is None:
            self._battery_reported_at = reported_at.timestamp()
            self._check_in_at = 0.0  # the next pass schedules the check-in from the restored time
            self._fire_update()

    @property
    def motion_wake_enabled(self) -> bool:
        return self._motion_wake_enabled

    @property
    def charging_mode(self) -> bool:
        """Charging Mode, saved in the entry options: motion never wakes the camera."""
        entry = getattr(self, "_entry", None)
        return bool(entry is not None and entry.options.get(CONF_CHARGING_MODE))

    @property
    def _motion_wake_held(self) -> bool:
        return self._sleep_hold or self.charging_mode

    @property
    def _idle_grab_allowed(self) -> bool:
        """The idle still grab (at start, its retries, after a quality change) wakes the camera.

        In Charging Mode only the hourly check-in may grab.
        """
        return self._initial_frame_grab and (not self.charging_mode or self._check_in_requested)

    def _snap_housekeeping(self, battery_interval_passed: bool) -> bool:
        """Once per snap-loop pass: request a due Charging Mode check-in; True means read the battery.

        The battery is read on its interval and right after a real wake ends.
        """
        now = time.monotonic()
        if not self._check_in_at:
            # An hour after the last battery report (kept across restarts), else an hour from now.
            age = 0.0 if self._battery_reported_at is None else max(0.0, time.time() - self._battery_reported_at)
            self._check_in_at = now + max(0.0, CHARGING_CHECK_IN_S - age)
        elif self.charging_mode and now >= self._check_in_at and not self._check_in_requested:
            self.request_check_in()
        return battery_interval_passed or self._wake_ended_at is not None

    def _note_camera_video(self) -> None:
        """A keyframe came from the camera: it is really awake, so it reports its battery."""
        self._camera_video_at = time.monotonic()
        self._idle_frame_ready.set()

    @property
    def motion_timeout(self) -> int:
        return self._motion_timeout

    @property
    def has_lamp(self) -> bool:
        return self._has_lamp

    @property
    def lamp_on(self) -> bool:
        return self._lamp_on

    @property
    def has_ptz(self) -> bool:
        return self._has_ptz

    def supports_iot(self, feature: str | None) -> bool:
        return supports_feature(self._capabilities, self._device, feature)

    def iot_capability(self, name: str | None) -> int | None:
        """Return an advertised capability, or None when it is unknown."""
        return capability_value(self._capabilities, name)

    def has_iot_code(self, code: int | str) -> bool:
        return self.get_iot_value(code) is not None

    def get_iot_value(self, code: int | str) -> Any:
        return iot_value(self._iot_data, code)

    @property
    def stream_port(self) -> int:
        if self._running:
            return self._stream_server.ensure_running()
        return self._stream_server.port

    @property
    def current_stream_port(self) -> int:
        """The listener's current port, without health-checking it.

        Safe to read on every entity update; ``stream_port`` may rebind.
        """
        return self._stream_server.port

    @property
    def stream_token(self) -> str:
        """Secret every ``stream_source`` URL carries; never log it."""
        return self._stream_server.token

    @property
    def stream_host_mode(self) -> str:
        return self._stream_host_mode

    @property
    def _stream_bind_host(self) -> str:
        return "127.0.0.1" if self._stream_host_mode == "local" else "0.0.0.0"

    @property
    def stream_host(self) -> str:
        if self._stream_host_mode == "local":
            return "127.0.0.1"
        if self._stream_host_mode == "docker":
            return socket.gethostname()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except OSError:
            return "127.0.0.1"

    @property
    def quality_profiles(self) -> dict[int, str]:
        return quality_profile_labels(self._device)

    @property
    def supports_auto_quality(self) -> bool:
        return supports_adaptive_stream(self._device)

    @property
    def vvp_quality(self) -> int | None:
        return self._vvp_quality

    @property
    def lens_sources(self) -> dict[int, str]:
        return lens_sources(self._device)

    @property
    def lens_v_id(self) -> int | None:
        """Chosen lens of a dual-lens camera: the saved option, else the first lens."""
        lenses = self.lens_sources
        entry = getattr(self, "_entry", None)
        saved = entry.options.get(CONF_LENS) if entry is not None else None
        if saved is not None and int(saved) in lenses:
            return int(saved)
        return next(iter(lenses), None)

    def set_lens(self, v_id: int) -> None:
        """Switch a dual-lens camera to another lens on the running session.

        Both lenses are always streamed, so this only changes the streamer's
        frame filter; the choice itself is saved in the entry options.
        """
        streamer = getattr(self, "_p2p_streamer", None)
        if streamer is not None and v_id in self.lens_sources:
            streamer.switch_lens(v_id)
        self._fire_update()

    def set_vvp_quality(self, quality: int | None) -> None:
        self._vvp_quality = quality
        self._fire_update()

    def set_stream_host_mode(self, mode: str) -> None:
        if mode not in STREAM_HOST_MODES:
            return
        self._stream_host_mode = mode
        if self._stream_server.host != self._stream_bind_host:
            # Rebind: also drops clients that came in on the old address.
            self._stream_server.host = self._stream_bind_host
            self._stream_server.stop()
            if self._running:
                self._stream_server.start()
        self._fire_update()

    def set_motion_wake_enabled(self, enabled: bool) -> None:
        self._motion_wake_enabled = bool(enabled)
        if self._motion_wake_enabled:
            self._sleep_hold = False
        if self._motion_wake_enabled and self._motion_detected and not self.charging_mode:
            self._wake_event.set()
            self._extend_live_deadline()
            self._set_camera_awake(True)
        self._fire_update()

    def set_motion_timeout(self, seconds: int) -> None:
        self._motion_timeout = max(10, min(600, int(seconds)))
        if self._camera_awake:
            self._extend_live_deadline()
        self._fire_update()

    def prefetch_battery(self, api: MeariApiClient) -> None:
        self._api = api
        if not self._is_snap:
            return
        try:
            info = api.get_battery_info(self._sn_num)
            if not info and api.openapi_server:
                info = api.get_device_iot_config(self._sn_num)
            changed = self._apply_iot_values(info)
            changed = self._apply_battery_info(info) or changed
            changed = self._apply_video_encryption_info(info) or changed
            if changed:
                self._fire_update()
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            _LOGGER.warning("Battery prefetch failed for %s: %s", self._sn_num, exc)

    def prefetch_status(self, api: MeariApiClient) -> None:
        self.prefetch_lamp(api)

    def prefetch_lamp(self, api: MeariApiClient) -> None:
        self._api = api
        if not api.openapi_server:
            return
        iot = self._read_iot_config(api, "Lamp/status prefetch")
        if iot is not None:
            self._apply_status_iot(iot)

    def set_lamp(self, enabled: bool) -> bool:
        return self.set_iot_value(IOT_CODE_LAMP, 1 if enabled else 0)

    def set_iot_value(self, code: int | str, value: Any) -> bool:
        api = self._api
        if api is None:
            return False
        ok = api.set_device_iot_value(self._sn_num, str(code), value)
        if ok:
            self._iot_data.update(normalize_iot_values({code: value}))
            if str(code) == IOT_CODE_LAMP:
                self._has_lamp = True
                self._lamp_on = self._as_int(value) == 1
            self._fire_update()
        return ok

    def ptz_move(self, direction: str) -> bool:
        api = self._api
        if api is None or direction not in PTZ_DIRECTIONS or self.charging_mode:
            return False
        return api.ptz_start(self._sn_num, direction)

    def ptz_stop(self) -> bool:
        api = self._api
        return bool(api and not self.charging_mode and api.ptz_stop(self._sn_num))

    def wake_camera(self) -> None:
        if self.charging_mode:
            _LOGGER.info("Wake Camera ignored for %s: Charging Mode is on", self._device_name)
            return
        self._sleep_hold = False
        self._wake_event.set()
        self._extend_live_deadline()
        self._set_camera_awake(True)

    def sleep_camera(self) -> None:
        """End the live session now and keep the camera asleep until Wake.

        The snap loop sees the passed deadline, stops the streamer and marks
        the camera asleep. A busy scene reports motion every few seconds,
        so motion wake-ups are held off until the next Wake press.
        """
        self._sleep_hold = True
        self.end_live_session()

    def end_live_session(self) -> None:
        """Let the snap loop stop the streamer and mark the camera asleep."""
        self._wake_event.clear()
        self._last_motion_time = 0.0
        self._live_deadline = 0.0

    def charging_settings_on(self) -> list[str]:
        """The ``CHARGING_OFF_SETTINGS`` codes that are on now (the ones to turn back on later)."""
        return [str(code) for code in CHARGING_OFF_SETTINGS if self._as_int(self.get_iot_value(code)) == 1]

    def charging_settings_off(self, codes: list[str]) -> list[str]:
        """Turn the given settings off; return the codes that failed.

        The values go to the Meari cloud; a sleeping camera picks them up when it
        next connects or wakes.
        """
        return self._set_charging_settings(codes, 0, "off")

    def charging_settings_restore(self, codes: list[str]) -> list[str]:
        """Turn the settings Charging Mode turned off back on; return the codes that failed."""
        return self._set_charging_settings(codes, 1, "back on")

    def _set_charging_settings(self, codes: list[str], value: int, action: str) -> list[str]:
        failed = []
        for code in codes:
            try:
                ok = self.set_iot_value(code, value)
            except (OSError, RuntimeError, ValueError) as exc:
                _LOGGER.debug("Charging Mode write of IoT setting %s failed: %s", code, exc)
                ok = False
            if not ok:
                failed.append(str(code))
                _LOGGER.warning(
                    "Charging Mode could not turn %s %s for %s",
                    CHARGING_OFF_SETTINGS.get(int(code), f"IoT setting {code}"),
                    action,
                    self._device_name,
                )
        return failed

    def request_check_in(self) -> None:
        """Run a Charging Mode check-in (a short still grab) on the next snap-loop pass."""
        self._check_in_requested_at = time.monotonic()
        self._check_in_requested = True
        self._idle_frame_refresh.set()

    def register_motion_callback(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._motion_callbacks.append(cb)
        return lambda: self._remove_callback(self._motion_callbacks, cb)

    def register_sound_callback(self, cb: Callable[[str], None]) -> Callable[[], None]:
        self._sound_callbacks.append(cb)
        return lambda: self._remove_callback(self._sound_callbacks, cb)

    def register_update_callback(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._update_callbacks.append(cb)
        return lambda: self._remove_callback(self._update_callbacks, cb)

    @staticmethod
    def _remove_callback(
        callbacks: list[Callable[..., None]],
        cb: Callable[..., None],
    ) -> None:
        try:
            callbacks.remove(cb)
        except ValueError:
            pass

    def _fire_update(self) -> None:
        if self.hass.loop.is_closed():
            return
        for cb in list(self._update_callbacks):
            self.hass.loop.call_soon_threadsafe(cb)

    def _fire_motion(self) -> None:
        if self.hass.loop.is_closed():
            return
        for cb in list(self._motion_callbacks):
            self.hass.loop.call_soon_threadsafe(cb)

    def _note_sound(self, sound_type: str) -> None:
        if self.hass.loop.is_closed():
            return
        for cb in list(self._sound_callbacks):
            self.hass.loop.call_soon_threadsafe(cb, sound_type)

    def _set_camera_awake(self, awake: bool) -> None:
        awake = bool(awake)
        if self._camera_awake == awake:
            return
        self._camera_awake = awake
        if awake:
            self._awake_since = time.monotonic()
        else:
            if self._check_in_requested_at <= self._awake_since:
                self._check_in_requested = False  # this wake served it; one asked for during it still runs
            self._check_in_at = time.monotonic() + CHARGING_CHECK_IN_S
            if self._camera_video_at >= self._awake_since:
                # It really woke, so it reported its battery: read it now, dated by this wake.
                self._wake_ended_at = time.time()
        self._fire_update()

    def _set_motion(self, detected: bool, motion_type: str = "") -> None:
        detected = bool(detected)
        motion_type = motion_type if detected else ""
        changed = self._motion_detected != detected or self._motion_type != motion_type
        self._motion_detected = detected
        self._motion_type = motion_type
        if changed:
            self._fire_motion()
            self._fire_update()

    def _extend_live_deadline(self, seconds: float | None = None) -> None:
        if self.charging_mode:
            return  # the wake paths extend the deadline here (_track_motion checks _motion_wake_held)
        duration = float(seconds if seconds is not None else self._motion_timeout)
        self._live_deadline = max(self._live_deadline, time.monotonic() + duration)

    def _wake_requested(self) -> bool:
        """A wake is pending. In Charging Mode a stray request (a race with motion) is dropped."""
        if self.charging_mode and self._wake_event.is_set():
            self._wake_event.clear()
            return False
        return self._wake_event.is_set()

    def _track_motion(self, now: float) -> None:
        """Keep a sleeping snap camera live through motion, or let Motion clear."""
        if not self._motion_detected:
            return
        motion_until = self._last_motion_time + float(self._motion_timeout)
        if self._motion_wake_enabled and not self._motion_wake_held:
            self._live_deadline = max(self._live_deadline, motion_until)
        elif now >= motion_until:
            # Only the end of a live session clears Motion, and none runs here.
            self._set_motion(False)

    def _note_motion(self, motion_type: str, event_image: bytes | None = None) -> None:
        if event_image:
            self._latest_image = event_image
            self._latest_image_source = "event"
            self._latest_image_generation += 1
            self._latest_image_updated_at = time.time()
            # Keep the exact alarm image authoritative while motion is active.
            # A wake-up may first replay an old keyframe from the previous session.
            self._last_snapshot_convert_time = time.monotonic()
            self._fire_update()
        self._last_motion_time = time.monotonic()
        self._set_motion(True, motion_type)
        if self._is_snap and self._motion_wake_enabled and not self._motion_wake_held:
            self._wake_event.set()
            self._extend_live_deadline()
            self._set_camera_awake(True)

    @staticmethod
    def _as_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _iot_value(self, info: dict[str, Any], code: str) -> Any:
        return iot_value(normalize_iot_values(info), code)

    def _apply_iot_values(self, info: dict[str, Any]) -> bool:
        values = normalize_iot_values(info)
        if not values:
            return False
        updated = dict(self._iot_data)
        updated.update(values)
        if updated == self._iot_data:
            return False
        self._iot_data = updated
        return True

    def _apply_battery_info(self, info: dict[str, Any]) -> bool:
        if not info:
            return False

        changed = False
        percent = self._as_int(self._iot_value(info, IOT_CODE_BATTERY_PERCENT))
        charging = self._as_int(self._iot_value(info, IOT_CODE_CHARGE_STATUS))

        if percent is not None and 0 <= percent <= 100:
            changed = changed or self._battery_percent != percent
            self._battery_percent = percent
        if charging is not None:
            is_charging = charging == 1
            changed = changed or self._battery_charging != is_charging
            self._battery_charging = is_charging
        return changed

    def _apply_video_encryption_info(self, info: dict[str, Any]) -> bool:
        state = self._as_int(self._iot_value(info, IOT_CODE_VIDEO_ENCRYPTION))
        if state is None:
            return False

        enabled = state == 1
        changed = self._video_e2ee_enabled != enabled
        self._video_e2ee_enabled = enabled

        effective_password = self._configured_video_password if enabled else ""
        if self._video_password != effective_password:
            changed = True
            self._video_password = effective_password
            if self._configured_video_password and not enabled:
                _LOGGER.debug(
                    "Ignoring stored video password for %s because E2EE is disabled",
                    self._sn_num,
                )
        return changed

    def _reauthenticate_api(self, api: MeariApiClient, context: str) -> bool:
        try:
            api.login()
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            _LOGGER.debug(
                "%s reauthentication failed for %s: %s",
                context,
                self._sn_num,
                exc,
            )
            return False
        _LOGGER.info("%s reauthenticated for %s", context, self._sn_num)
        return True

    def _poll_battery(self) -> None:
        woke_at, self._wake_ended_at = self._wake_ended_at, None
        api = self._api
        if not self._is_snap or api is None:
            return
        for attempt in range(2):
            try:
                info = api.get_battery_info(self._sn_num)
                if not info and api.openapi_server:
                    info = api.get_device_iot_config(self._sn_num)
                changed = self._apply_iot_values(info)
                changed = self._apply_battery_info(info) or changed
                changed = self._apply_video_encryption_info(info) or changed
                percent = self._as_int(self._iot_value(info, IOT_CODE_BATTERY_PERCENT))
                if woke_at is not None and percent is not None and 0 <= percent <= 100:
                    self._battery_reported_at = woke_at
                    changed = True
                if changed:
                    self._fire_update()
                return
            except (OSError, RuntimeError, ValueError, KeyError) as exc:
                if attempt == 0:
                    _LOGGER.debug("Battery poll retry for %s: %s", self._sn_num, exc)
                    self._reauthenticate_api(api, "Battery poll")
                    continue
                _LOGGER.debug("Battery poll failed for %s: %s", self._sn_num, exc)

    def _poll_status(self) -> None:
        api = self._api
        if api is None or not api.openapi_server:
            return
        iot = self._read_iot_config(api, "Status poll")
        if iot is not None:
            self._apply_status_iot(iot)

    def _read_iot_config(self, api: MeariApiClient, context: str) -> dict[str, Any] | None:
        """Read the device's IoT config, logging in again between failed attempts.

        Returns None when every attempt failed. Repeated failures are logged as a
        warning, because the device's IoT-backed entities stay unavailable until a
        read succeeds.
        """
        for attempt in range(IOT_READ_ATTEMPTS):
            try:
                iot = api.get_device_iot_config(self._sn_num)
            except (OSError, RuntimeError, ValueError, KeyError) as exc:
                if attempt + 1 < IOT_READ_ATTEMPTS:
                    _LOGGER.debug("%s retry for %s: %s", context, self._sn_num, exc)
                    # Back off before the login, not after: a login landing between
                    # ours and the read is what fails it. Jitter spreads out entries
                    # that HA reloaded together.
                    time.sleep(IOT_READ_RETRY_DELAY * (attempt + 1 + random.random()))
                    self._reauthenticate_api(api, context)
                    continue
                self._iot_read_failures += 1
                if self._iot_read_failures == IOT_READ_WARN_AFTER:
                    _LOGGER.warning(
                        "IoT config for %s could not be read %d times in a row (last error: %s); "
                        "its settings stay unavailable until a read succeeds",
                        self._sn_num,
                        self._iot_read_failures,
                        exc,
                    )
                else:
                    _LOGGER.debug("%s failed for %s: %s", context, self._sn_num, exc)
                return None
            if self._iot_read_failures >= IOT_READ_WARN_AFTER:
                _LOGGER.info("IoT config for %s readable again", self._sn_num)
            self._iot_read_failures = 0
            return iot
        return None

    def _apply_status_iot(self, iot: dict[str, Any]) -> None:
        changed = self._apply_iot_values(iot)
        changed = self._apply_battery_info(iot) or changed
        changed = self._apply_video_encryption_info(iot) or changed
        lamp = self._as_int(self.get_iot_value(IOT_CODE_LAMP))
        if lamp is not None:
            changed = changed or not self._has_lamp or self._lamp_on != (lamp == 1)
            self._has_lamp = True
            self._lamp_on = lamp == 1
        if changed:
            self._fire_update()
