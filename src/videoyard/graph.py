"""盛り上がり度グラフ — 採点と切り貼りを 1 枚の絵にする(U12)。

数字の羅列では「どこが山で、どこを切ったのか」が直感的に分からない。
analyze のたびに excitement.svg を書き出す:

* 折れ線 = 窓ごとの盛り上がり度(0〜100)
* 網掛け = 切る区間(cut)
* ★ = 盛り上がり度が最大の keep 区間

依存ゼロの自前 SVG 生成(文字列を組み立てるだけ)。ブラウザで開ける。
配色は 1 種類に固定し、明示的に塗る(環境で見た目が変わらない)。
"""

from __future__ import annotations

from xml.sax.saxutils import escape

from videoyard.cutplan import CutPlan

WIDTH = 960
HEIGHT = 240
MARGIN_LEFT = 44
MARGIN_RIGHT = 16
MARGIN_TOP = 28
MARGIN_BOTTOM = 32

_BG = "#101820"
_GRID = "#39414d"
_LINE = "#5ec8f2"
_CUT = "#000000"
_TEXT = "#d7dde5"
_STAR = "#ffd166"


def _x(time: float, duration: float) -> float:
    span = WIDTH - MARGIN_LEFT - MARGIN_RIGHT
    return MARGIN_LEFT + span * (time / duration)


def _y(score: float) -> float:
    span = HEIGHT - MARGIN_TOP - MARGIN_BOTTOM
    return MARGIN_TOP + span * (1.0 - score / 100.0)


def _time_step(duration: float) -> int:
    for step in (5, 10, 30, 60, 120, 300, 600):
        if duration / step <= 12:
            return step
    return 1200


def excitement_svg(scores: list[float], window: float, plan: CutPlan) -> str:
    """窓ごとの点数とカット計画 → SVG 文字列。純粋関数。"""
    duration = max(plan.duration, window * max(1, len(scores)))
    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
        f'viewBox="0 0 {WIDTH} {HEIGHT}" font-family="sans-serif" font-size="12">',
        f'<rect width="{WIDTH}" height="{HEIGHT}" fill="{_BG}"/>',
        f'<text x="{MARGIN_LEFT}" y="18" fill="{_TEXT}">'
        f'盛り上がり度(0〜100)と切り貼り — 網掛け=切る区間 / ★=最高点</text>',
    ]

    # 切る区間の網掛け
    for seg in plan.segments:
        if seg.action != "cut":
            continue
        x0, x1 = _x(seg.start, duration), _x(seg.end, duration)
        parts.append(
            f'<rect x="{x0:.1f}" y="{MARGIN_TOP}" width="{max(0.5, x1 - x0):.1f}" '
            f'height="{HEIGHT - MARGIN_TOP - MARGIN_BOTTOM}" '
            f'fill="{_CUT}" fill-opacity="0.45"/>'
        )

    # 目盛り(横: 点数 / 縦: 秒)
    for score in (0, 50, 100):
        y = _y(score)
        parts.append(f'<line x1="{MARGIN_LEFT}" y1="{y:.1f}" x2="{WIDTH - MARGIN_RIGHT}" '
                     f'y2="{y:.1f}" stroke="{_GRID}" stroke-width="1"/>')
        parts.append(f'<text x="6" y="{y + 4:.1f}" fill="{_TEXT}">{score}</text>')
    step = _time_step(duration)
    tick = 0
    while tick <= duration:
        x = _x(tick, duration)
        parts.append(f'<line x1="{x:.1f}" y1="{HEIGHT - MARGIN_BOTTOM}" x2="{x:.1f}" '
                     f'y2="{HEIGHT - MARGIN_BOTTOM + 4}" stroke="{_GRID}"/>')
        parts.append(f'<text x="{x:.1f}" y="{HEIGHT - 10}" fill="{_TEXT}" '
                     f'text-anchor="middle">{tick}s</text>')
        tick += step

    # 点数の折れ線
    if scores:
        points = " ".join(
            f"{_x((i + 0.5) * window, duration):.1f},{_y(s):.1f}"
            for i, s in enumerate(scores)
        )
        parts.append(f'<polyline points="{points}" fill="none" stroke="{_LINE}" '
                     f'stroke-width="2"/>')

    # ★ = 最高点の keep 区間の中央
    starred = [s for s in plan.keeps if "★" in s.reason]
    for seg in starred:
        center = (seg.start + seg.end) / 2
        score = seg.excite if seg.excite is not None else 100
        parts.append(f'<text x="{_x(center, duration):.1f}" '
                     f'y="{_y(score) - 8:.1f}" fill="{_STAR}" '
                     f'text-anchor="middle" font-size="16">★</text>')

    # keep 区間のテロップ(あれば)を下端に小さく
    for seg in plan.keeps:
        if not seg.telop:
            continue
        center = (seg.start + seg.end) / 2
        label = seg.telop if len(seg.telop) <= 8 else seg.telop[:7] + "…"
        parts.append(f'<text x="{_x(center, duration):.1f}" y="{MARGIN_TOP - 4}" '
                     f'fill="{_TEXT}" text-anchor="middle" font-size="10">'
                     f'{escape(label)}</text>')

    parts.append("</svg>")
    return "\n".join(parts) + "\n"
