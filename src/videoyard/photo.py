"""写真スライドショー — 写真一式から動画を作る(U18)。

旅行やイベントで「写真しかない」場合の入口。動画編集(cutplan)と
同じ分担で作る:

* photo コマンドが写真の並びから photoplan.json(人が読める計画)を書く
* 人が計画の秒数・テロップ・並びを直す(初回はそのまま作ってよい)
* レンダリングは計画だけから決まる(同じ計画 → 同じ動画)

演出は「ぼかし背景+中央配置」と、写真ごとに交互のゆっくりズーム
(Ken Burns)。テロップは動画テロップと同じ安全経路(textfile +
expansion=none)。EXIF は読まない — 位置情報などが動画に漏れる経路を
最初から作らない。写真はローカルで処理され、どこにも送られない。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from videoyard.cut import (
    BGM_FADE_OUT_SECONDS,
    VERTICAL_HEIGHT,
    VERTICAL_WIDTH,
)
from videoyard.fonts import resolve_font
from videoyard.render import (
    MANIFEST_VERSION,
    RenderError,
    _escape_filter_value,
    _sha256,
    wrap_text,
)
from videoyard.safezone import bottom_text_margin, text_wrap_width

PHOTO_FORMAT_VERSION = 1
MAX_PHOTOS = 200
MAX_TELOP_CHARS = 120
#: 1 枚あたりの既定秒数。短すぎると読めず、長すぎるとだれる。
DEFAULT_SECONDS_PER_PHOTO = 4.0
FPS = 30
#: ズームの振れ幅(1.0 → 1.08)。目立たない程度に「動画感」を出す。
ZOOM_MAX = 1.08
#: 横動画の既定キャンバス。
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 720
PHOTO_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")
TELOP_FONT_RATIO = 0.05


class PhotoError(RenderError):
    """写真スライドショーが作れない。出力は残っていない。"""


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise PhotoError(message)


@dataclass(frozen=True)
class PhotoScene:
    """写真 1 枚ぶんのシーン。file は photoplan.json からの相対パスも可。"""

    file: str
    sha256: str
    seconds: float = DEFAULT_SECONDS_PER_PHOTO
    telop: str = ""

    def __post_init__(self) -> None:
        _require(bool(self.file), "file は必須")
        _require(len(self.sha256) == 64, "sha256 は 16 進 64 文字")
        _require(0.5 <= self.seconds <= 60.0, "seconds は 0.5〜60")
        _require(isinstance(self.telop, str) and len(self.telop) <= MAX_TELOP_CHARS,
                 f"telop は {MAX_TELOP_CHARS} 文字以内")

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "file": self.file, "sha256": self.sha256, "seconds": self.seconds,
        }
        if self.telop:
            data["telop"] = self.telop
        return data


@dataclass(frozen=True)
class PhotoPlan:
    scenes: tuple[PhotoScene, ...]
    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    format_version: int = PHOTO_FORMAT_VERSION

    def __post_init__(self) -> None:
        _require(self.format_version == PHOTO_FORMAT_VERSION,
                 f"format_version は {PHOTO_FORMAT_VERSION}")
        _require(1 <= len(self.scenes) <= MAX_PHOTOS,
                 f"scenes は 1〜{MAX_PHOTOS} 枚")
        _require(self.width > 0 and self.height > 0, "width / height は正")

    @property
    def total_seconds(self) -> float:
        return sum(s.seconds for s in self.scenes)

    def to_json(self) -> str:
        data = {
            "format_version": self.format_version,
            "width": self.width,
            "height": self.height,
            "scenes": [s.to_dict() for s in self.scenes],
        }
        return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_dict(cls, data: object) -> PhotoPlan:
        _require(isinstance(data, dict), "photoplan はオブジェクト")
        assert isinstance(data, dict)
        unknown = set(data) - {"format_version", "width", "height", "scenes"}
        _require(not unknown, f"未知のキー: {sorted(unknown)}")
        raw_scenes = data.get("scenes")
        _require(isinstance(raw_scenes, list), "scenes は配列")
        assert isinstance(raw_scenes, list)
        scenes = []
        for i, raw in enumerate(raw_scenes):
            _require(isinstance(raw, dict), f"scenes[{i}] はオブジェクト")
            bad = set(raw) - {"file", "sha256", "seconds", "telop"}
            _require(not bad, f"scenes[{i}] の未知のキー: {sorted(bad)}")
            scenes.append(PhotoScene(**raw))
        rest = {k: v for k, v in data.items() if k != "scenes"}
        return cls(scenes=tuple(scenes), **rest)  # type: ignore[arg-type]

    @classmethod
    def load(cls, path: Path) -> PhotoPlan:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise PhotoError(f"photoplan がない: {path}") from None
        except json.JSONDecodeError as exc:
            raise PhotoError(f"photoplan が JSON として読めない: {exc}") from exc
        return cls.from_dict(data)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")


def collect_photos(photos_dir: Path) -> list[Path]:
    """フォルダから写真をファイル名順に集める。順序は決定的。"""
    if not photos_dir.is_dir():
        raise PhotoError(f"写真フォルダがない: {photos_dir}")
    found = sorted(
        p for p in photos_dir.iterdir()
        if p.suffix.lower() in PHOTO_SUFFIXES and p.is_file()
    )
    if not found:
        raise PhotoError(
            f"写真が 1 枚も無い: {photos_dir}(対応: {', '.join(PHOTO_SUFFIXES)})"
        )
    return found


def build_photo_plan(photos: list[Path], seconds: float = DEFAULT_SECONDS_PER_PHOTO,
                     vertical: bool = False) -> PhotoPlan:
    """写真の並びから計画の案を作る。テロップは発明しない(空のまま)。"""
    width, height = ((VERTICAL_WIDTH, VERTICAL_HEIGHT) if vertical
                     else (DEFAULT_WIDTH, DEFAULT_HEIGHT))
    scenes = tuple(
        PhotoScene(file=str(p), sha256=_sha256(p), seconds=seconds)
        for p in photos
    )
    return PhotoPlan(scenes=scenes, width=width, height=height)


def _scene_filter(index: int, scene: PhotoScene, plan: PhotoPlan,
                  telop_path: Path | None, font_path: Path) -> str:
    """写真 1 枚 → ぼかし背景+中央配置+交互ズーム(+テロップ)。"""
    w, h = plan.width, plan.height
    frames = max(2, round(scene.seconds * FPS))
    # 偶数枚目はズームイン、奇数枚目はズームアウト(単調にならないように)
    if index % 2 == 0:
        zoom = f"1+{ZOOM_MAX - 1:.3f}*on/{frames - 1}"
    else:
        zoom = f"{ZOOM_MAX:.3f}-{ZOOM_MAX - 1:.3f}*on/{frames - 1}"
    chain = (
        f"[{index}:v]split=2[bg{index}][fg{index}];"
        f"[bg{index}]scale={w}:{h}:force_original_aspect_ratio=increase"
        f",crop={w}:{h},boxblur=20:5[bgb{index}];"
        f"[fg{index}]scale={w}:{h}:force_original_aspect_ratio=decrease[fgs{index}];"
        f"[bgb{index}][fgs{index}]overlay=(W-w)/2:(H-h)/2"
        f",zoompan=z='{zoom}':d={frames}:x='iw/2-(iw/zoom/2)'"
        f":y='ih/2-(ih/zoom/2)':s={w}x{h}:fps={FPS}"
    )
    if telop_path is not None:
        font_size = max(16, int(h * TELOP_FONT_RATIO))
        # 縦画面ではスマホ UI に隠れない高さへ上げる(S5)
        margin = bottom_text_margin(w, h, minimum=max(20, font_size // 2))
        chain += (
            f",drawtext=fontfile={_escape_filter_value(str(font_path))}"
            f":textfile={_escape_filter_value(str(telop_path))}"
            ":expansion=none:fontcolor=0xffffff"
            f":fontsize={font_size}:line_spacing={font_size // 4}"
            ":box=1:boxcolor=0x000000@0.5"
            f":boxborderw={max(6, font_size // 4)}"
            f":x=(w-text_w)/2:y=h-text_h-{margin}"
        )
    chain += f",format=yuv420p[s{index}]"
    return chain


def write_photo_telops(plan: PhotoPlan, text_dir: Path,
                       font_path: Path) -> dict[int, Path]:
    text_dir.mkdir(parents=True, exist_ok=True)
    font_size = max(16, int(plan.height * TELOP_FONT_RATIO))
    paths: dict[int, Path] = {}
    for index, scene in enumerate(plan.scenes):
        if not scene.telop:
            continue
        # 右のボタン列に食い込まない幅で折り返す(S5)
        wrapped = wrap_text(scene.telop, font_size,
                            text_wrap_width(plan.width, plan.height))
        path = text_dir / f"phototelop_{index:03d}.txt"
        path.write_text(wrapped, encoding="utf-8")
        paths[index] = path
    return paths


def build_slideshow_command(
    plan: PhotoPlan,
    base_dir: Path,
    font_path: Path,
    telop_paths: dict[int, Path],
    output_path: Path,
    bgm: Path | None = None,
    bgm_gain_db: float = -16.0,
    fast: bool = False,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """photoplan から ffmpeg の引数列を組み立てる。純粋関数。"""
    args = [ffmpeg, "-hide_banner", "-nostdin", "-y"]
    for scene in plan.scenes:
        path = Path(scene.file)
        if not path.is_absolute():
            path = base_dir / path
        args += ["-i", str(path)]
    if bgm is not None:
        args += ["-stream_loop", "-1", "-i", str(bgm)]

    filters = [
        _scene_filter(i, scene, plan, telop_paths.get(i), font_path)
        for i, scene in enumerate(plan.scenes)
    ]
    labels = "".join(f"[s{i}]" for i in range(len(plan.scenes)))
    filters.append(f"{labels}concat=n={len(plan.scenes)}:v=1:a=0[outv]")

    audio_label = None
    if bgm is not None:
        total = round(plan.total_seconds, 3)
        fade_start = round(max(0.0, total - BGM_FADE_OUT_SECONDS), 3)
        filters.append(
            f"[{len(plan.scenes)}:a]atrim=0:{total},asetpts=PTS-STARTPTS"
            f",volume={bgm_gain_db}dB"
            f",afade=t=out:st={fade_start}:d={BGM_FADE_OUT_SECONDS}[bgma]"
        )
        audio_label = "[bgma]"

    args += ["-filter_complex", ";".join(filters), "-map", "[outv]"]
    if audio_label is not None:
        args += ["-map", audio_label, "-c:a", "aac", "-b:a", "192k"]
    encode = (["-preset", "veryfast", "-threads", "0"] if fast
              else ["-preset", "medium", "-threads", "1"])
    args += [
        "-c:v", "libx264", *encode, "-crf", "20", "-r", str(FPS),
        "-pix_fmt", "yuv420p",
        "-fflags", "+bitexact", "-flags:v", "+bitexact",
        "-map_metadata", "-1",  # EXIF ごと落とす(位置情報を出力に残さない)
        str(output_path),
    ]
    return args


def render_slideshow(production_dir: Path, bgm: Path | None = None,
                     bgm_gain_db: float = -16.0, fast: bool = False,
                     ffmpeg: str = "ffmpeg") -> dict[str, object]:
    """production の photoplan.json からスライドショーを書き出す。"""
    plan_path = production_dir / "photoplan.json"
    plan = PhotoPlan.load(plan_path)
    for i, scene in enumerate(plan.scenes):
        path = Path(scene.file)
        if not path.is_absolute():
            path = production_dir / path
        if not path.is_file():
            raise PhotoError(f"写真がない: {path}")
        if _sha256(path) != scene.sha256:
            raise PhotoError(
                f"scenes[{i}] の写真が計画を立てたときと違う: {path}。"
                "photo をやり直すこと。"
            )
    if bgm is not None and not bgm.is_file():
        raise PhotoError(f"BGM ファイルがない: {bgm}")

    font_path = resolve_font()
    out_dir = production_dir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    telop_paths = write_photo_telops(plan, out_dir / "text", font_path)
    final_path = out_dir / "video.mp4"
    tmp_path = out_dir / "video.tmp.mp4"
    tmp_path.unlink(missing_ok=True)

    args = build_slideshow_command(plan, production_dir, font_path, telop_paths,
                                   tmp_path, bgm=bgm, bgm_gain_db=bgm_gain_db,
                                   fast=fast, ffmpeg=ffmpeg)
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0 or not tmp_path.is_file() or tmp_path.stat().st_size == 0:
        tmp_path.unlink(missing_ok=True)
        tail = "\n".join(result.stderr.splitlines()[-15:])
        raise PhotoError(f"ffmpeg が失敗した(exit={result.returncode}):\n{tail}")

    manifest: dict[str, object] = {
        "manifest_version": MANIFEST_VERSION,
        "rendered_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "plan_file": "photoplan.json",
        "plan_sha256": _sha256(plan_path),
        "photo_count": len(plan.scenes),
        "font_path": str(font_path),
        "font_sha256": _sha256(font_path),
        "output_sha256": _sha256(tmp_path),
        "output_bytes": tmp_path.stat().st_size,
        "duration_seconds": plan.total_seconds,
        "fast": fast,
        "bgm_path": str(bgm) if bgm is not None else "",
        "bgm_sha256": _sha256(bgm) if bgm is not None else "",
        "bgm_gain_db": bgm_gain_db if bgm is not None else None,
        "command": args,
    }
    tmp_path.replace(final_path)
    (out_dir / "render_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def scene_with_telop(plan: PhotoPlan, index: int, telop: str) -> PhotoPlan:
    """index 枚目のテロップを差し替えた新しい計画。CLI とテストの補助。"""
    _require(0 <= index < len(plan.scenes), f"scenes[{index}] は存在しない")
    scenes = tuple(
        replace(s, telop=telop) if i == index else s
        for i, s in enumerate(plan.scenes)
    )
    return replace(plan, scenes=scenes)
