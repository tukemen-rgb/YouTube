"""CLI の表示 — 長い計画の要約(U4)。"""

import unittest

from videoyard.cli import format_plan_report
from videoyard.cutplan import CutPlan, PlanSegment


def _plan(segment_count: int) -> CutPlan:
    segments = []
    for i in range(segment_count):
        action = "keep" if i % 2 == 0 else "cut"
        segments.append(PlanSegment(
            start=float(i), end=float(i + 1), action=action,
            excite=(i * 7) % 101 if action == "keep" else None,
        ))
    return CutPlan(
        source_path="/s.mp4", source_sha256="0" * 64,
        duration=float(segment_count), width=320, height=240,
        has_audio=True, mode="static_or_silent", segments=tuple(segments),
    )


class Report(unittest.TestCase):
    def test_short_plan_shows_all_rows(self):
        lines = format_plan_report(_plan(6))
        self.assertEqual(len(lines), 1 + 6)  # 見出し + 全区間

    def test_long_plan_is_summarized(self):
        lines = format_plan_report(_plan(40))
        self.assertLess(len(lines), 12)
        self.assertTrue(any("上位" in line for line in lines))
        self.assertTrue(any("cutplan.json を参照" in line for line in lines))

    def test_summary_rows_are_top_excite_keeps_in_time_order(self):
        plan = _plan(40)
        lines = format_plan_report(plan)
        shown = [line for line in lines if "残す" in line]
        self.assertEqual(len(shown), 5)
        starts = [float(line.split("〜")[0]) for line in shown]
        self.assertEqual(starts, sorted(starts))  # 表示は時間順


if __name__ == "__main__":
    unittest.main()
