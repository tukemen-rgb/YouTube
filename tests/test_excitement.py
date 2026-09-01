"""盛り上がり度 — 測定出力の解析と点数計算(ffmpeg 不要の純粋部分)。"""

import unittest

from videoyard.analyze import AnalyzeParams, propose_segments, trim_to_target
from videoyard.excitement import (
    SILENCE_FLOOR_DB,
    bucketize,
    combine_scores,
    onsets,
    parse_metadata_series,
    range_score,
    zscores,
)

_METADATA = """\
frame:0    pts:0       pts_time:0
lavfi.signalstats.YDIF=1.5
frame:1    pts:512     pts_time:0.5
lavfi.signalstats.YDIF=3.0
frame:2    pts:1024    pts_time:1.0
lavfi.astats.Overall.RMS_level=-inf
"""


class Parsing(unittest.TestCase):
    def test_series_with_times(self):
        series = parse_metadata_series(_METADATA, "lavfi.signalstats.YDIF")
        self.assertEqual(series, [(0.0, 1.5), (0.5, 3.0)])

    def test_minus_inf_becomes_floor(self):
        series = parse_metadata_series(_METADATA, "lavfi.astats.Overall.RMS_level")
        self.assertEqual(series, [(1.0, SILENCE_FLOOR_DB)])


class Buckets(unittest.TestCase):
    def test_mean_per_window(self):
        series = [(0.0, 2.0), (0.2, 4.0), (0.6, 10.0)]
        self.assertEqual(bucketize(series, duration=1.0, window=0.5), [3.0, 10.0])

    def test_gap_carries_previous_value(self):
        series = [(0.0, 5.0)]
        self.assertEqual(bucketize(series, duration=1.5, window=0.5), [5.0, 5.0, 5.0])


class Scoring(unittest.TestCase):
    def test_zscores_flat_is_zero(self):
        self.assertEqual(zscores([3.0, 3.0, 3.0]), [0.0, 0.0, 0.0])

    def test_onsets_only_rises(self):
        self.assertEqual(onsets([-40.0, -20.0, -30.0]), [0.0, 20.0, 0.0])

    def test_flat_video_scores_50(self):
        self.assertEqual(combine_scores([1.0, 1.0], [0.0, 0.0]), [50.0, 50.0])

    def test_busy_window_scores_higher(self):
        motion = [1.0, 1.0, 9.0, 1.0]
        loudness = [-40.0, -40.0, -10.0, -40.0]
        scores = combine_scores(motion, loudness)
        self.assertEqual(max(range(4), key=lambda i: scores[i]), 2)
        self.assertEqual(scores[2], 100.0)

    def test_motion_only_when_no_audio(self):
        scores = combine_scores([1.0, 5.0], None)
        self.assertLess(scores[0], scores[1])

    def test_range_score_is_window_mean(self):
        scores = [0.0, 100.0, 50.0, 50.0]
        self.assertEqual(range_score(scores, 0.5, 1.0, window=0.5), 100.0)
        self.assertEqual(range_score(scores, 0.0, 1.0, window=0.5), 50.0)


class TrimToTarget(unittest.TestCase):
    """10 秒中 8 秒 keep(2+6)を、盛り上がり度上位で縮める。"""

    def _segments(self):
        return propose_segments(
            10.0, static=[(2, 4)], silent=[], params=AnalyzeParams()
        )  # keep 0-2 / cut 2-4 / keep 4-10

    def test_noop_when_target_is_larger(self):
        segments = self._segments()
        scores = [50.0] * 20
        self.assertEqual(trim_to_target(segments, scores, 999.0), segments)

    def test_keeps_only_top_scored_chunks(self):
        # 7〜10 秒だけ高得点(窓 0.5s → index 14〜19)
        scores = [10.0] * 14 + [90.0] * 6
        segments = trim_to_target(self._segments(), scores, target_seconds=3.0)
        keeps = [s for s in segments if s.action == "keep"]
        kept = sum(s.end - s.start for s in keeps)
        self.assertLessEqual(kept, 3.0 + 3.0)  # 目標+小片 1 個ぶんまで
        self.assertTrue(all(s.start >= 6.9 for s in keeps))  # 高得点帯だけ残る

    def test_excluded_parts_marked_as_length_cut(self):
        scores = [10.0] * 14 + [90.0] * 6
        segments = trim_to_target(self._segments(), scores, target_seconds=3.0)
        reasons = [s.reason for s in segments if s.action == "cut"]
        self.assertTrue(any("尺調整" in r for r in reasons))
        self.assertTrue(any("退屈" in r for r in reasons))  # 元の cut は元の理由のまま

    def test_chronological_and_contiguous(self):
        scores = [10.0] * 6 + [90.0] * 4 + [10.0] * 10
        segments = trim_to_target(self._segments(), scores, target_seconds=2.0)
        cursor = 0.0
        for seg in segments:
            self.assertAlmostEqual(seg.start, cursor, places=3)
            cursor = seg.end
        self.assertAlmostEqual(cursor, 10.0, places=3)

    def test_template_telops_renumbered(self):
        scores = [90.0] * 4 + [10.0] * 10 + [90.0] * 6
        segments = trim_to_target(self._segments(), scores, target_seconds=4.0)
        telops = [s.telop for s in segments if s.action == "keep" and s.telop]
        self.assertEqual(telops, [f"シーン {i+1}" for i in range(len(telops))])


if __name__ == "__main__":
    unittest.main()
