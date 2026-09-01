"""timeline.json → mp4 の決定的レンダリング。

ffmpeg のコマンドラインは ``build_command`` が timeline とファイルパスだけ
から組み立てる純粋関数で、実行せずにテストできる。同じ timeline からは
同じコマンドが、同じコマンドからは同じ mp4 が出る(bitexact 指定)。

出力はまず一時ファイルに書き、検証が通ったときだけ本来の名前に移す。
途中で失敗したら out/video.mp4 は現れない(fail-closed)。

レンダリングのたびに render_manifest.json を残す。入力(timeline・
フォント)と出力のダイジェスト、ffmpeg のバージョン、実行した引数の
記録で、「この mp4 は何から作られたか」に後からファイルで答えるための
来歴(provenance)。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

from videoyard.fonts import resolve_font
from videoyard.timeline import Timeline

MANIFEST_VERSION = 1


class RenderError(RuntimeError):
    """レンダリングが完了しなかった。出力は残っていない。"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _hex_color(value: str) -> str:
    # "#RRGGBB" → ffmpeg の "0xRRGGBB"
    return "0x" + value[1:]


def _escape_filter_value(value: str) -> str:
    # フィルタ記述内の値のエスケープ。パスに : や ' が入っていても
    # (例: Windows の C:\)フィルタ文法を壊さないようにする。
    out = []
    for ch in value:
        if ch in "\\':,;[]":
            out.append("\\")
        out.append(ch)
    return "".join(out)


def _char_em(ch: str) -> float:
    # 全角(日本語など)は 1 文字 ≈ 1em、半角は ≈ 0.55em として幅を見積もる。
    return 1.0 if unicodedata.east_asian_width(ch) in ("W", "F", "A") else 0.55


def wrap_text(text: str, font_size: int, frame_width: int) -> str:
    """画面幅に収まるよう文字を折り返す。

    drawtext は自動折り返しをしない(実測: 長文は左右にはみ出して
    読めない動画が出る)ので、レンダリング側で改行を入れる。日本語は
    分かち書きがないため、幅の見積もりで文字単位に折る。
    """
    usable_em = (frame_width * 0.9) / font_size
    wrapped_lines: list[str] = []
    for line in text.split("\n"):
        current = ""
        current_em = 0.0
        for ch in line:
            em = _char_em(ch)
            if current and current_em + em > usable_em:
                wrapped_lines.append(current)
                current = ch
                current_em = em
            else:
                current += ch
                current_em += em
        wrapped_lines.append(current)
    return "\n".join(wrapped_lines)


def check_vertical_fit(wrapped: str, font_size: int, frame_height: int) -> None:
    """折り返し後の行数が画面の高さに収まらなければ断る(fail-closed)。

    黙って下がはみ出た動画を出すより、その場で「文字が多すぎる」と
    言うほうが直せる。"""
    line_height = font_size + font_size // 4
    lines = wrapped.count("\n") + 1
    if lines * line_height > frame_height * 0.92:
        raise RenderError(
            f"文字が多すぎて画面に収まらない({lines} 行)。"
            "文字を減らすか font_size を小さくすること。"
        )


def write_text_files(timeline: Timeline, text_dir: Path) -> list[Path]:
    """シーンの文字を折り返してから 1 ファイルずつに書く。

    drawtext には文字列を直接渡さずファイル参照(textfile=)で渡す。
    本文にどんな記号や改行があってもフィルタ文法に混ざらないため、
    外部由来のテキストが「命令」になる余地を入口で断てる。
    """
    text_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, scene in enumerate(timeline.scenes):
        wrapped = wrap_text(scene.text, scene.font_size, timeline.width)
        check_vertical_fit(wrapped, scene.font_size, timeline.height)
        path = text_dir / f"scene_{index:03d}.txt"
        path.write_text(wrapped, encoding="utf-8")
        paths.append(path)
    return paths


