"""1 パス測定(S8)— 3 回に分けたときと同じ結果が出ること、そして速いこと。

分析でいちばん重いのは動画のデコード。検出・動き・音量を別々の ffmpeg
起動で 3 回デコードしていたのを 1 回にまとめた。速くなっても答えが
変わっては意味がないので、**旧経路との一致**をテストで縛る。
"""

import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from videoyard.analyze import AnalyzeParams, detection_filters
from videoyard.excitement import (
    bucketize,
    measure_all,
    measure_loudness,
    measure_motion,
)

_HAS_FFMPEG = shutil.which("ffmpeg") is not None


def _synth(base: Path, seconds: int = 6) -> Path:
    """動きと音が途中で変わる素材。単調だと差が出ても気づけない。"""
    pieces = []
    for i, (video, audio, dur) in enumerate((
        ("color=c=blue:s=320x240:r=30", "anullsrc=r=44100:cl=stereo", seconds // 2),
        ("testsrc=s=320x240:r=30", "sine=frequency=440:r=44100", seconds // 2),
    )):
        piece = base / f"p{i}.mp4"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", f"{video}:d={dur}",
             "-f", "lavfi", "-i", audio, "-t", str(dur),
             "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-ac", "2", "-shortest", str(piece)],
            check=True, capture_output=True)
        pieces.append(piece)
    listfile = base / "list.txt"
    listfile.write_text("".join(f"file '{p}'\n" for p in pieces), encoding="utf-8")
    source = base / "source.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "concat", "-safe", "0", "-i", str(listfile),
         "-c", "copy", str(source)], check=True, capture_output=True)
    return source


class FilterSpec(unittest.TestCase):
    def test_detection_filters_include_thresholds(self):
        video, audio = detection_filters(AnalyzeParams(), has_audio=True)
        self.assertIn("freezedetect", video)
        self.assertIn("silencedetect", audio)
        self.assertIn("-35", audio)  # 既定の無音しきい値

    def test_no_audio_means_no_silence_filter(self):
        _video, audio = detection_filters(AnalyzeParams(), has_audio=False)
        self.assertEqual(audio, "")


@unittest.skipUnless(_HAS_FFMPEG, "ffmpeg が無い環境ではスキップ")
class SamePassSameAnswer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.source = _synth(Path(cls._tmp.name))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _measure(self):
        video, audio = detection_filters(AnalyzeParams(), has_audio=True)
        with tempfile.TemporaryDirectory() as tmp:
            return measure_all(self.source, detect_video=video,
                               detect_audio=audio, has_audio=True,
                               out_dir=Path(tmp))

    def test_loudness_matches_the_separate_pass_exactly(self):
        # 音量の測り方は変えていないので、値も完全に一致すること
        old = bucketize(measure_loudness(self.source), 6.0)
        _stderr, _motion, loudness = self._measure()
        new = bucketize(loudness, 6.0)
        self.assertEqual(len(new), len(old))
        for i, (a, b) in enumerate(zip(new, old, strict=True)):
            self.assertAlmostEqual(a, b, places=3, msg=f"音量が窓 {i} で違う")

    def test_motion_still_separates_still_from_moving(self):
        """動きは測る頻度を落としたので値は一致しない。区別は保つこと。

        前半 3 秒は静止(YDIF ほぼ 0)、後半 3 秒は動き。窓ごとの値の
        大小関係が保たれていれば、切る判断も採点も従来どおり働く。
        """
        old = bucketize(measure_motion(self.source), 6.0)
        _stderr, motion, _loudness = self._measure()
        new = bucketize(motion, 6.0)
        self.assertEqual(len(new), len(old))

        half = len(new) // 2
        for label, values in (("1 パス", new), ("従来", old)):
            still, moving = values[:half - 1], values[half + 1:]
            # 静止側はほぼ 0
            self.assertLess(max(still), 0.5, f"{label}: 静止側が 0 でない")
            # どの動き窓も、どの静止窓より大きい(順序が入れ替わらない)
            self.assertGreater(min(moving), max(still),
                               f"{label}: 静止と動きの区別がつかない")
            # かろうじて大きい、では判定に効かない。はっきり差が要る
            still_mean = sum(still) / len(still)
            moving_mean = sum(moving) / len(moving)
            self.assertGreater(moving_mean, still_mean + 0.1,
                               f"{label}: 動きの差が小さすぎる")

    def test_lower_sampling_rate_widens_the_gap(self):
        """測る頻度を落とすと、むしろ静止と動きの差は広がる。

        YDIF は隣り合うフレームの差。33ms 間隔(30fps)より 100ms 間隔
        (10fps)のほうが動く絵の差は大きく出る。静止は間隔によらず 0 の
        ままなので、区別は鈍らずむしろはっきりする。速くなった代償に
        判定が甘くなっていないことの確認。
        """
        old = bucketize(measure_motion(self.source), 6.0)
        _stderr, motion, _loudness = self._measure()
        new = bucketize(motion, 6.0)
        half = len(new) // 2
        old_gap = sum(old[half + 1:]) / len(old[half + 1:])
        new_gap = sum(new[half + 1:]) / len(new[half + 1:])
        self.assertGreater(new_gap, old_gap)

    def test_detection_events_are_reported(self):
        video, audio = detection_filters(AnalyzeParams(), has_audio=True)
        with tempfile.TemporaryDirectory() as tmp:
            stderr, _motion, _loudness = measure_all(
                self.source, detect_video=video, detect_audio=audio,
                has_audio=True, out_dir=Path(tmp))
        # 前半は静止+無音なので、両方の検出が報告されるはず
        self.assertIn("freeze_start", stderr)
        self.assertIn("silence_start", stderr)

    def test_video_without_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            silent = Path(tmp) / "silent.mp4"
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-f", "lavfi", "-i", "testsrc=s=320x240:d=2:r=30",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", str(silent)],
                check=True, capture_output=True)
            video, audio = detection_filters(AnalyzeParams(), has_audio=False)
            _stderr, motion, loudness = measure_all(
                silent, detect_video=video, detect_audio=audio,
                has_audio=False, out_dir=Path(tmp))
            self.assertTrue(motion)
            self.assertEqual(loudness, [])

    def test_single_pass_is_faster(self):
        video, audio = detection_filters(AnalyzeParams(), has_audio=True)
        started = time.monotonic()
        with tempfile.TemporaryDirectory() as tmp:
            measure_all(self.source, detect_video=video, detect_audio=audio,
                        has_audio=True, out_dir=Path(tmp))
        single = time.monotonic() - started

        started = time.monotonic()
        measure_motion(self.source)
        measure_loudness(self.source)
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostdin", "-i", str(self.source),
             "-vf", video, "-af", audio, "-f", "null", "-"],
            capture_output=True, text=True, check=True)
        separate = time.monotonic() - started

        # 起動のばらつきがあるので「遅くなっていない」ことだけを縛る
        self.assertLess(single, separate * 1.2,
                        f"1 パス {single:.2f}s / 3 パス {separate:.2f}s")


if __name__ == "__main__":
    unittest.main()
