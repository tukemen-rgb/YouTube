"""○×編集シート — JSON を触らずにカット計画を直すための紙(U2)。

cutplan.json の手編集は JSON を知らない人には無理、という指摘への答え。
analyze が計画と一緒に cutplan.sheet.txt を書く:

    # ○=残す ×=切る。行頭の記号だけ書き換える。テロップは | の後ろ。
    × 1 00:00.0-00:03.0
    ○ 2 00:03.0-00:08.0 | シーン 1

人は行頭の ○/× とテロップ文字列だけを書き換え、apply コマンドが
cutplan.json に反映する。時刻と番号は照合用で、書き換えても効かない
(区間の切れ目を変えたいときだけ cutplan.json を直す)。

シートはあくまで入力の別形式で、正はこれまでどおり cutplan.json。
apply は「シート → 計画」の変換をして保存するだけで、検証は既存の
CutPlan がやる(全部×にすれば「残す区間が無い」と断られる)。
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from videoyard.cutplan import CutPlan, CutPlanError, PlanSegment

_KEEP_MARKS = {"○", "◯", "o", "O", "〇"}
_CUT_MARKS = {"×", "x", "X", "☓", "✕"}

_LINE = re.compile(
    r"^\s*(?P<mark>\S)\s+(?P<index>\d+)\s+\S+"  # 記号 番号 時刻(照合用)
    r"(?:\s*\|\s*(?P<telop>.*))?$"              # | テロップ(任意)
)


class SheetError(ValueError):
    """編集シートが読めない・計画と合っていない。"""


def _clock(seconds: float) -> str:
    minutes = int(seconds) // 60
    return f"{minutes:02d}:{seconds - minutes * 60:04.1f}"


def write_sheet(plan: CutPlan) -> str:
    """計画 → 人が直すためのシート文字列。"""
    lines = [
        "# ○=残す ×=切る。行頭の記号だけ書き換える。テロップは | の後ろ。",
        "# 番号と時刻は照合用(書き換えても効かない)。反映: python -m videoyard apply <dir>",
    ]
    for number, seg in enumerate(plan.segments, start=1):
        mark = "○" if seg.action == "keep" else "×"
        line = f"{mark} {number} {_clock(seg.start)}-{_clock(seg.end)}"
        if seg.telop:
            line += f" | {seg.telop}"
        if seg.excite is not None:
            line += f"   # 盛り上がり度{seg.excite}"
        lines.append(line)
    return "\n".join(lines) + "\n"


def parse_sheet(text: str) -> dict[int, tuple[bool, str]]:
    """シート → {番号: (残すか, テロップ)}。"""
    entries: dict[int, tuple[bool, str]] = {}
    for line_number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if not line.strip():
            continue
        m = _LINE.match(line)
        if not m:
            raise SheetError(f"{line_number} 行目が読めない: {raw!r}")
        mark = m.group("mark")
        if mark in _KEEP_MARKS:
            keep = True
        elif mark in _CUT_MARKS:
            keep = False
        else:
            raise SheetError(f"{line_number} 行目の記号が ○/× でない: {mark!r}")
        index = int(m.group("index"))
        if index in entries:
            raise SheetError(f"番号 {index} が 2 回出てくる")
        entries[index] = (keep, (m.group("telop") or "").strip())
    if not entries:
        raise SheetError("シートに区間の行が 1 つも無い")
    return entries


def apply_sheet(plan: CutPlan, text: str) -> CutPlan:
    """シートの ○/× とテロップを計画へ反映した新しい計画を返す。

    区間の数・並びは変えない(番号がずれていたらエラーで止める)。
    """
    entries = parse_sheet(text)
    expected = set(range(1, len(plan.segments) + 1))
    if set(entries) != expected:
        missing = sorted(expected - set(entries))
        extra = sorted(set(entries) - expected)
        detail = []
        if missing:
            detail.append(f"足りない番号: {missing}")
        if extra:
            detail.append(f"計画に無い番号: {extra}")
        raise SheetError("シートと計画の区間が合わない。" + " / ".join(detail))

    segments: list[PlanSegment] = []
    for number, seg in enumerate(plan.segments, start=1):
        keep, telop = entries[number]
        action = "keep" if keep else "cut"
        changes: dict[str, object] = {}
        if action != seg.action:
            changes["action"] = action
            changes["reason"] = seg.reason + "(シートで変更)" if seg.reason else "シートで変更"
        if keep:
            if telop != seg.telop:
                changes["telop"] = telop
        elif seg.telop:
            changes["telop"] = ""  # 切る区間にテロップは残さない
        segments.append(replace(seg, **changes) if changes else seg)
    try:
        return replace(plan, segments=tuple(segments))
    except CutPlanError as exc:
        raise SheetError(f"シートの内容では計画が成立しない: {exc}") from exc


def sheet_path(production_dir: Path) -> Path:
    return production_dir / "cutplan.sheet.txt"
