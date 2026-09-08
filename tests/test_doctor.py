"""環境診断(S2)— 判定と「直し方」の提示。ffmpeg 無しでも全部走る。"""

import tempfile
import unittest
from pathlib import Path

from videoyard.doctor import (
    REQUIRED_FILTERS,
    Check,
    check_data_dir,
    check_disk,
    check_filters,
    check_font,
    check_python,
    environment_summary,
    format_report,
    parse_filters,
)
from videoyard.fonts import FontError

#: 実際の `ffmpeg -filters` の出力を模したもの。
_FILTERS_OUTPUT = """\
Filters:
  T.. = Timeline support
  ... = Slice threading
 --- = Other
 ... abench            A->A       Benchmark part of a filtergraph.
 T.. atempo            A->A       Adjust audio tempo.
 ... silencedetect     A->A       Detect silence.
 ... loudnorm          A->A       EBU R128 loudness normalization
 TSC boxblur           V->V       Blur the input.
 T.C drawtext          V->V       Draw text on top of video frames
 ... freezedetect      V->V       Detects frozen video input.
 ..C zoompan           V->V       Apply Zoom & Pan effect.
"""


class Parsing(unittest.TestCase):
    def test_filter_names_extracted(self):
        names = parse_filters(_FILTERS_OUTPUT)
        self.assertIn("drawtext", names)
        self.assertIn("zoompan", names)
        self.assertIn("atempo", names)
        # 見出し行は拾わない
        self.assertNotIn("=", names)
        self.assertNotIn("Timeline", names)

    def test_empty_output(self):
        self.assertEqual(parse_filters(""), set())


class Judgements(unittest.TestCase):
    def test_python_version(self):
        self.assertEqual(check_python((3, 11)).level, "OK")
        self.assertEqual(check_python((3, 13)).level, "OK")
        old = check_python((3, 9))
        self.assertTrue(old.failed)
        self.assertIn("3.11", old.fix)

    def test_all_required_filters_present(self):
        checks = check_filters(parse_filters(_FILTERS_OUTPUT))
        self.assertEqual(len(checks), len(REQUIRED_FILTERS))
        self.assertTrue(all(c.level == "OK" for c in checks), checks)

    def test_missing_filter_is_reported_with_purpose_and_fix(self):
        available = parse_filters(_FILTERS_OUTPUT) - {"drawtext"}
        checks = check_filters(available)
        bad = next(c for c in checks if c.name == "filter:drawtext")
        self.assertTrue(bad.failed)
        self.assertIn("テロップ", bad.detail)
        self.assertTrue(bad.fix)

    def test_missing_command_has_install_hint(self):
        from videoyard.doctor import check_command
        check = check_command("ffmpeg", which=lambda _n: None)
        self.assertTrue(check.failed)
        self.assertTrue(check.fix)

    def test_found_command_reports_path(self):
        from videoyard.doctor import check_command
        check = check_command("ffmpeg", which=lambda _n: "/usr/bin/ffmpeg")
        self.assertEqual(check.level, "OK")
        self.assertEqual(check.detail, "/usr/bin/ffmpeg")

    def test_font_missing(self):
        def boom():
            raise FontError("フォントが見つからない")
        check = check_font(resolver=boom)
        self.assertTrue(check.failed)
        self.assertTrue(check.fix)

    def test_latin_only_font_warns_about_tofu(self):
        check = check_font(
            resolver=lambda: Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
        self.assertEqual(check.level, "注意")
        self.assertIn("豆腐", check.detail)

    def test_japanese_font_ok(self):
        check = check_font(
            resolver=lambda: Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"))
        self.assertEqual(check.level, "OK")

    def test_writable_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            check = check_data_dir(Path(tmp) / "sub")
            self.assertEqual(check.level, "OK")
            # 検査に使った一時ファイルを残さない
            self.assertEqual(list((Path(tmp) / "sub").iterdir()), [])

    def test_unwritable_data_dir_fails_with_fix(self):
        # 既存ファイルを「ディレクトリ」として渡すと mkdir が失敗する
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "not_a_dir"
            blocker.write_text("x", encoding="utf-8")
            check = check_data_dir(blocker)
            self.assertTrue(check.failed)
            self.assertIn("VIDEOYARD_DATA_DIR", check.fix)

    def test_disk_check_warns_when_small(self):
        check = check_disk(Path.cwd(), need_mb=10**9)  # 現実には満たせない値
        self.assertEqual(check.level, "注意")
        self.assertTrue(check.fix)


class Report(unittest.TestCase):
    def test_all_ok_message(self):
        lines = format_report([Check("a", "OK", "fine")])
        self.assertIn("すべて揃っている", "\n".join(lines))

    def test_failure_message_includes_fix(self):
        lines = format_report([
            Check("a", "OK", "fine"),
            Check("ffmpeg", "不足", "無い", "brew install ffmpeg"),
        ])
        text = "\n".join(lines)
        self.assertIn("直し方: brew install ffmpeg", text)
        self.assertIn("1 件足りない", text)

    def test_warning_only_still_usable(self):
        lines = format_report([Check("a", "注意", "小さい", "消す")])
        self.assertIn("動かせる状態", "\n".join(lines))

    def test_environment_summary_has_version_and_no_secrets(self):
        lines = environment_summary()
        self.assertTrue(any("videoyard" in line for line in lines))
        text = "\n".join(lines)
        # 環境変数は名前と値だけ。ホームディレクトリを丸ごと晒さない
        self.assertNotIn("PATH=", text)


if __name__ == "__main__":
    unittest.main()
