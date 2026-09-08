"""声を聞き取りやすくする処理(S7)— 掛け方と、実際に効いているか。"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from videoyard.cut import DENOISE_LEVELS, CutError, build_command
from videoyard.cutplan import CutPlan, PlanSegment

_HAS_FFMPEG = shutil.which("ffmpeg") is not None


def _plan(**overrides) -> CutPlan:
    fields = dict(
        source_path="source.mp4", source_sha256="0" * 64, duration=10.0,
        width=320, height=240, has_audio=True, mode="static_or_silent",
        segments=(
            PlanSegment(start=0.0, end=2.0, action="cut"),
            PlanSegment(start=2.0, end=8.0, action="keep"),
        ),
    )
    fields.update(overrides)
    return CutPlan(**fields)


def _filter_arg(**kwargs) -> str:
    """既定の計画で build_command を呼び、filter_complex の中身を返す。"""
    args = build_command(_plan(), Path("/s.mp4"), Path("/f.ttf"), {},
                         Path("/o.mp4"), **kwargs)
    return args[args.index("-filter_complex") + 1]


class CommandBuilding(unittest.TestCase):
    def test_off_by_default(self):
        # 音を作り変える処理なので、黙って掛からない
        self.assertNotIn("afftdn", _filter_arg())
        self.assertNotIn("highpass", _filter_arg())

    def test_light_and_strong_chains(self):
        light = _filter_arg(denoise="light")
        self.assertIn("highpass=f=80", light)
        self.assertIn("afftdn=nr=12:nf=-30", light)
        strong = _filter_arg(denoise="strong")
        self.assertIn("afftdn=nr=24:nf=-25", strong)

    def test_unknown_level_rejected(self):
        with self.assertRaises(CutError) as ctx:
            _filter_arg(denoise="めっちゃ強く")
        self.assertIn("none", str(ctx.exception))

    def test_applied_before_bgm_is_mixed(self):
        # せっかく選んだ BGM をノイズ扱いして削らないこと
        text = _filter_arg(denoise="light", bgm=Path("/m.mp3"))
        self.assertLess(text.index("afftdn"), text.index("amix"))

    def test_applied_before_loudness_normalisation(self):
        text = _filter_arg(denoise="light")
        self.assertLess(text.index("afftdn"), text.index("loudnorm"))

    def test_silent_video_is_untouched(self):
        args = build_command(_plan(has_audio=False), Path("/s.mp4"),
                             Path("/f.ttf"), {}, Path("/o.mp4"), denoise="strong")
        self.assertNotIn("afftdn", args[args.index("-filter_complex") + 1])

    def test_levels_are_documented(self):
        self.assertEqual(set(DENOISE_LEVELS), {"none", "light", "strong"})
        self.assertEqual(DENOISE_LEVELS["none"], ())


@unittest.skipUnless(_HAS_FFMPEG, "ffmpeg が無い環境ではスキップ")
class RealDenoise(unittest.TestCase):
    """ノイズ混じりの音を実際に処理して、ノイズ床が下がることを測る。"""

    def _noise_floor(self, path: Path) -> float:
        """無音のはずの区間の実効音量(dB)。小さいほど静か。"""
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", str(path),
             "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, check=True)
        for line in result.stderr.splitlines():
            if "mean_volume:" in line:
                return float(line.split("mean_volume:")[1].split("dB")[0])
        self.fail("mean_volume が読めない")

    def test_noise_floor_drops(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            noisy = base / "noisy.wav"
            # ホワイトノイズだけの音(=ノイズ床そのもの)
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-f", "lavfi", "-i", "anoisesrc=color=white:amplitude=0.05"
                                      ":r=44100:d=3",
                 "-ac", "2", str(noisy)], check=True, capture_output=True)
            before = self._noise_floor(noisy)

            cleaned = base / "clean.wav"
            chain = ",".join(DENOISE_LEVELS["strong"])
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-i", str(noisy), "-af", chain, str(cleaned)],
                check=True, capture_output=True)
            after = self._noise_floor(cleaned)

            self.assertLess(after, before - 3.0,
                            f"ノイズが減っていない({before:.1f}dB → {after:.1f}dB)")

    def test_speech_band_tone_survives(self):
        """声の帯域(440Hz)の音は残ること(効きすぎて無音にしない)。"""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            tone = base / "tone.wav"
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-f", "lavfi", "-i", "sine=frequency=440:r=44100:d=3",
                 "-ac", "2", str(tone)], check=True, capture_output=True)
            before = self._noise_floor(tone)

            cleaned = base / "clean.wav"
            chain = ",".join(DENOISE_LEVELS["light"])
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-i", str(tone), "-af", chain, str(cleaned)],
                check=True, capture_output=True)
            after = self._noise_floor(cleaned)

            self.assertGreater(after, before - 3.0,
                               f"声の帯域まで削っている({before:.1f}dB → {after:.1f}dB)")


if __name__ == "__main__":
    unittest.main()
