"""編集ソフトへの持ち出し(S1)— EDL / FCPXML の形式と中身。"""

import re
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from videoyard.cutplan import CutPlan, PlanSegment
from videoyard.export import (
    ExportError,
    rounding_note,
    timecode,
    to_edl,
    to_fcpxml,
    to_frames,
)


def _plan(**overrides) -> CutPlan:
    fields = dict(
        source_path="/videos/source.mp4",
        source_sha256="0" * 64,
        duration=100.0,
        width=1920,
        height=1080,
        has_audio=True,
        mode="static_or_silent",
        segments=(
            PlanSegment(start=0.0, end=10.0, action="cut"),
            PlanSegment(start=10.0, end=20.0, action="keep", telop="開幕"),
            PlanSegment(start=20.0, end=30.0, action="cut"),
            PlanSegment(start=30.0, end=50.0, action="keep", telop="決着"),
        ),
    )
    fields.update(overrides)
    return CutPlan(**fields)


class Timecode(unittest.TestCase):
    def test_frames_and_format(self):
        self.assertEqual(to_frames(1.5, 30), 45)
        self.assertEqual(timecode(0, 30), "00:00:00:00")
        self.assertEqual(timecode(45, 30), "00:00:01:15")
        self.assertEqual(timecode(30 * 3661 + 7, 30), "01:01:01:07")

    def test_negative_rejected(self):
        with self.assertRaises(ExportError):
            timecode(-1, 30)

    def test_rounding_note_only_when_needed(self):
        # 10.0/20.0 秒は 30fps のフレーム境界にちょうど乗る
        self.assertEqual(rounding_note(_plan(), 30), "")
        plan = _plan(segments=(
            PlanSegment(start=0.0, end=10.017, action="keep"),
            PlanSegment(start=10.017, end=20.0, action="cut"),
        ))
        self.assertIn("ミリ秒", rounding_note(plan, 30))


class Edl(unittest.TestCase):
    def test_header_and_events(self):
        edl = to_edl(_plan(), title="テスト")
        self.assertTrue(edl.startswith("TITLE: テスト\nFCM: NON-DROP FRAME"))
        events = [ln for ln in edl.splitlines() if re.match(r"^\d{3}  ", ln)]
        self.assertEqual(len(events), 2)  # keep が 2 区間

    def test_record_times_are_contiguous(self):
        # 出力側の時刻は前の区間の終わりから続く(切った分を詰める)
        edl = to_edl(_plan())
        events = [ln for ln in edl.splitlines() if re.match(r"^\d{3}  ", ln)]
        first, second = (ln.split() for ln in events)
        self.assertEqual(first[-2], "00:00:00:00")   # 1 本目の記録開始
        self.assertEqual(first[-1], "00:00:10:00")   # 10 秒使う
        self.assertEqual(second[-2], "00:00:10:00")  # 続きから
        self.assertEqual(second[-1], "00:00:30:00")  # さらに 20 秒

    def test_source_times_are_the_original_positions(self):
        edl = to_edl(_plan())
        first = next(ln for ln in edl.splitlines() if ln.startswith("001"))
        parts = first.split()
        self.assertEqual(parts[-4], "00:00:10:00")  # 元動画の 10 秒から
        self.assertEqual(parts[-3], "00:00:20:00")  # 20 秒まで

    def test_clip_name_and_telop_comments(self):
        edl = to_edl(_plan())
        self.assertIn("* FROM CLIP NAME: source.mp4", edl)
        self.assertIn("* COMMENT: 開幕", edl)

    def test_speed_segment_emits_motion_record(self):
        plan = _plan(segments=(
            PlanSegment(start=0.0, end=10.0, action="keep"),
            PlanSegment(start=10.0, end=30.0, action="speed"),
        ))
        edl = to_edl(plan)
        self.assertIn("M2   AX", edl)
        self.assertIn("120.0", edl)  # 30fps × 4 倍
        second = next(ln for ln in edl.splitlines() if ln.startswith("002"))
        parts = second.split()
        # 素材は 20 秒使うが、出力では 5 秒しか占めない
        self.assertEqual(parts[-4], "00:00:10:00")
        self.assertEqual(parts[-3], "00:00:30:00")
        self.assertEqual(parts[-2], "00:00:10:00")
        self.assertEqual(parts[-1], "00:00:15:00")

    def test_video_only_track(self):
        edl = to_edl(_plan(has_audio=False))
        first = next(ln for ln in edl.splitlines() if ln.startswith("001"))
        self.assertIn(" V ", first)

    def test_event_numbers_cannot_overflow(self):
        # EDL のイベント番号は 3 桁。計画の区間上限がそれを超えないこと。
        # (MAX_SEGMENTS を増やすときは EDL 側の対処が要る、という縛り)
        from videoyard.cutplan import MAX_SEGMENTS
        from videoyard.export import EDL_MAX_EVENTS
        self.assertLessEqual(MAX_SEGMENTS, EDL_MAX_EVENTS)


