"""編集ソフトへの持ち出し — EDL / FCPXML 書き出し(S1)。

競合調査(docs/MARKET_RESEARCH.md)で分かった最大のサービス面の穴:
auto-editor / AutoCut / Descript / Eddie AI は、AI が作った編集を
EDL・FCPXML でタイムラインごと Premiere / DaVinci Resolve / Final Cut に
渡せる。「AI で粗く切って、仕上げは使い慣れたソフトで」というプロの
本流の使い方が、mp4 しか吐けない videoyard ではできなかった。

うちの構造とはむしろ相性がいい: cutplan.json は元から「どこを使うか」の
決定リスト(= EDL そのもの)で、変換すれば済む。動画を作り直す必要も、
外部サービスに送る必要もない。

* EDL は CMX3600 形式(業界の最大公約数。Resolve / Premiere が読む)
* FCPXML は Final Cut Pro / Resolve が読む XML(テロップ名や倍速も運べる)
* 倍速(≫)区間は EDL では M2 モーション記録、FCPXML では timeMap で表す

時刻はフレーム単位に丸める(編集ソフトはフレームより細かい編集点を
持てない)。丸めた分のずれは書き出し時に注記する。
"""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import quoteattr

from videoyard.cutplan import SPEED_FACTOR, CutPlan
from videoyard.render import RenderError

#: 既定のフレームレート。元動画の実 fps が分かるならそれを渡す。
DEFAULT_FPS = 30
#: CMX3600 のイベント番号は 3 桁まで。cutplan の MAX_SEGMENTS(500)が
#: これを下回るので、実際に超えることはない(テストで縛っている)。
EDL_MAX_EVENTS = 999
#: EDL のリール名。ファイルベース素材の慣例で AX(auxiliary)。
EDL_REEL = "AX"


class ExportError(RenderError):
    """編集ソフト向けの書き出しができない。"""


def to_frames(seconds: float, fps: int) -> int:
    """秒 → フレーム番号。編集点はフレームに丸める。"""
    return int(round(seconds * fps))


def timecode(frame: int, fps: int) -> str:
    """フレーム番号 → HH:MM:SS:FF(ノンドロップフレーム)。"""
    if frame < 0:
        raise ExportError(f"負のフレーム番号: {frame}")
    hours, rest = divmod(frame, fps * 3600)
    minutes, rest = divmod(rest, fps * 60)
    seconds, frames = divmod(rest, fps)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}"


def _record_frames(action: str, source_frames: int) -> int:
    """素材で使う長さ → 出力タイムラインで占める長さ(倍速は縮む)。"""
    if action == "speed":
        return max(1, int(round(source_frames / SPEED_FACTOR)))
    return source_frames


def to_edl(plan: CutPlan, title: str = "videoyard", fps: int = DEFAULT_FPS,
           reel: str = EDL_REEL) -> str:
    """カット計画 → CMX3600 形式の EDL。純粋関数。

    1 区間 = 1 イベント。音声のある動画は V と A2(ステレオ)の両方を
    まとめて扱う B トラックとして書く(業界の標準的な書き方)。
    """
    renders = plan.renders
    if not renders:
        raise ExportError("出力する区間が無い")
    clip_name = Path(plan.source_path).name
    track = "B" if plan.has_audio else "V"  # B = 映像+音声

    lines = [f"TITLE: {title}", "FCM: NON-DROP FRAME", ""]
    record = 0
    for number, seg in enumerate(renders, start=1):
        src_in = to_frames(seg.start, fps)
        src_out = to_frames(seg.end, fps)
        src_len = max(1, src_out - src_in)
        rec_len = _record_frames(seg.action, src_len)
        lines.append(
            f"{number:03d}  {reel:<8} {track:<5} C        "
            f"{timecode(src_in, fps)} {timecode(src_in + src_len, fps)} "
            f"{timecode(record, fps)} {timecode(record + rec_len, fps)}"
        )
        if seg.action == "speed":
            # M2: モーション記録。速度は fps × 倍率で表す慣例。
            lines.append(
                f"M2   {reel:<8}       {fps * SPEED_FACTOR:>11.1f}"
                f"                {timecode(src_in, fps)}"
            )
        lines.append(f"* FROM CLIP NAME: {clip_name}")
        if seg.telop:
            # テロップは EDL では運べないのでコメントとして残す
            lines.append(f"* COMMENT: {seg.telop}")
        lines.append("")
        record += rec_len
    return "\n".join(lines) + "\n"


def _rational(frame: int, fps: int) -> str:
    """FCPXML の有理数時刻表記。フレーム境界に必ず乗る。"""
    return f"{frame}/{fps}s"


