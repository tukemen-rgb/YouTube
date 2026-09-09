"""盛り上がり度 — 測れるものだけから作る、場面の熱さの点数。

「盛り上がっている」を機械が直接理解することはできない。代わりに、
盛り上がる場面で実際に大きくなりやすい 3 つの量を毎秒測って合成する。

1. **動きの激しさ** — 隣り合うフレームの画素差(signalstats の YDIF)。
   激しい戦闘・素早い操作で大きくなり、メニュー画面や停止で小さくなる。
2. **音の大きさ** — 短い窓ごとの RMS 音量。歓声・効果音・実況の張り。
3. **音の急な立ち上がり** — 音量の前の窓からの増分。爆発や「うおっ!」の
   瞬間は、単に大きいより「急に大きくなる」に出る。
4. **発話らしさ** — 音量が人の音節の速さ(毎秒 4〜9 回)で上下しているか。
   音声認識は使わない。「何を言ったか」は分からないが、「喋っていそうな
   場面」なら測れる。解説・実況の本題を、静かでも拾うための項目。

はじめの 3 つをそれぞれ標準化(平均 0・散らばり 1)してから重み付きで足し、
動画内の最小〜最大を 0〜100 に割り付ける。**点数は動画内の相対値**で、
別の動画同士の比較には使えない。これは意図した設計で、静かな解説動画
にも必ず「その動画なりの山」が見つかる。

重みと窓幅はこのファイルの定数がすべて。学習済みモデルも隠れた状態も
なく、同じ動画からは同じ点数が出る。
"""

from __future__ import annotations

import bisect
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from videoyard.render import _escape_filter_value

#: 集計の窓幅(秒)。細かすぎるとノイズを拾い、粗すぎると山がなまる。
WINDOW_SECONDS = 0.5


@dataclass(frozen=True)
class ScoreWeights:
    """4 つの測定値の合成の重み。学習(learning.py)で差し替えられる。

    speech(発話らしさ)だけ数字の意味が違う。ほかの 3 つは標準化済み
    (散らばり 1)だが、speech は 0〜1 の生の値で、話がある動画でも
    散らばりは 0.4 ほど。**重み 0.3 は効き目 0.12 ぐらい**で、立ち上がり
    (0.2 × 1.0 = 0.2)より弱い。控えめにしてあるのは、音楽のトレモロを
    発話と取り違える既知の弱点があるため(tests/test_speechiness.py の
    KnownLimitation を参照)。根本的な解決は音声認識で、これは社長判断
    待ち(D1)。
    """

    motion: float = 0.5
    loudness: float = 0.3
    onset: float = 0.2
    speech: float = 0.3


DEFAULT_WEIGHTS = ScoreWeights()

#: 無音(-inf dB)の代わりに使う床の値。
SILENCE_FLOOR_DB = -90.0

#: 音量を測る astats の指定。**必要な 1 種類(全体の RMS)だけ**を計算・
#: 印字させる。既定のままだと 30 種類以上の統計を毎フレーム書き出すので、
#: 5 分の動画で 27 MB、1 時間なら 325 MB の一時ファイルになり、実測で
#: 3.8 秒 → 1.4 秒(2.7 倍)の差が出た。値は既定と完全に一致することを
#: tests/test_measure_all.py が縛っている。
#: measure_perchannel / measure_overall は ffmpeg 4.1 以降の指定。
_ASTATS = ("astats=metadata=1:reset=1"
           ":measure_perchannel=none:measure_overall=RMS_level")
#: 印字する metadata の鍵。これだけに絞ることで出力が 1/25 になる。
_RMS_KEY = "lavfi.astats.Overall.RMS_level"


class ExcitementError(RuntimeError):
    """測定が完了しなかった。点数は付いていない。"""


# ---- ffmpeg の測定パス ------------------------------------------------------

def measure_motion(source: Path, ffmpeg: str = "ffmpeg") -> list[tuple[float, float]]:
    """毎フレームの動き量 (時刻, YDIF) を測る。縮小してから測り高速化。"""
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostdin", "-i", str(source),
         "-vf", "scale=160:-2,signalstats,metadata=print:file=-",
         "-an", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise ExcitementError(f"動きの測定が失敗: {result.stderr[-300:]}")
    return parse_metadata_series(result.stdout, "lavfi.signalstats.YDIF")


