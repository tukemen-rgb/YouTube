"""まとめて処理 — 録画フォルダを一晩で片づける(S3)。

競合調査で分かったサービス面の弱点その 3: 実況者は週に何本も録るのに、
videoyard は 1 本ずつ手で回すしかなかった。

batch は録画フォルダの動画を順に auto と同じ流れで処理する。設計の要点:

* **1 本失敗しても止まらない。** 10 本の 7 本目でこけて全部やり直し、が
  いちばん腹立たしい。失敗は記録して次へ進み、最後にまとめて報告する
* **途中から再開できる。** すでに出来ている production は既定で飛ばす
  (`--force` で作り直す)。夜中に走らせて朝に足りない分だけ足せる
* 結果は batch_report.json に残る(何を作り、何がなぜ失敗したか)

判断はしない。1 本ごとの中身は既存の analyze / cut と同じで、ここは
順番に呼ぶだけ。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from videoyard.render import RenderError

#: 対象にする動画の拡張子。
VIDEO_SUFFIXES = (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v")
#: 1 回の batch で扱う上限。事故(巨大フォルダの取り違え)を防ぐ。
MAX_ITEMS = 100


class BatchError(RenderError):
    """まとめて処理が始められない。"""


@dataclass(frozen=True)
class Item:
    """処理 1 本ぶん。source は元動画、directory は作る production。"""

    source: Path
    directory: Path


@dataclass(frozen=True)
class Result:
    """1 本の結果。失敗も結果として残す(黙って消さない)。"""

    source: Path
    directory: Path
    status: str          # done / skipped / failed
    detail: str = ""
    seconds: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "source": str(self.source), "directory": str(self.directory),
            "status": self.status, "detail": self.detail,
            "output_seconds": round(self.seconds, 3),
        }


def collect_videos(sources_dir: Path) -> list[Path]:
    """フォルダから動画をファイル名順に集める。順序は決定的。"""
    if not sources_dir.is_dir():
        raise BatchError(f"録画フォルダがない: {sources_dir}")
    found = sorted(
        p for p in sources_dir.iterdir()
        if p.suffix.lower() in VIDEO_SUFFIXES and p.is_file()
    )
    if not found:
        raise BatchError(
            f"動画が 1 本も無い: {sources_dir}"
            f"(対象の拡張子: {', '.join(VIDEO_SUFFIXES)})"
        )
    if len(found) > MAX_ITEMS:
        raise BatchError(
            f"動画が {len(found)} 本ある(上限 {MAX_ITEMS})。"
            "フォルダを分けるか、選んでから実行すること。"
        )
    return found


def safe_name(source: Path) -> str:
    """元動画のファイル名 → production ディレクトリ名。

    パス区切りや空白で事故らないよう、英数字と一部記号だけ残す。
    日本語のファイル名はそのまま使える(パス区切りだけ落とす)。
    """
    name = "".join(
        "_" if ch in '/\\:*?"<>| \t' else ch
        for ch in source.stem
    ).strip("._")
    return name or "untitled"


def plan_items(videos: Iterable[Path], root: Path) -> list[Item]:
    """動画一覧 → 作る production の割り当て。純粋関数。

    名前がぶつかったら連番を足す(別フォルダの同名ファイル対策)。
    """
    used: set[str] = set()
    items = []
    for video in videos:
        base = safe_name(video)
        name = base
        suffix = 2
        while name in used:
            name = f"{base}_{suffix}"
            suffix += 1
        used.add(name)
        items.append(Item(source=video, directory=root / name))
    return items


def is_done(directory: Path) -> bool:
    """すでに出来上がっているか(再開の判定)。純粋関数に近い読み取りだけ。"""
    return (directory / "out" / "video.mp4").is_file()


def run_batch(items: list[Item], process: Callable[[Item], float],
              force: bool = False,
              progress: Callable[[str], None] | None = None) -> list[Result]:
    """1 本ずつ process を呼ぶ。失敗しても次へ進む。

    process は 1 本を処理して出来上がりの秒数を返す関数。テストでは
    ffmpeg を使わない偽物を渡せる。
    """
    say = progress or (lambda _m: None)
    results = []
    for number, item in enumerate(items, start=1):
        head = f"[{number}/{len(items)}] {item.source.name}"
        if not force and is_done(item.directory):
            say(f"{head} … すでに出来ている(飛ばす)")
            results.append(Result(item.source, item.directory, "skipped",
                                  "out/video.mp4 が既にある"))
            continue
        say(f"{head} … 処理中")
        try:
            seconds = process(item)
        except Exception as exc:  # 1 本の失敗で全体を止めない
            say(f"{head} … 失敗: {exc}")
            results.append(Result(item.source, item.directory, "failed",
                                  f"{type(exc).__name__}: {exc}"))
            continue
        say(f"{head} … 完了({seconds:.1f} 秒)")
        results.append(Result(item.source, item.directory, "done",
                              seconds=seconds))
    return results


#: まとめ行に出す失敗理由の長さ。全文は batch_report.json に残る。
SUMMARY_DETAIL_CHARS = 160


def one_line(text: str, limit: int = SUMMARY_DETAIL_CHARS) -> str:
    """複数行のエラーを 1 行に畳む。純粋関数。

    ffmpeg / ffprobe のエラーは何行も出るので、一覧では 1 行に。
    切り詰めたことは「…」で示し、全文は報告ファイルに残す。
    """
    joined = " ".join(text.split())
    if len(joined) <= limit:
        return joined
    return joined[:limit] + "…(全文は batch_report.json)"


def format_summary(results: list[Result]) -> list[str]:
    """最後にまとめて報告する行。純粋関数。失敗は必ず名指しする。"""
    done = [r for r in results if r.status == "done"]
    skipped = [r for r in results if r.status == "skipped"]
    failed = [r for r in results if r.status == "failed"]
    lines = [
        f"まとめ: 完了 {len(done)} 本 / 飛ばした {len(skipped)} 本 / "
        f"失敗 {len(failed)} 本",
    ]
    if done:
        total = sum(r.seconds for r in done)
        lines.append(f"  作った動画の合計 {total / 60:.1f} 分")
    for result in failed:
        lines.append(f"  失敗: {result.source.name} … {one_line(result.detail)}")
    if failed:
        lines.append("  失敗した分だけもう一度 batch を実行すれば、"
                     "出来ている分は飛ばして続きから進む。")
    return lines


def write_report(root: Path, results: list[Result]) -> Path:
    """batch_report.json を書く。何を作り、何がなぜ失敗したかの記録。"""
    root.mkdir(parents=True, exist_ok=True)
    path = root / "batch_report.json"
    payload = {
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "total": len(results),
        "done": sum(1 for r in results if r.status == "done"),
        "skipped": sum(1 for r in results if r.status == "skipped"),
        "failed": sum(1 for r in results if r.status == "failed"),
        "items": [r.to_dict() for r in results],
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path
