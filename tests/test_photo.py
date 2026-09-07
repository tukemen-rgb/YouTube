"""写真スライドショー(U18)— 計画の検証・コマンド組み立て・実レンダリング。"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from videoyard.photo import (
    PhotoError,
    PhotoPlan,
    PhotoScene,
    build_slideshow_command,
    collect_photos,
    scene_with_telop,
)

_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _scene(**overrides) -> PhotoScene:
    fields = dict(file="a.jpg", sha256="0" * 64, seconds=4.0)
    fields.update(overrides)
    return PhotoScene(**fields)


def _plan(**overrides) -> PhotoPlan:
    fields = dict(scenes=(_scene(), _scene(file="b.jpg")))
    fields.update(overrides)
    return PhotoPlan(**fields)


class PlanValidation(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(_plan().total_seconds, 8.0)

    def test_rejects_bad_seconds(self):
        with self.assertRaises(PhotoError):
            _scene(seconds=0.1)
        with self.assertRaises(PhotoError):
            _scene(seconds=120.0)

    def test_rejects_unknown_keys(self):
        with self.assertRaises(PhotoError):
            PhotoPlan.from_dict({"format_version": 1, "scenes": [], "gps": True})

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "photoplan.json"
            plan = _plan()
            plan.save(path)
            self.assertEqual(PhotoPlan.load(path), plan)

    def test_telop_replacement(self):
        plan = scene_with_telop(_plan(), 1, "ごはん")
        self.assertEqual(plan.scenes[1].telop, "ごはん")
        self.assertEqual(plan.scenes[0].telop, "")


class Collecting(unittest.TestCase):
    def test_sorted_and_filtered(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for name in ("b.jpg", "a.png", "c.txt", "d.JPG"):
                (base / name).write_bytes(b"x")
            photos = [p.name for p in collect_photos(base)]
            self.assertEqual(photos, ["a.png", "b.jpg", "d.JPG"])

    def test_empty_dir_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(PhotoError):
            collect_photos(Path(tmp))


class CommandBuilding(unittest.TestCase):
    def _args(self, plan, telops=None):
        return build_slideshow_command(
            plan, Path("/prod"), Path("/f.ttf"), telops or {}, Path("/o.mp4"))

    def test_deterministic(self):
        self.assertEqual(self._args(_plan()), self._args(_plan()))

    def test_zoom_alternates_and_concat(self):
        filter_arg = self._args(_plan())[
            self._args(_plan()).index("-filter_complex") + 1]
        self.assertIn("zoompan=z='1+0.080*on/", filter_arg)      # 1 枚目: ズームイン
        self.assertIn("zoompan=z='1.080-0.080*on/", filter_arg)  # 2 枚目: ズームアウト
        self.assertIn("concat=n=2:v=1:a=0[outv]", filter_arg)
        self.assertIn("boxblur", filter_arg)  # ぼかし背景

    def test_exif_stripped(self):
        # 位置情報などのメタデータを出力に残さない
        args = self._args(_plan())
        self.assertIn("-map_metadata", args)
        self.assertEqual(args[args.index("-map_metadata") + 1], "-1")

    def test_telop_uses_safe_path(self):
        args = build_slideshow_command(
            _plan(), Path("/prod"), Path("/f.ttf"),
            {0: Path("/prod/out/text/t0.txt")}, Path("/o.mp4"))
        filter_arg = args[args.index("-filter_complex") + 1]
        self.assertIn("textfile=", filter_arg)
        self.assertIn("expansion=none", filter_arg)

    def test_vertical_canvas(self):
        plan = _plan(width=1080, height=1920)
        filter_arg = self._args(plan)[self._args(plan).index("-filter_complex") + 1]
        self.assertIn("s=1080x1920", filter_arg)

    def test_bgm_added_with_fadeout(self):
        args = build_slideshow_command(
            _plan(), Path("/prod"), Path("/f.ttf"), {}, Path("/o.mp4"),
            bgm=Path("/m.mp3"))
        self.assertIn("-stream_loop", args)
        filter_arg = args[args.index("-filter_complex") + 1]
        self.assertIn("volume=-16.0dB", filter_arg)
        self.assertIn("afade=t=out", filter_arg)


@unittest.skipUnless(_HAS_FFMPEG, "ffmpeg が無い環境ではスキップ")
class RealSlideshow(unittest.TestCase):
    def test_photos_to_video(self):
        import contextlib
        import io

        from videoyard.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            photos = base / "photos"
            photos.mkdir()
            for i, color in enumerate(("red", "green", "blue")):
                subprocess.run(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                     "-f", "lavfi", "-i", f"color=c={color}:s=640x480:d=0.1",
                     "-frames:v", "1", str(photos / f"p{i}.png")],
                    check=True, capture_output=True)
            directory = base / "prod"
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(["photo", str(directory), "--photos", str(photos),
                             "--seconds", "1.0", "--fast"])
            self.assertEqual(code, 0)
            self.assertTrue((directory / "photoplan.json").is_file())
            out = directory / "out" / "video.mp4"
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries",
                 "format=duration", "-of", "csv=p=0", str(out)],
                capture_output=True, text=True, check=True)
            self.assertAlmostEqual(float(probe.stdout.strip()), 3.0, delta=0.3)
            # 計画を書き換えた再実行は同じ計画から作り直せる(2 回目は
            # photoplan を作り直さない)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["photo", str(directory), "--fast"]), 0)


if __name__ == "__main__":
    unittest.main()
