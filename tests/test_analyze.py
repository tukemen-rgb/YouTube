"""分析ロジック — ffmpeg 無しで検証できる純粋部分のテスト。"""

import unittest

from videoyard.analyze import (
    AnalyzeError,
    AnalyzeParams,
    complement,
    intersect,
    mark_highlight,
    normalize,
    parse_freeze,
    parse_silence,
    propose_segments,
)

# 実際の ffmpeg が出す形式を模した stderr
_STDERR = """\
[freezedetect @ 0x1] lavfi.freezedetect.freeze_start: 0
[freezedetect @ 0x1] lavfi.freezedetect.freeze_duration: 2.002
[freezedetect @ 0x1] lavfi.freezedetect.freeze_end: 2.002
[silencedetect @ 0x2] silence_start: 0
[silencedetect @ 0x2] silence_end: 2.0 | silence_duration: 2.0
[freezedetect @ 0x1] lavfi.freezedetect.freeze_start: 5.0
[silencedetect @ 0x2] silence_start: 5.1
"""


class Parsing(unittest.TestCase):
    def test_parse_freeze_pairs(self):
        self.assertEqual(parse_freeze(_STDERR, 10.0), [(0.0, 2.002), (5.0, 10.0)])

    def test_parse_silence_unclosed_closes_at_duration(self):
        self.assertEqual(parse_silence(_STDERR, 10.0), [(0.0, 2.0), (5.1, 10.0)])

    def test_empty_stderr(self):
        self.assertEqual(parse_freeze("", 10.0), [])


class IntervalMath(unittest.TestCase):
    def test_normalize_merges_close(self):
        self.assertEqual(normalize([(0, 1), (1.1, 2), (5, 6)]), [(0, 2), (5, 6)])

    def test_intersect(self):
        self.assertEqual(intersect([(0, 3)], [(2, 5)]), [(2, 3)])

    def test_complement(self):
        self.assertEqual(complement([(2, 4)], 10.0), [(0.0, 2), (4, 10.0)])


class Proposal(unittest.TestCase):
    def _params(self, mode="static_or_silent"):
        return AnalyzeParams(mode=mode)

    def test_or_mode_cuts_union(self):
        segments = propose_segments(
            10.0, static=[(0, 2)], silent=[(5, 7)], params=self._params()
        )
        actions = [(s.start, s.end, s.action) for s in segments]
        self.assertEqual(actions, [
            (0.0, 2.0, "cut"), (2.0, 5.0, "keep"),
            (5.0, 7.0, "cut"), (7.0, 10.0, "keep"),
        ])

    def test_and_mode_cuts_only_overlap(self):
        segments = propose_segments(
            10.0, static=[(0, 4)], silent=[(2, 6)],
            params=self._params("static_and_silent"),
        )
        cuts = [(s.start, s.end) for s in segments if s.action == "cut"]
        self.assertEqual(cuts, [(2.0, 4.0)])

    def test_segments_cover_whole_video_without_overlap(self):
        segments = propose_segments(
            10.0, static=[(1, 3), (8, 9.5)], silent=[(2.5, 5)], params=self._params()
        )
        cursor = 0.0
        for seg in segments:
            self.assertAlmostEqual(seg.start, cursor, places=3)
            cursor = seg.end
        self.assertAlmostEqual(cursor, 10.0, places=3)

    def test_short_boring_not_cut(self):
        # min_cut(1 秒)未満の退屈は切らない = 全編 keep 1 区間
        segments = propose_segments(
            10.0, static=[(3, 3.5)], silent=[], params=self._params()
        )
        self.assertEqual([s.action for s in segments], ["keep"])

    def test_all_boring_fails_closed(self):
        with self.assertRaises(AnalyzeError):
            propose_segments(10.0, static=[(0, 10)], silent=[], params=self._params())

    def test_keeps_get_scene_telops(self):
        segments = propose_segments(
            10.0, static=[(4, 6)], silent=[], params=self._params()
        )
        telops = [s.telop for s in segments if s.action == "keep"]
        self.assertEqual(telops, ["シーン 1", "シーン 2"])