def measure_loudness(source: Path, ffmpeg: str = "ffmpeg") -> list[tuple[float, float]]:
    """短い窓ごとの音量 (時刻, RMS dB) を測る。"""
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostdin", "-i", str(source),
         "-vn", "-af", f"{_ASTATS},ametadata=print:key={_RMS_KEY}:file=-",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise ExcitementError(f"音量の測定が失敗: {result.stderr[-300:]}")
    return parse_metadata_series(result.stdout, _RMS_KEY)


#: 発話らしさ用の音量を測るときの 1 点あたりのサンプル数。astats の既定
#: (音声フレームそのまま = 2048)では 1 秒に 22 点しか出ず、10 Hz より
#: 速い上下が「折り返して」ゆっくりに見える(エイリアシング)。毎秒 15 回
#: 撃つ連射音が発話らしさ 0.20 と誤判定された実測がある。512 に細かく
#: すると毎秒 86 点になり、同じ連射音は 0.00 に落ちる。
#: **音量そのものの測定(loudness)には使わない。** 平均を取る幅が変わると
#: 既存の点数がずれるため、発話らしさ専用の枝を別に立てている。
SPEECH_FRAME_SAMPLES = 512

#: 動き量を測るときの縮小後の幅。小さいほど速いが、細かい動きを拾えない。
MOTION_SCALE_WIDTH = 160
#: 動き量を測る頻度(1 秒あたり)。10 分の動画での内訳測定では、毎フレーム
#: (30fps)の signalstats が分析時間の 3 分の 1 を占めていた。点数は
#: 0.5 秒の窓にまとめるので、窓あたり 5 点あれば足りる。**静止判定に使う
#: freezedetect は別の枝で全フレームのまま**なので、切る位置の精度は
#: 落ちない。
MOTION_SAMPLE_FPS = 10


def measure_speech(source: Path, ffmpeg: str = "ffmpeg") -> list[tuple[float, float]]:
    """発話らしさ用の、細かい刻みの音量列 (時刻, RMS dB) を測る。"""
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostdin", "-i", str(source), "-vn",
         "-af", f"asetnsamples=n={SPEECH_FRAME_SAMPLES}:p=0,{_ASTATS},"
                f"ametadata=print:key={_RMS_KEY}:file=-",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise ExcitementError(f"発話らしさの測定が失敗: {result.stderr[-300:]}")
    return parse_metadata_series(result.stdout, _RMS_KEY)


