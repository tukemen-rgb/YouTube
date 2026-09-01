"""カット計画とその実行 — 検証・コマンド組み立て・実 ffmpeg での通し。"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from videoyard.cut import CutError, build_command, cut, write_telop_files
from videoyard.cutplan import CutPlan, CutPlanError, PlanSegment

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
            PlanSegment(start=0.0, end=2.0, action="cut", reason="無音"),
            PlanSegment(start=2.0, end=5.0, action="keep", telop="シーン 1"),
            PlanSegment(start=5.0, end=7.0, action="cut"),
            PlanSegment(start=7.0, end=10.0, action="keep", telop="シーン 2"),
        ),
    )
    fields.update(overrides)
    return CutPlan(**fields)


class PlanValidation(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(_plan().kept_seconds, 6.0)

    def test_rejects_overlap(self):
        with self.assertRaises(CutPlanError):
            _plan(segments=(
                PlanSegment(start=0.0, end=5.0, action="keep"),
                PlanSegment(start=4.0, end=6.0, action="keep"),
            ))

    def test_rejects_beyond_duration(self):
        with self.assertRaises(CutPlanError):
            _plan(segments=(PlanSegment(start=0.0, end=99.0, action="keep"),))

    def test_rejects_all_cut(self):
        with self.assertRaises(CutPlanError):
            _plan(segments=(PlanSegment(start=0.0, end=10.0, action="cut"),))

    def test_rejects_unknown_action(self):
        with self.assertRaises(CutPlanError):
            PlanSegment(start=0.0, end=1.0, action="maybe")

    def test_rejects_unknown_keys(self):
        data = _plan().to_dict()
        data["speed"] = 2.0
        with self.assertRaises(CutPlanError):
            CutPlan.from_dict(data)

    def test_round_trip(self):
        plan = _plan()
        self.assertEqual(CutPlan.from_dict(plan.to_dict()), plan)


class CommandBuilding(unittest.TestCase):
    def test_deterministic(self):
        plan = _plan()
        a = build_command(plan, Path("/s.mp4"), Path("/f.ttf"), {}, Path("/o.mp4"))
        b = build_command(plan, Path("/s.mp4"), Path("/f.ttf"), {}, Path("/o.mp4"))
        self.assertEqual(a, b)

    def test_only_keep_segments_in_filter(self):
        args = build_command(_plan(), Path("/s.mp4"), Path("/f.ttf"), {}, Path("/o.mp4"))
        filter_arg = args[args.index("-filter_complex") + 1]
        self.assertIn("trim=start=2.0:end=5.0", filter_arg)
        self.assertNotIn("trim=start=0.0:end=2.0", filter_arg)  # cut 区間は現れない
        self.assertIn("concat=n=2:v=1:a=1", filter_arg)

    def test_audio_fades_at_joins(self):
        # つなぎ目のクリック音対策: 各 keep 区間の入りと終わりにフェード
        args = build_command(_plan(), Path("/s.mp4"), Path("/f.ttf"), {}, Path("/o.mp4"))
        filter_arg = args[args.index("-filter_complex") + 1]
        self.assertIn("afade=t=in:st=0:d=0.15", filter_arg)
        self.assertIn("afade=t=out:st=2.85:d=0.15", filter_arg)  # 3 秒区間の終わり

    def test_no_audio_concat_video_only(self):
        plan = _plan(has_audio=False)
        args = build_command(plan, Path("/s.mp4"), Path("/f.ttf"), {}, Path("/o.mp4"))
        filter_arg = args[args.index("-filter_complex") + 1]
        self.assertIn("concat=n=2:v=1:a=0", filter_arg)
        self.assertNotIn("atrim", filter_arg)

    def test_telop_uses_textfile_and_no_expansion(self):
        with tempfile.TemporaryDirectory() as tmp:
            telops = write_telop_files(_plan(), Path(tmp))
            args = build_command(_plan(), Path("/s.mp4"), Path("/f.ttf"), telops, Path("/o.mp4"))
            filter_arg = args[args.index("-filter_complex") + 1]
            self.assertIn("textfile=", filter_arg)
            self.assertIn("expansion=none", filter_arg)

    def test_too_long_telop_rejected(self):
        plan = _plan(segments=(
            PlanSegment(start=0.0, end=10.0, action="keep", telop="あ" * 120),
        ))
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(CutError):
            write_telop_files(plan, Path(tmp))


@unittest.skipUnless(_HAS_FFMPEG, "ffmpeg が無い環境ではスキップ")
class RealCut(unittest.TestCase):
    """既知の構造の動画を合成し、退屈な区間が実際に切れることを通しで確認。

    構造: 0-2s 静止+無音 / 2-5s 動き+音 / 5-7s 静止+無音 / 7-10s 動き+音
    """

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
            # -ac 2: sine はモノラルなので、全ピースをステレオに揃える。
            # 揃えないと連結後に音声の形式が途中で変わる壊れた動画になる。
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

    def test_analyze_then_cut(self):
        from videoyard.analyze import AnalyzeParams, analyze

        directory = Path(self._tmp.name) / "prod"
        directory.mkdir()
        plan = analyze(directory, self.source, AnalyzeParams())
        # 退屈な 2 区間(冒頭と真ん中)が cut、動きのある 2 区間が keep
        self.assertEqual(len(plan.keeps), 2)
        # keep には盛り上がり度が付き、★ がどこか 1 区間に付く
        self.assertTrue(all(s.excite is not None for s in plan.keeps))
        self.assertEqual(sum("★" in s.reason for s in plan.keeps), 1)
        self.assertLess(plan.kept_seconds, plan.duration - 3.0)
        # 実行 → 出力の長さが残した秒数に近いこと
        manifest = cut(directory)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(directory / "out" / "video.mp4")],
            capture_output=True, text=True, check=True,
        )
        out_duration = float(probe.stdout.strip())
        self.assertAlmostEqual(out_duration, plan.kept_seconds, delta=0.6)
        self.assertTrue((directory / "out" / "render_manifest.json").is_file())
        self.assertEqual(manifest["plan_file"], "cutplan.json")

    def test_analyze_reports_progress(self):
        from videoyard.analyze import AnalyzeParams, analyze

        directory = Path(self._tmp.name) / "prod_progress"
        directory.mkdir()
        messages: list[str] = []
        analyze(directory, self.source, AnalyzeParams(), progress=messages.append)
        # 工程の節目が順に知らされる(無言で数分待たせない: U1)
        self.assertGreaterEqual(len(messages), 4)
        self.assertIn("確認中", messages[0])
        self.assertTrue(any("測定中" in m for m in messages))

    def test_target_seconds_trims_to_budget(self):
        from videoyard.analyze import AnalyzeParams, analyze

        directory = Path(self._tmp.name) / "prod_target"
        directory.mkdir()
        plan = analyze(directory, self.source,
                       AnalyzeParams(target_seconds=3.0, chunk_seconds=2.0))
        self.assertLessEqual(plan.kept_seconds, 3.0 + 2.0)
        self.assertGreater(plan.kept_seconds, 0.0)

    def test_cut_rejects_changed_source(self):
        from videoyard.analyze import AnalyzeParams, analyze

        directory = Path(self._tmp.name) / "prod2"
        directory.mkdir()
        changed = Path(self._tmp.name) / "changed.mp4"
        shutil.copyfile(self.source, changed)
        analyze(directory, changed, AnalyzeParams())
        changed.write_bytes(changed.read_bytes() + b"x")  # 中身を変える
        with self.assertRaises(CutError):
            cut(directory)
        self.assertFalse((directory / "out" / "video.mp4").exists())


if __name__ == "__main__":
    unittest.main()