class Fcpxml(unittest.TestCase):
    def test_is_well_formed_xml(self):
        root = ET.fromstring(to_fcpxml(_plan()))
        self.assertEqual(root.tag, "fcpxml")
        self.assertEqual(root.get("version"), "1.9")

    def test_asset_and_format(self):
        root = ET.fromstring(to_fcpxml(_plan()))
        fmt = root.find("./resources/format")
        self.assertEqual(fmt.get("frameDuration"), "1/30s")
        self.assertEqual((fmt.get("width"), fmt.get("height")), ("1920", "1080"))
        rep = root.find("./resources/asset/media-rep")
        self.assertTrue(rep.get("src").startswith("file:///videos/source.mp4"))

    def test_clips_carry_telop_names_and_offsets(self):
        root = ET.fromstring(to_fcpxml(_plan()))
        clips = root.findall(".//spine/asset-clip")
        self.assertEqual([c.get("name") for c in clips], ["開幕", "決着"])
        self.assertEqual([c.get("offset") for c in clips], ["0/30s", "300/30s"])
        self.assertEqual([c.get("start") for c in clips], ["300/30s", "900/30s"])
        self.assertEqual([c.get("duration") for c in clips], ["300/30s", "600/30s"])

    def test_speed_segment_uses_timemap(self):
        plan = _plan(segments=(
            PlanSegment(start=0.0, end=10.0, action="keep"),
            PlanSegment(start=10.0, end=30.0, action="speed"),
        ))
        root = ET.fromstring(to_fcpxml(plan))
        clip = root.findall(".//spine/asset-clip")[1]
        points = clip.findall("./timeMap/timept")
        self.assertEqual(len(points), 2)
        # 出力 5 秒(150f)で素材 20 秒(600f)を通す = 4 倍速
        self.assertEqual(points[1].get("time"), "150/30s")
        self.assertEqual(points[1].get("value"), "600/30s")

    def test_special_characters_are_escaped(self):
        plan = _plan(segments=(
            PlanSegment(start=0.0, end=10.0, action="keep",
                        telop='ここが"神"場面 & 最高 <!>'),
        ))
        root = ET.fromstring(to_fcpxml(plan))  # 壊れた XML なら例外
        clip = root.find(".//spine/asset-clip")
        self.assertEqual(clip.get("name"), 'ここが"神"場面 & 最高 <!>')

    def test_relative_source_path_rejected(self):
        with self.assertRaises(ExportError):
            to_fcpxml(_plan(source_path="source.mp4"))

    def test_deterministic(self):
        self.assertEqual(to_fcpxml(_plan()), to_fcpxml(_plan()))
        self.assertEqual(to_edl(_plan()), to_edl(_plan()))

    def test_custom_fps_changes_time_base(self):
        root = ET.fromstring(to_fcpxml(_plan(), fps=60))
        self.assertEqual(root.find("./resources/format").get("frameDuration"),
                         "1/60s")
        self.assertEqual(root.find(".//spine/asset-clip").get("start"), "600/60s")


class WriteExports(unittest.TestCase):
    def test_writes_both_files(self):
        import tempfile

        from videoyard.export import write_exports
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _plan().save(directory / "cutplan.json")
            written = write_exports(directory)
            self.assertEqual([p.name for p in written],
                             ["edit.edl", "edit.fcpxml"])
            self.assertTrue(all(p.is_file() for p in written))


if __name__ == "__main__":
    unittest.main()