def build_command(
    timeline: Timeline,
    font_path: Path,
    text_paths: list[Path],
    output_path: Path,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """timeline から ffmpeg の引数列を組み立てる。純粋関数。"""
    if len(text_paths) != len(timeline.scenes):
        raise RenderError("テキストファイル数がシーン数と一致しない")

    args: list[str] = [ffmpeg, "-hide_banner", "-nostdin", "-y"]
    for scene in timeline.scenes:
        source = (
            f"color=c={_hex_color(scene.background)}"
            f":s={timeline.width}x{timeline.height}"
            f":r={timeline.fps}:d={float(scene.duration_seconds)}"
        )
        args += ["-f", "lavfi", "-i", source]

    filters = []
    for index, (scene, text_path) in enumerate(zip(timeline.scenes, text_paths, strict=True)):
        if scene.text == "":
            # 無地の間(ポーズ)。drawtext は空ファイルを嫌うので通さない。
            filters.append(f"[{index}:v]null[v{index}]")
            continue
        drawtext = (
            f"drawtext=fontfile={_escape_filter_value(str(font_path))}"
            f":textfile={_escape_filter_value(str(text_path))}"
            # expansion=none: 本文中の %{...} を drawtext の命令として展開
            # しない。展開を許すと本文の記号次第で描画が壊れる(実測:
            # %{eval:...} 入りの本文で文字が全部消えた)し、外部由来の
            # テキストに描画エンジンへの命令を書けてしまう。
            ":expansion=none"
            f":fontcolor={_hex_color(scene.text_color)}"
            f":fontsize={scene.font_size}"
            f":line_spacing={scene.font_size // 4}"
            ":x=(w-text_w)/2:y=(h-text_h)/2"
        )
        filters.append(f"[{index}:v]{drawtext}[v{index}]")
    joined_inputs = "".join(f"[v{i}]" for i in range(len(timeline.scenes)))
    filters.append(f"{joined_inputs}concat=n={len(timeline.scenes)}:v=1:a=0[out]")

    args += [
        "-filter_complex", ";".join(filters),
        "-map", "[out]",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        # 決定性のため: スレッドを 1 に固定し、時刻などの環境依存
        # メタデータを出力へ書かない。
        "-threads", "1",
        "-fflags", "+bitexact",
        "-flags:v", "+bitexact",
        "-map_metadata", "-1",
        str(output_path),
    ]
    return args


def render(production_dir: Path, ffmpeg: str = "ffmpeg") -> dict[str, object]:
    """production ディレクトリの timeline.json を out/video.mp4 にする。"""
    if shutil.which(ffmpeg) is None:
        raise RenderError(
            f"{ffmpeg} が見つからない。ffmpeg をインストールすること。"
        )
    timeline_path = production_dir / "timeline.json"
    timeline = Timeline.load(timeline_path)
    font_path = resolve_font()

    out_dir = production_dir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    text_paths = write_text_files(timeline, out_dir / "text")
    final_path = out_dir / "video.mp4"
    tmp_path = out_dir / "video.tmp.mp4"
    tmp_path.unlink(missing_ok=True)

    args = build_command(timeline, font_path, text_paths, tmp_path, ffmpeg=ffmpeg)
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0 or not tmp_path.is_file() or tmp_path.stat().st_size == 0:
        tmp_path.unlink(missing_ok=True)
        tail = "\n".join(result.stderr.splitlines()[-15:])
        raise RenderError(f"ffmpeg が失敗した(exit={result.returncode}):\n{tail}")

    version_line = subprocess.run(
        [ffmpeg, "-version"], capture_output=True, text=True
    ).stdout.splitlines()
    manifest: dict[str, object] = {
        "manifest_version": MANIFEST_VERSION,
        "rendered_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "ffmpeg_version": version_line[0] if version_line else "unknown",
        "plan_file": "timeline.json",
        "plan_sha256": _sha256(timeline_path),
        "font_path": str(font_path),
        "font_sha256": _sha256(font_path),
        "output_sha256": _sha256(tmp_path),
        "output_bytes": tmp_path.stat().st_size,
        "duration_seconds": timeline.total_seconds,
        "command": args,
    }
    # 検証が全部通ってから、初めて本来の名前にする。
    tmp_path.replace(final_path)
    manifest_path = out_dir / "render_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest
