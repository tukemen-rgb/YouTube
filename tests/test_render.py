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
    render,
    write_text_files,
)
from videoyard.timeline import Scene, Timeline

_HAS_FFMPEG = shutil.which("ffmpeg") is not None


def _timeline() -> Timeline:
    return Timeline(
        scenes=(
            Scene(text="表題", duration_seconds=1.0),
            Scene(text="two: lines\n'quoted'", duration_seconds=1.5, background="#1d3557"),
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
