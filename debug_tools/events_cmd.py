"""``debug.py events`` command: watch motion / doorbell alarms live.

Runs the integration's own ``MotionEventListener`` (MQTT push + event-log
polling) with the same per-camera routing Home Assistant uses, and prints
each delivered motion or doorbell event. Every parsed alarm's type number is
also logged, so unknown alarm types can be identified on real hardware.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import time
from typing import Any

from .auth import _login_api_with_fallback
from .bootstrap import _bootstrap_integration_modules
from .devices import _select_device

_MOTION_LOGGER = "custom_components.cloudplus.coordinator.motion"


def _stamp() -> str:
    return time.strftime("%H:%M:%S")


def _register_camera(listener: Any, dev: dict[str, Any], doorbells: set[str]) -> None:
    name = str(dev.get("deviceName") or dev.get("deviceID"))
    category = str(dev.get("_category", "")).lower()

    def on_motion(motion_type: str, image: bytes | None) -> None:
        print(
            f"[{_stamp()}] MOTION {name}: {motion_type} "
            f"(image: {'yes' if image else 'no'})",
            flush=True,
        )

    def on_doorbell(alarm_name: str) -> None:
        print(f"[{_stamp()}] RING   {name}: {alarm_name}", flush=True)

    # Same gating as the coordinator: only doorbell categories get rings.
    listener.register(
        int(dev.get("deviceID", 0) or 0),
        str(dev.get("snNum", "")),
        on_motion,
        on_doorbell if category in doorbells else None,
    )
    rings = "on" if category in doorbells else "off"
    print(
        f"Watching {name} (id={dev.get('deviceID')}, "
        f"category={category or 'unknown'}, rings={rings})"
    )


async def cmd_events(args) -> int:
    mods = _bootstrap_integration_modules()
    motion_mod = importlib.import_module(_MOTION_LOGGER)
    doorbells = mods["const"].DOORBELL_CATEGORIES
    # Alarm-type lines are debug-level; show them without the full --debug noise.
    logging.getLogger(_MOTION_LOGGER).setLevel(logging.DEBUG)

    api = _login_api_with_fallback(mods["api"].MeariApiClient, args)
    devices = api.get_camera_devices()
    if args.device_id is not None or args.sn:
        devices = [_select_device(devices, args.device_id, args.sn)]
    if not devices:
        print("No camera devices found")
        return 1

    print("=" * 78)
    print(
        "Note: this opens the account's MQTT session, which evicts Home "
        "Assistant's\nand the phone app's session while it runs. Events that "
        "already exist at\nstartup are seeded silently; press the bell after "
        "the first poll (~15 s)."
    )
    print("=" * 78)
    listener = motion_mod.MotionEventListener(api)
    for dev in devices:
        _register_camera(listener, dev, doorbells)

    listener.start()
    try:
        if args.duration > 0:
            await asyncio.sleep(args.duration)
        else:
            while True:
                await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        listener.stop()
        print(f"[{_stamp()}] Listener stopped")
    return 0
