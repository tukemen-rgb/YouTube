"""盛り上がり度 — 測れるものだけから作る、場面の熱さの点数。

「盛り上がっている」を機械が直接理解することはできない。代わりに、
盛り上がる場面で実際に大きくなりやすい 3 つの量を毎秒測って合成する。

1. **動きの激しさ** — 隣り合うフレームの画素差(signalstats の YDIF)。
   激しい戦闘・素早い操作で大きくなり、メニュー画面や停止で小さくなる。
2. **音の大きさ** — 短い窓ごとの RMS 音量。歓声・効果音・実況の張り。
3. **音の急な立ち上がり** — 音量の前の窓からの増分。爆発や「うおっ!」の
   瞬間は、単に大きいより「急に大きくなる」に出る。

3 つをそれぞれ標準化(平均 0・散らばり 1)してから重み付きで足し、
動画内の最小〜最大を 0〜100 に割り付ける。**点数は動画内の相対値**で、
別の動画同士の比較には使えない。これは意図した設計で、静かな解説動画
にも必ず「その動画なりの山」が見つかる。

重みと窓幅はこのファイルの定数がすべて。学習済みモデルも隠れた状態も
なく、同じ動画からは同じ点数が出る。
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: 集計の窓幅(秒)。細かすぎるとノイズを拾い、粗すぎると山がなまる。
WINDOW_SECONDS = 0.5


@dataclass(frozen=True)
class ScoreWeights:
    """3 つの測定値の合成の重み。学習(learning.py)で差し替えられる。"""

    motion: float = 0.5
    loudness: float = 0.3
    onset: float = 0.2


DEFAULT_WEIGHTS = ScoreWeights()

#: 無音(-inf dB)の代わりに使う床の値。
SILENCE_FLOOR_DB = -90.0


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
         "-vn", "-af", "astats=metadata=1:reset=1,ametadata=print:file=-",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise ExcitementError(f"音量の測定が失敗: {result.stderr[-300:]}")
    return parse_metadata_series(result.stdout, "lavfi.astats.Overall.RMS_level")


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
    for previous, current in zip(loudness, loudness[1:]):
        out.append(max(0.0, current - previous))
    return out


def window_features(motion: list[float],
                    loudness: list[float] | None) -> dict[str, list[float] | None]:
    """窓ごとの標準化済み特徴量。学習の入力と同じ形で保存もされる。"""
    return {
        "motion": zscores(motion),
        "loudness": zscores(loudness) if loudness is not None else None,
        "onset": zscores(onsets(loudness)) if loudness is not None else None,
    }


def combine_features(features: dict[str, list[float] | None],
                     weights: ScoreWeights = DEFAULT_WEIGHTS) -> list[float]:
    """特徴量 → 窓ごとの盛り上がり度 0〜100。音が無ければ動きだけで作る。"""
    z_motion = features["motion"] or []
    z_loud = features["loudness"]
    z_onset = features["onset"]
    if z_loud is None or z_onset is None:
        raw = [weights.motion * m for m in z_motion]
    else:
        raw = [
            weights.motion * m + weights.loudness * l + weights.onset * o
            for m, l, o in zip(z_motion, z_loud, z_onset)
        ]
    if not raw:
        return []
    low, high = min(raw), max(raw)
    if high - low < 1e-9:
        return [50.0] * len(raw)
    return [(v - low) / (high - low) * 100.0 for v in raw]


def combine_scores(motion: list[float], loudness: list[float] | None,
                   weights: ScoreWeights = DEFAULT_WEIGHTS) -> list[float]:
    """生の測定値 → 盛り上がり度。window_features + combine_features の近道。"""
    return combine_features(window_features(motion, loudness), weights)


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
    loudness = (
        bucketize(measure_loudness(source, ffmpeg=ffmpeg), duration)
        if has_audio else None
    )
    features = window_features(motion, loudness)
    return combine_features(features, weights), features
