"""Dual-lens cameras: lens discovery from msc and per-lens frame filtering."""

from __future__ import annotations

import importlib
import json
import unittest
from unittest.mock import patch

from debug_tools.bootstrap import _bootstrap_integration_modules

_bootstrap_integration_modules()
ENGINE = importlib.import_module("custom_components.cloudplus.p2p_streamer.engine")
QUALITY = importlib.import_module("custom_components.cloudplus.p2p_streamer.quality")

# Clear-text headers captured from a dual-lens camera (firmware B415B): the
# same instant on both lenses, v_id 4 (0x40) and v_id 3 (0x30).
I4 = bytes.fromhex(
    "000001fc0200f087f2dac86aaeb000002b010000010040010100650001000000"
    "f0000000000000008007000038040000"
)
I3 = bytes.fromhex(
    "000001fc0200f087f2dac86a260601002a010000000030010100650001000000"
    "f0000000000000008007000038040000"
)
P4 = bytes.fromhex(
    "000001fd7f09000033010000010040010100650001000000f100000000000000"
    "8007000038040000"
)
P3 = bytes.fromhex(
    "000001fd211b000039010000000030010100650001000000f100000000000000"
    "8007000038040000"
)
AUDIO = b"\x00\x00\x01\xfa" + bytes(60)
NAMES = {I4: "I4", I3: "I3", P4: "P4", P3: "P3", AUDIO: "audio"}

DUAL_LENS = {
    "capability": json.dumps(
        {
            "caps": {
                "msc": json.dumps(
                    [
                        {"v_id": 4, "ptz2": 1, "bps2": {"0": "640x360", "1": "1920x1080"}},
                        {"v_id": 3, "ptz2": 0, "bps2": {"0": "640x360", "1": "1920x1080"}},
                    ]
                )
            }
        }
    )
}


class LensSourcesTests(unittest.TestCase):
    def test_dual_lens_camera_lists_both_lenses(self):
        self.assertEqual(
            QUALITY.lens_sources(DUAL_LENS),
            {4: "Pan-tilt (source 4)", 3: "Fixed (source 3)"},
        )

    def test_single_lens_camera_has_no_choice(self):
        single = {"capability": json.dumps({"caps": {"msc": json.dumps([{"v_id": 3}])}})}
        self.assertEqual(QUALITY.lens_sources(single), {})
        self.assertEqual(QUALITY.lens_sources({"capability": ""}), {})

    def test_malformed_capability_is_ignored(self):
        self.assertEqual(QUALITY.lens_sources({"capability": '{"caps": {"msc": "[{"}}'}), {})


class FrameLensTests(unittest.TestCase):
    def test_reads_lens_from_i_and_p_frames(self):
        self.assertEqual([QUALITY.frame_lens(f) for f in (I4, I3, P4, P3)], [4, 3, 4, 3])

    def test_other_frames_have_no_lens(self):
        self.assertIsNone(QUALITY.frame_lens(AUDIO))
        self.assertIsNone(QUALITY.frame_lens(b"\x00\x00\x01"))
        self.assertIsNone(QUALITY.frame_lens(I4[:0x10]))


class StreamerLensFilterTests(unittest.TestCase):
    def setUp(self):
        self.streamer = object.__new__(ENGINE.P2PStreamer)
        self.seen: list[str] = []
        self.streamer._parse_stream_chunk = lambda chunk: chunk
        self.streamer._handle_parsed_frame = (
            lambda parsed, wait_for_keyframe=False: self.seen.append(NAMES[parsed]) or False
        )

    def feed(self, frames):
        self.seen.clear()
        with patch.object(ENGINE, "split_stream_frames", return_value=frames):
            self.streamer._handle_stream_payload(b"payload")
        return list(self.seen)

    def test_no_lens_choice_keeps_every_frame(self):
        self.assertEqual(self.feed([I4, I3, P4, AUDIO]), ["I4", "I3", "P4", "audio"])

    def test_keeps_only_the_selected_lens(self):
        self.streamer.lens_v_id = 4
        self.assertEqual(self.feed([I4, I3, P4, P3, AUDIO, P4]), ["I4", "P4", "audio", "P4"])

    def test_switch_waits_for_the_new_lens_keyframe(self):
        self.streamer.lens_v_id = 4
        self.feed([I4, P4])
        self.streamer.switch_lens(3)
        self.assertEqual(self.feed([P4, P3, AUDIO, I4, I3, P3]), ["audio", "I3", "P3"])
        self.assertEqual(self.feed([P3, P4, I3]), ["P3", "I3"])


if __name__ == "__main__":
    unittest.main()
