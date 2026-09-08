"""確認用の 1 枚ページ — 出来上がりを一目で見る(S4)。

競合調査で分かったサービス面の弱点その 4: 出来上がりを確かめる手段が
「mp4 を再生する」しかなかった。競合はプレビュー画面を持つ。

review は production の中身を 1 枚の HTML にまとめる:

* 出来た動画(その場で再生できる)
* 盛り上がりグラフ(そのまま埋め込む)
* サムネ候補(クリックで拡大)
* カット計画の全区間と、切った/残した理由
* 説明文の下書きとチャプター

外部の CSS も JavaScript ライブラリも読まない(ネットに繋がなくても
開ける)。動画とサムネは同じフォルダの相対パスで参照するので、
production ごと持ち歩ける。

**テロップや理由の文字列は必ず HTML エスケープする。** 元動画の名前や
人が書いたテロップは「データ」であって、ページへの命令ではない
(drawtext で expansion=none にしているのと同じ考え方)。
"""

from __future__ import annotations

import json
import re
from html import escape
from pathlib import Path

from videoyard.cutplan import SPEED_FACTOR, CutPlan
from videoyard.meta import build_description, format_timestamp, output_chapters
from videoyard.render import RenderError

#: 行動別の見た目。計画の読み方をそのまま色にする。
_ACTION_LABEL = {"keep": "残す", "cut": "切る", "speed": "≫ 倍速"}
#: SVG をそのまま埋め込む前に落とす要素(グラフに script は要らない)。
_SCRIPT_TAG = re.compile(r"<script.*?</script>", re.IGNORECASE | re.DOTALL)

_STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; padding: 24px; font-family: system-ui, -apple-system,
  "Hiragino Sans", "Noto Sans JP", sans-serif; line-height: 1.7;
  max-width: 1000px; margin-inline: auto; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 32px 0 8px; padding-bottom: 4px;
  border-bottom: 1px solid rgba(128,128,128,.35); }
