"""Shared ffmpeg subprocess helpers for the audio encoder and muxer."""

from __future__ import annotations

import logging
import subprocess

from ..p2p_streamer.codecs import CodecName, demuxer_for

_LOGGER = logging.getLogger(__name__)

# Common ffmpeg invocation prefix: quiet banner, warnings only.
FFMPEG_BASE_CMD = ["ffmpeg", "-hide_banner", "-loglevel", "warning"]


def terminate_process(proc: subprocess.Popen | None) -> None:
    """Best-effort teardown of an ffmpeg subprocess.

    Closes stdin, asks the process to terminate, then escalates to kill.
    Every step is guarded so teardown never raises.
    """
    if proc is None:
        return
    try:
        if proc.stdin is not None:
            proc.stdin.close()
    except (OSError, ValueError):
        pass
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except (OSError, subprocess.SubprocessError):
        try:
            proc.kill()
            proc.wait(timeout=1)
        except (OSError, subprocess.SubprocessError):
            pass


def video_to_jpeg(codec: CodecName | str, payload: bytes) -> bytes | None:
    """Decode one keyframe to a JPEG, or None if ffmpeg fails."""
    video_format = demuxer_for(codec)
    try:
        proc = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                video_format,
                "-probesize",
                "32768",
                "-analyzeduration",
                "500000",
                "-i",
                "pipe:0",
                "-vframes",
                "1",
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "-q:v",
                "5",
                "pipe:1",
            ],
            input=payload,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _LOGGER.debug("Snapshot conversion failed: %s", exc)
        return None
    return proc.stdout if proc.returncode == 0 and proc.stdout else None
