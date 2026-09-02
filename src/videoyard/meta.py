"""メタデータ段の一歩目 — チャプターと説明文の下書き(C16)。

カット計画は「出力動画のどの時刻に何が始まるか」を全部知っている。
YouTube の説明欄に貼るチャプター(0:00 目次)と説明文の下書きを、
その情報だけから機械的に組み立てる。文言はテロップと定型見出しのみで、
ここでも発明はしない。

YouTube のチャプター表示の決まり(最初は 0:00、3 個以上、各 10 秒以上)
を満たさない場合は、下書きの中に注として明記する(黙って壊れた目次を
出さない)。
"""

from __future__ import annotations

import json
from pathlib import Path

from videoyard.cutplan import CutPlan

#: YouTube でチャプターが目次として機能する条件。
MIN_CHAPTERS = 3
MIN_CHAPTER_SECONDS = 10.0


def format_timestamp(seconds: float) -> str:
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def output_chapters(plan: CutPlan) -> list[tuple[float, str]]:
    """(出力動画での開始秒, 見出し) の一覧。純粋関数。

    見出しはテロップ、無ければ「シーン n」。時刻は切った分を詰めた
    出力動画の時間軸で数える。
    """
    chapters: list[tuple[float, str]] = []
    cursor = 0.0
    for scene, seg in enumerate(plan.keeps, start=1):
        label = seg.telop if seg.telop else f"シーン {scene}"
        chapters.append((round(cursor, 3), label))
        cursor += seg.end - seg.start
    return chapters


def chapter_warnings(chapters: list[tuple[float, str]],
                     total_seconds: float) -> list[str]:
    """YouTube の目次として機能しない条件を指摘する。純粋関数。"""
    warnings = []
    if len(chapters) < MIN_CHAPTERS:
        warnings.append(
            f"チャプターが {len(chapters)} 個(YouTube の目次表示は "
            f"{MIN_CHAPTERS} 個以上が条件)"
        )
    boundaries = [t for t, _ in chapters] + [total_seconds]
    for (start, label), end in zip(chapters, boundaries[1:], strict=False):
        if end - start < MIN_CHAPTER_SECONDS:
            warnings.append(
                f"「{label}」が {end - start:.0f} 秒"
                f"(各 {MIN_CHAPTER_SECONDS:.0f} 秒以上が条件)"
            )
    return warnings


def build_description(plan: CutPlan, bgm_name: str = "") -> str:
    """説明文の下書き。チャプター+クレジット。文言は発明しない。"""
    chapters = output_chapters(plan)
    lines = ["【チャプター】"]
    lines += [f"{format_timestamp(t)} {label}" for t, label in chapters]
    warnings = chapter_warnings(chapters, plan.kept_seconds)
    if warnings:
        lines.append("")
        lines.append("(注: このままでは YouTube の目次として表示されない: "
                     + " / ".join(warnings) + ")")
    if bgm_name:
        lines += ["", f"BGM: {bgm_name}(権利表記が必要な素材なら、ここに規約どおりの"
                      "クレジットを書くこと)"]
    return "\n".join(lines) + "\n"


def write_description(production_dir: Path) -> Path:
    """out/description.txt を書く。BGM 名は来歴 manifest から拾う。"""
    plan = CutPlan.load(production_dir / "cutplan.json")
    bgm_name = ""
    manifest_path = production_dir / "out" / "render_manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        bgm_path = manifest.get("bgm_path") or ""
        if bgm_path:
            bgm_name = Path(bgm_path).name
    out_path = production_dir / "out" / "description.txt"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(build_description(plan, bgm_name), encoding="utf-8")
    return out_path
