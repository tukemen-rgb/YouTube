"""timeline の入口検査 — 不正な記述はオブジェクトになれないこと。"""

import json
import tempfile
import unittest
from pathlib import Path

from videoyard.timeline import (
    MAX_TOTAL_SECONDS,
    Scene,
    Timeline,
    TimelineError,
)


def _valid_timeline() -> Timeline:
    return Timeline(scenes=(Scene(text="こんにちは", duration_seconds=2.0),))


class SceneValidation(unittest.TestCase):
    def test_valid_scene(self):
        Scene(text="OK", duration_seconds=1.0)

    def test_rejects_zero_duration(self):
        with self.assertRaises(TimelineError):
            Scene(text="x", duration_seconds=0)

    def test_rejects_bad_color(self):
        with self.assertRaises(TimelineError):
            Scene(text="x", duration_seconds=1.0, background="red")

    def test_rejects_huge_text(self):
        with self.assertRaises(TimelineError):
            Scene(text="あ" * 501, duration_seconds=1.0)


class TimelineValidation(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(_valid_timeline().total_seconds, 2.0)

    def test_rejects_empty(self):
        with self.assertRaises(TimelineError):
            Timeline(scenes=())

    def test_rejects_odd_dimensions(self):
        with self.assertRaises(TimelineError):
            Timeline(scenes=(Scene(text="x", duration_seconds=1.0),), width=1279)

    def test_rejects_over_total_duration(self):
        scenes = tuple(
            Scene(text="x", duration_seconds=120.0)
            for _ in range(int(MAX_TOTAL_SECONDS / 120) + 1)
        )
        with self.assertRaises(TimelineError):
            Timeline(scenes=scenes)

    def test_rejects_unknown_keys(self):
        data = _valid_timeline().to_dict()
        data["autoplay"] = True
        with self.assertRaises(TimelineError):
            Timeline.from_dict(data)

    def test_rejects_unknown_scene_keys(self):
        data = _valid_timeline().to_dict()
        data["scenes"][0]["volume"] = 5
        with self.assertRaises(TimelineError):
            Timeline.from_dict(data)


class TimelineRoundTrip(unittest.TestCase):
    def test_save_load_identical(self):
        timeline = Timeline(
            scenes=(
                Scene(text="一行目\n二行目", duration_seconds=1.5, background="#1d3557"),
                Scene(text="end", duration_seconds=2.0),
            ),
            width=1280,
            height=720,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "timeline.json"
            timeline.save(path)
            self.assertEqual(Timeline.load(path), timeline)

    def test_json_bytes_stable(self):
        # 同じ内容なら同じバイト列(来歴のダイジェストが安定する前提)。
        a = _valid_timeline().to_json()
        b = Timeline.from_dict(json.loads(a)).to_json()
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
