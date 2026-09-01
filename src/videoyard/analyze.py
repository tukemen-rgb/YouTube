"""元動画の分析 — 退屈な区間を見つけて、カット計画の「案」を作る。

やることは 3 つ。

1. ffmpeg の freezedetect(静止画検出)と silencedetect(無音検出)を
   1 パスで走らせ、stderr の報告を区間のリストに読み取る。
2. 区間を方針(mode)に従って「切る区間」にまとめ、その補集合を
   「残す区間」にする。短すぎる切り貼りは整理する。
3. 残す各区間の平均音量を測り、一番大きい区間に「盛り上がり候補」の
   印をつける(音量は盛り上がりの代用値。本物の理解ではない)。

出力は CutPlan(cutplan.json)の「案」。ここで動画は 1 バイトも
書き換えない。人が案を直してから cut が実行する。判断するのは人、
という分担は docs/ARCHITECTURE.md のとおり。

stderr の解析やカット案の組み立ては純粋関数にしてあり、ffmpeg の
無い環境でも本文のロジックはテストできる。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from videoyard.cutplan import CutPlan, PlanSegment
from videoyard.excitement import (
    WINDOW_SECONDS,
    ScoreWeights,
    bucketize,
    combine_features,
    measure_loudness,
    measure_motion,
    range_score,
    window_features,
)
from videoyard.llm import OllamaTelopWriter, SceneBrief

Interval = tuple[float, float]


class AnalyzeError(RuntimeError):
    """分析が完了しなかった。計画は書かれていない。"""


#: 切る区間の決め方。
#: static_or_silent  静止画 または 無音なら切る(既定)
#: static_and_silent 静止画 かつ 無音のときだけ切る
MODES = ("static_or_silent", "static_and_silent", "static_only", "silent_only")


@dataclass(frozen=True)
class AnalyzeParams:
    mode: str = "static_or_silent"
    silence_db: float = -35.0     # これより静かなら「無音」
    min_silence: float = 0.8      # 無音とみなす最短秒数
    still_noise: float = 0.003    # 静止画判定の許容ノイズ(0..1)
    min_still: float = 1.0        # 静止画とみなす最短秒数
    near_still_ydif: float = 0.2  # 動き量(YDIF)がこれ未満なら「ほぼ静止」。0 で無効
    min_cut: float = 1.0          # これより短い退屈は切らない(細切れ防止)
    min_keep: float = 0.6         # これより短い残しは諦めて切る
    target_seconds: float | None = None  # 指定すると盛り上がり度上位でこの尺に収める
    chunk_seconds: float = 3.0    # 尺調整で切り出す最小の粒(秒)

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise AnalyzeError(f"mode は {MODES} のどれか: {self.mode}")
        if self.target_seconds is not None and self.target_seconds <= 0:
            raise AnalyzeError("target_seconds は正の秒数")


# ---- ffmpeg / ffprobe の呼び出し ------------------------------------------

def probe_source(source: Path, ffprobe: str = "ffprobe") -> dict[str, object]:
    """動画の長さ・画面サイズ・音声の有無を読む。"""
    if shutil.which(ffprobe) is None:
        raise AnalyzeError(f"{ffprobe} が見つからない")
    if not source.is_file():
        raise AnalyzeError(f"元動画がない: {source}")
    result = subprocess.run(
        [ffprobe, "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(source)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise AnalyzeError(f"ffprobe が失敗: {result.stderr[-300:]}")
    info = json.loads(result.stdout)
    video = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    if not video:
        raise AnalyzeError("映像ストリームがない")
    duration = float(info.get("format", {}).get("duration", 0.0))
    if duration <= 0:
        raise AnalyzeError("動画の長さが読めない")
    return {
        "duration": duration,
        "width": int(video[0]["width"]),
        "height": int(video[0]["height"]),
        "has_audio": any(s.get("codec_type") == "audio" for s in info["streams"]),
    }


def run_detection(source: Path, params: AnalyzeParams, has_audio: bool,
                  ffmpeg: str = "ffmpeg") -> str:
    """freezedetect / silencedetect を 1 パスで走らせ、stderr を返す。"""
    args = [ffmpeg, "-hide_banner", "-nostdin", "-i", str(source),
            "-vf", f"freezedetect=n={params.still_noise}:d={params.min_still}"]
    if has_audio:
        args += ["-af", f"silencedetect=noise={params.silence_db}dB:d={params.min_silence}"]
    args += ["-f", "null", "-"]
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        raise AnalyzeError(f"検出パスが失敗: {result.stderr[-300:]}")
    return result.stderr


_FREEZE_START = re.compile(r"freeze_start:\s*([0-9.]+)")
_FREEZE_END = re.compile(r"freeze_end:\s*([0-9.]+)")
_SILENCE_START = re.compile(r"silence_start:\s*([0-9.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*([0-9.]+)")


def _parse_pairs(stderr: str, start_re: re.Pattern[str], end_re: re.Pattern[str],
                 duration: float) -> list[Interval]:
    """start/end の報告を順に組にする。閉じていない start は動画末尾で閉じる。"""
    events: list[tuple[float, str]] = []
    for line in stderr.splitlines():
        if m := start_re.search(line):
            events.append((float(m.group(1)), "start"))
        elif m := end_re.search(line):
            events.append((float(m.group(1)), "end"))
    intervals: list[Interval] = []
    open_at: float | None = None
    for time, kind in events:
        if kind == "start" and open_at is None:
            open_at = time
        elif kind == "end" and open_at is not None:
            intervals.append((open_at, min(time, duration)))
            open_at = None
    if open_at is not None:
        intervals.append((open_at, duration))
    return [(s, e) for s, e in intervals if e > s]


def parse_freeze(stderr: str, duration: float) -> list[Interval]:
    return _parse_pairs(stderr, _FREEZE_START, _FREEZE_END, duration)


def parse_silence(stderr: str, duration: float) -> list[Interval]:
    return _parse_pairs(stderr, _SILENCE_START, _SILENCE_END, duration)


def low_motion_intervals(motion: list[float], window: float, threshold: float,
                         min_still: float) -> list[Interval]:
    """動き量が threshold 未満のまま min_still 秒以上続く区間。

    freezedetect は画素の完全一致に近い基準なので、実写のノイズや
    点滅カーソル程度の微動で「静止」を取りこぼす(実測)。既に測って
    いる窓ごとの動き量(YDIF)で「ほぼ静止」を拾い、補完する。
    threshold は生の YDIF 値で、0 にするとこの検出は無効になる。
    """
    if threshold <= 0:
        return []
    intervals: list[Interval] = []
    start: float | None = None
    for i, value in enumerate(motion):
        if value < threshold:
            if start is None:
                start = i * window
        elif start is not None:
            if i * window - start >= min_still:
                intervals.append((start, i * window))
            start = None
    if start is not None and len(motion) * window - start >= min_still:
        intervals.append((start, len(motion) * window))
    return intervals


# ---- 区間の計算(純粋関数) ----------------------------------------------

def normalize(intervals: list[Interval], gap: float = 0.25) -> list[Interval]:
    """並べ替えて、重なり・すき間 gap 未満の区間をつなげる。"""
    merged: list[list[float]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def intersect(a: list[Interval], b: list[Interval]) -> list[Interval]:
    out = []
    for s1, e1 in a:
        for s2, e2 in b:
            s, e = max(s1, s2), min(e1, e2)
            if e > s:
                out.append((s, e))
    return normalize(out, gap=0.0)


def complement(intervals: list[Interval], duration: float) -> list[Interval]:
    out = []
    cursor = 0.0
    for start, end in intervals:
        if start > cursor:
            out.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration:
        out.append((cursor, duration))
    return out


def propose_segments(duration: float, static: list[Interval], silent: list[Interval],
                     params: AnalyzeParams) -> list[PlanSegment]:
    """検出結果 → 切る/残すの案。全体を隙間なく覆う並びにする。"""
    static = normalize(static)
    silent = normalize(silent)
    if params.mode == "static_or_silent":
        boring = normalize(static + silent)
    elif params.mode == "static_and_silent":
        boring = intersect(static, silent)
    elif params.mode == "static_only":
        boring = static
    else:
        boring = silent
    boring = [(s, e) for s, e in boring if e - s >= params.min_cut]
    keeps = [(s, e) for s, e in complement(boring, duration) if e - s >= params.min_keep]
    # 残す区間が決まったら、その外側はすべて「切る」。min_keep で諦めた
    # 細切れも自然に切る側へ落ちる。
    cuts = complement(keeps, duration)

    segments = []
    scene = 0
    for start, end in sorted(keeps + cuts):
        if (start, end) in keeps:
            scene += 1
            segments.append(PlanSegment(
                start=round(start, 3), end=round(end, 3), action="keep",
                telop=f"シーン {scene}", reason="動きまたは音がある",
            ))
        else:
            segments.append(PlanSegment(
                start=round(start, 3), end=round(end, 3), action="cut",
                reason="退屈な区間(静止画/無音)",
            ))
    if not any(s.action == "keep" for s in segments):
        raise AnalyzeError(
            "残す区間が 1 つも無い。検出の閾値が厳しすぎるか、"
            "元動画がすべて静止画/無音。パラメータを調整すること。"
        )
    return segments


# ---- 盛り上がり度による印付けと尺調整 -------------------------------------

def mark_highlight(segments: list[PlanSegment], scores: dict[int, float]) -> list[PlanSegment]:
    """点数が最大の keep に印をつける。"""
    if not scores:
        return segments
    best = max(scores, key=lambda i: scores[i])
    out = []
    for i, seg in enumerate(segments):
        if i == best:
            out.append(seg.replaced(reason=seg.reason + " ★盛り上がり候補(盛り上がり度が最大)"))
        else:
            out.append(seg)
    return out


_SCENE_TELOP = re.compile(r"^シーン \d+$")


def _renumber_scene_telops(segments: list[PlanSegment]) -> list[PlanSegment]:
    """テンプレートのままのテロップ(シーン n)を並び順で振り直す。

    尺調整で keep が割れたり消えたりした後も、番号が飛ばないように。
    人や AI が書いた文言には触らない。"""
    out = []
    scene = 0
    for seg in segments:
        if seg.action == "keep" and _SCENE_TELOP.match(seg.telop):
            scene += 1
            out.append(seg.replaced(telop=f"シーン {scene}"))
        else:
            if seg.action == "keep":
                scene += 1
            out.append(seg)
    return out


def trim_to_target(segments: list[PlanSegment], scores: list[float],
                   target_seconds: float, chunk_seconds: float = 3.0) -> list[PlanSegment]:
    """盛り上がり度の高い部分から順に残し、合計を目標の尺に収める。

    keep 区間を chunk_seconds 刻みの小片に割り、点数の高い小片から
    目標秒数に達するまで採用する。採用されなかった部分は
    「尺調整のため除外」と理由を付けて cut になる。時間順は変えない
    (動画の流れを並べ替えない)。目標が現状より長ければ何もしない。
    """
    keeps = [s for s in segments if s.action == "keep"]
    total = sum(s.end - s.start for s in keeps)
    if target_seconds >= total:
        return segments

    chunks: list[tuple[float, float, float, PlanSegment]] = []  # (score, start, end, 親)
    for seg in keeps:
        cursor = seg.start
        while cursor < seg.end - 1e-6:
            end = min(seg.end, cursor + chunk_seconds)
            if seg.end - end < chunk_seconds / 2:  # 端数は最後の小片に吸収
                end = seg.end
            chunks.append((range_score(scores, cursor, end), cursor, end, seg))
            cursor = end

    chosen: list[tuple[float, float, PlanSegment]] = []
    remaining = target_seconds
    for _score, start, end, parent in sorted(chunks, key=lambda c: -c[0]):
        if remaining <= 0:
            break
        chosen.append((start, end, parent))
        remaining -= end - start
    chosen.sort()
    # 隣り合う採用小片は 1 区間にまとめる(不要な切れ目とフェードを作らない)
    merged: list[tuple[float, float, PlanSegment]] = []
    for start, end, parent in chosen:
        if merged and merged[-1][2] is parent and start <= merged[-1][1] + 1e-6:
            merged[-1] = (merged[-1][0], end, parent)
        else:
            merged.append((start, end, parent))
    chosen = merged

    out: list[PlanSegment] = []
    for seg in segments:
        if seg.action == "cut":
            out.append(seg)
            continue
        cursor = seg.start
        parts = [(s, e) for s, e, parent in chosen if parent is seg]
        for start, end in parts:
            if start > cursor + 1e-6:
                out.append(PlanSegment(
                    start=round(cursor, 3), end=round(start, 3), action="cut",
                    reason="尺調整のため除外(盛り上がり度が低い)",
                ))
            out.append(seg.replaced(start=round(start, 3), end=round(end, 3)))
            cursor = end
        if cursor < seg.end - 1e-6:
            out.append(PlanSegment(
                start=round(cursor, 3), end=round(seg.end, 3), action="cut",
                reason="尺調整のため除外(盛り上がり度が低い)",
            ))
    return _renumber_scene_telops(out)


# ---- テロップ文言のローカル AI 下書き --------------------------------------

def draft_telops(segments: list[PlanSegment], writer: OllamaTelopWriter,
                 hint: str) -> list[PlanSegment]:
    """keep 区間のテロップをローカル AI の下書きに置き換える。

    下書きが得られなかった区間はテンプレート(シーン n)のまま。
    どの文言が AI 由来かは reason に残す(人が直すときの判断材料)。
    """
    out = []
    for seg in segments:
        if seg.action != "keep":
            out.append(seg)
            continue
        brief = SceneBrief(
            number=len([s for s in out if s.action == "keep"]) + 1,
            start=seg.start,
            duration=seg.end - seg.start,
            is_highlight="盛り上がり" in seg.reason,
            hint=hint,
        )
        draft = writer.write(brief)
        if draft:
            out.append(seg.replaced(
                telop=draft,
                reason=seg.reason + "(テロップ文言はローカルAIの下書き)",
            ))
        else:
            out.append(seg)
    return out


# ---- 入口 -----------------------------------------------------------------

def analyze(production_dir: Path, source: Path, params: AnalyzeParams,
            writer: OllamaTelopWriter | None = None, hint: str = "",
            weights: ScoreWeights | None = None,
            progress: Callable[[str], None] | None = None,
            ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> CutPlan:
    """元動画を分析し、cutplan.json の案を production_dir に書く。

    progress を渡すと工程の節目ごとに一行ずつ知らせる。長い動画では
    測定に数分かかるため、無言だと固まったように見える(U1)。
    """
    def report(message: str) -> None:
        if progress is not None:
            progress(message)

    report("元動画を確認中…")
    info = probe_source(source, ffprobe=ffprobe)
    has_audio = bool(info["has_audio"])
    if not has_audio and params.mode not in ("static_only",):
        # 音が無い動画に無音検出は意味を持たない。黙って全編無音扱いに
        # するのが一番危ないので、静止画のみの判定へ明示的に落とす。
        params = AnalyzeParams(
            mode="static_only", silence_db=params.silence_db,
            min_silence=params.min_silence, still_noise=params.still_noise,
            min_still=params.min_still, min_cut=params.min_cut,
            min_keep=params.min_keep,
        )
    report("静止画・無音の区間を検出中…")
    stderr = run_detection(source, params, has_audio, ffmpeg=ffmpeg)
    duration = float(info["duration"])  # type: ignore[arg-type]

    # 動き・音量の測定は 1 回だけ行い、静止判定と盛り上がり度の両方に使う。
    report("動きの激しさを測定中…")
    motion = bucketize(measure_motion(source, ffmpeg=ffmpeg), duration)
    if has_audio:
        report("音量を測定中…")
    loudness = (
        bucketize(measure_loudness(source, ffmpeg=ffmpeg), duration)
        if has_audio else None
    )

    # 静止 = freezedetect(完全一致に近い)+ 動き量による「ほぼ静止」の補完
    static = parse_freeze(stderr, duration) + low_motion_intervals(
        motion, WINDOW_SECONDS, params.near_still_ydif, params.min_still
    )
    silent = parse_silence(stderr, duration) if has_audio else []
    segments = propose_segments(duration, static, silent, params)

    # 盛り上がり度: 測定済みの特徴量から窓ごとの点数を作り、keep 区間へ
    # 注釈する。重みは学習済みのものが渡されればそれを、無ければ既定。
    report("盛り上がり度を採点中…")
    features = window_features(motion, loudness)
    scores = combine_features(features, weights or ScoreWeights())
    if params.target_seconds is not None:
        segments = trim_to_target(
            segments, scores, params.target_seconds, params.chunk_seconds
        )
    keep_scores = {
        i: range_score(scores, seg.start, seg.end)
        for i, seg in enumerate(segments) if seg.action == "keep"
    }
    segments = [
        seg.replaced(excite=round(keep_scores[i])) if i in keep_scores else seg
        for i, seg in enumerate(segments)
    ]
    segments = mark_highlight(segments, keep_scores)

    if writer is not None:
        report("テロップの下書きをローカル AI に依頼中…")
        segments = draft_telops(segments, writer, hint)

    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    plan = CutPlan(
        # 絶対パスで記録する。「どこから実行したか」で意味が変わるパスを
        # 計画に書くと、cut 側が別の場所を探して見失う(実測したバグ)。
        source_path=str(source.resolve()),
        source_sha256=digest,
        duration=duration,
        width=int(info["width"]),        # type: ignore[arg-type]
        height=int(info["height"]),      # type: ignore[arg-type]
        has_audio=has_audio,
        mode=params.mode,
        segments=tuple(segments),
    )
    plan.save(production_dir / "cutplan.json")
    # 「AI の案そのまま」の控えと、窓ごとの測定値も残す。人が cutplan.json
    # を直したあと、案との差分が学習(learning.py)の教師データになる。
    plan.save(production_dir / "cutplan.proposed.json")
    windows = {
        "window_seconds": WINDOW_SECONDS,
        "duration": duration,
        "features": features,
    }
    (production_dir / "analysis_windows.json").write_text(
        json.dumps(windows, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return plan
