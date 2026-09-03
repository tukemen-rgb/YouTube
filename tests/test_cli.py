"""CLI — 長い計画の要約表示(U4)と auto の一気通貫(U14)。"""

import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from videoyard.cli import format_plan_report, main
from videoyard.cutplan import CutPlan, PlanSegment

_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


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


@unittest.skipUnless(_HAS_FFMPEG, "ffmpeg が無い環境ではスキップ")
class AutoCommand(unittest.TestCase):
    """auto 1 回で 動画・サムネ・説明文・グラフ が全部そろうこと(U14)。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        base = Path(cls._tmp.name)
        pieces = []
        for i, (kind, dur) in enumerate(
            (("still", 2), ("move", 3), ("still", 2), ("move", 3))
        ):
            piece = base / f"p{i}.mp4"
            if kind == "still":
                video = f"color=c=blue:s=320x240:d={dur}:r=30"
                audio = "anullsrc=r=44100:cl=stereo"
            else:
                video = f"testsrc=s=320x240:d={dur}:r=30"
                audio = "sine=frequency=440:r=44100"
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-f", "lavfi", "-i", video, "-f", "lavfi", "-i", audio,
                 "-t", str(dur), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-ac", "2", "-shortest", str(piece)],
                check=True, capture_output=True,
            )
            pieces.append(piece)
        listfile = base / "list.txt"
        listfile.write_text("".join(f"file '{p}'\n" for p in pieces), encoding="utf-8")
        cls.source = base / "source.mp4"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "concat", "-safe", "0", "-i", str(listfile),
             "-c", "copy", str(cls.source)],
            check=True, capture_output=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_one_shot_produces_everything(self):
        base = Path(self._tmp.name)
        directory = base / "prod_auto"
        data = base / "learn_data"
        old_env = os.environ.get("VIDEOYARD_DATA_DIR")
        os.environ["VIDEOYARD_DATA_DIR"] = str(data)
        try:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["auto", str(directory),
                             "--source", str(self.source), "--fast"])
        finally:
            if old_env is None:
                del os.environ["VIDEOYARD_DATA_DIR"]
            else:
                os.environ["VIDEOYARD_DATA_DIR"] = old_env
        self.assertEqual(code, 0)
        # 一気通貫の成果物が全部そろう
        self.assertTrue((directory / "out" / "video.mp4").is_file())
        self.assertTrue((directory / "out" / "description.txt").is_file())
        self.assertTrue((directory / "excitement.svg").is_file())
        self.assertTrue((directory / "cutplan.sheet.txt").is_file())
        thumbs = list((directory / "out" / "thumbnails").glob("thumb_*.png"))
        self.assertTrue(thumbs)
        # 段の記録: assembly と metadata が done
        from videoyard.job import ProductionJob
        job = ProductionJob.load(directory)
        self.assertEqual(job.stage_status("assembly"), "done")
        self.assertEqual(job.stage_status("metadata"), "done")
        # 人の確認を経ていないので学習用の添削は記録されない
        self.assertFalse((data / "feedback.jsonl").exists())
        # 直し方(sheet → apply → cut)が案内される
        self.assertIn("apply", stdout.getvalue())

    def test_shorts_produces_vertical_video(self):
        # U17: auto --shorts は 9:16(1080x1920)の縦動画を一発で出す
        directory = Path(self._tmp.name) / "prod_auto_shorts"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(["auto", str(directory),
                         "--source", str(self.source), "--shorts", "--fast"])
        self.assertEqual(code, 0)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=p=0", str(directory / "out" / "video.mp4")],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(probe.stdout.strip(), "1080,1920")


if __name__ == "__main__":
    unittest.main()