def measure_all(source: Path, detect_video: str = "", detect_audio: str = "",
                has_audio: bool = True, out_dir: Path | None = None,
                duration: float = 0.0,
                progress: Callable[[float], None] | None = None,
                ffmpeg: str = "ffmpeg") -> tuple[str, list[tuple[float, float]],
                                                 list[tuple[float, float]],
                                                 list[tuple[float, float]]]:
    """動き・音量・検出フィルタを **1 パス**でまとめて測る(S8)。

    従来は 3 回に分けて ffmpeg を起動していた(検出 / 動き / 音量)。
    動画のデコードは分析でいちばん重い処理なので、3 回が 1 回になれば
    そのぶん速くなる。10 分の動画で実測 74 秒 → 41 秒(1.8 倍)。

    1 パスにした代わりに「長い無言時間」ができるので(3 時間の配信なら
    12 分)、ffmpeg の -progress を読んで進み具合を報告する(S9)。
    duration を渡すと割合と残り時間を出せる。

    返すのは (検出パスの stderr, 動きの列, 音量の列, 発話用の細かい音量列)。
    metadata の出力は枝ごとに別々のファイルへ書く。同じ標準出力へ混ぜると、
    どの枝の pts_time なのか分からなくなり時刻がずれる。

    detect_video / detect_audio は freezedetect / silencedetect の指定。
    空なら検出は行わない。
    """
    directory = out_dir or Path(tempfile.mkdtemp(prefix="videoyard-measure-"))
    directory.mkdir(parents=True, exist_ok=True)
    motion_path = directory / "motion.txt"
    loudness_path = directory / "loudness.txt"
    speech_path = directory / "speech.txt"

    # 検出の枝は捨てる(nullsink)。測定の枝はグラフの出口にして -map する。
    # ffmpeg は出口の無いフィルタグラフを受け付けないため。
    chains = ["[0:v]split=2[vdet][vmot]"]
    chains.append(
        f"[vdet]{detect_video},nullsink" if detect_video else "[vdet]nullsink")
    chains.append(
        f"[vmot]fps={MOTION_SAMPLE_FPS},scale={MOTION_SCALE_WIDTH}:-2"
        ",signalstats"
        f",metadata=print:file={_escape_filter_value(str(motion_path))}[vout]"
    )
    maps = ["-map", "[vout]"]
    if has_audio:
        chains.append("[0:a]asplit=3[adet][alou][aspe]")
        chains.append(
            f"[adet]{detect_audio},anullsink" if detect_audio
            else "[adet]anullsink")
        chains.append(
            f"[alou]{_ASTATS},ametadata=print:key={_RMS_KEY}"
            f":file={_escape_filter_value(str(loudness_path))}[aout]"
        )
        # 発話らしさ用は刻みを細かくした別の枝。デコードは共通なので
        # 増えるのは RMS の計算だけで、時間はほとんど変わらない。
        chains.append(
            f"[aspe]asetnsamples=n={SPEECH_FRAME_SAMPLES}:p=0,{_ASTATS}"
            f",ametadata=print:key={_RMS_KEY}"
            f":file={_escape_filter_value(str(speech_path))}[sout]"
        )
        maps += ["-map", "[aout]", "-map", "[sout]"]

    args = [ffmpeg, "-hide_banner", "-nostdin", "-i", str(source),
            "-filter_complex", ";".join(chains), *maps]
    if progress is not None:
        # -progress は「今どこまで処理したか」を機械可読で書き出す。
        # metadata はファイルへ出すので標準出力は空いている。
        args += ["-progress", "pipe:1", "-nostats"]
    args += ["-f", "null", "-"]

    stderr_text = _run_with_progress(args, duration, progress)

    _ = stderr_text
    motion = parse_metadata_series(
        motion_path.read_text(encoding="utf-8", errors="replace")
        if motion_path.is_file() else "", "lavfi.signalstats.YDIF")
    loudness: list[tuple[float, float]] = []
    speech: list[tuple[float, float]] = []
    if has_audio:
        for path, into in ((loudness_path, "loudness"), (speech_path, "speech")):
            if not path.is_file():
                continue
            series = parse_metadata_series(
                path.read_text(encoding="utf-8", errors="replace"), _RMS_KEY)
            if into == "loudness":
                loudness = series
            else:
                speech = series
    return stderr_text, motion, loudness, speech


#: -progress の出力から進み具合を読む鍵(マイクロ秒)。
_OUT_TIME = re.compile(r"^out_time_us=(\d+)", re.MULTILINE)


def _run_with_progress(args: list[str], duration: float,
                       progress: Callable[[float], None] | None) -> str:
    """ffmpeg を走らせ、-progress を読みながら進み具合を知らせる。

    stderr は一時ファイルへ受ける。パイプ 2 本を同時に読まないと
    詰まって止まるため(バッファがいっぱいになると ffmpeg が待つ)。
    """
    if progress is None:
        result = subprocess.run(args, capture_output=True, text=True)
        if result.returncode != 0:
            raise ExcitementError(f"測定パスが失敗: {result.stderr[-300:]}")
        return result.stderr

    with tempfile.TemporaryFile(mode="w+", encoding="utf-8",
                                errors="replace") as err:
        with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=err,
                              text=True, bufsize=1) as proc:
            assert proc.stdout is not None
            for line in proc.stdout:
                if m := _OUT_TIME.match(line):
                    seconds = int(m.group(1)) / 1_000_000
                    progress(seconds / duration if duration > 0 else 0.0)
        err.seek(0)
        stderr_text = err.read()
    if proc.returncode != 0:
        raise ExcitementError(f"測定パスが失敗: {stderr_text[-300:]}")
    return stderr_text


_PTS_TIME = re.compile(r"pts_time:([0-9.]+)")


def parse_metadata_series(output: str, key: str) -> list[tuple[float, float]]:
    """metadata=print の出力から (pts_time, key の値) の列を読む。"""
    value_re = re.compile(re.escape(key) + r"=(-?(?:[0-9.]+|inf))")
    series: list[tuple[float, float]] = []
    current_time: float | None = None
    for line in output.splitlines():
        if m := _PTS_TIME.search(line):
            current_time = float(m.group(1))
        elif (m := value_re.search(line)) and current_time is not None:
            raw = m.group(1)
            value = SILENCE_FLOOR_DB if raw == "-inf" else float(raw)
            series.append((current_time, value))
    return series


