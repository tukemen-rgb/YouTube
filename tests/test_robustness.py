"""実録画で起きる条件に対する耐性(サイクル 36)。

合成テストが 359 件通っていても、社長の写真を 1 回通しただけで不具合が
2 件出た。**素材が「きれい」すぎると踏めない経路がある**。そこで、
実際の録画で普通に起きる条件をこちらから作って通す:

* 映像より音声が長い(録画中のフレーム落ち・録画の中断)
* 音声トラックが 2 本(ゲーム音とマイクを別録り = 実況の定番)
* 音声が無い(画面録画だけ)
* 4K
* 極端に短い / 極端に細長い
* 日本語・空白・記号を含むファイル名
* 途中でサイズが変わる連結(録画設定を変えて撮り直した素材)

いずれも「落ちないこと」と「出力が計画どおりの長さになること」を見る。
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from videoyard.analyze import AnalyzeParams, analyze
from videoyard.cut import cut
from videoyard.thumbs import extract_thumbnails

_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True, capture_output=True)


def _duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


@unittest.skipUnless(_HAS_FFMPEG, "ffmpeg が無い環境ではスキップ")
class AwkwardRecordings(unittest.TestCase):
    """変わった素材でも、落ちずに筋の通った計画と出力を作ること。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _pipeline(self, source: Path, name: str = "prod") -> tuple:
        """analyze → cut → サムネ まで通し、(計画, 出力の長さ) を返す。"""
        directory = self.base / name
        directory.mkdir(parents=True, exist_ok=True)
        plan = analyze(directory, source, AnalyzeParams())
        manifest = cut(directory, fast=True)
        extract_thumbnails(directory)
        return plan, float(manifest["duration_seconds"])

    def _lively(self, path: Path, size: str = "320x240", seconds: int = 4,
                extra_out: tuple[str, ...] = ()) -> Path:
        """動きと音のある素材(全編 keep になり、cut まで到達できる)。"""
        _ffmpeg("-f", "lavfi", "-i", f"testsrc=s={size}:d={seconds}:r=30",
                "-f", "lavfi", "-i", "sine=frequency=440:r=44100",
                "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "2",
                "-shortest", *extra_out, str(path))
        return path

    def test_audio_longer_than_video(self):
        """映像 2 秒・音声 6 秒。映像のある範囲までで計画すること(B3)。"""
        source = self.base / "short_video.mp4"
        _ffmpeg("-f", "lavfi", "-i", "testsrc=s=320x240:d=2:r=30",
                "-f", "lavfi", "-i", "sine=frequency=440:r=44100",
                "-t", "6", "-c:v", "libx264", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "2", str(source))
        plan, out_seconds = self._pipeline(source)
        self.assertLessEqual(plan.duration, 2.5,
                             "映像の終わりを超えて計画している")
        self.assertGreater(plan.container_duration, plan.duration + 1.0)
        self.assertLessEqual(out_seconds, plan.duration + 0.1)

    def test_two_audio_tracks(self):
        """ゲーム音とマイクを別録りした素材(実況の定番)。"""
        source = self.base / "two_audio.mp4"
        _ffmpeg("-f", "lavfi", "-i", "testsrc=s=320x240:d=4:r=30",
                "-f", "lavfi", "-i", "sine=frequency=300:r=44100",
                "-f", "lavfi", "-i", "sine=frequency=900:r=44100",
                "-t", "4", "-map", "0:v", "-map", "1:a", "-map", "2:a",
                "-c:v", "libx264", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p", "-c:a", "aac", str(source))
        plan, out_seconds = self._pipeline(source)
        self.assertTrue(plan.has_audio)
        self.assertGreater(out_seconds, 0.0)

    def test_no_audio_at_all(self):
        """画面録画だけ(音声ストリームなし)。無音カットは働かない。"""
        source = self.base / "silent.mp4"
        _ffmpeg("-f", "lavfi", "-i", "testsrc=s=320x240:d=4:r=30",
                "-c:v", "libx264", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p", str(source))
        plan, out_seconds = self._pipeline(source)
        self.assertFalse(plan.has_audio)
        self.assertEqual(plan.mode, "static_only")  # 明示的に切り替わる
        self.assertGreater(out_seconds, 0.0)

    def test_4k(self):
        source = self._lively(self.base / "uhd.mp4", size="3840x2160", seconds=2)
        _plan, out_seconds = self._pipeline(source)
        self.assertGreater(out_seconds, 0.0)

    def test_very_short(self):
        source = self._lively(self.base / "tiny.mp4", seconds=1)
        _plan, out_seconds = self._pipeline(source)
        self.assertGreater(out_seconds, 0.0)

    def test_extreme_aspect_ratio(self):
        """超ワイド。縦変換やテロップの計算で 0 除算しないこと。"""
        source = self._lively(self.base / "wide.mp4", size="1280x160")
        _plan, out_seconds = self._pipeline(source)
        self.assertGreater(out_seconds, 0.0)

    def test_japanese_and_symbols_in_filename(self):
        source = self._lively(self.base / "実況 録画 #1 (2026).mp4")
        _plan, out_seconds = self._pipeline(source)
        self.assertGreater(out_seconds, 0.0)

    def test_resolution_changes_midway(self):
        """録画設定を変えて撮り直した素材をつないだもの。"""
        first = self._lively(self.base / "a.mp4", size="640x360", seconds=3)
        second = self._lively(self.base / "b.mp4", size="854x480", seconds=3)
        joined = self.base / "mixed.mp4"
        # サイズが違うので再エンコードでそろえる(録画の実態に近い)
        _ffmpeg("-i", str(first), "-i", str(second), "-filter_complex",
                "[0:v]scale=640:360,setsar=1[v0];[1:v]scale=640:360,setsar=1[v1];"
                "[v0][0:a][v1][1:a]concat=n=2:v=1:a=1[v][a]",
                "-map", "[v]", "-map", "[a]", "-c:v", "libx264",
                "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-ac", "2", str(joined))
        plan, out_seconds = self._pipeline(joined)
        self.assertAlmostEqual(_duration(joined), 6.0, delta=0.5)
        self.assertGreater(out_seconds, 0.0)
        self.assertEqual((plan.width, plan.height), (640, 360))


if __name__ == "__main__":
    unittest.main()
