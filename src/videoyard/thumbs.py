"""サムネイル候補の抽出 — 盛り上がり度の高い瞬間を静止画にする。

YouTube では動画そのものよりサムネイルが再生数を左右する。せっかく
窓ごとの盛り上がり度を測っているので、その上位の瞬間を元動画から
静止画として切り出し、サムネ選びの候補にする(metadata 段の一歩目)。

決まりごと:

* 抽出元は出力動画ではなく**元動画**(再エンコード前の画質で切り出す)。
* 候補は keep 区間の中からだけ選ぶ(切った場面をサムネにしない)。
* 近すぎる瞬間は避ける(min_gap)。ほぼ同じ絵が 3 枚出ても選べない。
* どの秒のフレームをなぜ選んだか(点数)を thumbnails.json に記録する。
  選定は analysis_windows.json の測定値だけから決まる純粋関数で、
  同じ分析からは同じ候補が出る。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from videoyard.cutplan import CutPlan
from videoyard.excitement import combine_features
from videoyard.render import RenderError

DEFAULT_COUNT = 3
MIN_GAP_SECONDS = 2.0


class ThumbsError(RenderError):
    """サムネイル抽出が完了しなかった。"""


def pick_thumbnail_times(
    scores: list[float],
    keeps: list[tuple[float, float]],
    window: float,
    count: int = DEFAULT_COUNT,
    min_gap: float = MIN_GAP_SECONDS,
) -> list[tuple[float, float]]:
    """盛り上がり度上位の (時刻, 点数) を点数順に返す。純粋関数。"""
    candidates = []
    for i, score in enumerate(scores):
        center = (i + 0.5) * window
        if any(start <= center < end for start, end in keeps):
            candidates.append((score, center))
    candidates.sort(key=lambda c: (-c[0], c[1]))
    chosen: list[tuple[float, float]] = []
    for score, center in candidates:
        if all(abs(center - t) >= min_gap for t, _ in chosen):
            chosen.append((center, score))
        if len(chosen) >= count:
            break
    return chosen


def extract_thumbnails(production_dir: Path, count: int = DEFAULT_COUNT,
                       ffmpeg: str = "ffmpeg") -> list[Path]:
    """cutplan と分析結果からサムネ候補を out/thumbnails/ に書く。"""
    windows_path = production_dir / "analysis_windows.json"
    if not windows_path.is_file():
        return []  # v0.1 系(timeline 生成)の production には分析が無い
    plan = CutPlan.load(production_dir / "cutplan.json")
    source = Path(plan.source_path)
    if not source.is_absolute():
        source = production_dir / source
    if not source.is_file():
        raise ThumbsError(f"元動画がない: {source}")

    windows = json.loads(windows_path.read_text(encoding="utf-8"))
    scores = combine_features(windows["features"])
    window_seconds = float(windows["window_seconds"])
    keeps = [(s.start, s.end) for s in plan.keeps]
    picks = pick_thumbnail_times(scores, keeps, window_seconds, count=count)
    if not picks:
        return []

    out_dir = production_dir / "out" / "thumbnails"
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    records = []
    for rank, (time, score) in enumerate(picks, start=1):
        path = out_dir / f"thumb_{rank}.png"
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-nostdin", "-y",
             "-ss", f"{time:.3f}", "-i", str(source),
             "-frames:v", "1", str(path)],
            capture_output=True, text=True,
        )
        if result.returncode != 0 or not path.is_file():
            tail = "\n".join(result.stderr.splitlines()[-5:])
            raise ThumbsError(f"フレーム抽出が失敗(t={time:.1f}s):\n{tail}")
        written.append(path)
        records.append({"rank": rank, "time_seconds": round(time, 3),
                        "excite": round(score), "file": path.name})
    (out_dir / "thumbnails.json").write_text(
        json.dumps({"source_sha256": plan.source_sha256, "candidates": records},
                   ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return written
