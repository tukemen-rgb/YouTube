"""差分再エンコード(U5)— 内容キー・コマンド組み立て・実際の再利用。"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from videoyard.cutplan import CutPlan, PlanSegment
from videoyard.incremental import (
    build_audio_command,
    build_segment_command,
    segment_fades,
    segment_key,
)

_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _plan(**overrides) -> CutPlan:
    fields = dict(
        source_path="source.mp4",
        source_sha256="0" * 64,
        duration=10.0,
        width=320,
        height=240,
        has_audio=True,
        mode="static_or_silent",
        segments=(
            PlanSegment(start=0.0, end=2.0, action="cut"),
            PlanSegment(start=2.0, end=5.0, action="keep", telop="シーン 1"),
            PlanSegment(start=5.0, end=7.0, action="cut"),
            PlanSegment(start=7.0, end=10.0, action="keep", telop="シーン 2"),
        ),
    )
    fields.update(overrides)
    return CutPlan(**fields)


class Keys(unittest.TestCase):
    def test_same_content_same_key(self):
        plan = _plan()
        a = segment_key(plan, 1, 0, 2, "シーン 1", "none", False)
        b = segment_key(plan, 1, 0, 2, "シーン 1", "none", False)
        self.assertEqual(a, b)

    def test_key_changes_with_content(self):
        plan = _plan()
        base = segment_key(plan, 1, 0, 2, "シーン 1", "none", False)
        self.assertNotEqual(base, segment_key(plan, 1, 0, 2, "別の文字", "none", False))
        self.assertNotEqual(base, segment_key(plan, 1, 0, 2, "シーン 1", "dip", False))
        self.assertNotEqual(base, segment_key(plan, 1, 0, 2, "シーン 1", "none", True))
        other_source = _plan(source_sha256="1" * 64)
        self.assertNotEqual(base, segment_key(other_source, 1, 0, 2, "シーン 1",
                                              "none", False))

    def test_dip_fades_depend_on_position(self):
        # 先頭は頭フェードなし、末尾は尻フェードなし(cut 本体と同じ規則)
        self.assertEqual(segment_fades(0, 3, 10.0, "dip")[0], 0.0)
        self.assertGreater(segment_fades(1, 3, 10.0, "dip")[0], 0.0)
        self.assertEqual(segment_fades(2, 3, 10.0, "dip")[1], 0.0)
        self.assertEqual(segment_fades(0, 3, 10.0, "none"), (0.0, 0.0))


class Commands(unittest.TestCase):
    def test_segment_command_is_video_only(self):
        args = build_segment_command(_plan(), 1, 0, 2, Path("/s.mp4"),
                                     Path("/f.ttf"), None, Path("/o.mp4"),
                                     "none", False)
        self.assertIn("-an", args)
        self.assertIn("trim=start=2.0:end=5.0", args[args.index("-filter_complex") + 1])

    def test_audio_command_matches_cut_rules(self):
        args = build_audio_command(_plan(), Path("/s.mp4"), Path("/a.m4a"),
                                   normalize_loudness=True, bgm=Path("/b.mp3"),
                                   bgm_gain_db=-16.0)
        assert args is not None
        self.assertIn("-vn", args)
        filter_arg = args[args.index("-filter_complex") + 1]
        self.assertIn("afade=t=in", filter_arg)
        self.assertIn("amix=inputs=2:duration=first:normalize=0", filter_arg)
        self.assertIn("loudnorm=I=-14.0", filter_arg)

    def test_no_audio_no_bgm_returns_none(self):
        self.assertIsNone(build_audio_command(
            _plan(has_audio=False), Path("/s.mp4"), Path("/a.m4a"),
            normalize_loudness=True, bgm=None, bgm_gain_db=-16.0))


@unittest.skipUnless(_HAS_FFMPEG, "ffmpeg が無い環境ではスキップ")
class RealIncremental(unittest.TestCase):
    def test_second_cut_reuses_everything_and_edit_reencodes_one(self):
        from videoyard.analyze import AnalyzeParams, analyze
        from videoyard.incremental import cut_incremental
        from videoyard.sheet import apply_sheet, sheet_path, write_sheet

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "src.mp4"
            pieces = []
            for i, (kind, dur) in enumerate(
                (("still", 2), ("move", 3), ("still", 2), ("move", 3))
            ):
                piece = base / f"p{i}.mp4"
                video = (f"color=c=blue:s=320x240:d={dur}:r=30" if kind == "still"
                         else f"testsrc=s=320x240:d={dur}:r=30")
                audio = ("anullsrc=r=44100:cl=stereo" if kind == "still"
                         else "sine=frequency=440:r=44100")
                subprocess.run(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                     "-f", "lavfi", "-i", video, "-f", "lavfi", "-i", audio,
                     "-t", str(dur), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                     "-c:a", "aac", "-ac", "2", "-shortest", str(piece)],
                    check=True, capture_output=True,
                )
                pieces.append(piece)
            (base / "list.txt").write_text(
                "".join(f"file '{p}'\n" for p in pieces), encoding="utf-8")
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-f", "concat", "-safe", "0", "-i", str(base / "list.txt"),
                 "-c", "copy", str(source)], check=True, capture_output=True)

            directory = base / "prod"
            directory.mkdir()
            analyze(directory, source, AnalyzeParams())

            first = cut_incremental(directory)
            self.assertGreater(first["encoded_segments"], 0)
            self.assertEqual(first["reused_segments"], 0)

            # 何も変えずに再カット → 全区間を再利用
            second = cut_incremental(directory)
            self.assertEqual(second["encoded_segments"], 0)
            self.assertEqual(second["reused_segments"], first["encoded_segments"])

            # テロップを 1 区間だけ変える → その区間だけ再エンコード
            plan = None
            from videoyard.cutplan import CutPlan
            plan = CutPlan.load(directory / "cutplan.json")
            sheet = write_sheet(plan).replace("| シーン 1", "| 変更後のテロップ")
            apply_sheet(plan, sheet).save(directory / "cutplan.json")
            sheet_path(directory).write_text(sheet, encoding="utf-8")
            third = cut_incremental(directory)
            self.assertEqual(third["encoded_segments"], 1)

            # 出力の長さは通常カットと同じ規則
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(directory / "out" / "video.mp4")],
                capture_output=True, text=True, check=True,
            )
            self.assertAlmostEqual(float(probe.stdout.strip()),
                                   plan.kept_seconds, delta=0.7)


if __name__ == "__main__":
    unittest.main()