class NearStill(unittest.TestCase):
    def test_sustained_low_motion_detected(self):
        from videoyard.analyze import low_motion_intervals
        motion = [0.05, 0.05, 0.05, 0.05, 5.0, 5.0, 0.05, 5.0]  # 窓 0.5s
        intervals = low_motion_intervals(motion, window=0.5, threshold=0.2, min_still=1.0)
        # 最初の 2 秒だけ(3〜3.5 秒の 1 窓は min_still 未満で拾わない)
        self.assertEqual(intervals, [(0.0, 2.0)])

    def test_tail_low_motion_detected(self):
        from videoyard.analyze import low_motion_intervals
        motion = [5.0, 5.0, 0.05, 0.05, 0.05]
        self.assertEqual(
            low_motion_intervals(motion, window=0.5, threshold=0.2, min_still=1.0),
            [(1.0, 2.5)],
        )

    def test_zero_threshold_disables(self):
        from videoyard.analyze import low_motion_intervals
        self.assertEqual(
            low_motion_intervals([0.0] * 10, window=0.5, threshold=0.0, min_still=1.0),
            [],
        )


class Highlight(unittest.TestCase):
    def test_loudest_marked(self):
        segments = propose_segments(
            10.0, static=[(4, 6)], silent=[], params=AnalyzeParams()
        )
        marked = mark_highlight(segments, {0: -30.0, 2: -12.0})
        self.assertNotIn("盛り上がり", marked[0].reason)
        self.assertIn("盛り上がり", marked[2].reason)

    def test_no_volumes_no_change(self):
        segments = propose_segments(
            10.0, static=[(4, 6)], silent=[], params=AnalyzeParams()
        )
        self.assertEqual(mark_highlight(segments, {}), segments)


class Diagnosis(unittest.TestCase):
    """自己診断(U13)— 極端な結果に気づいてノブを提案する。"""

    def _plan(self, segments):
        from videoyard.cutplan import CutPlan
        return CutPlan(
            source_path="/s.mp4", source_sha256="0" * 64, duration=100.0,
            width=320, height=240, has_audio=True, mode="static_or_silent",
            segments=tuple(segments),
        )

    def test_overcut_flagged(self):
        from videoyard.analyze import diagnose
        from videoyard.cutplan import PlanSegment
        plan = self._plan([
            PlanSegment(start=0.0, end=90.0, action="cut"),
            PlanSegment(start=90.0, end=100.0, action="keep"),
        ])
        advice = diagnose(plan, AnalyzeParams(), has_audio=True)
        self.assertTrue(any("切りすぎ" in a for a in advice))

    def test_nothing_cut_flagged(self):
        from videoyard.analyze import diagnose
        from videoyard.cutplan import PlanSegment
        plan = self._plan([PlanSegment(start=0.0, end=100.0, action="keep")])
        advice = diagnose(plan, AnalyzeParams(), has_audio=True)
        self.assertTrue(any("切れていない" in a for a in advice))

    def test_no_audio_downgrade_noted(self):
        from videoyard.analyze import diagnose
        from videoyard.cutplan import PlanSegment
        plan = self._plan([
            PlanSegment(start=0.0, end=50.0, action="cut"),
            PlanSegment(start=50.0, end=100.0, action="keep"),
        ])
        advice = diagnose(plan, AnalyzeParams(), has_audio=False)
        self.assertTrue(any("音声が無い" in a for a in advice))

    def test_choppy_keeps_flagged(self):
        from videoyard.analyze import diagnose
        from videoyard.cutplan import PlanSegment
        segments = []
        cursor = 0.0
        for _ in range(4):  # 1 秒 keep と 24 秒 cut を繰り返す細切れ
            segments.append(PlanSegment(start=cursor, end=cursor + 1.0, action="keep"))
            segments.append(PlanSegment(start=cursor + 1.0, end=cursor + 25.0, action="cut"))
            cursor += 25.0
        advice = diagnose(self._plan(segments), AnalyzeParams(), has_audio=True)
        self.assertTrue(any("細切れ" in a for a in advice))

    def test_normal_result_has_no_advice(self):
        from videoyard.analyze import diagnose
        from videoyard.cutplan import PlanSegment
        plan = self._plan([
            PlanSegment(start=0.0, end=40.0, action="cut"),
            PlanSegment(start=40.0, end=100.0, action="keep"),
        ])
        self.assertEqual(diagnose(plan, AnalyzeParams(), has_audio=True), [])


if __name__ == "__main__":
    unittest.main()
