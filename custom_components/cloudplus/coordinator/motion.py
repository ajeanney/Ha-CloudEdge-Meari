"""MQTT motion listener for Meari cloud events."""

from __future__ import annotations

import hashlib
import json
import logging
import ssl
import threading
import time
from typing import Any, Callable

import paho.mqtt.client as mqtt

from ..api import MeariApiClient
from ..motion_event import parse_motion_event

_LOGGER = logging.getLogger(__name__)
ALARM_POLL_INTERVAL = 15.0
ALARM_POLL_ERROR_INTERVAL = 60.0
# Cap on remembered event ids so the dedup set can't grow without bound.
_SEEN_ALARM_CAP = 4000
MotionCallback = Callable[[str, bytes | None], None]
DoorbellCallback = Callable[[str], None]
# (device_id, sn_num, on_motion, on_doorbell)
_Registration = tuple[str, str, MotionCallback, DoorbellCallback | None]
_IMAGE_URL_KEYS = (
    "imageUrl",
    "imageURL",
    "picUrl",
    "picURL",
    "thumbUrl",
    "snapshotUrl",
)


def _find_image_url(raw_event: dict[str, Any]) -> str:
    """Return the first event snapshot URL found in a nested payload."""
    queue: list[Any] = [raw_event]
    seen: set[int] = set()
    while queue:
        item = queue.pop(0)
        if isinstance(item, dict):
            if id(item) in seen:
                continue
            seen.add(id(item))
            for key in _IMAGE_URL_KEYS:
                value = item.get(key)
                if isinstance(value, str) and value.startswith(
                    ("http://", "https://")
                ):
                    return value
            queue.extend(item.values())
        elif isinstance(item, list):
            queue.extend(item)
    return ""


def _decode_event_snapshot(data: bytes, sn_num: str) -> bytes | None:
    """Decode Meari event JPEG obfuscation for the owning camera."""
    if data.startswith(b"\xff\xd8\xff"):
        return data
    if not data or not sn_num:
        return None

    key_material = f"{sn_num}|{len(sn_num)}|meari.stream".encode()
    key = hashlib.md5(key_material, usedforsecurity=False).hexdigest().encode()  # noqa: S324
    encrypted_size = min(1024, len(data))
    decoded = bytes(
        value ^ key[index % len(key)]
        for index, value in enumerate(data[:encrypted_size])
    ) + data[encrypted_size:]
    if not decoded.startswith(b"\xff\xd8\xff"):
        return None
    return decoded


def _mqtt_callback_code(args: tuple) -> object:
    """Return rc/reason_code from Paho v1/v2 callback tail args."""
    if len(args) >= 2:
        return args[1]
    if args:
        return args[0]
    return 0


def _mqtt_code_ok(code: object) -> bool:
    if code is None or code == 0:
        return True
    value = getattr(code, "value", None)
    if value == 0:
        return True
    try:
        return int(code) == 0
    except (TypeError, ValueError):
        return False


def _event_topics(api: MeariApiClient) -> list[str]:
    user_id = str(api.user_id or "").strip()
    if not user_id:
        return []

    prefixes = [user_id]
    client_id = str(getattr(api, "client_id", "") or "").strip()
    if client_id and client_id != user_id:
        prefixes.insert(0, client_id)

    topics = [
        f"$bsssvr/iot/{prefix}/{user_id}/event/update/accepted" for prefix in prefixes
    ]
    return list(dict.fromkeys(topics))