def to_fcpxml(plan: CutPlan, fps: int = DEFAULT_FPS,
              project_name: str = "videoyard digest") -> str:
    """カット計画 → FCPXML(version 1.9)。純粋関数。

    テロップは各クリップの name に入れる(編集ソフト上で区間の目印に
    なる)。倍速区間は timeMap で「出力の時間 → 素材の時間」を書く。
    """
    renders = plan.renders
    if not renders:
        raise ExportError("出力する区間が無い")
    source = Path(plan.source_path)
    if not source.is_absolute():
        raise ExportError(
            f"FCPXML には元動画の絶対パスが要る: {source}"
            "(analyze をやり直すと絶対パスで記録される)"
        )
    total_frames = to_frames(plan.duration, fps)
    out_frames = 0
    clips = []
    for seg in renders:
        src_in = to_frames(seg.start, fps)
        src_len = max(1, to_frames(seg.end, fps) - src_in)
        rec_len = _record_frames(seg.action, src_len)
        name = seg.telop or f"{seg.start:.1f}s"
        clip = (
            f'          <asset-clip ref="r2" offset={quoteattr(_rational(out_frames, fps))}'
            f' name={quoteattr(name)}'
            f' start={quoteattr(_rational(src_in, fps))}'
            f' duration={quoteattr(_rational(rec_len, fps))}'
            f' format="r1"'
        )
        if seg.action == "speed":
            clip += ">\n"
            clip += (
                f'            <timeMap>\n'
                f'              <timept time="0s" value="0s" interp="linear"/>\n'
                f'              <timept time={quoteattr(_rational(rec_len, fps))}'
                f' value={quoteattr(_rational(src_len, fps))} interp="linear"/>\n'
                f'            </timeMap>\n'
                f'          </asset-clip>'
            )
        else:
            clip += "/>"
        clips.append(clip)
        out_frames += rec_len

    return _assemble(plan, fps, project_name, source, total_frames,
                     out_frames, clips, "1" if plan.has_audio else "0")


def _assemble(plan: CutPlan, fps: int, project_name: str, source: Path,
              total_frames: int, out_frames: int, clips: list[str],
              has_audio: str) -> str:
    """FCPXML の外枠。素材(asset)を 1 つ置き、その上に区間を並べる。"""
    head = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!DOCTYPE fcpxml>\n"
        '<fcpxml version="1.9">\n'
        "  <resources>\n"
        f'    <format id="r1" frameDuration="1/{fps}s"'
        f' width="{plan.width}" height="{plan.height}"/>\n'
        f'    <asset id="r2" name={quoteattr(source.stem)} start="0s"'
        f' duration={quoteattr(_rational(total_frames, fps))}'
        f' hasVideo="1" hasAudio="{has_audio}" format="r1">\n'
        '      <media-rep kind="original-media"'
        f' src={quoteattr(source.as_uri())}/>\n'
        "    </asset>\n"
        "  </resources>\n"
        "  <library>\n"
        f'    <event name={quoteattr("videoyard")}>\n'
        f"      <project name={quoteattr(project_name)}>\n"
        '        <sequence format="r1"'
        f' duration={quoteattr(_rational(out_frames, fps))}'
        ' tcStart="0s" tcFormat="NDF">\n'
        "        <spine>\n"
    )
    tail = (
        "        </spine>\n"
        "        </sequence>\n"
        "      </project>\n"
        "    </event>\n"
        "  </library>\n"
        "</fcpxml>\n"
    )
    return head + "\n".join(clips) + "\n" + tail


def rounding_note(plan: CutPlan, fps: int) -> str:
    """フレーム丸めで生じるずれの注記。黙って値を変えない。"""
    worst = 0.0
    for seg in plan.renders:
        for value in (seg.start, seg.end):
            worst = max(worst, abs(value - to_frames(value, fps) / fps))
    if worst < 1e-9:
        return ""
    return (f"編集点を {fps}fps のフレームに丸めた"
            f"(最大 {worst * 1000:.0f} ミリ秒のずれ)")


def write_exports(production_dir: Path, fps: int = DEFAULT_FPS,
                  title: str = "videoyard") -> list[Path]:
    """out/edit.edl と out/edit.fcpxml を書く。動画は作らない。"""
    plan = CutPlan.load(production_dir / "cutplan.json")
    out_dir = production_dir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    edl_path = out_dir / "edit.edl"
    edl_path.write_text(to_edl(plan, title=title, fps=fps), encoding="utf-8")
    written.append(edl_path)
    xml_path = out_dir / "edit.fcpxml"
    xml_path.write_text(to_fcpxml(plan, fps=fps, project_name=title),
                        encoding="utf-8")
    written.append(xml_path)
    return written


__all__ = [
    "DEFAULT_FPS",
    "ExportError",
    "rounding_note",
    "timecode",
    "to_edl",
    "to_fcpxml",
    "to_frames",
    "write_exports",
]
