"""差分再エンコード(U5)— 変わっていない区間のエンコードを再利用する。

計画を少し直して cut をやり直すたび全部を再エンコードするのは、長尺の
録画では待ち時間の大半を占める。そこで:

1. keep 区間 1 つを「映像だけの小さな mp4」として単独でエンコードし、
   **内容キー**(元動画の指紋+区間の秒範囲+テロップの文字と様式+
   つなぎ目フェードの有無+エンコード設定)で out/segments/ に置く。
2. 次回の cut では、内容キーが同じ区間はエンコードせずファイルを再利用。
3. 区間ファイル群を concat デマルチプレクサで**無劣化結合**(-c copy)し、
   音声(atrim/フェード/BGM/ラウドネス)は従来どおり元動画から作り直して
   多重化する。音声のエンコードは映像に比べ十分軽い。

決まりごと:

* 内容キーに関わる要素が 1 つでも変われば別ファイルになる(古い絵が
  混ざる事故は起きない)。キャッシュの正しさは「キー=内容」で保証する。
* --vertical とは併用できない(縦変換は結合後の全体に掛けるため、
  区間単位の再利用と両立しない)。正直にエラーで断る。
* 通常の cut と同じエンコード設定を使うので、画質は変わらない。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from videoyard.cut import (
    AUDIO_FADE_SECONDS,
    BGM_DEFAULT_GAIN_DB,
    BGM_FADE_OUT_SECONDS,
    LOUDNESS_RANGE,
    LOUDNESS_TARGET_LUFS,
    LOUDNESS_TRUE_PEAK,
    TRANSITIONS,
    VIDEO_FADE_SECONDS,
    CutError,
    _drawtext,
    write_telop_files,
)
from videoyard.cutplan import CutPlan
from videoyard.fonts import resolve_font
from videoyard.render import MANIFEST_VERSION, _sha256

#: エンコード設定の指紋。設定を変えたら必ず値を変える(古いキャッシュと
#: 混ざらないように)。
_ENCODER_TAG = "x264-crf20"


def _encode_args(fast: bool) -> list[str]:
    if fast:
        return ["-preset", "veryfast", "-threads", "0"]
    return ["-preset", "medium", "-threads", "1"]


def segment_fades(position: int, keep_count: int, seg_seconds: float,
                  transition: str) -> tuple[float, float]:
    """(フェードイン秒, フェードアウト秒)。cut 本体の dip と同じ規則。"""
    if transition != "dip" or keep_count <= 1:
        return (0.0, 0.0)
    fade = min(VIDEO_FADE_SECONDS, seg_seconds / 4)
    fade_in = fade if position > 0 else 0.0
    fade_out = fade if position < keep_count - 1 else 0.0
    return (fade_in, fade_out)


def segment_key(plan: CutPlan, index: int, position: int, keep_count: int,
                telop_text: str, transition: str, fast: bool) -> str:
    """区間の内容キー。これが同じ = 出来上がる絵が同じ。"""
    seg = plan.segments[index]
    fade_in, fade_out = segment_fades(position, keep_count, seg.end - seg.start,
                                      transition)
    payload = {
        "source_sha256": plan.source_sha256,
        "start": round(seg.start, 3),
        "end": round(seg.end, 3),
        "telop": telop_text,           # 折り返し後の実際の文字
        "telop_pos": seg.telop_pos,
        "telop_color": seg.telop_color,
        "fade_in": fade_in,
        "fade_out": fade_out,
        "width": plan.width,
        "height": plan.height,
        "encoder": _ENCODER_TAG,
        "fast": fast,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def build_segment_command(plan: CutPlan, index: int, position: int, keep_count: int,
                          source: Path, font_path: Path, text_path: Path | None,
                          out_path: Path, transition: str, fast: bool,
                          ffmpeg: str = "ffmpeg") -> list[str]:
    """keep 区間 1 つを映像のみでエンコードする引数列。純粋関数。"""
    seg = plan.segments[index]
    chain = f"[0:v]trim=start={seg.start}:end={seg.end},setpts=PTS-STARTPTS"
    if text_path is not None:
        chain += "," + _drawtext(seg, plan, font_path, text_path)
    fade_in, fade_out = segment_fades(position, keep_count, seg.end - seg.start,
                                      transition)
    if fade_in:
        chain += f",fade=t=in:st=0:d={fade_in}"
    if fade_out:
        fade_out_at = round(max(0.0, (seg.end - seg.start) - fade_out), 3)
        chain += f",fade=t=out:st={fade_out_at}:d={fade_out}"
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", str(source),
        "-filter_complex", f"{chain}[v]", "-map", "[v]", "-an",
        "-c:v", "libx264", *_encode_args(fast),
        "-crf", "20", "-pix_fmt", "yuv420p",
        "-fflags", "+bitexact", "-flags:v", "+bitexact",
        "-map_metadata", "-1", str(out_path),
    ]


def build_audio_command(plan: CutPlan, source: Path, out_path: Path,
                        normalize_loudness: bool, bgm: Path | None,
                        bgm_gain_db: float, ffmpeg: str = "ffmpeg") -> list[str] | None:
    """音声だけを元動画から作る引数列。音が無く BGM も無ければ None。

    フィルタの規則(区間フェード・BGM・ラウドネス)は cut 本体と同じ。
    """
    keep_indexes = [i for i, s in enumerate(plan.segments) if s.action == "keep"]
    filters: list[str] = []
    labels: list[str] = []
    if plan.has_audio:
        for n, index in enumerate(keep_indexes):
            seg = plan.segments[index]
            seg_duration = seg.end - seg.start
            fade = min(AUDIO_FADE_SECONDS, seg_duration / 4)
            fade_out_at = round(max(0.0, seg_duration - fade), 3)
            filters.append(
                f"[0:a]atrim=start={seg.start}:end={seg.end},asetpts=PTS-STARTPTS"
                f",afade=t=in:st=0:d={fade}"
                f",afade=t=out:st={fade_out_at}:d={fade}[a{n}]"
            )
            labels.append(f"[a{n}]")
        filters.append(f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1[outa]")
        audio_label = "[outa]"
    else:
        audio_label = None

    if bgm is not None:
        kept = round(plan.kept_seconds, 3)
        fade_start = round(max(0.0, kept - BGM_FADE_OUT_SECONDS), 3)
        filters.append(
            f"[1:a]atrim=0:{kept},asetpts=PTS-STARTPTS"
            f",volume={bgm_gain_db}dB"
            f",afade=t=out:st={fade_start}:d={BGM_FADE_OUT_SECONDS}[bgma]"
        )
        if audio_label is not None:
            filters.append(
                f"{audio_label}[bgma]amix=inputs=2:duration=first:normalize=0[mixa]"
            )
            audio_label = "[mixa]"
        else:
            audio_label = "[bgma]"

    if audio_label is None:
        return None
    if normalize_loudness:
        filters.append(
            f"{audio_label}loudnorm=I={LOUDNESS_TARGET_LUFS}"
            f":TP={LOUDNESS_TRUE_PEAK}:LRA={LOUDNESS_RANGE}"
            ",aresample=48000[outn]"
        )
        audio_label = "[outn]"

    args = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", str(source)]
    if bgm is not None:
        args += ["-stream_loop", "-1", "-i", str(bgm)]
    args += ["-filter_complex", ";".join(filters), "-map", audio_label, "-vn",
             "-c:a", "aac", "-b:a", "192k", "-fflags", "+bitexact",
             "-map_metadata", "-1", str(out_path)]
    return args


def _run(args: list[str], what: str) -> None:
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        tail = "\n".join(result.stderr.splitlines()[-8:])
        raise CutError(f"{what}が失敗した(exit={result.returncode}):\n{tail}")


def cut_incremental(production_dir: Path, normalize_loudness: bool = True,
                    fast: bool = False, bgm: Path | None = None,
                    bgm_gain_db: float = BGM_DEFAULT_GAIN_DB,
                    transition: str = "none",
                    ffmpeg: str = "ffmpeg") -> dict[str, object]:
    """差分再エンコードで out/video.mp4 を作る。"""
    if transition not in TRANSITIONS:
        raise CutError(f"transition は {TRANSITIONS} のどれか: {transition}")
    plan_path = production_dir / "cutplan.json"
    plan = CutPlan.load(plan_path)
    source = Path(plan.source_path)
    if not source.is_absolute():
        source = production_dir / source
    if not source.is_file():
        raise CutError(f"元動画がない: {source}")
    if _sha256(source) != plan.source_sha256:
        raise CutError(
            "元動画が計画を立てたときのファイルと一致しない。analyze をやり直すこと。"
        )
    if bgm is not None and not bgm.is_file():
        raise CutError(f"BGM ファイルがない: {bgm}")

    font_path = resolve_font()
    out_dir = production_dir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    telop_paths = write_telop_files(plan, out_dir / "text")

    keep_indexes = [i for i, s in enumerate(plan.segments) if s.action == "keep"]
    if not keep_indexes:
        raise CutError("keep の区間が無い")
    cache_dir = out_dir / "segments"
    cache_dir.mkdir(parents=True, exist_ok=True)

    encoded = 0
    reused = 0
    segment_files: list[Path] = []
    for position, index in enumerate(keep_indexes):
        text_path = telop_paths.get(index)
        telop_text = text_path.read_text(encoding="utf-8") if text_path else ""
        key = segment_key(plan, index, position, len(keep_indexes),
                          telop_text, transition, fast)
        seg_path = cache_dir / f"{key}.mp4"
        if seg_path.is_file() and seg_path.stat().st_size > 0:
            reused += 1
        else:
            args = build_segment_command(plan, index, position, len(keep_indexes),
                                         source, font_path, text_path, seg_path,
                                         transition, fast, ffmpeg=ffmpeg)
            _run(args, f"区間 {index} のエンコード")
            encoded += 1
        segment_files.append(seg_path)

    # 無劣化結合(全区間が同じエンコード設定なので -c copy でつながる)
    list_path = out_dir / "segments.txt"
    list_path.write_text(
        "".join(f"file '{p.resolve()}'\n" for p in segment_files), encoding="utf-8"
    )
    video_only = out_dir / "video_only.tmp.mp4"
    _run([ffmpeg, "-hide_banner", "-nostdin", "-y",
          "-f", "concat", "-safe", "0", "-i", str(list_path),
          "-c", "copy", "-fflags", "+bitexact", str(video_only)], "区間の結合")

    audio_args = build_audio_command(plan, source, out_dir / "audio.tmp.m4a",
                                     normalize_loudness, bgm, bgm_gain_db,
                                     ffmpeg=ffmpeg)
    tmp_path = out_dir / "video.tmp.mp4"
    if audio_args is None:
        video_only.replace(tmp_path)
    else:
        _run(audio_args, "音声の生成")
        _run([ffmpeg, "-hide_banner", "-nostdin", "-y",
              "-i", str(video_only), "-i", str(out_dir / "audio.tmp.m4a"),
              "-map", "0:v", "-map", "1:a", "-c", "copy",
              "-fflags", "+bitexact", str(tmp_path)], "映像と音声の多重化")
        video_only.unlink(missing_ok=True)
        (out_dir / "audio.tmp.m4a").unlink(missing_ok=True)
    if not tmp_path.is_file() or tmp_path.stat().st_size == 0:
        raise CutError("出力が空になった")

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
        "source_sha256": plan.source_sha256,
        "font_path": str(font_path),
        "font_sha256": _sha256(font_path),
        "output_sha256": _sha256(tmp_path),
        "output_bytes": tmp_path.stat().st_size,
        "duration_seconds": plan.kept_seconds,
        "vertical": False,
        "fast": fast,
        "bgm_path": str(bgm) if bgm is not None else "",
        "bgm_sha256": _sha256(bgm) if bgm is not None else "",
        "bgm_gain_db": bgm_gain_db if bgm is not None else None,
        "transition": transition,
        "mode": "incremental",
        "encoded_segments": encoded,
        "reused_segments": reused,
    }
    tmp_path.replace(out_dir / "video.mp4")
    (out_dir / "render_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest
