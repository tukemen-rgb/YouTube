"""ショート候補の複数生成(C21)— 選定ロジックと通し。"""

import shutil
import unittest

from videoyard.cutplan import CutPlan, PlanSegment
from videoyard.shorts import ShortsError, propose_short_plans

_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _plan() -> CutPlan:
    return CutPlan(
        source_path="/s.mp4", source_sha256="0" * 64, duration=300.0,
        width=1280, height=720, has_audio=True, mode="static_or_silent",
        segments=(
            PlanSegment(start=0.0, end=30.0, action="cut"),
            PlanSegment(start=30.0, end=60.0, action="keep", telop="序盤", excite=40),
            PlanSegment(start=60.0, end=90.0, action="cut"),
            PlanSegment(start=90.0, end=200.0, action="keep", telop="中盤", excite=90),
            PlanSegment(start=200.0, end=230.0, action="cut"),
            PlanSegment(start=230.0, end=260.0, action="keep", telop="終盤", excite=70),
        ),
    )


class Proposal(unittest.TestCase):
    def test_top_excite_first_and_time_ordered(self):
        plans = propose_short_plans(_plan(), count=2)
        # 上位 2 つ(90 と 70)が選ばれ、時間順に並ぶ
        keeps = [p.keeps[0] for p in plans]
        self.assertEqual([k.excite for k in keeps], [90, 70])
        self.assertLess(keeps[0].start, keeps[1].start)

    def test_each_clip_has_single_keep(self):
        original_keeps = {(s.start, s.end) for s in _plan().keeps}
        for clip in propose_short_plans(_plan(), count=3):
            self.assertEqual(len(clip.keeps), 1)
            # 元は keep だった区間が cut になったら理由は「候補外」
            for s in clip.segments:
                if s.action == "cut" and (s.start, s.end) in original_keeps:
                    self.assertEqual(s.reason, "ショート候補外")

    def test_long_keep_trimmed_to_center_60s(self):
        plans = propose_short_plans(_plan(), count=1)
        keep = plans[0].keeps[0]
        # 110 秒の keep(90〜200)は中央 145 を核に 60 秒へ
        self.assertAlmostEqual(keep.end - keep.start, 60.0, places=3)
        self.assertAlmostEqual((keep.start + keep.end) / 2, 145.0, places=3)
        self.assertIn("短縮", keep.reason)

    def test_short_keep_not_trimmed(self):
        plans = propose_short_plans(_plan(), count=3)
        last = plans[-1].keeps[0]
        self.assertEqual((last.start, last.end), (230.0, 260.0))

    def test_candidates_do_not_overlap(self):
        plans = propose_short_plans(_plan(), count=3)
        spans = sorted((p.keeps[0].start, p.keeps[0].end) for p in plans)
        for (_s1, e1), (s2, _) in zip(spans, spans[1:], strict=False):
            self.assertLessEqual(e1, s2)

    def test_no_scores_fails_closed(self):
        plan = CutPlan(
            source_path="/s.mp4", source_sha256="0" * 64, duration=10.0,
            width=320, height=240, has_audio=True, mode="static_or_silent",
            segments=(PlanSegment(start=0.0, end=10.0, action="keep"),),
        )
        with self.assertRaises(ShortsError):
            propose_short_plans(plan)

    def test_count_bounds(self):
        with self.assertRaises(ShortsError):
            propose_short_plans(_plan(), count=0)
        with self.assertRaises(ShortsError):
            propose_short_plans(_plan(), count=99)

    def test_fewer_keeps_than_count_is_fine(self):
        # keep が 3 つしか無いのに count=10 は 3 本になる…ではなく
        # count 上限 10 以内なら keep の数まで
        plans = propose_short_plans(_plan(), count=10)
        self.assertEqual(len(plans), 3)


@unittest.skipUnless(_HAS_FFMPEG, "ffmpeg が無い環境ではスキップ")
class RealShorts(unittest.TestCase):
    def test_two_vertical_clips(self):
        import contextlib
        import io
        import subprocess
        import tempfile
        from pathlib import Path

        try:
            from test_accuracy import synth_video
        except ImportError:  # tests を名前空間パッケージ経由で実行したとき
            from tests.test_accuracy import synth_video

        from videoyard.analyze import AnalyzeParams, analyze
        from videoyard.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = synth_video(base)
            directory = base / "prod"
            directory.mkdir()
            analyze(directory, source, AnalyzeParams())
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(["shorts", str(directory), "--count", "2", "--fast"])
            self.assertEqual(code, 0)
            for i in (1, 2):
                out = directory / "shorts" / f"clip_{i}" / "out" / "video.mp4"
                probe = subprocess.run(
                    ["ffprobe", "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=width,height",
                     "-of", "csv=p=0", str(out)],
                    capture_output=True, text=True, check=True)
                self.assertEqual(probe.stdout.strip(), "1080,1920")


if __name__ == "__main__":
    unittest.main()
