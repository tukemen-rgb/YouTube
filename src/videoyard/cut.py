"""カット計画の実行 — cutplan.json のとおりに元動画を切ってつなぐ。

判断はしない。analyze(と、それを直した人)が決めた keep 区間を
そのまま切り出し、テロップを載せ、つないで 1 本にするだけ。計画に
無い編集は起きない。

render.py と同じ決まり: コマンドは計画とパスだけから組み立てる純粋
関数、出力は一時ファイル経由で検証後に置く、来歴を manifest に残す。
実行前に元動画のダイジェストを照合し、計画を立てたときと別の動画に
同じ計画を適用してしまう事故を断る(fail-closed)。
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from videoyard.cutplan import CutPlan, PlanSegment
from videoyard.fonts import resolve_font
from videoyard.render import (
    MANIFEST_VERSION,
    RenderError,
    _escape_filter_value,
    _sha256,
    wrap_text,
)

#: テロップの見た目。v0.2 は 1 種類だけ(増やすなら計画の記述に載せる)。
TELOP_FONT_SIZE_RATIO = 0.05   # 画面高さに対する文字サイズ
TELOP_MAX_HEIGHT_RATIO = 0.3   # テロップが占めてよい高さ

#: つなぎ目の音のフェード秒数。切った端の波形が途中で断たれると
#: 「ブツッ」というクリック音になるため、各区間の入りと終わりを
#: この長さだけ滑らかにする。映像はあえてハードカットのまま
#: (ゲーム動画の標準的な編集)。
AUDIO_FADE_SECONDS = 0.15

#: 書き出し時に揃えるラウドネス(LUFS)。YouTube は再生時に約 -14 LUFS
#: へ音量を合わせるため、これより大きい音は自動で下げられ、小さい音は
#: 小さいまま再生される。書き出しで揃えておけば動画ごとの音量のばらつき
#: が出ない。1 パスの loudnorm は固定パラメータなので決定的。
LOUDNESS_TARGET_LUFS = -14.0
LOUDNESS_TRUE_PEAK = -1.5
LOUDNESS_RANGE = 11.0

#: ショート(縦動画)の出力サイズ。YouTube ショートの標準。
VERTICAL_WIDTH = 1080
VERTICAL_HEIGHT = 1920
#: ショートとして推奨される最長秒数(超えても作れるが警告する)。
SHORTS_RECOMMENDED_SECONDS = 60.0


class CutError(RenderError):
    """カットが完了しなかった。出力は残っていない。"""


def telop_font_size(plan: CutPlan) -> int:
    return max(16, int(plan.height * TELOP_FONT_SIZE_RATIO))


def write_telop_files(plan: CutPlan, text_dir: Path) -> dict[int, Path]:
    """keep 区間のテロップを折り返してファイルに書く(index → path)。"""
    text_dir.mkdir(parents=True, exist_ok=True)
    font_size = telop_font_size(plan)
    line_height = font_size + font_size // 4
    paths: dict[int, Path] = {}
    for index, seg in enumerate(plan.segments):
        if seg.action != "keep" or not seg.telop:
            continue
        wrapped = wrap_text(seg.telop, font_size, plan.width)
        lines = wrapped.count("\n") + 1
        if lines * line_height > plan.height * TELOP_MAX_HEIGHT_RATIO:
            raise CutError(
                f"segments[{index}] のテロップが長すぎて画面に収まらない"
                f"({lines} 行)。短くすること。"
            )
        path = text_dir / f"telop_{index:03d}.txt"
        path.write_text(wrapped, encoding="utf-8")
        paths[index] = path
    return paths


def _drawtext(seg: PlanSegment, plan: CutPlan, font_path: Path, text_path: Path) -> str:
    font_size = telop_font_size(plan)
    return (
        f"drawtext=fontfile={_escape_filter_value(str(font_path))}"
        f":textfile={_escape_filter_value(str(text_path))}"
        ":expansion=none"
        ":fontcolor=0xffffff"
        f":fontsize={font_size}"
        f":line_spacing={font_size // 4}"
        ":box=1:boxcolor=0x000000@0.5"
        f":boxborderw={max(6, font_size // 4)}"
        f":x=(w-text_w)/2:y=h-text_h-{max(20, font_size // 2)}"
    )


def build_command(
    plan: CutPlan,
    source: Path,
    font_path: Path,
    telop_paths: dict[int, Path],
    output_path: Path,
    normalize_loudness: bool = True,
    vertical: bool = False,
    fast: bool = False,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """cutplan から ffmpeg の引数列を組み立てる。純粋関数。"""
    keep_indexes = [i for i, s in enumerate(plan.segments) if s.action == "keep"]
    if not keep_indexes:
        raise CutError("keep の区間が無い")

    filters = []
    labels_v = []
    labels_a = []
    for n, index in enumerate(keep_indexes):
        seg = plan.segments[index]
        chain = f"[0:v]trim=start={seg.start}:end={seg.end},setpts=PTS-STARTPTS"
        if index in telop_paths:
            chain += "," + _drawtext(seg, plan, font_path, telop_paths[index])
        filters.append(f"{chain}[v{n}]")
        labels_v.append(f"[v{n}]")
        if plan.has_audio:
            seg_duration = seg.end - seg.start
            fade = min(AUDIO_FADE_SECONDS, seg_duration / 4)
            fade_out_at = round(max(0.0, seg_duration - fade), 3)
            filters.append(
                f"[0:a]atrim=start={seg.start}:end={seg.end},asetpts=PTS-STARTPTS"
                f",afade=t=in:st=0:d={fade}"
                f",afade=t=out:st={fade_out_at}:d={fade}[a{n}]"
            )
            labels_a.append(f"[a{n}]")

    n = len(keep_indexes)
    video_label = "[outv]"
    audio_label = "[outa]"
    if plan.has_audio:
        pairs = "".join(v + a for v, a in zip(labels_v, labels_a, strict=True))
        filters.append(f"{pairs}concat=n={n}:v=1:a=1[outv][outa]")
        if normalize_loudness:
            # loudnorm は内部で 192kHz 化するので、通常のレートへ戻す。
            filters.append(
                f"[outa]loudnorm=I={LOUDNESS_TARGET_LUFS}"
                f":TP={LOUDNESS_TRUE_PEAK}:LRA={LOUDNESS_RANGE}"
                ",aresample=48000[outn]"
            )
            audio_label = "[outn]"
    else:
        filters.append(f"{''.join(labels_v)}concat=n={n}:v=1:a=0[outv]")

    if vertical:
        # ショート(9:16)化。中央クロップは端の UI が欠けるので、定番の
        # 「引き伸ばしてぼかした背景の上に、元映像を幅いっぱいで中央配置」。
        filters.append(
            "[outv]split=2[bg][fg];"
            f"[bg]scale={VERTICAL_WIDTH}:{VERTICAL_HEIGHT}"
            ":force_original_aspect_ratio=increase"
            f",crop={VERTICAL_WIDTH}:{VERTICAL_HEIGHT},boxblur=20:5[bgb];"
            f"[fg]scale={VERTICAL_WIDTH}:-2[fgs];"
            "[bgb][fgs]overlay=(W-w)/2:(H-h)/2[vout]"
        )
        video_label = "[vout]"

    args = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", str(source),
            "-filter_complex", ";".join(filters), "-map", video_label]
    if plan.has_audio:
        args += ["-map", audio_label, "-c:a", "aac", "-b:a", "192k"]
    if fast:
        # 速さ優先(C4/U5): 全コア+高速プリセット。同じ入力から同じ
        # バイト列が出る保証はこのモードでは捨てる(来歴に fast を記録)。
        encode = ["-preset", "veryfast", "-threads", "0"]
    else:
        encode = ["-preset", "medium", "-threads", "1"]
    args += [
        "-c:v", "libx264",
        *encode,
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-fflags", "+bitexact",
        "-flags:v", "+bitexact",
        "-map_metadata", "-1",
        str(output_path),
    ]
    return args


def cut(production_dir: Path, normalize_loudness: bool = True,
        vertical: bool = False, fast: bool = False,
        ffmpeg: str = "ffmpeg") -> dict[str, object]:
    """production ディレクトリの cutplan.json を実行して out/video.mp4 を作る。"""
    plan_path = production_dir / "cutplan.json"
    plan = CutPlan.load(plan_path)
    source = Path(plan.source_path)
    if not source.is_absolute():
        source = production_dir / source
    if not source.is_file():
        raise CutError(f"元動画がない: {source}")
    actual = _sha256(source)
    if actual != plan.source_sha256:
        raise CutError(
            "元動画が計画を立てたときのファイルと一致しない"
            "(中身が変わっている)。analyze をやり直すこと。"
        )
    font_path = resolve_font()
    out_dir = production_dir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    telop_paths = write_telop_files(plan, out_dir / "text")
    final_path = out_dir / "video.mp4"
    tmp_path = out_dir / "video.tmp.mp4"
    tmp_path.unlink(missing_ok=True)

    args = build_command(plan, source, font_path, telop_paths, tmp_path,
                         normalize_loudness=normalize_loudness, vertical=vertical,
                         fast=fast, ffmpeg=ffmpeg)
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0 or not tmp_path.is_file() or tmp_path.stat().st_size == 0:
        tmp_path.unlink(missing_ok=True)
        tail = "\n".join(result.stderr.splitlines()[-15:])
        raise CutError(f"ffmpeg が失敗した(exit={result.returncode}):\n{tail}")

    version_line = subprocess.run(
        [ffmpeg, "-version"], capture_output=True, text=True
    ).stdout.splitlines()
    manifest: dict[str, object] = {
        "manifest_version": MANIFEST_VERSION,
        "rendered_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "ffmpeg_version": version_line[0] if version_line else "unknown",
        "plan_file": "cutplan.json",
        "plan_sha256": _sha256(plan_path),
        "source_path": str(source),
        "source_sha256": actual,
        "font_path": str(font_path),
        "font_sha256": _sha256(font_path),
        "output_sha256": _sha256(tmp_path),
        "output_bytes": tmp_path.stat().st_size,
        "duration_seconds": plan.kept_seconds,
        "vertical": vertical,
        "fast": fast,
        "command": args,
    }
    tmp_path.replace(final_path)
    (out_dir / "render_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest
