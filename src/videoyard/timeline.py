"""タイムライン = 編集内容の完全な記述。

動画の見た目を決める情報はすべて timeline.json にあり、レンダリングは
この記述以外の何も参照しない。だから「なぜこの動画はこうなったか」は
常にファイルで答えられるし、同じ timeline.json からは同じ動画が出る。

v0.1 のシーンは「単色背景+中央の文字」だけ。素材画像・音声はこの上に
段階計画どおり後の版で足す(足すときも記述はこのファイルに増える)。

sidra-ai の Provenance と同じく、検証は構築時に行う。不正な値を持った
Timeline オブジェクトは存在できない。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

FORMAT_VERSION = 1

#: 上限。誤設定や暴走生成で巨大なレンダリングが走らないための入口検査。
MAX_SCENES = 200
MAX_SCENE_SECONDS = 120.0
MAX_TOTAL_SECONDS = 1200.0  # 20 分
MAX_TEXT_CHARS = 500

_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


class TimelineError(ValueError):
    """timeline.json が記述として成立していない。"""


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise TimelineError(message)


@dataclass(frozen=True)
class Scene:
    """単色背景に文字を置いた 1 カット。"""

    text: str
    duration_seconds: float
    background: str = "#101820"
    text_color: str = "#f5f5f5"
    font_size: int = 56

    def __post_init__(self) -> None:
        _require(isinstance(self.text, str), "text は文字列")
        _require(len(self.text) <= MAX_TEXT_CHARS, f"text は {MAX_TEXT_CHARS} 文字以内")
        _require(
            isinstance(self.duration_seconds, (int, float))
            and 0 < float(self.duration_seconds) <= MAX_SCENE_SECONDS,
            f"duration_seconds は 0 より大きく {MAX_SCENE_SECONDS} 以下",
        )
        for name, value in (("background", self.background), ("text_color", self.text_color)):
            _require(bool(_HEX_COLOR.match(value)), f"{name} は #RRGGBB 形式")
        _require(
            isinstance(self.font_size, int) and 8 <= self.font_size <= 200,
            "font_size は 8〜200 の整数",
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "duration_seconds": float(self.duration_seconds),
            "background": self.background,
            "text_color": self.text_color,
            "font_size": self.font_size,
        }


@dataclass(frozen=True)
class Timeline:
    """動画 1 本ぶんの編集記述。"""

    scenes: tuple[Scene, ...]
    width: int = 1920
    height: int = 1080
    fps: int = 30
    format_version: int = field(default=FORMAT_VERSION)

    def __post_init__(self) -> None:
        _require(self.format_version == FORMAT_VERSION, f"format_version は {FORMAT_VERSION}")
        _require(len(self.scenes) >= 1, "シーンが 1 つもない")
        _require(len(self.scenes) <= MAX_SCENES, f"シーンは {MAX_SCENES} 個以内")
        _require(all(isinstance(s, Scene) for s in self.scenes), "scenes は Scene の列")
        for name, value, low, high in (
            ("width", self.width, 16, 3840),
            ("height", self.height, 16, 2160),
            ("fps", self.fps, 1, 60),
        ):
            _require(isinstance(value, int) and low <= value <= high, f"{name} は {low}〜{high} の整数")
        _require(
            self.width % 2 == 0 and self.height % 2 == 0,
            "width / height は偶数(H.264 の制約)",
        )
        _require(
            self.total_seconds <= MAX_TOTAL_SECONDS,
            f"合計 {MAX_TOTAL_SECONDS} 秒以内",
        )

    @property
    def total_seconds(self) -> float:
        return sum(float(s.duration_seconds) for s in self.scenes)

    def to_dict(self) -> dict[str, object]:
        return {
            "format_version": self.format_version,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "scenes": [s.to_dict() for s in self.scenes],
        }

    def to_json(self) -> str:
        # キー順・インデントを固定し、同じ内容なら同じバイト列にする
        # (レンダリング来歴のダイジェスト計算を安定させるため)。
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_dict(cls, data: object) -> "Timeline":
        _require(isinstance(data, dict), "timeline はオブジェクト")
        assert isinstance(data, dict)
        known = {"format_version", "width", "height", "fps", "scenes"}
        unknown = set(data) - known
        # 知らないキーは黙って無視しない。書いたのに効いていない記述は
        # 「記述の完全性」を壊すので、その場で断る。
        _require(not unknown, f"未知のキー: {sorted(unknown)}")
        raw_scenes = data.get("scenes")
        _require(isinstance(raw_scenes, list), "scenes は配列")
        assert isinstance(raw_scenes, list)
        scenes = []
        for i, raw in enumerate(raw_scenes):
            _require(isinstance(raw, dict), f"scenes[{i}] はオブジェクト")
            scene_known = {"text", "duration_seconds", "background", "text_color", "font_size"}
            scene_unknown = set(raw) - scene_known
            _require(not scene_unknown, f"scenes[{i}] の未知のキー: {sorted(scene_unknown)}")
            _require("text" in raw and "duration_seconds" in raw, f"scenes[{i}] に text と duration_seconds は必須")
            scenes.append(Scene(**raw))
        kwargs: dict[str, object] = {}
        for key in ("format_version", "width", "height", "fps"):
            if key in data:
                kwargs[key] = data[key]
        return cls(scenes=tuple(scenes), **kwargs)  # type: ignore[arg-type]

    @classmethod
    def load(cls, path: Path) -> "Timeline":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise TimelineError(f"timeline がない: {path}")
        except json.JSONDecodeError as exc:
            raise TimelineError(f"timeline が JSON として読めない: {path}: {exc}")
        return cls.from_dict(data)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")
