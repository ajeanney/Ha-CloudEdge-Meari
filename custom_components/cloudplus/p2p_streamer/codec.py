"""Stream frame decryption and camera frame parsing."""

from __future__ import annotations

import struct
import threading
from dataclasses import dataclass

from Crypto.Cipher import DES3

from .protocol import (
    STREAM_TYPE_IFRAME,
    STREAM_TYPE_PFRAME,
    STREAM_TYPE_AUDIO,
    STREAM_TYPE_INFO,
    STREAM_ENCRYPT_KEY,
)

# ---------------------------------------------------------------------------
# Stream decryption
# ---------------------------------------------------------------------------

MAX_FRAME_DATA_BYTES = 8 * 1024 * 1024
VIDEO_ENCRYPTED_HEADER_BYTES = 0x80
_TLS = threading.local()

# Both layouts encrypt from the compact offset; extended frames carry another
# 12 bytes of metadata before the media payload.
IFRAME_HEADER_COMPACT = 0x30
IFRAME_HEADER_EXTENDED = 0x3C
PFRAME_HEADER_COMPACT = 0x28
PFRAME_HEADER_EXTENDED = 0x34
AUDIO_HEADER_COMPACT = 0x28
AUDIO_HEADER_EXTENDED = 0x34
_VIDEO_HEADER_SIZES = {
    STREAM_TYPE_IFRAME: (IFRAME_HEADER_COMPACT, IFRAME_HEADER_EXTENDED),
    STREAM_TYPE_PFRAME: (PFRAME_HEADER_COMPACT, PFRAME_HEADER_EXTENDED),
}


def _starts_annexb(data: bytes, offset: int) -> bool:
    return data[offset : offset + 3] == b"\x00\x00\x01" or data[
        offset : offset + 4
    ] == b"\x00\x00\x00\x01"


def _video_header_size(data: bytes, frame_type: int) -> int | None:
    """Choose compact when an Annex-B payload begins at its offset."""
    compact, extended = _VIDEO_HEADER_SIZES[frame_type]
    if _starts_annexb(data, compact):
        return compact
    return extended if len(data) >= extended else None


def _audio_header_size(data: bytes) -> int:
    """Use the extended audio layout only when its length fits exactly."""
    if len(data) >= AUDIO_HEADER_EXTENDED:
        data_len = struct.unpack_from("<I", data, AUDIO_HEADER_EXTENDED - 4)[0]
        if 0 < data_len < 2000 and len(data) == AUDIO_HEADER_EXTENDED + data_len:
            return AUDIO_HEADER_EXTENDED
    return AUDIO_HEADER_COMPACT


def _video_payload(data: bytes, header_size: int) -> bytes | None:
    """Validate and slice an extended payload, or use the whole compact one."""
    if header_size in (IFRAME_HEADER_EXTENDED, PFRAME_HEADER_EXTENDED):
        data_len = struct.unpack_from("<I", data, header_size - 4)[0]
        if data_len > MAX_FRAME_DATA_BYTES or len(data) < header_size + data_len:
            return None
        return (
            data[header_size : header_size + data_len]
            if data_len
            else data[header_size:]
        )
    return data[header_size:]


@dataclass(frozen=True)
class StreamFrame:
    """A parsed VVP stream frame: type, header size and payload."""

    frame_type: int
    header_size: int
    payload: bytes
    timestamp_ms: int | None = None
    sequence: int | None = None


class FrameSequenceTracker:
    """Drop duplicate/backward VVP video frames and honor keyframe gates."""

    def __init__(self) -> None:
        self._last: int | None = None
        self._await_keyframe = False

    def reset(self) -> None:
        self._last = None
        self._await_keyframe = False

    def require_keyframe(self) -> None:
        self._await_keyframe = True

    def should_drop(self, sequence: int | None, *, recovery: bool) -> bool:
        if recovery:
            self._last = sequence
            self._await_keyframe = False
            return False
        if sequence is None:
            return self._await_keyframe
        if self._last is not None:
            delta = (sequence - self._last) & 0xFFFFFFFF
            if delta == 0 or delta > 0x80000000:
                return True
        self._last = sequence
        return self._await_keyframe


def _stream_cipher():
    cipher = getattr(_TLS, "stream_cipher", None)
    if cipher is None:
        key = STREAM_ENCRYPT_KEY[:24]
        if len(key) < 24:
            key = key + b"\x00" * (24 - len(key))
        cipher = DES3.new(key, DES3.MODE_ECB)
        _TLS.stream_cipher = cipher
    return cipher


def _des3_ecb_decrypt_block(data: bytes, key: bytes = STREAM_ENCRYPT_KEY) -> bytes:
    k = key[:24]
    if len(k) < 24:
        k = k + b"\x00" * (24 - len(k))
    if k == STREAM_ENCRYPT_KEY.ljust(24, b"\x00"):
        return _stream_cipher().decrypt(data)
    return DES3.new(k, DES3.MODE_ECB).decrypt(data)


