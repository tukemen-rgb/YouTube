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


if __name__ == "__main__":
    unittest.main()