# ---- 点数の計算(純粋関数) ------------------------------------------------

def bucketize(series: list[tuple[float, float]], duration: float,
              window: float = WINDOW_SECONDS) -> list[float]:
    """時系列を窓ごとの平均に落とす。測定の無い窓は前の値を引き継ぐ。"""
    count = max(1, int(duration / window + 0.999))
    sums = [0.0] * count
    counts = [0] * count
    for time, value in series:
        index = min(count - 1, int(time / window))
        sums[index] += value
        counts[index] += 1
    out: list[float] = []
    previous = 0.0
    for i in range(count):
        if counts[i]:
            previous = sums[i] / counts[i]
        out.append(previous)
    return out


def zscores(values: list[float]) -> list[float]:
    if not values:
        return []
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    std = variance ** 0.5
    if std < 1e-9:
        return [0.0] * len(values)
    return [(v - mean) / std for v in values]


def onsets(loudness: list[float]) -> list[float]:
    """音量の「急な立ち上がり」= 前の窓からの増分(下がりは 0)。"""
    out = [0.0]
    for previous, current in zip(loudness, loudness[1:], strict=False):
        out.append(max(0.0, current - previous))
    return out


#: 人が話すときの音節の速さの中心(Hz)。英語の音節はおよそ毎秒 4〜5 個、
#: 日本語のモーラはもう少し速く 6〜8 個。両方を拾うため中間に置く。
SPEECH_RATE_HZ = 5.5
#: 発話らしいとみなす速さの幅(Hz)。2〜9 Hz を通し、外れるほど 0 に近づく。
#: 上限は測定の細かさにも縛られる。astats は 1 秒に約 21 点しか出さない
#: ので、10 Hz を超える上下はそもそも数えられない(標本化定理)。
SPEECH_RATE_TOLERANCE = 3.5
#: 発話らしさを測るときに見る前後の長さ(秒)。0.5 秒の窓だけでは
#: 5 Hz の上下が 2〜3 周期しか入らず、数え方の当たり外れが大きい。
#: 前後あわせて 1.5 秒ぶんを使えば 7 周期ほど入り、安定する。
SPEECH_CONTEXT_SECONDS = 1.5
#: 判定に必要な最低の測定点数。これを下回る窓は 0(判断しない)。
SPEECH_MIN_SAMPLES = 8
#: 「はっきり上下している」と言い切れる音量の散らばり(dB)。人の声は
#: 音節の山と谷で 5〜15 dB 上下する。持続音(BGM・環境音)はもっと平ら。
SPEECH_SPREAD_FULL_DB = 6.0


def modulation_score(values: list[float], seconds: float) -> float:
    """音量の並びが「人が話している」ような上下をしているかを 0〜1 で返す。

    音声認識は使わない。使うのは、**話し声は音節ごとに音量が上下する**
    という物理的な性質だけ:

    * 平均をまたぐ回数から、上下の速さ(Hz)を出す。1 周期で 2 回またぐ。
    * その速さが人の音節の速さ(約 5 Hz)にどれだけ近いかを見る。
    * どれだけ大きく上下しているか(dB のばらつき)を掛ける。

    持続音(BGM・エンジン音・ホワイトノイズ)は平らなので、ばらつきが
    小さく 0 に近づく。無音は全点が同じ床の値になるので必ず 0。
    ゆっくりした波(1 Hz の効果音)や速すぎる震え(20 Hz)は、速さの
    項で落ちる。**「声かどうか」ではなく「声のような揺れ方かどうか」**
    しか分からない近似であることを忘れないこと。
    """
    if seconds <= 0 or len(values) < SPEECH_MIN_SAMPLES:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    spread = variance ** 0.5
    if spread < 1e-9:
        return 0.0

    crossings = 0
    previous = values[0] - mean
    for value in values[1:]:
        current = value - mean
        if current == 0:
            continue
        if (previous > 0) != (current > 0):
            crossings += 1
        previous = current
    rate_hz = crossings / (2.0 * seconds)

    plausibility = max(
        0.0, 1.0 - abs(rate_hz - SPEECH_RATE_HZ) / SPEECH_RATE_TOLERANCE)
    strength = min(1.0, spread / SPEECH_SPREAD_FULL_DB)
    return plausibility * strength