def _available_encrypted_len(data_len: int, offset: int, limit: int) -> int:
    available = max(0, data_len - offset)
    return min(limit, (available // 8) * 8)


def decrypt_stream_frame(data: bytearray) -> bytearray:
    if len(data) < 4:
        return data
    frame_type = data[3]
    if frame_type == STREAM_TYPE_IFRAME:
        enc_offset = 0x30
        enc_len = _available_encrypted_len(
            len(data), enc_offset, VIDEO_ENCRYPTED_HEADER_BYTES
        )
    elif frame_type == STREAM_TYPE_PFRAME:
        enc_offset = 0x28
        enc_len = _available_encrypted_len(
            len(data), enc_offset, VIDEO_ENCRYPTED_HEADER_BYTES
        )
    elif frame_type == STREAM_TYPE_AUDIO:
        enc_offset = 0x28
        remaining = len(data) - enc_offset
        enc_len = (remaining // 8) * 8
    else:
        return data
    if enc_len < 8 or len(data) < enc_offset + enc_len:
        return data
    encrypted = bytes(data[enc_offset : enc_offset + enc_len])
    decrypted = _des3_ecb_decrypt_block(encrypted, STREAM_ENCRYPT_KEY)
    data[enc_offset : enc_offset + enc_len] = decrypted
    return data


def _find_stream_start(data: bytes, start: int = 0) -> int:
    i = max(0, start)
    while i + 3 < len(data):
        if data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 1:
            if data[i + 3] in (
                STREAM_TYPE_IFRAME,
                STREAM_TYPE_PFRAME,
                STREAM_TYPE_AUDIO,
                STREAM_TYPE_INFO,
            ):
                return i
        i += 1
    return -1


def _peek_video_total_len(frame: bytes, frame_type: int) -> int | None:
    enc_offset, header_size = _VIDEO_HEADER_SIZES[frame_type]
    enc_len = _available_encrypted_len(len(frame), enc_offset, 16)
    if enc_len < 16:
        return None
    if _starts_annexb(frame, enc_offset):
        return None
    if _starts_annexb(frame, header_size):
        data_len = struct.unpack_from("<I", frame, header_size - 4)[0]
        return header_size + data_len if 0 < data_len <= MAX_FRAME_DATA_BYTES else None
    header = _des3_ecb_decrypt_block(bytes(frame[enc_offset : enc_offset + enc_len]))
    if _starts_annexb(header, 0):
        return None
    data_len = struct.unpack_from("<I", header, 8)[0]
    if 0 < data_len <= MAX_FRAME_DATA_BYTES:
        return header_size + data_len
    return None


def _peek_audio_total_len(frame: bytes) -> int | None:
    if len(frame) < 0x34:
        return None
    data_len = struct.unpack_from("<I", frame, 0x30)[0]
    if 0 < data_len < 2000:
        return 0x34 + data_len
    if len(frame) < 0x38:
        return None
    header = _des3_ecb_decrypt_block(bytes(frame[0x28:0x38]))
    data_len = struct.unpack_from("<I", header, 8)[0]
    if 0 < data_len < 2000:
        return 0x34 + data_len
    return None


def _peek_frame_total_len(data: bytes, start: int) -> int | None:
    frame_type = data[start + 3]
    frame = data[start:]
    if frame_type in (STREAM_TYPE_IFRAME, STREAM_TYPE_PFRAME):
        return _peek_video_total_len(frame, frame_type)
    if frame_type == STREAM_TYPE_AUDIO:
        return _peek_audio_total_len(frame)
    if frame_type == STREAM_TYPE_INFO and len(frame) >= 8:
        data_len = struct.unpack_from("<H", frame, 6)[0]
        return 8 + data_len
    return None


# ---------------------------------------------------------------------------
# Stream frame parsing
# ---------------------------------------------------------------------------


def parse_stream_frame(data: bytes):
    if len(data) < 8:
        return None
    if data[0] != 0 or data[1] != 0 or data[2] != 1:
        return None
    frame_type = data[3]
    if frame_type in _VIDEO_HEADER_SIZES:
        compact, _ = _VIDEO_HEADER_SIZES[frame_type]
        if len(data) < compact:
            return None
        header_size = _video_header_size(data, frame_type)
        if header_size is None:
            return None
        payload = _video_payload(data, header_size)
        if payload is None:
            return None
        sequence = struct.unpack_from(
            "<I", data, 0x10 if frame_type == STREAM_TYPE_IFRAME else 0x08
        )[0]
        timestamp_ms = struct.unpack_from("<I", data, header_size - 0x0C)[0]
        return StreamFrame(frame_type, header_size, payload, timestamp_ms, sequence)
    if frame_type == STREAM_TYPE_AUDIO:
        if len(data) < AUDIO_HEADER_COMPACT:
            return None
        header_size = _audio_header_size(data)
        timestamp_ms = struct.unpack_from("<I", data, header_size - 0x0C)[0]
        return StreamFrame(frame_type, header_size, data[header_size:], timestamp_ms)
    if frame_type == STREAM_TYPE_INFO:
        if len(data) < 8:
            return None
        data_len = struct.unpack_from("<H", data, 6)[0]
        payload = data[8 : 8 + data_len] if data_len > 0 else data[8:]
        return StreamFrame(frame_type, 8, payload)
    return None


def split_stream_frames(data: bytes) -> list[bytes]:
    """Split payload into candidate 00 00 01 frame chunks.

    Camera payloads can contain multiple frame chunks or leading bytes
    before the first frame marker.
    """
    if len(data) < 4:
        return []

    chunks: list[bytes] = []
    pos = 0
    while pos + 3 < len(data):
        start = _find_stream_start(data, pos)
        if start < 0:
            break

        total_len = _peek_frame_total_len(data, start)
        end = start + total_len if total_len is not None else -1
        if total_len is not None and end <= len(data) and (
            end == len(data) or _find_stream_start(data, end) == end
        ):
            chunks.append(data[start:end])
            pos = end
            continue

        next_start = _find_stream_start(data, start + 4)
        if next_start < 0:
            if start == 0:
                chunks.append(data[start:])
            break
        if next_start > start:
            chunks.append(data[start:next_start])
        pos = next_start

    if not chunks:
        return [data]
    return chunks
