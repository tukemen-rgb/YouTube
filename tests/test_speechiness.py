"""発話らしさ(音声認識なし)— 声のような揺れ方だけを見分けられること。

音声認識(Whisper 等)は社長の判断待ち(D1)。それまでの間、**声かどうか**
は分からなくても **声のような音量の揺れ方かどうか** なら測れる、という
のがこの近似の主張。テストはその主張を数字で縛る:

* 人の音節の速さ(毎秒 4〜9)で上下する音 → 高い点
* 一定の音(BGM・エンジン音)・無音・ゆっくりした波 → ほぼ 0
* 上下が小さすぎる音 → 低い点
* 0.5 秒に均した列からは復元できない(＝生の列を使う必要がある)

合成音での ffmpeg 実測も 1 本置いて、机上の数列だけで満足しないようにする。
"""

import math
import random
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from videoyard.excitement import (
    SILENCE_FLOOR_DB,
    SPEECH_FRAME_SAMPLES,
    SPEECH_MIN_SAMPLES,
    WINDOW_SECONDS,
    ScoreWeights,
    bucketize,
    combine_features,
    measure_loudness,
    measure_speech,
    modulation_score,
    speechiness,
    window_features,
)

_HAS_FFMPEG = shutil.which("ffmpeg") is not None

#: 発話らしさ用の測定点の頻度(44.1 kHz / SPEECH_FRAME_SAMPLES=512)。
#: astats の既定(2048 サンプル = 毎秒 22 点)では 10 Hz より速い上下が
#: 折り返して発話の速さに化ける。実測で 15 Hz の連射音が 0.20 を取った。
SAMPLES_PER_SECOND = 44100 / SPEECH_FRAME_SAMPLES
#: 判定に使う窓の長さ(秒)。excitement.SPEECH_CONTEXT_SECONDS と同じ。
SPAN = 1.5


def _wave(hz: float, swing_db: float, seconds: float = SPAN,
          level: float = -25.0) -> list[float]:
    """一定の速さで上下する音量の列。"""
    n = int(seconds * SAMPLES_PER_SECOND)
    return [level + swing_db * math.sin(2 * math.pi * hz * i / SAMPLES_PER_SECOND)
            for i in range(n)]


def _syllables(seed: int, per_second: float, seconds: float = SPAN) -> list[float]:
    """不規則な音節の列。正弦波より本物の話し声に近い形。

    音節ごとに長さが 0.6〜1.4 倍ばらつき、山と谷の高さにも揺らぎを足す。
    きれいな正弦波でしか動かない判定ならここで落ちる。
    """
    rnd = random.Random(seed)
    half = 0.5 / per_second      # 山 1 つ(または谷 1 つ)ぶんの長さ
    edges: list[float] = []
    at = 0.0
    while at < seconds + 1.0:
        at += rnd.uniform(0.6, 1.4) * half
        edges.append(at)

    def level(x: float) -> float:
        index = sum(1 for edge in edges if edge <= x)
        peak = -18.0 if index % 2 == 0 else -34.0
        return peak + rnd.uniform(-1.5, 1.5)

    return [level(i / SAMPLES_PER_SECOND)
            for i in range(int(seconds * SAMPLES_PER_SECOND))]


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


class NotSpeech(unittest.TestCase):
    """声でないものが高い点を取らないこと。誤検出のほうが害が大きい。"""

    def test_steady_level_is_zero(self):
        # ずっと同じ音量(持続する BGM・エンジン音・ホワイトノイズの平均)
        self.assertEqual(modulation_score([-20.0] * 40, SPAN), 0.0)

    def test_silence_is_zero(self):
        self.assertEqual(modulation_score([SILENCE_FLOOR_DB] * 40, SPAN), 0.0)

    def test_slow_wave_is_zero(self):
        # 0.5 Hz = 2 秒に 1 回の波。寄せては返す環境音や長いフェード
        self.assertEqual(modulation_score(_wave(0.5, 8.0), SPAN), 0.0)

    def test_too_fast_is_low(self):
        # 15 Hz。毎秒 15 発の連射音や機械の振動。声の音節ではない
        self.assertLess(modulation_score(_wave(15.0, 8.0), SPAN), 0.1)

    def test_much_too_fast_is_low(self):
        # 30 Hz。細かく測っているので折り返さずに落とせること
        self.assertLess(modulation_score(_wave(30.0, 8.0), SPAN), 0.1)

    def test_small_swing_is_low(self):
        # 速さは声と同じでも、上下が 0.3 dB しかないものは声ではない
        self.assertLess(modulation_score(_wave(5.5, 0.3), SPAN), 0.1)

    def test_too_few_samples_is_zero(self):
        short = _wave(5.5, 8.0)[:SPEECH_MIN_SAMPLES - 1]
        self.assertEqual(modulation_score(short, SPAN), 0.0)

    def test_zero_length_is_zero(self):
        self.assertEqual(modulation_score(_wave(5.5, 8.0), 0.0), 0.0)


