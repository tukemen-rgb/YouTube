"""○×編集シート(U2)— 書き出し・読み取り・計画への反映。"""

import unittest

from videoyard.cutplan import CutPlan, PlanSegment
from videoyard.sheet import SheetError, apply_sheet, parse_sheet, write_sheet


def _plan() -> CutPlan:
    return CutPlan(
        source_path="/s.mp4", source_sha256="0" * 64, duration=10.0,
        width=320, height=240, has_audio=True, mode="static_or_silent",
        segments=(
            PlanSegment(start=0.0, end=2.0, action="cut", reason="無音"),
            PlanSegment(start=2.0, end=5.0, action="keep", telop="シーン 1", excite=60),
            PlanSegment(start=5.0, end=10.0, action="keep", telop="シーン 2", excite=40),
        ),
    )


class RoundTrip(unittest.TestCase):
    def test_sheet_reflects_plan(self):
        sheet = write_sheet(_plan())
        self.assertIn("× 1 00:00.0-00:02.0", sheet)
        self.assertIn("○ 2 00:02.0-00:05.0 | シーン 1", sheet)
        self.assertIn("盛り上がり度60", sheet)

    def test_unedited_sheet_applies_to_same_plan(self):
        plan = _plan()
        self.assertEqual(apply_sheet(plan, write_sheet(plan)), plan)


class Editing(unittest.TestCase):
    def test_flip_and_retitle(self):
        plan = _plan()
        sheet = write_sheet(plan)
        sheet = sheet.replace("× 1 00:00.0-00:02.0", "○ 1 00:00.0-00:02.0")
        sheet = sheet.replace("| シーン 2", "| ここが神場面")
        updated = apply_sheet(plan, sheet)
        self.assertEqual(updated.segments[0].action, "keep")
        self.assertIn("シートで変更", updated.segments[0].reason)
        self.assertEqual(updated.segments[2].telop, "ここが神場面")
        # 触っていない区間はそのまま
        self.assertEqual(updated.segments[1], plan.segments[1])

    def test_ascii_marks_accepted(self):
        plan = _plan()
        sheet = write_sheet(plan).replace("○ 2", "o 2").replace("× 1", "x 1")
        self.assertEqual(apply_sheet(plan, sheet), plan)

    def test_cutting_removes_telop(self):
        plan = _plan()
        sheet = write_sheet(plan).replace("○ 2 00:02.0-00:05.0", "× 2 00:02.0-00:05.0")
        updated = apply_sheet(plan, sheet)
        self.assertEqual(updated.segments[1].action, "cut")
        self.assertEqual(updated.segments[1].telop, "")


class Rejections(unittest.TestCase):
    def test_missing_row_rejected(self):
        plan = _plan()
        lines = [row for row in write_sheet(plan).splitlines() if " 2 " not in row]
        with self.assertRaises(SheetError):
            apply_sheet(plan, "\n".join(lines))

    def test_bad_mark_rejected(self):
        with self.assertRaises(SheetError):
            parse_sheet("? 1 00:00.0-00:02.0")

    def test_all_cut_rejected_via_plan_validation(self):
        plan = _plan()
        sheet = write_sheet(plan).replace("○", "×")
        with self.assertRaises(SheetError):
            apply_sheet(plan, sheet)

    def test_comment_and_blank_lines_ignored(self):
        entries = parse_sheet("# メモ\n\n○ 1 00:00.0-00:02.0 | あ # 盛り上がり度9\n")
        self.assertEqual(entries, {1: (True, "あ")})


if __name__ == "__main__":
    unittest.main()
