"""Synthetic VVP frames covering both media header layouts."""

from __future__ import annotations

import importlib
import struct
import unittest

from Crypto.Cipher import DES3

from debug_tools.bootstrap import _bootstrap_integration_modules

_bootstrap_integration_modules()
API = importlib.import_module("custom_components.cloudplus.api")
CODEC = importlib.import_module("custom_components.cloudplus.p2p_streamer.codec")
ENGINE = importlib.import_module("custom_components.cloudplus.p2p_streamer.engine")
PROTOCOL = importlib.import_module("custom_components.cloudplus.p2p_streamer.protocol")

VIDEO = b"\x00\x00\x00\x01\x67\x42\x00\x1f" + b"\x55" * 32
AUDIO = bytes(range(160))


def frame(frame_type: int, payload: bytes, *, extended: bool) -> bytes:
    if frame_type == PROTOCOL.STREAM_TYPE_AUDIO:
        compact, full = CODEC.AUDIO_HEADER_COMPACT, CODEC.AUDIO_HEADER_EXTENDED
    else:
        compact, full = CODEC._VIDEO_HEADER_SIZES[frame_type]
    size = full if extended else compact
    data = bytearray(size + len(payload))
    data[:4] = b"\x00\x00\x01" + bytes([frame_type])
    if frame_type != PROTOCOL.STREAM_TYPE_AUDIO:
        struct.pack_into(
            "<I", data, 0x10 if frame_type == PROTOCOL.STREAM_TYPE_IFRAME else 0x08, 42
        )
    struct.pack_into("<I", data, size - 0x0C, 1234)
    if extended:
        struct.pack_into("<I", data, size - 4, len(payload))
    data[size:] = payload
    return bytes(data)


def encrypted(data: bytes) -> bytes:
    result = bytearray(data)
    offset = 0x30 if data[3] == PROTOCOL.STREAM_TYPE_IFRAME else 0x28
    length = (len(data) - offset) // 8 * 8
    if data[3] != PROTOCOL.STREAM_TYPE_AUDIO:
        length = min(length, CODEC.VIDEO_ENCRYPTED_HEADER_BYTES)
    cipher = DES3.new(PROTOCOL.STREAM_ENCRYPT_KEY.ljust(24, b"\x00"), DES3.MODE_ECB)
    result[offset : offset + length] = cipher.encrypt(data[offset : offset + length])
    return bytes(result)


class VvpMediaLayoutTests(unittest.TestCase):
    def test_compact_video_plain_and_encrypted(self):
        for kind in (PROTOCOL.STREAM_TYPE_IFRAME, PROTOCOL.STREAM_TYPE_PFRAME):
            plain = frame(kind, VIDEO, extended=False)
            following = frame(PROTOCOL.STREAM_TYPE_AUDIO, AUDIO, extended=True)
            for wire in (plain, encrypted(plain)):
                with self.subTest(kind=kind, encrypted=wire != plain):
                    self.assertEqual(
                        CODEC.split_stream_frames(wire + following), [wire, following]
                    )
                    streamer = object.__new__(ENGINE.P2PStreamer)
                    streamer._video_decrypt = None
                    parsed = streamer._parse_video_chunk(wire)
                    self.assertEqual(parsed.payload, VIDEO)
                    self.assertEqual(parsed.header_size, CODEC._VIDEO_HEADER_SIZES[kind][0])
                    self.assertEqual(parsed.timestamp_ms, 1234)
                    self.assertEqual(streamer._video_decrypt, wire != plain)

    def test_compact_video_ignores_fake_extended_length(self):
        compact = bytearray(frame(PROTOCOL.STREAM_TYPE_IFRAME, VIDEO, extended=False))
        struct.pack_into("<I", compact, CODEC.IFRAME_HEADER_EXTENDED - 4, 4)
        next_frame = frame(PROTOCOL.STREAM_TYPE_AUDIO, AUDIO, extended=True)
        self.assertEqual(
            CODEC.split_stream_frames(bytes(compact) + next_frame),
            [bytes(compact), next_frame],
        )

    def test_extended_video_keeps_declared_length(self):
        for kind in (PROTOCOL.STREAM_TYPE_IFRAME, PROTOCOL.STREAM_TYPE_PFRAME):
            plain = frame(kind, VIDEO, extended=True)
            for wire in (plain, encrypted(plain)):
                with self.subTest(kind=kind, encrypted=wire != plain):
                    self.assertEqual(CODEC.split_stream_frames(wire), [wire])
                    parsed = CODEC.parse_stream_frame(
                        bytes(CODEC.decrypt_stream_frame(bytearray(wire)))
                        if wire != plain
                        else wire
                    )
                    self.assertEqual(parsed.payload, VIDEO)
                    self.assertEqual(parsed.header_size, CODEC._VIDEO_HEADER_SIZES[kind][1])
                    self.assertEqual(parsed.timestamp_ms, 1234)

    def test_compact_audio_follows_video_encryption(self):
        streamer = object.__new__(ENGINE.P2PStreamer)
        streamer._audio_decrypt = None
        streamer._video_decrypt = False
        compact = frame(PROTOCOL.STREAM_TYPE_AUDIO, AUDIO, extended=False)
        parsed = streamer._parse_stream_chunk(compact)
        self.assertEqual(parsed.payload, AUDIO)
        self.assertEqual(parsed.header_size, CODEC.AUDIO_HEADER_COMPACT)
        self.assertFalse(streamer._audio_decrypt)

        streamer._video_decrypt = True
        encrypted_compact = encrypted(compact)
        parsed = streamer._parse_stream_chunk(encrypted_compact)
        self.assertEqual(parsed.payload, AUDIO)
        self.assertTrue(streamer._audio_decrypt)

    def test_encrypted_extended_audio_updates_mode(self):
        streamer = object.__new__(ENGINE.P2PStreamer)
        streamer._audio_decrypt = None
        streamer._video_decrypt = None
        wire = encrypted(frame(PROTOCOL.STREAM_TYPE_AUDIO, AUDIO, extended=True))
        parsed = streamer._parse_stream_chunk(wire)
        self.assertEqual(parsed.payload, AUDIO)
        self.assertEqual(parsed.timestamp_ms, 1234)
        self.assertTrue(streamer._audio_decrypt)

    def test_plain_extended_audio_updates_mode(self):
        streamer = object.__new__(ENGINE.P2PStreamer)
        streamer._audio_decrypt = None
        streamer._video_decrypt = True
        parsed = streamer._parse_stream_chunk(
            frame(PROTOCOL.STREAM_TYPE_AUDIO, AUDIO, extended=True)
        )
        self.assertEqual(parsed.payload, AUDIO)
        self.assertFalse(streamer._audio_decrypt)

    def test_arenti_flag_is_profile_scoped(self):
        self.assertEqual(API.APP_PROFILE_CONFIG["arenti"].vvp_stream_flag, 0)
        self.assertEqual(API.APP_PROFILE_CONFIG["cloudplus"].vvp_stream_flag, 1)


if __name__ == "__main__":
    unittest.main()