.sub { color: #777; font-size: 13px; margin: 0 0 24px; }
video { width: 100%; border-radius: 8px; background: #000; }
.facts { display: flex; flex-wrap: wrap; gap: 8px 24px; font-size: 13px;
  color: #666; margin: 8px 0 0; padding: 0; list-style: none; }
.graph svg { width: 100%; height: auto; }
.thumbs { display: flex; gap: 12px; flex-wrap: wrap; }
.thumbs figure { margin: 0; width: 220px; }
.thumbs img { width: 100%; border-radius: 6px; display: block; }
.thumbs figcaption { font-size: 12px; color: #777; margin-top: 4px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; padding: 6px 8px;
  border-bottom: 1px solid rgba(128,128,128,.25); vertical-align: top; }
th { font-weight: 600; color: #666; white-space: nowrap; }
td.num { text-align: right; font-variant-numeric: tabular-nums;
  white-space: nowrap; }
tr.cut { color: #999; }
tr.speed td.act { color: #b26a00; }
tr.keep td.act { font-weight: 600; }
.star { color: #d4a017; }
pre { background: rgba(128,128,128,.12); padding: 12px; border-radius: 6px;
  white-space: pre-wrap; font-size: 13px; }
.note { font-size: 12px; color: #777; }
"""


class ReviewError(RenderError):
    """確認ページが作れない。"""


def _seconds(value: float) -> str:
    return f"{value:.1f}"


def plan_rows(plan: CutPlan) -> list[dict[str, str]]:
    """計画 → 表の行。純粋関数(値はまだエスケープしない)。

    出力動画での位置も一緒に出す。「元動画の 3 分 12 秒 = 出来上がりの
    0 分 40 秒」がその場で分かるようにする。
    """
    rows = []
    cursor = 0.0
    for number, seg in enumerate(plan.segments, start=1):
        length = seg.end - seg.start
        if seg.action in ("keep", "speed"):
            out_at = format_timestamp(cursor)
            cursor += length / (SPEED_FACTOR if seg.action == "speed" else 1.0)
        else:
            out_at = "—"
        rows.append({
            "number": str(number),
            "action": seg.action,
            "label": _ACTION_LABEL[seg.action],
            "source": f"{_seconds(seg.start)}〜{_seconds(seg.end)}",
            "length": f"{_seconds(length)} 秒",
            "output_at": out_at,
            "excite": "" if seg.excite is None else str(seg.excite),
            "telop": seg.telop,
            "reason": seg.reason,
        })
    return rows


def sanitize_svg(svg_text: str) -> str:
    """埋め込む SVG から script を落とす。純粋関数。

    自分で書いた SVG しか読まないが、ページに差し込むものを無検査で
    通す経路は作らない(fail-closed の考え方をここにも通す)。
    """
    without_script = _SCRIPT_TAG.sub("", svg_text)
    # XML 宣言はページ中では不要
    return re.sub(r"<\?xml.*?\?>", "", without_script, flags=re.DOTALL).strip()


def thumbnail_caption(src: str, index: int) -> str:
    """サムネ候補の説明。文字入りの版はそう分かるようにする。純粋関数。

    thumbs は 1 枚ごとに素のフレームと文字入りの 2 種を作るので、
    並べただけでは同じ絵が 2 回出ているように見えてしまう。
    """
    stem = Path(src).stem
    number = stem.removeprefix("thumb_").removesuffix("_titled") or str(index)
    if stem.endswith("_titled"):
        return f"候補 {number}(文字入り)"
    return f"候補 {number}"


def build_review_html(plan: CutPlan, title: str, facts: list[str],
                      rows: list[dict[str, str]], chapters: list[tuple[float, str]],
                      description: str, svg_text: str = "",
                      video_src: str = "", thumbnails: list[str] | None = None,
                      ) -> str:
    """確認ページの HTML。純粋関数。文字列はすべてエスケープする。"""
    parts = [
        "<!doctype html>",
        '<html lang="ja"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{escape(title)}</title>",
        f"<style>{_STYLE}</style></head><body>",
        f"<h1>{escape(title)}</h1>",
        '<p class="sub">videoyard の確認ページ。'
        "この 1 枚で、何をどう切ったかと出来上がりを確かめられる。</p>",
    ]

    if video_src:
        parts.append("<h2>出来た動画</h2>")
        parts.append(f'<video controls preload="metadata" src="{escape(video_src)}">'
                     "動画を再生できないブラウザです</video>")
    if facts:
        parts.append('<ul class="facts">')
        parts += [f"<li>{escape(fact)}</li>" for fact in facts]
        parts.append("</ul>")

    if svg_text:
        parts.append("<h2>盛り上がり</h2>")
        parts.append(f'<div class="graph">{sanitize_svg(svg_text)}</div>')

    thumbnails = thumbnails or []
    if thumbnails:
        parts.append("<h2>サムネ候補</h2>")
        parts.append('<div class="thumbs">')
        for index, src in enumerate(thumbnails, start=1):
            caption = thumbnail_caption(src, index)
            parts.append(
                f'<figure><a href="{escape(src)}">'
                f'<img src="{escape(src)}" alt="{escape(caption)}"></a>'
                f"<figcaption>{escape(caption)}</figcaption></figure>"
            )
        parts.append("</div>")

    parts.append("<h2>カット計画</h2>")
    parts.append("<table><thead><tr>"
                 "<th>#</th><th>扱い</th><th>元動画</th><th>長さ</th>"
                 "<th>出来上がり</th><th>盛り上がり</th><th>テロップ</th>"
                 "<th>理由</th></tr></thead><tbody>")
    for row in rows:
        star = ' <span class="star">★</span>' if "★" in row["reason"] else ""
        parts.append(
            f'<tr class="{row["action"]}">'
            f'<td class="num">{escape(row["number"])}</td>'
            f'<td class="act">{escape(row["label"])}{star}</td>'
            f'<td class="num">{escape(row["source"])}</td>'
            f'<td class="num">{escape(row["length"])}</td>'
            f'<td class="num">{escape(row["output_at"])}</td>'
            f'<td class="num">{escape(row["excite"])}</td>'
            f'<td>{escape(row["telop"])}</td>'
            f'<td>{escape(row["reason"])}</td></tr>'
        )
    parts.append("</tbody></table>")
    parts.append(
        '<p class="note">直すときは cutplan.sheet.txt の ○(残す)/ ×(切る)/ '
        "≫(倍速)を書き換えて apply → cut。</p>")

    if chapters:
        parts.append("<h2>チャプター</h2>")
        parts.append("<table><tbody>")
        for at, label in chapters:
            parts.append(f'<tr><td class="num">{escape(format_timestamp(at))}</td>'
                         f"<td>{escape(label)}</td></tr>")
        parts.append("</tbody></table>")

    if description:
        parts.append("<h2>説明文の下書き</h2>")
        parts.append(f"<pre>{escape(description)}</pre>")

    parts.append("</body></html>")
    return "\n".join(parts) + "\n"


def write_review(production_dir: Path) -> Path:
    """out/review.html を書く。無いものは黙って省く(壊れない)。"""
    plan_path = production_dir / "cutplan.json"
    if not plan_path.is_file():
        raise ReviewError(
            f"カット計画がない: {plan_path}(先に analyze を実行すること)")
    plan = CutPlan.load(plan_path)
    out_dir = production_dir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    facts = [
        f"元動画 {_seconds(plan.duration)} 秒 → 出来上がり "
        f"{_seconds(plan.output_seconds)} 秒",
        f"残す {len(plan.keeps)} 区間 / 全 {len(plan.segments)} 区間",
        f"{plan.width}×{plan.height}",
        Path(plan.source_path).name,
    ]
    manifest_path = out_dir / "render_manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("vertical"):
            facts.append("ショート 9:16")
        if manifest.get("bgm_path"):
            facts.append(f"BGM: {Path(str(manifest['bgm_path'])).name}")
        facts.append(f"書き出し {manifest.get('rendered_at', '')}")

    svg_path = production_dir / "excitement.svg"
    svg_text = svg_path.read_text(encoding="utf-8") if svg_path.is_file() else ""

    video_src = "video.mp4" if (out_dir / "video.mp4").is_file() else ""
    thumb_dir = out_dir / "thumbnails"
    thumbnails = [f"thumbnails/{p.name}" for p in sorted(thumb_dir.glob("thumb_*.png"))
                  ] if thumb_dir.is_dir() else []

    description_path = out_dir / "description.txt"
    description = (description_path.read_text(encoding="utf-8")
                   if description_path.is_file() else build_description(plan))

    html = build_review_html(
        plan,
        title=f"{Path(plan.source_path).stem} の確認",
        facts=facts,
        rows=plan_rows(plan),
        chapters=output_chapters(plan),
        description=description,
        svg_text=svg_text,
        video_src=video_src,
        thumbnails=thumbnails,
    )
    path = out_dir / "review.html"
    path.write_text(html, encoding="utf-8")
    return path