class LooksLikeSpeech(unittest.TestCase):
    """人の音節の速さで上下する音が、ちゃんと高い点を取ること。"""

    def test_speech_rate_beats_everything_else(self):
        speech = modulation_score(_wave(5.5, 8.0), SPAN)
        self.assertGreater(speech, 0.8)
        for other in (_wave(0.5, 8.0), _wave(15.0, 8.0), [-20.0] * 40,
                      _wave(5.5, 0.3)):
            self.assertGreater(speech, modulation_score(other, SPAN) + 0.5)

    def test_irregular_syllables_still_score(self):
        # 正弦波でなく、長さのばらつく本物寄りの音節列でも通ること
        for per_second in (4.0, 5.0, 6.0, 7.0, 8.0):
            scores = [modulation_score(_syllables(seed, per_second), SPAN)
                      for seed in range(6)]
            self.assertGreater(_mean(scores), 0.3,
                               f"{per_second} 音節/秒 が低すぎる: {scores}")

    def test_response_peaks_in_the_human_range(self):
        # 速さを変えたときの反応が「人の速さで山」になっていること
        curve = {rate: _mean([modulation_score(_syllables(s, rate), SPAN)
                              for s in range(6)])
                 for rate in (1.0, 2.0, 5.5, 11.0)}
        self.assertGreater(curve[5.5], curve[1.0] + 0.5)
        self.assertGreater(curve[5.5], curve[2.0] + 0.5)
        self.assertGreater(curve[5.5], curve[11.0] + 0.3)


class WindowSeries(unittest.TestCase):
    """窓ごとの列にしたときの形と、生の列でなければならない理由。"""

    def _series(self, values: list[float]) -> list[tuple[float, float]]:
        return [(i / SAMPLES_PER_SECOND, v) for i, v in enumerate(values)]

    def test_one_value_per_window(self):
        series = self._series(_wave(5.5, 8.0, seconds=6.0))
        self.assertEqual(len(speechiness(series, 6.0)), int(6.0 / WINDOW_SECONDS))

    def test_empty_series_is_all_zero(self):
        self.assertEqual(speechiness([], 2.0), [0.0] * 4)

    def test_values_stay_in_zero_to_one(self):
        series = self._series(_syllables(7, 6.0, seconds=6.0))
        for value in speechiness(series, 6.0):
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)

    def test_deterministic(self):
        series = self._series(_wave(5.5, 8.0, seconds=6.0))
        self.assertEqual(speechiness(series, 6.0), speechiness(series, 6.0))

    def test_bucketized_series_loses_the_signal(self):
        """0.5 秒に均した列からは発話らしさを取り出せない。

        これがこの機能を bucketize の前に置いている理由。均した時点で
        音節の上下(0.2 秒周期)は消える。**あとから足せない**。
        """
        raw = self._series(_wave(5.5, 8.0, seconds=6.0))
        averaged = bucketize(raw, 6.0)
        self.assertGreater(_mean(speechiness(raw, 6.0)), 0.8)
        self.assertLess(modulation_score(averaged, 6.0), 0.2)


class FeedsTheScore(unittest.TestCase):
    """点数への配線: 無くても壊れない、あれば効く。"""

    def _features(self, speech):
        return window_features([1.0, 2.0, 3.0, 4.0],
                               [-30.0, -20.0, -25.0, -35.0], speech)

    def test_missing_speech_behaves_like_before(self):
        # v0.22 より前の analysis_windows.json には speech が無い
        old = {"motion": [0.0, 1.0], "loudness": [0.0, 1.0], "onset": [0.0, 0.0]}
        new = dict(old, speech=None)
        self.assertEqual(combine_features(old), combine_features(new))

    def test_wrong_length_speech_is_ignored_not_crashed(self):
        broken = {"motion": [0.0, 1.0], "loudness": [0.0, 1.0],
                  "onset": [0.0, 0.0], "speech": [0.5]}
        self.assertEqual(len(combine_features(broken)), 2)

    def test_speech_raises_the_window_it_marks(self):
        weights = ScoreWeights(motion=0.0, loudness=0.0, onset=0.0, speech=1.0)
        scores = combine_features(self._features([0.0, 0.0, 1.0, 0.0]), weights)
        self.assertEqual(max(range(4), key=lambda i: scores[i]), 2)

    def test_speech_is_not_zscored(self):
        # 全部 0(全編 BGM だけ)の動画で、無理に山を作らないこと
        features = self._features([0.0, 0.0, 0.0, 0.0])
        self.assertEqual(features["speech"], [0.0, 0.0, 0.0, 0.0])


