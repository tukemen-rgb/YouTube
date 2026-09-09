"""長い出力の書き出し(S11)— メモリで死なないこと。

1 時間の録画から 5 分のダイジェストを作ろうとして、ffmpeg がメモリ不足で
強制終了した(実測 exit=-9)。filter_complex で区間を concat すると
出力のほぼ全部が生フレームとしてキューに溜まるため。

対処は「区間ごとに入力側シークで書き出して無劣化結合」。ここでは
見積り・分岐の判断・コマンドの組み立てを縛る(実際の書き出しは
時間がかかるので短い素材で 1 本だけ通す)。
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from videoyard.chunked import (
    MEMORY_BUDGET_BYTES,
    build_join_command,
    build_segment_command,
    estimated_buffer_bytes,
    needs_chunking,
    render_indexes,
)
from videoyard.cutplan import CutPlan, PlanSegment

_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _plan(**overrides) -> CutPlan:
    fields = dict(
        source_path="/v/source.mp4", source_sha256="0" * 64, duration=3600.0,
        width=1280, height=720, has_audio=True, mode="static_or_silent",
        segments=(
            PlanSegment(start=0.0, end=100.0, action="cut"),
            PlanSegment(start=100.0, end=130.0, action="keep", telop="場面 1"),
            PlanSegment(start=130.0, end=2000.0, action="cut"),
            PlanSegment(start=2000.0, end=2030.0, action="keep", telop="場面 2"),
        ),
    )
    fields.update(overrides)
    return CutPlan(**fields)


class Estimate(unittest.TestCase):
    def test_estimate_matches_hand_calculation(self):
        # 60 秒 × 30fps × 1280×720 × 1.5 バイト
        expected = 60 * 30 * 1280 * 720 * 1.5
        self.assertAlmostEqual(estimated_buffer_bytes(_plan()), expected, places=0)

    def test_short_output_does_not_need_chunking(self):
        plan = _plan(segments=(
            PlanSegment(start=0.0, end=5.0, action="keep"),
            PlanSegment(start=5.0, end=3600.0, action="cut"),
        ))
        self.assertFalse(needs_chunking(plan))

    def test_long_output_needs_chunking(self):
        # 5 分の 720p 出力 = 実測で 16GB の機械を殺した規模
        plan = _plan(segments=(
            PlanSegment(start=0.0, end=300.0, action="keep"),
            PlanSegment(start=300.0, end=3600.0, action="cut"),
        ))
        self.assertTrue(needs_chunking(plan))
        self.assertGreater(estimated_buffer_bytes(plan), MEMORY_BUDGET_BYTES)

    def test_smaller_frames_allow_longer_output(self):
        small = _plan(width=320, height=240, segments=(
            PlanSegment(start=0.0, end=300.0, action="keep"),
            PlanSegment(start=300.0, end=3600.0, action="cut"),
        ))
        self.assertFalse(needs_chunking(small))


class SegmentCommand(unittest.TestCase):
    def test_uses_input_seeking(self):
        """-ss を -i の前に置く。これが無いと毎回頭からデコードして遅い。"""
        args = build_segment_command(
            _plan(), 1, 0, Path("/v/source.mp4"), Path("/f.ttf"), None,
            Path("/o.mp4"), transition="none", fast=True)
        self.assertLess(args.index("-ss"), args.index("-i"))
        self.assertEqual(args[args.index("-ss") + 1], "100.000")
        self.assertEqual(args[args.index("-t") + 1], "30.000")

    def test_times_are_rebased_to_zero(self):
        """入力側シークの後は区間の先頭が 0 秒。フェードもそれに合わせる。"""
        args = build_segment_command(
            _plan(), 1, 1, Path("/v/source.mp4"), Path("/f.ttf"), None,
            Path("/o.mp4"), transition="dip", fast=True, count=3)
        filter_arg = args[args.index("-filter_complex") + 1]
        self.assertIn("fade=t=in:st=0:", filter_arg)
        self.assertIn("fade=t=out:st=29.85", filter_arg)  # 30 秒区間の終わり
        self.assertNotIn("trim=start=100", filter_arg)

    def _video_chain(self, args) -> str:
        filter_arg = args[args.index("-filter_complex") + 1]
        return next(c for c in filter_arg.split(";") if c.startswith("[0:v]"))

    def test_first_segment_has_no_fade_in(self):
        """動画の頭には演出を入れない(cut 本体と同じ決まり)。"""
        args = build_segment_command(
            _plan(), 1, 0, Path("/v/source.mp4"), Path("/f.ttf"), None,
            Path("/o.mp4"), transition="dip", fast=True, count=2)
        self.assertNotIn("fade=t=in", self._video_chain(args))

    def test_last_segment_has_no_fade_out(self):
        """動画の尻にも入れない。"""
        args = build_segment_command(
            _plan(), 3, 1, Path("/v/source.mp4"), Path("/f.ttf"), None,
            Path("/o.mp4"), transition="dip", fast=True, count=2)
        chain = self._video_chain(args)
        self.assertIn("fade=t=in", chain)
        self.assertNotIn("fade=t=out", chain)

    def test_speed_segment_is_sped_up(self):
        plan = _plan(segments=(
            PlanSegment(start=0.0, end=40.0, action="speed"),
            PlanSegment(start=40.0, end=3600.0, action="cut"),
        ))
        args = build_segment_command(
            plan, 0, 0, Path("/v/source.mp4"), Path("/f.ttf"), None,
            Path("/o.mp4"), transition="none", fast=True)
        filter_arg = args[args.index("-filter_complex") + 1]
        self.assertIn("setpts=PTS/4", filter_arg)
        self.assertIn("atempo=2.0,atempo=2.0", filter_arg)

    def test_sar_is_normalised(self):
        """無劣化結合するので、区間ごとに画素比が違ってはいけない。"""
        args = build_segment_command(
            _plan(), 1, 0, Path("/v/source.mp4"), Path("/f.ttf"), None,
            Path("/o.mp4"), transition="none", fast=True)
        self.assertIn("setsar=1", args[args.index("-filter_complex") + 1])

    def test_silent_source_has_no_audio_map(self):
        args = build_segment_command(
            _plan(has_audio=False), 1, 0, Path("/v/source.mp4"), Path("/f.ttf"),
            None, Path("/o.mp4"), transition="none", fast=True)
        self.assertNotIn("[a]", args)


class JoinCommand(unittest.TestCase):
    def _join(self, **kwargs):
        defaults = dict(
            batch_files=[Path("/p/1.mp4")], list_file=Path("/p/list.txt"),
            output_path=Path("/o.mp4"), plan=_plan(),
            normalize_loudness=True, vertical=False, fast=True,
            bgm=None, bgm_gain_db=-16.0)
        defaults.update(kwargs)
        return build_join_command(**defaults)

    def test_video_is_copied_when_not_vertical(self):
        """絵はもう出来ている。焼き直すと画質が落ちて遅い。"""
        args = self._join()
        self.assertEqual(args[args.index("-c:v") + 1], "copy")

    def test_vertical_re_encodes_with_blur_background(self):
        args = self._join(vertical=True)
        filter_arg = args[args.index("-filter_complex") + 1]
        self.assertIn("boxblur", filter_arg)
        self.assertIn("1080:1920", filter_arg)
        self.assertEqual(args[args.index("-c:v") + 1], "libx264")

    def test_whole_output_effects_are_applied_here(self):
        """ノイズ除去・BGM・音量正規化は結合のときにまとめて掛ける。"""
        args = self._join(bgm=Path("/m.mp3"), denoise="light")
        filter_arg = args[args.index("-filter_complex") + 1]
        self.assertIn("afftdn", filter_arg)
        self.assertIn("amix", filter_arg)
        self.assertIn("loudnorm", filter_arg)
        self.assertLess(filter_arg.index("afftdn"), filter_arg.index("amix"))
        self.assertLess(filter_arg.index("amix"), filter_arg.index("loudnorm"))

    def test_unknown_denoise_rejected(self):
        from videoyard.cut import CutError
        with self.assertRaises(CutError):
            self._join(denoise="めっちゃ強く")

    def test_render_indexes_include_speed(self):
        plan = _plan(segments=(
            PlanSegment(start=0.0, end=10.0, action="keep"),
            PlanSegment(start=10.0, end=20.0, action="speed"),
            PlanSegment(start=20.0, end=30.0, action="cut"),
        ))
        self.assertEqual(render_indexes(plan), [0, 1])


@unittest.skipUnless(_HAS_FFMPEG, "ffmpeg が無い環境ではスキップ")
class RealChunkedRun(unittest.TestCase):
    def test_chunked_output_matches_the_plan(self):
        """短い素材を無理やり分割経路に通し、長さが計画どおりになること。"""
        from videoyard.chunked import cut_chunked
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "src.mp4"
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-f", "lavfi", "-i", "testsrc=s=320x240:d=12:r=30",
                 "-f", "lavfi", "-i", "sine=frequency=440:r=44100",
                 "-t", "12", "-c:v", "libx264", "-preset", "ultrafast",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "2",
                 "-shortest", str(source)], check=True, capture_output=True)
            from videoyard.render import _sha256
            directory = base / "prod"
            directory.mkdir()
            CutPlan(
                source_path=str(source), source_sha256=_sha256(source),
                duration=12.0, width=320, height=240, has_audio=True,
                mode="static_or_silent",
                segments=(
                    PlanSegment(start=0.0, end=3.0, action="keep", telop="前半"),
                    PlanSegment(start=3.0, end=6.0, action="cut"),
                    PlanSegment(start=6.0, end=9.0, action="keep", telop="後半"),
                    PlanSegment(start=9.0, end=12.0, action="cut"),
                ),
            ).save(directory / "cutplan.json")

            manifest = cut_chunked(directory, fast=True)
            self.assertEqual(manifest["mode"], "chunked")
            self.assertEqual(manifest["parts"], 2)
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(directory / "out" / "video.mp4")],
                capture_output=True, text=True, check=True)
            self.assertAlmostEqual(float(probe.stdout.strip()), 6.0, delta=0.5)


if __name__ == "__main__":
    unittest.main()
