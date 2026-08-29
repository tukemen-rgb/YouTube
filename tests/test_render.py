"""レンダリング — コマンド組み立ての決定性と、実 ffmpeg での再現性。

ffmpeg が無い環境では実行系のテストだけ自動スキップし、コマンド組み立て
(純粋関数)のテストは常に走る。
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from videoyard.render import (
    RenderError,
    _escape_filter_value,
    build_command,
    check_vertical_fit,
    render,
    wrap_text,
    write_text_files,
)
from videoyard.timeline import Scene, Timeline

_HAS_FFMPEG = shutil.which("ffmpeg") is not None


def _timeline() -> Timeline:
    # font_size は 320px 幅に収まる大きさにする(はみ出すと折り返しが
    # 入り、「書いた通りのファイルになる」系のテストが成立しない)。
    return Timeline(
        scenes=(
            Scene(text="表題", duration_seconds=1.0, font_size=20),
            Scene(
                text="two: lines\n'quoted'",
                duration_seconds=1.5,
                background="#1d3557",
                font_size=20,
            ),
        ),
        width=320,
        height=240,
        fps=10,
    )


class CommandBuilding(unittest.TestCase):
    def test_deterministic(self):
        timeline = _timeline()
        font = Path("/tmp/font.ttf")
        texts = [Path("/tmp/a.txt"), Path("/tmp/b.txt")]
        out = Path("/tmp/out.mp4")
        self.assertEqual(
            build_command(timeline, font, texts, out),
            build_command(timeline, font, texts, out),
        )

    def test_scene_count_mismatch_rejected(self):
        with self.assertRaises(RenderError):
            build_command(_timeline(), Path("/f.ttf"), [Path("/only-one.txt")], Path("/o.mp4"))

    def test_filter_escaping(self):
        # Windows 風のパスに含まれる : や \ がフィルタ文法に混ざらない
        self.assertEqual(
            _escape_filter_value(r"C:\Fonts\a.ttc"),
            r"C\:\\Fonts\\a.ttc",
        )

    def test_bitexact_and_single_thread(self):
        args = build_command(_timeline(), Path("/f.ttf"), [Path("/a"), Path("/b")], Path("/o.mp4"))
        self.assertIn("+bitexact", args)
        self.assertIn("-threads", args)

    def test_text_goes_through_files_not_filter(self):
        # 本文の記号がフィルタ文字列に現れないこと(textfile= 経由の確認)
        args = build_command(_timeline(), Path("/f.ttf"), [Path("/a"), Path("/b")], Path("/o.mp4"))
        filter_arg = args[args.index("-filter_complex") + 1]
        self.assertNotIn("quoted", filter_arg)
        self.assertIn("textfile=", filter_arg)


class Wrapping(unittest.TestCase):
    def test_long_japanese_wraps_within_width(self):
        # 意地悪テストで実測したバグの再発防止: 長文が画面幅を超えない
        text = "あ" * 100
        wrapped = wrap_text(text, font_size=56, frame_width=640)
        usable_chars = int((640 * 0.9) / 56)  # 全角換算で 1 行に入る文字数
        for line in wrapped.split("\n"):
            self.assertLessEqual(len(line), usable_chars)
        self.assertEqual(wrapped.replace("\n", ""), text)  # 文字は失わない

    def test_existing_newlines_kept(self):
        self.assertEqual(wrap_text("a\nb", 56, 1280), "a\nb")

    def test_half_width_counts_narrower(self):
        # 半角 100 文字は全角 100 文字より少ない行数で収まる
        full = wrap_text("あ" * 100, 56, 640).count("\n")
        half = wrap_text("a" * 100, 56, 640).count("\n")
        self.assertLess(half, full)

    def test_vertical_overflow_rejected(self):
        too_many_lines = "\n".join("x" for _ in range(20))
        with self.assertRaises(RenderError):
            check_vertical_fit(too_many_lines, font_size=56, frame_height=360)


class ExpansionSafety(unittest.TestCase):
    def test_drawtext_expansion_disabled(self):
        # 本文中の %{...} が drawtext への命令として展開されない設定
        # (実測: 展開を許すと %{eval:...} 入りの本文で文字が全部消えた)
        args = build_command(_timeline(), Path("/f.ttf"), [Path("/a"), Path("/b")], Path("/o.mp4"))
        filter_arg = args[args.index("-filter_complex") + 1]
        self.assertIn("expansion=none", filter_arg)

    def test_empty_text_scene_uses_no_drawtext(self):
        timeline = Timeline(
            scenes=(Scene(text="", duration_seconds=1.0),), width=320, height=240, fps=10
        )
        args = build_command(timeline, Path("/f.ttf"), [Path("/a")], Path("/o.mp4"))
        filter_arg = args[args.index("-filter_complex") + 1]
        self.assertNotIn("drawtext", filter_arg)


class TextFiles(unittest.TestCase):
    def test_one_file_per_scene_with_exact_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_text_files(_timeline(), Path(tmp))
            self.assertEqual(len(paths), 2)
            self.assertEqual(paths[1].read_text(encoding="utf-8"), "two: lines\n'quoted'")


@unittest.skipUnless(_HAS_FFMPEG, "ffmpeg が無い環境ではスキップ")
class RealRendering(unittest.TestCase):
    def test_render_twice_same_bytes(self):
        # 決定性の実測: 同じ timeline.json から 2 回レンダリングして
        # バイト単位で一致すること。
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "prod"
            directory.mkdir()
            _timeline().save(directory / "timeline.json")
            first = render(directory)
            second = render(directory)
            self.assertEqual(first["output_sha256"], second["output_sha256"])
            self.assertTrue((directory / "out" / "video.mp4").is_file())
            self.assertTrue((directory / "out" / "render_manifest.json").is_file())

    def test_percent_text_is_drawn_literally(self):
        # %{frame_num} 入りの本文でも文字が描かれること(描かれていれば
        # 空文字だけの動画とはバイト列が変わる)。
        with tempfile.TemporaryDirectory() as tmp:
            def _make(text: str, name: str) -> str:
                directory = Path(tmp) / name
                directory.mkdir()
                Timeline(
                    scenes=(Scene(text=text, duration_seconds=1.0, font_size=20),),
                    width=320, height=240, fps=10,
                ).save(directory / "timeline.json")
                return str(render(directory)["output_sha256"])

            with_text = _make("frame=%{frame_num} eval=%{eval:1+1}", "a")
            without_text = _make("", "b")
            self.assertNotEqual(with_text, without_text)

    def test_failure_leaves_no_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "prod"
            directory.mkdir()
            # timeline.json が無い → 失敗し、out/video.mp4 は現れない
            with self.assertRaises(Exception):
                render(directory)
            self.assertFalse((directory / "out" / "video.mp4").exists())


if __name__ == "__main__":
    unittest.main()
