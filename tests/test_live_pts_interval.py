"""Live video PTS follow the camera's measured frame rate."""

from __future__ import annotations

import importlib
import unittest

from debug_tools.bootstrap import _bootstrap_integration_modules

_bootstrap_integration_modules()
MUXER = importlib.import_module("custom_components.cloudplus.coordinator.muxer")


class CameraFrameIntervalTests(unittest.TestCase):
    """Exercise the fixed-step interval derived from camera timestamps."""

    def test_measures_twenty_fps_with_jitter(self):
        meter = MUXER.CameraFrameInterval()
        meter.update(None)
        # 20 fps with alternating jitter; no estimate until enough frames.
        intervals = [meter.update(55 if i % 2 else 45) for i in range(40)]
        self.assertIsNone(intervals[28])
        self.assertAlmostEqual(intervals[-1], 0.05, places=3)

    def test_gap_restarts_run_but_keeps_last_rate(self):
        meter = MUXER.CameraFrameInterval()
        for _ in range(40):
            meter.update(50)
        meter.update(5000)
        intervals = [meter.update(100) for _ in range(5)]
        self.assertEqual(intervals, [0.05] * 5)


if __name__ == "__main__":
    unittest.main()
