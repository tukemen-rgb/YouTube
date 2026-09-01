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
from videoyard.fonts import resolve_font
from videoyard.render import RenderError, _escape_filter_value, wrap_text

DEFAULT_COUNT = 3
MIN_GAP_SECONDS = 2.0

#: サムネ文字の鉄則(C12): 大きく・少なく・縁取り。
TITLE_MAX_CHARS = 20
TITLE_FONT_RATIO = 0.16      # 画面高さに対する文字サイズ(動画テロップよりずっと大きい)


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


def title_drawtext(text_path: Path, font_path: Path, width: int, height: int) -> str:
    """サムネ用タイトル文字の drawtext。純粋関数(テストで検証)。

    動画テロップと同じ安全経路(textfile + expansion=none)で、見た目は
    サムネ用に大きく: 太い縁取り+半透明の帯、左下寄せ。
    """
    font_size = max(32, int(height * TITLE_FONT_RATIO))
    return (
        f"drawtext=fontfile={_escape_filter_value(str(font_path))}"
        f":textfile={_escape_filter_value(str(text_path))}"
        ":expansion=none"
        ":fontcolor=0xffffff"
        f":fontsize={font_size}"
        f":line_spacing={font_size // 5}"
        f":borderw={max(3, font_size // 12)}:bordercolor=0x000000"
        ":box=1:boxcolor=0x000000@0.35"
        f":boxborderw={max(10, font_size // 4)}"
        f":x={max(20, width // 24)}:y=h-text_h-{max(20, height // 12)}"
    )


def extract_thumbnails(production_dir: Path, count: int = DEFAULT_COUNT,
                       text: str = "", ffmpeg: str = "ffmpeg") -> list[Path]:
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

    title_args: list[str] = []
    if text:
        if len(text) > TITLE_MAX_CHARS:
            raise ThumbsError(
                f"サムネ文字は {TITLE_MAX_CHARS} 文字以内(大きく・少なくが鉄則)。"
                f"{len(text)} 文字は多すぎる。"
            )
        font_path = resolve_font()
        font_size = max(32, int(plan.height * TITLE_FONT_RATIO))
        text_path = out_dir / "title.txt"
        text_path.write_text(wrap_text(text, font_size, plan.width), encoding="utf-8")
        title_args = ["-vf", title_drawtext(text_path, font_path, plan.width, plan.height)]

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
        record = {"rank": rank, "time_seconds": round(time, 3),
                  "excite": round(score), "file": path.name}
        if title_args:
            titled = out_dir / f"thumb_{rank}_titled.png"
            result = subprocess.run(
                [ffmpeg, "-hide_banner", "-nostdin", "-y",
                 "-i", str(path), *title_args, str(titled)],
                capture_output=True, text=True,
            )
            if result.returncode != 0 or not titled.is_file():
                tail = "\n".join(result.stderr.splitlines()[-5:])
                raise ThumbsError(f"タイトル文字の合成が失敗:\n{tail}")
            written.append(titled)
            record["titled_file"] = titled.name
            record["title_text"] = text
        records.append(record)
    (out_dir / "thumbnails.json").write_text(
        json.dumps({"source_sha256": plan.source_sha256, "candidates": records},
                   ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return written