@unittest.skipUnless(_HAS_FFMPEG, "ffmpeg が要る")
class RealAudio(unittest.TestCase):
    """合成した音を実際に ffmpeg で測って、机上の数列と同じ結論になること。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="videoyard-speech-")
        cls.base = Path(cls._tmp.name)
        cls.clips = {
            # 300 Hz の音を 4.5 Hz で強弱させる ＝ 話し声の音量の形
            "speech": "aevalsrc=sin(2*PI*300*t)*(0.55+0.45*sin(2*PI*4.5*t)):"
                      "d=6:s=44100",
            "tone": "aevalsrc=sin(2*PI*300*t):d=6:s=44100",
            "noise": "anoisesrc=d=6:c=pink:r=44100:a=0.5",
            "slow": "aevalsrc=sin(2*PI*300*t)*(0.55+0.45*sin(2*PI*0.5*t)):"
                    "d=6:s=44100",
            # 毎秒 15 発の連射音。粗く測ると発話の速さに化けるもの
            "fast": "aevalsrc=sin(2*PI*300*t)*(0.55+0.45*sin(2*PI*15*t)):"
                    "d=6:s=44100",
        }
        for name, source in cls.clips.items():
            path = cls.base / f"{name}.wav"
            subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", source, "-c:a", "pcm_s16le", str(path)],
                check=True)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _score(self, name: str) -> float:
        series = measure_speech(self.base / f"{name}.wav")
        return _mean(speechiness(series, 6.0))

    def test_modulated_tone_beats_steady_tone_and_noise(self):
        speech = self._score("speech")
        self.assertGreater(speech, 0.5)
        for other in ("tone", "noise", "slow", "fast"):
            self.assertGreater(speech, self._score(other) + 0.3,
                               f"{other} と差が付いていない")

    def test_speech_branch_measures_finer_than_the_loudness_branch(self):
        """発話用の枝だけ刻みを細かくしてあること(音量の枝は元のまま)。"""
        coarse = measure_loudness(self.base / "speech.wav")
        fine = measure_speech(self.base / "speech.wav")
        self.assertGreater(len(fine), len(coarse) * 3)

    def test_fast_pulse_would_fool_the_coarse_measurement(self):
        """粗い測定なら 15 Hz が発話に化けることを、実測で残しておく。

        この差が SPEECH_FRAME_SAMPLES を別枝で持っている理由。数字が
        逆転したら、細かく測る意味が無くなったということ。
        """
        coarse = _mean(speechiness(measure_loudness(self.base / "fast.wav"), 6.0))
        fine = _mean(speechiness(measure_speech(self.base / "fast.wav"), 6.0))
        self.assertGreater(coarse, 0.1, "粗い測定でも化けないなら前提が違う")
        self.assertLess(fine, 0.05)


@unittest.skipUnless(_HAS_FFMPEG, "ffmpeg が要る")
class ChangesTheChoice(unittest.TestCase):
    """機能として意味があること — 残す場面の選び方が実際に変わること。

    意地悪な形で試す: **喋っているところは音が小さく、喋っていない
    ところは大きい BGM** の動画。音量だけを見ていると、必ず BGM のほうが
    選ばれる。発話らしさが効いていれば、静かな話の場面を拾い直す。

    どこまで効くかは実測してある(2026-09-09、既定の重み 0.3):

        話し声が BGM より  0.0 dB 小さい → 拾える
        話し声が BGM より −5.0 dB 小さい → 拾える
        話し声が BGM より −8.5 dB 小さい → 拾えない
        話し声が BGM より −13  dB 小さい → 拾えない

    **5 dB 差までの押し返し**が今の実力。重みを 0.6 にすれば 13 dB 差でも
    勝てるが、音楽のトレモロを発話と間違える弱点(KnownLimitation)も
    同じだけ強く出るので上げていない。
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="videoyard-choice-")
        cls.base = Path(cls._tmp.name)
        cls.source = cls.base / "talk.mp4"
        # 0〜7 秒と 14〜21 秒 = 大きい持続音、7〜14 秒 = 5 dB 小さい話し声
        audio = ("aevalsrc='if(between(t,7,14),"
                 "0.45*sin(2*PI*300*t)*(0.55+0.45*sin(2*PI*5*t)),"
                 "0.8*sin(2*PI*200*t))':d=21:s=44100")
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc=s=320x240:d=21:r=30",
             "-f", "lavfi", "-i", audio,
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
             "-shortest", str(cls.source)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _talk_seconds(self, speech_weight: float,
                      source: Path | None = None) -> float:
        from videoyard.analyze import AnalyzeParams, analyze
        source = source or self.source
        directory = self.base / f"w{int(speech_weight * 100)}_{source.stem}"
        directory.mkdir()
        plan = analyze(directory, source,
                       AnalyzeParams(target_seconds=6.0),
                       weights=ScoreWeights(speech=speech_weight))
        return sum(min(s.end, 14.0) - max(s.start, 7.0) for s in plan.keeps
                   if s.end > 7.0 and s.start < 14.0)

    def test_speech_pulls_the_selection_toward_the_talking_part(self):
        without = self._talk_seconds(0.0)
        with_speech = self._talk_seconds(ScoreWeights().speech)
        self.assertEqual(without, 0.0, "音量だけなら大きい BGM が勝つはず")
        self.assertGreater(with_speech, 0.0,
                           "発話らしさを足しても話の場面を 1 秒も拾えていない")

    def test_it_gives_up_when_the_gap_is_too_large(self):
        """効かない範囲も書いておく。「万能」と誤解させないため。

        13 dB 差(話し声が BGM のほぼ 5 分の 1 の音量)では拾えない。
        直すには重みを上げるか、音声認識(D1)を入れるしかない。
        """
        quiet = self.base / "quiet_talk.mp4"
        audio = ("aevalsrc='if(between(t,7,14),"
                 "0.18*sin(2*PI*300*t)*(0.55+0.45*sin(2*PI*5*t)),"
                 "0.8*sin(2*PI*200*t))':d=21:s=44100")
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc=s=320x240:d=21:r=30",
             "-f", "lavfi", "-i", audio,
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
             "-shortest", str(quiet)], check=True)
        self.assertEqual(self._talk_seconds(ScoreWeights().speech, quiet), 0.0)


