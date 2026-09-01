"""サムネイル候補 — 選定の純粋ロジックと実抽出。"""

import shutil
import unittest

from videoyard.thumbs import pick_thumbnail_times

_HAS_FFMPEG = shutil.which("ffmpeg") is not None


class Picking(unittest.TestCase):
    def test_top_scores_within_keeps_only(self):
        scores = [10.0, 90.0, 20.0, 95.0, 30.0, 80.0]  # 窓 1.0s、時刻 0.5〜5.5
        keeps = [(0.0, 3.0)]  # 3 秒以降は切られている
        picks = pick_thumbnail_times(scores, keeps, window=1.0, count=3, min_gap=1.0)
        times = [t for t, _ in picks]
        self.assertIn(1.5, times)          # keep 内の最高点(90)
        self.assertNotIn(3.5, times)       # 95 点でも cut 区間からは選ばない

    def test_min_gap_prevents_near_duplicates(self):
        scores = [90.0, 89.0, 10.0, 70.0]
        keeps = [(0.0, 4.0)]
        picks = pick_thumbnail_times(scores, keeps, window=1.0, count=3, min_gap=2.0)
        times = sorted(t for t, _ in picks)
        for a, b in zip(times, times[1:], strict=False):
            self.assertGreaterEqual(b - a, 2.0)

    def test_ranked_by_score(self):
        scores = [50.0, 90.0, 10.0, 70.0]
        keeps = [(0.0, 4.0)]
        picks = pick_thumbnail_times(scores, keeps, window=1.0, count=2, min_gap=1.0)
        self.assertEqual([s for _, s in picks], sorted((s for _, s in picks), reverse=True))

    def test_empty_when_no_keeps(self):
        self.assertEqual(pick_thumbnail_times([50.0], [], window=1.0), [])


class TitleStyle(unittest.TestCase):
    def test_drawtext_is_safe_and_bold(self):
        from pathlib import Path

        from videoyard.thumbs import title_drawtext
        f = title_drawtext(Path("/t.txt"), Path("/f.ttc"), 1280, 720)
        self.assertIn("expansion=none", f)   # 文字は命令にならない
        self.assertIn("textfile=", f)
        self.assertIn("borderw=", f)         # 縁取り(C12)
        self.assertIn("boxcolor=0x000000@0.35", f)  # 半透明の帯
        self.assertIn(f"fontsize={int(720 * 0.16)}", f)  # 大きな文字


@unittest.skipUnless(_HAS_FFMPEG, "ffmpeg が無い環境ではスキップ")
class RealExtraction(unittest.TestCase):
    def test_thumbnails_written_with_provenance(self):
        import json
        import subprocess
        import tempfile
        from pathlib import Path

        from videoyard.analyze import AnalyzeParams, analyze
        from videoyard.thumbs import extract_thumbnails

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "src.mp4"
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-f", "lavfi", "-i", "testsrc=s=320x240:d=6:r=10",
                 "-f", "lavfi", "-i", "sine=frequency=440:r=44100",
                 "-t", "6", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-ac", "2", "-shortest", str(source)],
                check=True, capture_output=True,
            )
            directory = base / "prod"
            directory.mkdir()
            analyze(directory, source, AnalyzeParams())
            written = extract_thumbnails(directory, count=2)
            self.assertTrue(written)
            for path in written:
                self.assertTrue(path.is_file())
                self.assertGreater(path.stat().st_size, 0)
            meta = json.loads(
                (directory / "out" / "thumbnails" / "thumbnails.json").read_text()
            )
            self.assertEqual(len(meta["candidates"]), len(written))
            self.assertIn("time_seconds", meta["candidates"][0])

            # 文字入り版(C5): --text 相当で titled ファイルが増える
            from videoyard.thumbs import ThumbsError
            titled = extract_thumbnails(directory, count=1, text="神プレイ集")
            names = [p.name for p in titled]
            self.assertIn("thumb_1_titled.png", names)
            with self.assertRaises(ThumbsError):
                extract_thumbnails(directory, count=1, text="あ" * 21)  # 文字数超過


if __name__ == "__main__":
    unittest.main()
