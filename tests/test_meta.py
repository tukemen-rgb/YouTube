"""チャプターと説明文の下書き(C16)— 純粋部分の検証。"""

import unittest

from videoyard.cutplan import CutPlan, PlanSegment
from videoyard.meta import (
    build_description,
    chapter_warnings,
    format_timestamp,
    output_chapters,
)


def _plan() -> CutPlan:
    return CutPlan(
        source_path="/s.mp4", source_sha256="0" * 64, duration=300.0,
        width=320, height=240, has_audio=True, mode="static_or_silent",
        segments=(
            PlanSegment(start=0.0, end=30.0, action="cut"),
            PlanSegment(start=30.0, end=90.0, action="keep", telop="開幕"),
            PlanSegment(start=90.0, end=120.0, action="cut"),
            PlanSegment(start=120.0, end=180.0, action="keep"),  # テロップなし
            PlanSegment(start=180.0, end=240.0, action="keep", telop="決着"),
        ),
    )


class Chapters(unittest.TestCase):
    def test_output_time_axis_and_labels(self):
        chapters = output_chapters(_plan())
        # 出力動画の時間軸(切った分を詰める): 0 / 60 / 120 秒
        self.assertEqual(chapters, [(0.0, "開幕"), (60.0, "シーン 2"), (120.0, "決着")])

    def test_timestamp_format(self):
        self.assertEqual(format_timestamp(0), "0:00")
        self.assertEqual(format_timestamp(65), "1:05")
        self.assertEqual(format_timestamp(3661), "1:01:01")

    def test_warnings_for_short_or_few_chapters(self):
        self.assertTrue(chapter_warnings([(0.0, "a")], 30.0))       # 3 個未満
        self.assertTrue(chapter_warnings(
            [(0.0, "a"), (5.0, "b"), (10.0, "c")], 15.0))           # 5 秒章
        self.assertEqual(chapter_warnings(
            [(0.0, "a"), (20.0, "b"), (40.0, "c")], 60.0), [])      # 問題なし


class Description(unittest.TestCase):
    def test_contains_chapters_and_no_invented_text(self):
        text = build_description(_plan())
        self.assertIn("0:00 開幕", text)
        self.assertIn("1:00 シーン 2", text)
        self.assertIn("2:00 決着", text)
        self.assertNotIn("注:", text)  # 条件を満たすので注は付かない

    def test_bgm_credit_line(self):
        text = build_description(_plan(), bgm_name="battle.mp3")
        self.assertIn("BGM: battle.mp3", text)

    def test_warning_note_included_when_broken(self):
        plan = CutPlan(
            source_path="/s.mp4", source_sha256="0" * 64, duration=20.0,
            width=320, height=240, has_audio=True, mode="static_or_silent",
            segments=(PlanSegment(start=0.0, end=8.0, action="keep"),
                      PlanSegment(start=8.0, end=20.0, action="cut")),
        )
        self.assertIn("注:", build_description(plan))


if __name__ == "__main__":
    unittest.main()