@unittest.skipUnless(_HAS_FFMPEG, "ffmpeg が要る")
class KnownLimitation(unittest.TestCase):
    """分かっている弱点を、隠さずテストとして残す。

    音量の揺れ方しか見ていないので、**人の音節と同じ速さで音量が上下する
    音楽**は発話と区別できない。トレモロ(6 Hz で音を震わせる演奏法)、
    速いハイハット、一部の効果音がこれに当たる。

    「音節の間隔のばらつき」で区別できないかを実測した(2026-09-09):

        本物寄りの不規則な音節列  ばらつき 0.32〜0.49
        トレモロ 6 Hz            ばらつき 0.28〜0.31  ← 重なる
        ホワイトノイズ            ばらつき 0.53〜0.77  ← いちばん高い

    速すぎる音と遅すぎる音は落とせるが、ばらつきでは分けられず、しかも
    ノイズが「いちばん人らしい」ことになってしまう。**確かめずに直したと
    言わないため、ここに数字ごと残す。** 根本的な解決は音声認識(D1、
    社長判断待ち)。それまでは重み 0.3 で影響を抑えて使う。
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="videoyard-tremolo-")
        cls.base = Path(cls._tmp.name)
        cls.path = cls.base / "tremolo.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", "aevalsrc=0.8*sin(2*PI*440*t)*(0.5+0.5*sin(2*PI*6*t)):"
                   "d=6:s=44100",
             "-c:a", "pcm_s16le", str(cls.path)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_musical_tremolo_is_mistaken_for_speech(self):
        score = _mean(speechiness(measure_speech(self.path), 6.0))
        # 直したらこのテストが落ちる。落ちたら「直った」と書き換えること
        self.assertGreater(score, 0.5,
                           "トレモロを弾けるようになったなら docs を更新すること")

    def test_the_mistake_is_bounded_by_the_weight(self):
        """間違えても、点数への影響が抑えてあること。"""
        self.assertLessEqual(ScoreWeights().speech, 0.3)


if __name__ == "__main__":
    unittest.main()