def speechiness(series: list[tuple[float, float]], duration: float,
                window: float = WINDOW_SECONDS,
                context: float = SPEECH_CONTEXT_SECONDS) -> list[float]:
    """生の音量の列(bucketize する前)から、窓ごとの発話らしさを作る。

    bucketize 済みの列を使ってはいけない。0.5 秒の平均を取った時点で、
    音節ごとの上下(0.2 秒周期)は消えてしまう。**発話らしさは平均を
    取る前の細かさにしか残っていない。**
    """
    count = max(1, int(duration / window + 0.999))
    if not series:
        return [0.0] * count
    times = [t for t, _ in series]
    values = [v for _, v in series]
    span = duration if duration > 0 else times[-1] + window
    out: list[float] = []
    for i in range(count):
        centre = (i + 0.5) * window
        low = max(0.0, centre - context / 2)
        high = min(span, centre + context / 2)
        first = bisect.bisect_left(times, low)
        last = bisect.bisect_left(times, high)
        out.append(modulation_score(values[first:last], high - low))
    return out


def window_features(motion: list[float], loudness: list[float] | None,
                    speech: list[float] | None = None,
                    ) -> dict[str, list[float] | None]:
    """窓ごとの標準化済み特徴量。学習の入力と同じ形で保存もされる。

    speech(発話らしさ)だけは標準化しない。0〜1 の絶対的な尺度で、
    「この動画の中では相対的に喋っている」ではなく「喋っている」を
    表すため。全編無音の動画なら全部 0 のままでよい。
    """
    return {
        "motion": zscores(motion),
        "loudness": zscores(loudness) if loudness is not None else None,
        "onset": zscores(onsets(loudness)) if loudness is not None else None,
        "speech": list(speech) if speech is not None else None,
    }


def combine_features(features: dict[str, list[float] | None],
                     weights: ScoreWeights = DEFAULT_WEIGHTS) -> list[float]:
    """特徴量 → 窓ごとの盛り上がり度 0〜100。音が無ければ動きだけで作る。"""
    z_motion = features["motion"] or []
    z_loud = features["loudness"]
    z_onset = features["onset"]
    # speech は古い analysis_windows.json には無い。無ければ 0(効かない)。
    speech = features.get("speech") or []
    if len(speech) != len(z_motion):
        speech = [0.0] * len(z_motion)
    if z_loud is None or z_onset is None:
        raw = [weights.motion * m + weights.speech * sp
               for m, sp in zip(z_motion, speech, strict=True)]
    else:
        raw = [
            weights.motion * m + weights.loudness * loud
            + weights.onset * o + weights.speech * sp
            for m, loud, o, sp in zip(z_motion, z_loud, z_onset, speech,
                                      strict=True)
        ]
    if not raw:
        return []
    low, high = min(raw), max(raw)
    if high - low < 1e-9:
        return [50.0] * len(raw)
    return [(v - low) / (high - low) * 100.0 for v in raw]


def combine_scores(motion: list[float], loudness: list[float] | None,
                   weights: ScoreWeights = DEFAULT_WEIGHTS,
                   speech: list[float] | None = None) -> list[float]:
    """生の測定値 → 盛り上がり度。window_features + combine_features の近道。"""
    return combine_features(window_features(motion, loudness, speech), weights)


def range_score(scores: list[float], start: float, end: float,
                window: float = WINDOW_SECONDS) -> float:
    """区間 start〜end の平均点。"""
    if not scores:
        return 0.0
    first = min(len(scores) - 1, int(start / window))
    last = min(len(scores) - 1, max(first, int((end - 1e-6) / window)))
    section = scores[first:last + 1]
    return sum(section) / len(section)


def score_source(source: Path, duration: float, has_audio: bool,
                 weights: ScoreWeights = DEFAULT_WEIGHTS, ffmpeg: str = "ffmpeg",
                 ) -> tuple[list[float], dict[str, list[float] | None]]:
    """元動画 → (窓ごとの盛り上がり度, 特徴量)。測定 1〜2 パスで済む。"""
    motion = bucketize(measure_motion(source, ffmpeg=ffmpeg), duration)
    loudness = (bucketize(measure_loudness(source, ffmpeg=ffmpeg), duration)
                if has_audio else None)
    speech = (speechiness(measure_speech(source, ffmpeg=ffmpeg), duration)
              if has_audio else None)
    features = window_features(motion, loudness, speech)
    return combine_features(features, weights), features
