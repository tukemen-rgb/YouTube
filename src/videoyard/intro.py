"""ゲーム紹介動画の生成器 — facts から timeline.json を組み立てる。

sidra-ai の creation の原則をそのまま使う: **生成器は渡された facts の
範囲でしか語らない。** 動画に出る文言は、facts ファイルに書かれた
ゲーム名・説明・特徴・操作方法・URL と、定型の見出し(「あそびかた」
など)だけ。生成器が気の利いた宣伝文句を発明することはない。だから
「この動画のこの一文はどこから来たか」に必ず facts で答えられる。

facts はいまは人が書く JSON(examples/facts-sample.json が見本)。
ゲームのリポジトリから自動で抜き出す仕組みは、リポジトリ側の構造を
決めてからの次段階。

出力は v0.1 の timeline.json なので、レンダリングは既存の render が
そのまま使える(決定的・来歴付き)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from videoyard.timeline import Scene, Timeline

MAX_FEATURES = 5
MAX_CONTROLS = 6
MAX_LINE_CHARS = 60

#: シーンの配色(順に使い回す)。見た目の決まりであって内容ではない。
_PALETTE = ("#101820", "#1d3557", "#2a2a2a", "#14342b", "#3d1f2b")


class IntroError(ValueError):
    """facts が紹介動画の材料として成立していない。"""


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise IntroError(message)


def _check_line(name: str, value: str) -> None:
    _require(isinstance(value, str) and value.strip() != "", f"{name} は空でない文字列")
    _require(len(value) <= MAX_LINE_CHARS, f"{name} は {MAX_LINE_CHARS} 文字以内: {value!r}")
    _require("\n" not in value, f"{name} に改行は入れない(折り返しは自動)")


@dataclass(frozen=True)
class GameFacts:
    """紹介動画が語ってよいことのすべて。"""

    name: str
    tagline: str = ""
    features: tuple[str, ...] = field(default_factory=tuple)
    controls: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    play_url: str = ""

    def __post_init__(self) -> None:
        _check_line("name", self.name)
        if self.tagline:
            _check_line("tagline", self.tagline)
        _require(len(self.features) <= MAX_FEATURES, f"features は {MAX_FEATURES} 個以内")
        for i, feature in enumerate(self.features):
            _check_line(f"features[{i}]", feature)
        _require(len(self.controls) <= MAX_CONTROLS, f"controls は {MAX_CONTROLS} 個以内")
        for i, pair in enumerate(self.controls):
            _require(isinstance(pair, tuple) and len(pair) == 2,
                     f"controls[{i}] は [キー, 動作] の組")
            _check_line(f"controls[{i}] のキー", pair[0])
            _check_line(f"controls[{i}] の動作", pair[1])
        if self.play_url:
            parsed = urlparse(self.play_url)
            _require(parsed.scheme == "https" and bool(parsed.hostname),
                     f"play_url は https の URL: {self.play_url!r}")

    @classmethod
    def load(cls, path: Path) -> GameFacts:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise IntroError(f"facts ファイルがない: {path}") from None
        except json.JSONDecodeError as exc:
            raise IntroError(f"facts が JSON として読めない: {exc}") from exc
        _require(isinstance(data, dict), "facts はオブジェクト")
        known = {"name", "tagline", "features", "controls", "play_url"}
        unknown = set(data) - known
        _require(not unknown, f"facts の未知のキー: {sorted(unknown)}")
        _require("name" in data, "facts に name は必須")
        features = data.get("features", [])
        _require(isinstance(features, list), "features は配列")
        controls = data.get("controls", [])
        _require(isinstance(controls, list), "controls は配列")
        control_pairs = []
        for i, raw in enumerate(controls):
            _require(isinstance(raw, list) and len(raw) == 2,
                     f"controls[{i}] は [キー, 動作] の 2 要素の配列")
            control_pairs.append((raw[0], raw[1]))
        return cls(
            name=data["name"],
            tagline=data.get("tagline", ""),
            features=tuple(features),
            controls=tuple(control_pairs),
            play_url=data.get("play_url", ""),
        )


def build_timeline(facts: GameFacts, width: int = 1280, height: int = 720,
                   fps: int = 30) -> Timeline:
    """facts → 紹介動画のタイムライン。facts に無い文言は定型見出しのみ。"""
    font = max(24, height // 12)
    small = max(20, height // 16)
    scenes: list[Scene] = []

    def add(text: str, seconds: float, font_size: int) -> None:
        scenes.append(Scene(
            text=text,
            duration_seconds=seconds,
            background=_PALETTE[len(scenes) % len(_PALETTE)],
            font_size=font_size,
        ))

    add(facts.name, 3.0, font)
    if facts.tagline:
        add(facts.tagline, 3.0, small)
    for feature in facts.features:
        add(feature, 2.5, small)
    if facts.controls:
        lines = "\n".join(f"{key} … {action}" for key, action in facts.controls)
        add(f"あそびかた\n{lines}", 2.0 + 1.0 * len(facts.controls), small)
    if facts.play_url:
        add(f"いますぐ遊べる\n{facts.play_url}", 3.5, small)
    return Timeline(scenes=tuple(scenes), width=width, height=height, fps=fps)
