"""Fake Meari cloud + fake battery camera for the e2e harness.

The fake camera behaves like a dormant snap camera: each session waits
``SIM_WAKE_DELAY_S`` before the first frame (a few seconds if a session ended
just before, as the camera is still awake), then streams 15 fps H.264 plus
G.711 µ-law audio. After ``SIM_SESSION_CAP_S`` it goes silent, like the ieGeek
firmware's ~118 s live-session cap, and ends the session 1.5 s later, as the
real engine does when it spots that cap.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

_LOGGER = logging.getLogger(__name__)

FPS = 15
WAKE_DELAY_S = float(os.environ.get("SIM_WAKE_DELAY_S") or 20)
SESSION_CAP_S = float(os.environ.get("SIM_SESSION_CAP_S") or 118)
# A session starting this soon after the last one finds the camera awake.
STILL_AWAKE_S = 10.0
STILL_AWAKE_DELAY_S = 2.0
CAP_RECONNECT_S = 1.5
AUDIO_CHUNK = 8000 // FPS
AUD_RE = re.compile(rb"\x00\x00\x00\x01\x09.", re.DOTALL)

SIM_DEVICE: dict[str, Any] = {
    "deviceID": 4242,
    "snNum": "SIMCAM0001",
    "deviceUUID": "SIMCAM0001",
    "deviceName": "Sim Camera",
    "_category": "snap",
    "capability": {"caps": {"bat": 1}},
}

_MEDIA_LOCK = threading.Lock()
# Shared by every fake session: the encoded clip and when the last one ended.
_STATE: dict[str, Any] = {"media": None, "last_session_end": 0.0}


def _load_media() -> tuple[list[bytes], bytes]:
    """Encode a 10 s test clip once: H.264 access units and µ-law audio."""
    with _MEDIA_LOCK:
        if _STATE["media"] is None:
            out = Path(tempfile.mkdtemp(prefix="cloudplus_sim_"))
            video, audio = out / "clip.h264", out / "clip.ulaw"
            ffmpeg = ["ffmpeg", "-loglevel", "error", "-y", "-t", "10", "-f", "lavfi"]
            subprocess.run(
                [*ffmpeg, "-i", f"testsrc2=size=1280x720:rate={FPS}",
                 "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                 "-g", str(FPS * 2), "-bf", "0",
                 "-x264-params", "aud=1:repeat-headers=1", "-f", "h264", str(video)],
                check=True,
            )
            subprocess.run(
                [*ffmpeg, "-i", "sine=frequency=440:sample_rate=8000",
                 "-f", "mulaw", str(audio)],
                check=True,
            )
            data = video.read_bytes()
            starts = [m.end() for m in AUD_RE.finditer(data)]
            ends = [m.start() for m in AUD_RE.finditer(data)][1:] + [len(data)]
            _STATE["media"] = ([data[s:e] for s, e in zip(starts, ends)],
                               audio.read_bytes())
            _LOGGER.info("Sim clip ready: %d video frames", len(starts))
        return _STATE["media"]


class FakeApiClient:
    """The slice of MeariApiClient cloudplus uses, answered locally."""

    openapi_server = ""  # no IoT endpoint: IoT reads are skipped
    mqtt_host = ""  # no MQTT: the motion listener only polls events
    user_id = ""
    access_id = ""
    mqtt_signature = ""

    def __init__(self, **kwargs: Any) -> None:
        self.app_profile = kwargs.get("app_profile", "")

    def login(self) -> bool:
        return True

    def get_camera_devices(self) -> list[dict]:
        return [dict(SIM_DEVICE)]

    def get_battery_info(self, sn_num: str) -> dict:
        del sn_num
        return {}

    def get_device_iot_config(self, sn_num: str) -> dict:
        del sn_num
        return {}

    def set_device_iot_value(self, sn_num: str, code: str, value: Any) -> bool:
        del sn_num, code, value
        return True

    def wake_device(self, sn_num: str, device_id: int) -> bool:
        _LOGGER.info("Sim wake_device %s/%s", sn_num, device_id)
        return True

    def get_new_device_events(self) -> list:
        return []

    def get_device_events(self, device_id: Any, day: str, **kwargs: Any) -> list:
        del device_id, day, kwargs
        return []

    def download_snapshot(self, url: str) -> bytes:
        del url
        return b""

    def ptz_start(self, sn_num: str, direction: str) -> bool:
        del sn_num, direction
        return False

    def ptz_stop(self, sn_num: str) -> bool:
        del sn_num
        return False


def build_fake_api_client(*args: Any, **kwargs: Any) -> FakeApiClient:
    """Drop-in for ``build_api_client``."""
    del args
    return FakeApiClient(**kwargs)


class FakeP2PStreamer:  # pylint: disable=too-many-instance-attributes
    """Drop-in for P2PStreamer that plays the test clip as a dormant camera."""

    def __init__(
        self,
        api: Any,
        device: dict[str, Any],
        *,
        on_video: Callable[[bytes, int | None], None] | None = None,
        on_audio: Callable[[bytes], None] | None = None,
        **kwargs: Any,
    ) -> None:
        del api, kwargs
        self._sn_num = device.get("snNum", "")
        self.on_video = on_video
        self.on_audio = on_audio
        self.awaiting_wake = False
        self.direct_confirmed = True
        self._stop = threading.Event()
        self._frames = 0

    def request_stop(self) -> None:
        self._stop.set()

    def request_keyframe(self) -> None:
        """The clip has a keyframe every 2 s; nothing to nudge."""

    @property
    def diagnostics(self) -> dict[str, Any]:
        return {"codec": "h264", "video_frames": self._frames, "simulated": True}

    def run_session(self) -> tuple[int, int]:
        try:
            self._run()
        finally:
            _STATE["last_session_end"] = time.monotonic()
        return self._frames, 0

    def _run(self) -> None:
        frames, audio = _load_media()  # pylint: disable=unpacking-non-sequence
        recent = time.monotonic() - _STATE["last_session_end"] < STILL_AWAKE_S
        delay = STILL_AWAKE_DELAY_S if recent else WAKE_DELAY_S
        _LOGGER.info("Sim session %s: first frame in %.0fs", self._sn_num, delay)
        self.awaiting_wake = not recent
        if self._stop.wait(delay):
            return
        self.awaiting_wake = False
        started = next_frame = time.monotonic()
        while not self._stop.is_set():
            if time.monotonic() - started >= SESSION_CAP_S:
                # The camera goes silent; the real engine spots the cap and
                # ends the session after LIVE_SESSION_CAP_RECONNECT_S.
                _LOGGER.info("Sim session %s hit its live cap; going silent", self._sn_num)
                self._stop.wait(CAP_RECONNECT_S)
                return
            index = self._frames % len(frames)
            if self.on_video is not None:
                self.on_video(frames[index], self._frames * 1000 // FPS)
            if self.on_audio is not None:
                offset = (index * AUDIO_CHUNK) % max(1, len(audio) - AUDIO_CHUNK)
                self.on_audio(audio[offset:offset + AUDIO_CHUNK])
            self._frames += 1
            next_frame += 1.0 / FPS
            self._stop.wait(max(0.0, next_frame - time.monotonic()))
