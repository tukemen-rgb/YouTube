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

    def test_loudness_normalized_by_default(self):
        # YouTube の実効基準(約 -14 LUFS)へ書き出しで揃える(C6)
        args = build_command(_plan(), Path("/s.mp4"), Path("/f.ttf"), {}, Path("/o.mp4"))
        filter_arg = args[args.index("-filter_complex") + 1]
        self.assertIn("loudnorm=I=-14.0", filter_arg)
        self.assertIn("[outn]", args)  # 正規化後の音声を書き出す

    def test_loudnorm_can_be_disabled(self):
        args = build_command(_plan(), Path("/s.mp4"), Path("/f.ttf"), {}, Path("/o.mp4"),
                             normalize_loudness=False)
        self.assertNotIn("loudnorm", args[args.index("-filter_complex") + 1])

    def test_vertical_uses_blur_background(self):
        # ショート化は中央クロップではなく「ぼかし背景+中央配置」(C8)
        args = build_command(_plan(), Path("/s.mp4"), Path("/f.ttf"), {}, Path("/o.mp4"),
                             vertical=True)
        filter_arg = args[args.index("-filter_complex") + 1]
        self.assertIn("boxblur", filter_arg)
        self.assertIn("1080:1920", filter_arg)
        self.assertIn("overlay=(W-w)/2:(H-h)/2", filter_arg)
        self.assertIn("[vout]", args)

    def test_default_output_is_not_vertical(self):
        args = build_command(_plan(), Path("/s.mp4"), Path("/f.ttf"), {}, Path("/o.mp4"))
        self.assertNotIn("boxblur", args[args.index("-filter_complex") + 1])
        self.assertIn("[outv]", args)

    def test_fast_mode_uses_all_cores(self):
        # 速さ優先(C4/U5): 全コア+高速プリセット。既定は決定的なまま
        args = build_command(_plan(), Path("/s.mp4"), Path("/f.ttf"), {}, Path("/o.mp4"),
                             fast=True)
        self.assertIn("veryfast", args)
        self.assertEqual(args[args.index("-threads") + 1], "0")
        default = build_command(_plan(), Path("/s.mp4"), Path("/f.ttf"), {}, Path("/o.mp4"))
        self.assertIn("medium", default)
        self.assertEqual(default[default.index("-threads") + 1], "1")

    def test_bgm_mixed_under_game_audio_then_normalized(self):
        # BGM はゲーム音の下に控えめに敷き(C10)、ミックス後に正規化する
        args = build_command(_plan(), Path("/s.mp4"), Path("/f.ttf"), {}, Path("/o.mp4"),
                             bgm=Path("/music.mp3"))
        self.assertIn("-stream_loop", args)  # 短い BGM はループ
        filter_arg = args[args.index("-filter_complex") + 1]
        self.assertIn("volume=-16.0dB", filter_arg)
        self.assertIn("amix=inputs=2:duration=first:normalize=0", filter_arg)
        self.assertLess(filter_arg.index("amix"), filter_arg.index("loudnorm"))
        self.assertIn("afade=t=out", filter_arg)  # 末尾フェードアウト

    def test_bgm_becomes_sole_audio_for_silent_video(self):
        plan = _plan(has_audio=False)
        args = build_command(plan, Path("/s.mp4"), Path("/f.ttf"), {}, Path("/o.mp4"),
                             bgm=Path("/music.mp3"))
        filter_arg = args[args.index("-filter_complex") + 1]
        self.assertNotIn("amix", filter_arg)   # 混ぜる相手がいない
        self.assertIn("-c:a", args)            # BGM が唯一の音声になる

    def test_dip_transition_fades_only_at_joins(self):
        # 暗転つなぎ(C3): つなぎ目だけフェード。冒頭と末尾は演出しない
        args = build_command(_plan(), Path("/s.mp4"), Path("/f.ttf"), {}, Path("/o.mp4"),
                             transition="dip")
        filter_arg = args[args.index("-filter_complex") + 1]
        chains = [c for c in filter_arg.split(";") if c.startswith("[0:v]")]
        self.assertEqual(len(chains), 2)  # keep 2 区間
        self.assertNotIn("fade=t=in", chains[0])   # 最初の区間は頭フェードなし
        self.assertIn("fade=t=out", chains[0])     # つなぎ目へ向けて暗転
        self.assertIn("fade=t=in", chains[1])      # つなぎ目から明転
        self.assertNotIn("fade=t=out", chains[1])  # 最後の区間は尻フェードなし

    def test_default_transition_is_hard_cut(self):
        args = build_command(_plan(), Path("/s.mp4"), Path("/f.ttf"), {}, Path("/o.mp4"))
        filter_arg = args[args.index("-filter_complex") + 1]
        video_chains = [c for c in filter_arg.split(";") if c.startswith("[0:v]")]
        self.assertTrue(all("fade" not in c for c in video_chains))

    def test_unknown_transition_rejected(self):
        with self.assertRaises(CutError):
            build_command(_plan(), Path("/s.mp4"), Path("/f.ttf"), {}, Path("/o.mp4"),
                          transition="wipe")

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

    def test_vertical_cut_outputs_1080x1920(self):
        from videoyard.analyze import AnalyzeParams, analyze

        directory = Path(self._tmp.name) / "prod_vertical"
        directory.mkdir()
        analyze(directory, self.source, AnalyzeParams())
        cut(directory, vertical=True)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0",
             str(directory / "out" / "video.mp4")],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(probe.stdout.strip(), "1080,1920")

    def test_cut_with_bgm_produces_audio(self):
        from videoyard.analyze import AnalyzeParams, analyze

        base = Path(self._tmp.name)
        bgm = base / "bgm.wav"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "sine=frequency=220:r=44100",
             "-t", "2", "-ac", "2", str(bgm)],
            check=True, capture_output=True,
        )
        directory = base / "prod_bgm"
        directory.mkdir()
        analyze(directory, self.source, AnalyzeParams())
        manifest = cut(directory, bgm=bgm)
        self.assertEqual(len(manifest["bgm_sha256"]), 64)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0",
             str(directory / "out" / "video.mp4")],
            capture_output=True, text=True, check=True,
        )
        self.assertIn("audio", probe.stdout)

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