class MotionEventListener:
    """Account-scoped Meari listener dispatching camera motion and doorbell events."""

    def __init__(self, api: MeariApiClient) -> None:
        self._api = api
        self._client = None
        self._callbacks: list[_Registration] = []
        self._lock = threading.Lock()
        self._stop_poll = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._seen_alarm_keys: set[tuple[str, str]] = set()
        self._poll_day: str = ""
        # Devices whose event log answered in the latest poll pass.
        self._event_log_devices: set[str] = set()
        self._mqtt_connect_warned = False

    @property
    def has_callbacks(self) -> bool:
        with self._lock:
            return bool(self._callbacks)

    def register(
        self,
        device_id: int,
        sn_num: str,
        on_motion: MotionCallback,
        on_doorbell: DoorbellCallback | None = None,
    ) -> Callable[[], None]:
        item = (str(device_id), str(sn_num), on_motion, on_doorbell)
        with self._lock:
            self._callbacks.append(item)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._callbacks.remove(item)
                except ValueError:
                    pass

        return unsubscribe

    def start(self) -> None:
        self._start_alarm_poll()
        if self._client is not None:
            return
        if not self._api.mqtt_host:
            return

        user_id = str(self._api.user_id or "").strip()
        topics = _event_topics(self._api)
        if not user_id or not topics:
            return

        def on_connect(client, _userdata, *args):
            rc = _mqtt_callback_code(args)
            if _mqtt_code_ok(rc):
                self._mqtt_connect_warned = False
                for topic in topics:
                    client.subscribe(topic, qos=2)
                _LOGGER.debug(
                    "MQTT motion listener subscribed to %s",
                    ", ".join(topics),
                )
            else:
                msg = "MQTT motion listener connect failed: rc=%s; using poll fallback"
                if self._mqtt_connect_warned:
                    _LOGGER.debug(msg, rc)
                else:
                    self._mqtt_connect_warned = True
                    _LOGGER.warning(msg, rc)

        def on_disconnect(_client, _userdata, *args):
            rc = _mqtt_callback_code(args)
            if not _mqtt_code_ok(rc):
                _LOGGER.debug("MQTT motion listener disconnected: rc=%s", rc)

        def on_message(_client, _userdata, msg):
            self._handle_payload(msg.payload)

        try:
            client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id=user_id,
                clean_session=True,
                protocol=mqtt.MQTTv311,
            )
        except (AttributeError, TypeError):
            client = mqtt.Client(
                client_id=user_id,
                clean_session=True,
                protocol=mqtt.MQTTv311,
            )

        client.username_pw_set(self._api.access_id, self._api.mqtt_signature)
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        client.tls_set_context(ssl_ctx)
        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = on_message
        client.reconnect_delay_set(min_delay=3, max_delay=60)

        self._client = client
        try:
            client.connect_async(
                self._api.mqtt_host, self._api.mqtt_port, keepalive=300
            )
            client.loop_start()
        except (OSError, ValueError) as exc:
            _LOGGER.warning("MQTT motion listener failed to start: %s", exc)
            self._client = None
            try:
                client.disconnect()
            except OSError:
                pass

    def stop(self) -> None:
        self._stop_alarm_poll()
        client = self._client
        self._client = None
        if client is None:
            return
        try:
            client.loop_stop()
            client.disconnect()
        except OSError:
            pass

    def _start_alarm_poll(self) -> None:
        if self._poll_thread is not None and self._poll_thread.is_alive():
            return
        self._stop_poll.clear()
        self._poll_thread = threading.Thread(target=self._alarm_poll_loop, daemon=True)
        self._poll_thread.start()

    def _stop_alarm_poll(self) -> None:
        self._stop_poll.set()
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=2)
            self._poll_thread = None

    def _alarm_poll_loop(self) -> None:
        # The first pass seeds the "already seen" set without dispatching, so
        # pre-existing events don't fire motion at startup. This is the primary
        # delivery path whenever the live MQTT push is unavailable — most often
        # because the broker requires clientId == userID, so a phone logged in
        # to the same account keeps evicting our MQTT session.
        seeded = False
        while not self._stop_poll.is_set():
            try:
                if not self._poll_device_events(dispatch=seeded):
                    raise RuntimeError("all motion event endpoints failed")
                seeded = True
                wait_s = ALARM_POLL_INTERVAL
            except (OSError, RuntimeError, ValueError, KeyError) as exc:
                _LOGGER.debug("Alarm event poll failed: %s", exc)
                wait_s = (
                    ALARM_POLL_INTERVAL
                    if self._reauthenticate_api()
                    else ALARM_POLL_ERROR_INTERVAL
                )
            self._stop_poll.wait(wait_s)

    def _reauthenticate_api(self) -> bool:
        try:
            self._api.login()
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            _LOGGER.warning("Motion event reauthentication failed: %s", exc)
            return False

        client = self._client
        if client is not None:
            client.username_pw_set(
                self._api.access_id,
                self._api.mqtt_signature,
            )
        _LOGGER.info("Motion event cloud session reauthenticated")
        return True

    def _remember_event(self, device_id: str, event_id: str) -> bool:
        """Return True first time a cloud event key is seen."""
        key = (device_id, event_id)
        if key in self._seen_alarm_keys:
            return False
        self._seen_alarm_keys.add(key)
        return True

    def _trim_seen_events(self) -> None:
        if len(self._seen_alarm_keys) > _SEEN_ALARM_CAP:
            # Keep bounded; trimming oldest is fine because only events newer
            # than the last poll can ever be dispatched.
            self._seen_alarm_keys = set(list(self._seen_alarm_keys)[-_SEEN_ALARM_CAP:])

    def _event_id(self, raw_event: dict, *, prefix: str = "") -> str:
        event_id = str(
            raw_event.get("msgID")
            or raw_event.get("msgId")
            or raw_event.get("eventTime")
            or raw_event.get("devLocalTime")
            or ""
        ).strip()
        if event_id:
            return f"{prefix}{event_id}"
        return f"{prefix}{json.dumps(raw_event, sort_keys=True, separators=(',', ':'))}"

    def _poll_new_event_summary(
        self, devices: set[tuple[str, str]], dispatch: bool
    ) -> bool:
        registered_ids = {device_id for device_id, _sn in devices}
        try:
            events = self._api.get_new_device_events()
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            _LOGGER.debug("New-event summary poll failed: %s", exc)
            return False
        for raw_event in events:
            device_id = str(
                raw_event.get("deviceID")
                or raw_event.get("deviceId")
                or raw_event.get("device_id")
                or ""
            ).strip()
            if not device_id or device_id not in registered_ids:
                continue
            event_id = self._event_id(raw_event, prefix="summary:")
            if not self._remember_event(device_id, event_id):
                continue
            if dispatch:
                self._handle_payload(json.dumps(raw_event).encode(), source="summary")
        return True

    def _poll_device_events(self, dispatch: bool) -> bool:
        """Poll each registered device's event log and dispatch new motions."""
        day = time.strftime("%Y%m%d")
        if day != self._poll_day:
            # Day rolled over: yesterday's ids won't reappear in today's log, so
            # drop them. New events on the fresh day still dispatch normally.
            self._poll_day = day
            self._seen_alarm_keys.clear()

        with self._lock:
            devices = {(item[0], item[1]) for item in self._callbacks}
        if not devices:
            return True

        event_log_ok = False
        event_log_devices: set[str] = set()
        for device_id, _sn in devices:
            try:
                events = self._api.get_device_events(device_id, day)
            except (OSError, RuntimeError, ValueError, KeyError) as exc:
                _LOGGER.debug("Event list poll failed for %s: %s", device_id, exc)
                continue
            event_log_ok = True
            event_log_devices.add(str(device_id))
            for raw_event in events:
                if not isinstance(raw_event, dict):
                    continue
                msg_id = str(
                    raw_event.get("msgID")
                    or raw_event.get("msgId")
                    or raw_event.get("eventTime")
                    or ""
                ).strip()
                if not msg_id:
                    continue
                key = (str(device_id), msg_id)
                if key in self._seen_alarm_keys:
                    continue
                self._seen_alarm_keys.add(key)
                if dispatch:
                    self._handle_payload(json.dumps(raw_event).encode(), source="poll")

        self._event_log_devices = event_log_devices
        summary_ok = self._poll_new_event_summary(devices, dispatch)

        self._trim_seen_events()
        return event_log_ok or summary_ok

    def _matching_callbacks(
        self, device_id: str, license_id: str
    ) -> list[_Registration]:
        with self._lock:
            callbacks = list(self._callbacks)
        out: list[_Registration] = []
        for item in callbacks:
            registered_device_id, registered_sn = item[0], item[1]
            if device_id and device_id == registered_device_id:
                out.append(item)
            elif not device_id and license_id and license_id == registered_sn:
                out.append(item)
        return out

    def _dispatch_doorbell(self, event: dict[str, Any], source: str) -> None:
        """Ring each matching doorbell once, whichever source reports it first.

        A press can arrive by MQTT, then again in the event log (same msgID),
        and again as a summary row (no msgID). A spurious second ring would
        re-trigger automations, so MQTT ids are recorded for the poller and
        summary rows only ring when that device's event log was unavailable.
        """
        msg_id = str(event.get("msg_id") or "")
        for dev, _sn, _on_motion, on_doorbell in self._matching_callbacks(
            event["device_id"], event["license_id"]
        ):
            if on_doorbell is None:
                continue
            if source == "mqtt" and msg_id and not self._remember_event(dev, msg_id):
                continue
            if source == "summary" and dev in self._event_log_devices:
                continue
            # Ring immediately; a snapshot download could delay it by seconds.
            on_doorbell(str(event["evt_name"]))

    def _handle_payload(self, payload: bytes, source: str = "mqtt") -> None:
        try:
            event = parse_motion_event(payload)
        except (ValueError, KeyError, TypeError) as exc:
            _LOGGER.debug("MQTT motion payload parse failed: %s", exc)
            return
        if not event:
            return
        # Alarm type and name only: never ids, serials or image URLs.
        _LOGGER.debug(
            "Meari alarm event source=%s type=%s name=%s",
            source,
            event["evt_raw"],
            event["evt_name"],
        )
        if not event["is_motion"] and not event["is_doorbell"]:
            return

        device_id = event["device_id"]
        license_id = event["license_id"]
        if not device_id and not license_id:
            return

        if event["is_doorbell"]:
            self._dispatch_doorbell(event, source)
            return

        motion_type = str(event["evt_name"])
        raw_event = event["raw"]
        image_url = _find_image_url(raw_event)
        encrypted_image = (
            self._api.download_snapshot(image_url) if image_url else None
        )
        for _dev, sn_num, callback, _on_doorbell in self._matching_callbacks(
            device_id, license_id
        ):
            event_image = (
                _decode_event_snapshot(encrypted_image, sn_num)
                if encrypted_image
                else None
            )
            callback(motion_type, event_image)
