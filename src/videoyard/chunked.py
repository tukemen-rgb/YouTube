"""長い動画を分けて書き出す — メモリで死なないための経路(S11)。

1 時間の録画から 5 分のダイジェストを作ろうとして、**ffmpeg が
メモリ不足で強制終了した**(実測: exit=-9、16GB の機械で 98 区間)。

原因は区間の数ではなく、**出力全体がメモリに載る**こと。
filter_complex で N 個の区間を concat すると、後ろの区間の絵は
「自分の番が来るまで」キューに溜まる。デコーダは先へ進むので、
出力のほぼ全部が生のフレームとして積み上がる:

    5 分 × 30fps × 1280×720 × 1.5 バイト ≒ 12 GB

対処は区間ごとに分けて書き出すこと。しかも **入力側シーク**
(`-ss <開始> -i 元動画 -t <長さ>`)で、その区間だけをデコードする。
これで 2 つ効く:

* メモリは 1 区間ぶんしか要らない(録画がどれだけ長くても止まらない)
* デコードもその区間だけ。区間ごとに 1 時間ぶん読み直さないので速い
  (束にまとめる方式は、束ごとに全編デコードするため遅く、しかも
  1.4GB 相当の見積りでも実測では落ちた)

区間ファイルは無劣化結合(-c copy)でつなぐ。**全体に掛けるもの**
(ノイズ除去・BGM・音量正規化・縦変換)は結合のときにまとめて掛ける。
区間ごとに掛けると音が段になるため。
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from videoyard.cut import (
    AUDIO_FADE_SECONDS,
    BGM_DEFAULT_GAIN_DB,
    BGM_FADE_OUT_SECONDS,
    DENOISE_LEVELS,
    LOUDNESS_RANGE,
    LOUDNESS_TARGET_LUFS,
    LOUDNESS_TRUE_PEAK,
    VERTICAL_HEIGHT,
    VERTICAL_WIDTH,
    VIDEO_FADE_SECONDS,
    CutError,
    _drawtext,
    write_telop_files,
)
from videoyard.cutplan import SPEED_FACTOR, CutPlan
from videoyard.fonts import resolve_font
from videoyard.render import MANIFEST_VERSION, _sha256

#: 生フレーム 1 画素あたりのバイト数(yuv420p は 1.5)。
BYTES_PER_PIXEL = 1.5
#: 1 度に抱えてよい生フレームの量。実測で 12GB 相当が 16GB の機械を
#: 殺したので、余裕をみて 1.5GB。小さいほど安全だが束が増える。
MEMORY_BUDGET_BYTES = 1.5 * 1024 ** 3
#: 見積りに使うフレームレート。実測より大きめに見ておく(安全側)。
ASSUMED_FPS = 30


def estimated_buffer_bytes(plan: CutPlan, fps: int = ASSUMED_FPS) -> float:
    """1 本のフィルタで書き出したときに抱える生フレームの量。純粋関数。"""
    return plan.output_seconds * fps * plan.width * plan.height * BYTES_PER_PIXEL


def needs_chunking(plan: CutPlan, budget: float = MEMORY_BUDGET_BYTES,
                   fps: int = ASSUMED_FPS) -> bool:
    """分けて書き出すべきか。純粋関数。"""
    return estimated_buffer_bytes(plan, fps) > budget


def render_indexes(plan: CutPlan) -> list[int]:
    """出力に現れる区間の index。純粋関数。"""
    return [i for i, s in enumerate(plan.segments)
            if s.action in ("keep", "speed")]


def build_segment_command(plan: CutPlan, index: int, position: int,
                          source: Path, font_path: Path,
                          text_path: Path | None, out_path: Path,
                          transition: str, fast: bool, count: int = 0,
                          telop_style: str = "band",
                          ffmpeg: str = "ffmpeg") -> list[str]:
    """区間 1 つを **入力側シーク** で切り出す引数列。純粋関数。

    `-ss` を `-i` の前に置くと、その時刻へ直接飛んでからデコードする。
    区間ごとに元動画を頭から読み直さないので、長い録画でも速い。
    時刻は区間の先頭を 0 とした相対になるので、フェードもそれに合わせる。
    """
    seg = plan.segments[index]
    length = seg.end - seg.start
    out_length = length / SPEED_FACTOR if seg.action == "speed" else length

    chain = "[0:v]setpts=PTS-STARTPTS"
    if seg.action == "speed":
        chain += f",setpts=PTS/{SPEED_FACTOR:g}"
    if text_path is not None:
        chain += "," + _drawtext(seg, plan, font_path, text_path,
                                 telop_style=telop_style)
    if transition == "dip":
        # つなぎ目だけ演出する。動画の頭と尻には入れない(cut 本体と同じ)
        fade = min(VIDEO_FADE_SECONDS, out_length / 4)
        if position > 0:
            chain += f",fade=t=in:st=0:d={fade}"
        if count and position < count - 1:
            fade_at = round(max(0.0, out_length - fade), 3)
            chain += f",fade=t=out:st={fade_at}:d={fade}"
    chain += ",setsar=1[v]"

    filters = [chain]
    maps = ["-map", "[v]"]
    if plan.has_audio:
        achain = "[0:a]asetpts=PTS-STARTPTS"
        if seg.action == "speed":
            achain += ",atempo=2.0,atempo=2.0"
        fade = min(AUDIO_FADE_SECONDS, out_length / 4)
        fade_at = round(max(0.0, out_length - fade), 3)
        achain += (f",afade=t=in:st=0:d={fade}"
                   f",afade=t=out:st={fade_at}:d={fade}[a]")
        filters.append(achain)
        maps += ["-map", "[a]", "-c:a", "aac", "-b:a", "192k"]

    encode = (["-preset", "veryfast", "-threads", "0"] if fast
              else ["-preset", "medium", "-threads", "1"])
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-y",
        "-ss", f"{seg.start:.3f}", "-i", str(source), "-t", f"{length:.3f}",
        "-filter_complex", ";".join(filters), *maps,
        "-c:v", "libx264", *encode, "-crf", "20", "-pix_fmt", "yuv420p",
        "-fflags", "+bitexact", "-flags:v", "+bitexact",
        "-map_metadata", "-1", str(out_path),
    ]


def build_join_command(batch_files: list[Path], list_file: Path,
                       output_path: Path, plan: CutPlan,
                       normalize_loudness: bool, vertical: bool, fast: bool,
                       bgm: Path | None, bgm_gain_db: float,
                       denoise: str = "none",
                       ffmpeg: str = "ffmpeg") -> list[str]:
    """束の mp4 をつなぎ、全体に掛けるもの(縦変換・BGM・音量)を掛ける。

    純粋関数(list_file は呼び出し側が書く)。縦変換をしないなら映像は
    無劣化コピーなので速く、画質も落ちない。
    """
    args = [ffmpeg, "-hide_banner", "-nostdin", "-y",
            "-f", "concat", "-safe", "0", "-i", str(list_file)]
    if bgm is not None:
        args += ["-stream_loop", "-1", "-i", str(bgm)]

    filters: list[str] = []
    video_map = ["-map", "0:v"]
    if vertical:
        filters.append(
            "[0:v]split=2[bg][fg];"
            f"[bg]scale={VERTICAL_WIDTH}:{VERTICAL_HEIGHT}"
            ":force_original_aspect_ratio=increase"
            f",crop={VERTICAL_WIDTH}:{VERTICAL_HEIGHT},boxblur=20:5[bgb];"
            f"[fg]scale={VERTICAL_WIDTH}:-2[fgs];"
            "[bgb][fgs]overlay=(W-w)/2:(H-h)/2[vout]"
        )
        video_map = ["-map", "[vout]"]

    audio_label: str | None = "0:a" if plan.has_audio else None
    if audio_label is not None and denoise != "none":
        if denoise not in DENOISE_LEVELS:
            raise CutError(
                f"denoise は {tuple(DENOISE_LEVELS)} のどれか: {denoise}")
        filters.append(
            f"[{audio_label}]" + ",".join(DENOISE_LEVELS[denoise]) + "[aclean]")
        audio_label = "aclean"
    if bgm is not None:
        total = round(plan.output_seconds, 3)
        fade_start = round(max(0.0, total - BGM_FADE_OUT_SECONDS), 3)
        filters.append(
            f"[1:a]atrim=0:{total},asetpts=PTS-STARTPTS"
            f",volume={bgm_gain_db}dB"
            f",afade=t=out:st={fade_start}:d={BGM_FADE_OUT_SECONDS}[bgma]"
        )
        if audio_label is not None:
            filters.append(
                f"[{audio_label}][bgma]amix=inputs=2:duration=first"
                ":normalize=0[mixa]")
            audio_label = "mixa"
        else:
            audio_label = "bgma"
    if audio_label is not None and normalize_loudness:
        filters.append(
            f"[{audio_label}]loudnorm=I={LOUDNESS_TARGET_LUFS}"
            f":TP={LOUDNESS_TRUE_PEAK}:LRA={LOUDNESS_RANGE}"
            ",aresample=48000[outn]")
        audio_label = "outn"

    if filters:
        args += ["-filter_complex", ";".join(filters)]
    args += video_map
    if audio_label is not None:
        label = f"[{audio_label}]" if not audio_label.startswith("0:") else audio_label
        args += ["-map", label, "-c:a", "aac", "-b:a", "192k"]
    if vertical:
        encode = (["-preset", "veryfast", "-threads", "0"] if fast
                  else ["-preset", "medium", "-threads", "1"])
        args += ["-c:v", "libx264", *encode, "-crf", "20", "-pix_fmt", "yuv420p"]
    else:
        args += ["-c:v", "copy"]   # 束はもう正しい絵。焼き直さない
    args += ["-fflags", "+bitexact", "-flags:v", "+bitexact",
             "-map_metadata", "-1", str(output_path)]
    return args


def _run(args: list[str], what: str) -> None:
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        tail = "\n".join(result.stderr.splitlines()[-15:])
        raise CutError(f"{what} が失敗した(exit={result.returncode}):\n{tail}")


def cut_chunked(production_dir: Path, normalize_loudness: bool = True,
                vertical: bool = False, fast: bool = False,
                bgm: Path | None = None,
                bgm_gain_db: float = BGM_DEFAULT_GAIN_DB,
                transition: str = "none", denoise: str = "none",
                telop_style: str = "band",
                progress=None, ffmpeg: str = "ffmpeg") -> dict[str, object]:
    """分けて書き出してから結合する。長い出力でもメモリが破裂しない。"""
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
            "(中身が変わっている)。analyze をやり直すこと。")
    if bgm is not None and not bgm.is_file():
        raise CutError(f"BGM ファイルがない: {bgm}")

    say = progress or (lambda _m: None)
    font_path = resolve_font()
    out_dir = production_dir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "parts"
    work.mkdir(parents=True, exist_ok=True)
    for stale in work.glob("*.mp4"):
        stale.unlink()

    telop_paths = write_telop_files(plan, out_dir / "text", vertical=vertical)
    indexes = render_indexes(plan)
    say(f"出力が長いので区間ごとに書き出す({len(indexes)} 区間 / 推定 "
        f"{estimated_buffer_bytes(plan) / 1024 ** 3:.1f} GB 相当を一度に"
        "抱えないため)")

    batch_files: list[Path] = []
    for position, index in enumerate(indexes):
        if position % 10 == 0 or position == len(indexes) - 1:
            say(f"[{position + 1}/{len(indexes)}] 区間を書き出し中…")
        path = work / f"part_{position:04d}.mp4"
        args = build_segment_command(
            plan, index, position, source, font_path,
            telop_paths.get(index), path, transition=transition, fast=fast,
            count=len(indexes), telop_style=telop_style, ffmpeg=ffmpeg)
        _run(args, f"{position + 1} 区間目の書き出し")
        batch_files.append(path)

    say("束をつないで仕上げ中…")
    list_file = work / "parts.txt"
    list_file.write_text(
        "".join(f"file '{p}'\n" for p in batch_files), encoding="utf-8")
    final_path = out_dir / "video.mp4"
    tmp_path = out_dir / "video.tmp.mp4"
    tmp_path.unlink(missing_ok=True)
    join_args = build_join_command(
        batch_files, list_file, tmp_path, plan,
        normalize_loudness=normalize_loudness, vertical=vertical, fast=fast,
        bgm=bgm, bgm_gain_db=bgm_gain_db, denoise=denoise, ffmpeg=ffmpeg)
    _run(join_args, "束の結合")
    if not tmp_path.is_file() or tmp_path.stat().st_size == 0:
        raise CutError("結合の結果が空だった")

    version_line = subprocess.run(
        [ffmpeg, "-version"], capture_output=True, text=True
    ).stdout.splitlines()
    manifest: dict[str, object] = {
        "manifest_version": MANIFEST_VERSION,
        "rendered_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "ffmpeg_version": version_line[0] if version_line else "unknown",
        "mode": "chunked",
        "parts": len(batch_files),
        "plan_file": "cutplan.json",
        "plan_sha256": _sha256(plan_path),
        "source_path": str(source),
        "source_sha256": actual,
        "font_path": str(font_path),
        "font_sha256": _sha256(font_path),
        "output_sha256": _sha256(tmp_path),
        "output_bytes": tmp_path.stat().st_size,
        "duration_seconds": plan.output_seconds,
        "vertical": vertical,
        "fast": fast,
        "bgm_path": str(bgm) if bgm is not None else "",
        "bgm_sha256": _sha256(bgm) if bgm is not None else "",
        "bgm_gain_db": bgm_gain_db if bgm is not None else None,
        "transition": transition,
        "denoise": denoise,
        "telop_style": telop_style,
        "command": join_args,
    }
    tmp_path.replace(final_path)
    (out_dir / "render_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    return manifest


__all__ = [
    "MEMORY_BUDGET_BYTES",
    "build_join_command",
    "build_segment_command",
    "cut_chunked",
    "estimated_buffer_bytes",
    "needs_chunking",
    "render_indexes",
]
