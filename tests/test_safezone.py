"""セーフゾーン(S5)— スマホ UI に隠れる場所へ文字を置かないこと。

実測(2026-09-08)で photo --vertical のテロップが下端 300px と右端 120px の
両方に掛かっていた。同じことが二度起きないよう、幾何の計算と、実際に
書き出したフレームの画素の両方で縛る。
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from videoyard.safezone import (
    BOTTOM_RESERVED,
    REFERENCE_HEIGHT,
    REFERENCE_WIDTH,
    RIGHT_RESERVED,
    bottom_text_margin,
    is_vertical,
    letterbox_top,
    reserved_bottom,
    safe_box,
    source_bottom_margin,
    source_wrap_width,
    text_wrap_width,
)

_HAS_FFMPEG = shutil.which("ffmpeg") is not None


class Geometry(unittest.TestCase):
    def test_vertical_detection(self):
        self.assertTrue(is_vertical(1080, 1920))
        self.assertFalse(is_vertical(1920, 1080))
        self.assertFalse(is_vertical(1000, 1000))  # 正方形は縦扱いしない

    def test_safe_box_for_shorts(self):
        box = safe_box(REFERENCE_WIDTH, REFERENCE_HEIGHT)
        self.assertEqual(box.y, 120)
        self.assertEqual(box.bottom, REFERENCE_HEIGHT - BOTTOM_RESERVED)
        self.assertEqual(box.right, REFERENCE_WIDTH - RIGHT_RESERVED)
        # 3 アプリ共通の目安(中央 900×1160)をすべて含むこと
        self.assertLessEqual(box.x, (REFERENCE_WIDTH - 900) // 2)
        self.assertGreaterEqual(box.width, 900)

    def test_horizontal_is_not_restricted(self):
        box = safe_box(1920, 1080)
        self.assertEqual((box.x, box.y, box.width, box.height), (0, 0, 1920, 1080))
        self.assertEqual(bottom_text_margin(1920, 1080), 20)

    def test_reserved_scales_with_size(self):
        # 半分の大きさなら空ける画素も半分
        self.assertEqual(reserved_bottom(1920), 300)
        self.assertEqual(reserved_bottom(960), 150)

    def test_bottom_margin_for_vertical(self):
        self.assertEqual(bottom_text_margin(1080, 1920), 300)
        # 呼び出し側の最低値が大きくてもセーフゾーンが勝つ
        self.assertEqual(bottom_text_margin(1080, 1920, minimum=40), 300)

    def test_wrap_width_avoids_button_column(self):
        self.assertEqual(text_wrap_width(1080, 1920), 1080 - 120 - 40)
        self.assertEqual(text_wrap_width(1920, 1080), 1920)  # 横は制限しない


class LetterboxedVertical(unittest.TestCase):
    """cut --vertical はテロップを変換前の絵に描く。逆算が正しいこと。"""

    def test_16x9_source_needs_no_extra_margin(self):
        # 上下に帯が付くので、元の絵の下端でも出力では安全な位置に来る
        self.assertEqual(source_bottom_margin(1920, 1080, minimum=20), 20)

    def test_already_vertical_source_needs_the_full_margin(self):
        # 縦の素材は画面いっぱいに広がるので、300px 分を確保しないと隠れる
        self.assertEqual(source_bottom_margin(1080, 1920, minimum=20), 300)

    def test_square_source_still_fits_in_the_letterbox(self):
        # 1080×1080 は帯付きで下端 1500px に収まる(1620px より上)
        self.assertEqual(source_bottom_margin(1080, 1080, minimum=20), 20)

    def test_tall_but_not_full_height_source_gets_a_partial_margin(self):
        # 1080×1600 は帯が細く、下端が 1760px まで来るので一部だけ上げる
        margin = source_bottom_margin(1080, 1600, minimum=20)
        self.assertGreater(margin, 20)
        self.assertLess(margin, 300)

    def test_computed_margin_lands_inside_the_safe_box(self):
        box = safe_box(REFERENCE_WIDTH, REFERENCE_HEIGHT)
        for width, height in ((1920, 1080), (1080, 1920), (1080, 1080),
                              (1280, 720), (640, 480)):
            margin = source_bottom_margin(width, height, minimum=20)
            scale = REFERENCE_WIDTH / width
            top = letterbox_top(width, height)
            text_bottom = top + (height - margin) * scale
            self.assertLessEqual(
                round(text_bottom), box.bottom,
                f"{width}x{height} のテロップが下端の UI 領域に入る")

    def test_source_wrap_width_keeps_text_out_of_the_button_column(self):
        box = safe_box(REFERENCE_WIDTH, REFERENCE_HEIGHT)
        for width in (1920, 1080, 1280, 640):
            wrap = source_wrap_width(width)
            scale = REFERENCE_WIDTH / width
            self.assertLessEqual(round(wrap * scale), box.width,
                                 f"幅 {width} の折り返しが広すぎる")


@unittest.skipUnless(_HAS_FFMPEG, "ffmpeg が無い環境ではスキップ")
class RenderedPixels(unittest.TestCase):
    """実際に書き出したフレームの、隠れる帯に明るい画素が無いこと。"""

    def _brightest(self, frame: Path, crop: str) -> int:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-y", "-i", str(frame),
             "-vf", f"crop={crop},signalstats,"
                    "metadata=print:key=lavfi.signalstats.YMAX",
             "-f", "null", "-"],
            capture_output=True, text=True, check=True)
        for line in result.stderr.splitlines():
            if "YMAX=" in line:
                return int(float(line.split("YMAX=")[1]))
        self.fail("YMAX が読めない")

    def test_vertical_photo_telop_avoids_phone_ui(self):
        from videoyard.photo import PhotoPlan, PhotoScene, render_slideshow
        from videoyard.render import _sha256

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            photo = base / "black.png"
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-f", "lavfi", "-i", "color=c=black:s=1080x1920:d=0.1",
                 "-frames:v", "1", str(photo)], check=True, capture_output=True)
            directory = base / "prod"
            directory.mkdir()
            PhotoPlan(
                scenes=(PhotoScene(file=str(photo), sha256=_sha256(photo),
                                   seconds=1.0, telop="隠れないこと"),),
                width=1080, height=1920,
            ).save(directory / "photoplan.json")
            render_slideshow(directory, fast=True)

            frame = base / "frame.png"
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-ss", "0.4", "-i", str(directory / "out" / "video.mp4"),
                 "-frames:v", "1", str(frame)], check=True, capture_output=True)

            # 黒い写真+白いテロップなので、明るい画素があれば文字がある
            self.assertGreater(self._brightest(frame, "1080:1920:0:0"), 100,
                               "そもそもテロップが描かれていない")
            self.assertLess(
                self._brightest(frame, "1080:300:0:1620"), 60,
                "下端 300px(キャプション欄)にテロップが入っている")
            self.assertLess(
                self._brightest(frame, "120:1920:960:0"), 60,
                "右端 120px(ボタン列)にテロップが入っている")


if __name__ == "__main__":
    unittest.main()
