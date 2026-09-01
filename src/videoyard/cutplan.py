"""カット計画(cutplan.json)— 人が読んで、直して、確定するファイル。

analyze が「案」として書き、人が action(keep/cut)・telop・区間を
自由に書き換え、cut がそれを実行する。動画にどんな編集が入ったかは
常にこのファイルで説明できる。

timeline.json と同じ決まり: 検証は構築時、未知のキーは黙って無視
しない、同じ内容なら同じバイト列。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

FORMAT_VERSION = 1

MAX_SEGMENTS = 500
MAX_TELOP_CHARS = 120

_ACTIONS = ("keep", "cut")


class CutPlanError(ValueError):
    """cutplan.json が計画として成立していない。"""


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise CutPlanError(message)


@dataclass(frozen=True)
class PlanSegment:
    """元動画の 1 区間と、その扱い。"""

    start: float
    end: float
    action: str
    telop: str = ""
    reason: str = ""
    #: 盛り上がり度 0〜100(動画内の相対値)。analyze が付ける。無くてもよい。
    excite: int | None = None

    def __post_init__(self) -> None:
        _require(isinstance(self.start, (int, float)) and self.start >= 0, "start は 0 以上")
        _require(isinstance(self.end, (int, float)) and self.end > self.start,
                 f"end は start より大きい({self.start}〜{self.end})")
        _require(self.action in _ACTIONS, f"action は {_ACTIONS} のどれか: {self.action}")
        _require(isinstance(self.telop, str) and len(self.telop) <= MAX_TELOP_CHARS,
                 f"telop は {MAX_TELOP_CHARS} 文字以内")
        _require(isinstance(self.reason, str), "reason は文字列")
        _require(
            self.excite is None
            or (isinstance(self.excite, int) and 0 <= self.excite <= 100),
            "excite は 0〜100 の整数か無し",
        )

    def replaced(self, **changes: object) -> "PlanSegment":
        return replace(self, **changes)  # type: ignore[arg-type]

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "start": float(self.start), "end": float(self.end), "action": self.action,
        }
        if self.telop:
            data["telop"] = self.telop
        if self.reason:
            data["reason"] = self.reason
        if self.excite is not None:
            data["excite"] = self.excite
        return data


@dataclass(frozen=True)
class CutPlan:
    source_path: str
    source_sha256: str
    duration: float
    width: int
    height: int
    has_audio: bool
    mode: str
    segments: tuple[PlanSegment, ...]
    format_version: int = FORMAT_VERSION

    def __post_init__(self) -> None:
        _require(self.format_version == FORMAT_VERSION, f"format_version は {FORMAT_VERSION}")
        _require(bool(self.source_path), "source_path は必須")
        _require(len(self.source_sha256) == 64, "source_sha256 は sha256 の 16 進 64 文字")
        _require(self.duration > 0, "duration は正")
        _require(self.width > 0 and self.height > 0, "width / height は正")
        _require(1 <= len(self.segments) <= MAX_SEGMENTS,
                 f"segments は 1〜{MAX_SEGMENTS} 個")
        previous_end = 0.0
        for i, seg in enumerate(self.segments):
            _require(isinstance(seg, PlanSegment), f"segments[{i}] が PlanSegment でない")
            _require(seg.start >= previous_end - 1e-6,
                     f"segments[{i}] が前の区間と重なっている")
            _require(seg.end <= self.duration + 0.5,
                     f"segments[{i}] が動画の長さ({self.duration}s)を超えている")
            previous_end = seg.end
        _require(any(s.action == "keep" for s in self.segments),
                 "keep の区間が 1 つも無い(全部切ると動画が残らない)")

    @property
    def keeps(self) -> tuple[PlanSegment, ...]:
        return tuple(s for s in self.segments if s.action == "keep")

    @property
    def kept_seconds(self) -> float:
        return sum(s.end - s.start for s in self.keeps)

    def to_dict(self) -> dict[str, object]:
        return {
            "format_version": self.format_version,
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
            "duration": self.duration,
            "width": self.width,
            "height": self.height,
            "has_audio": self.has_audio,
            "mode": self.mode,
            "segments": [s.to_dict() for s in self.segments],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_dict(cls, data: object) -> "CutPlan":
        _require(isinstance(data, dict), "cutplan はオブジェクト")
        assert isinstance(data, dict)
        known = {"format_version", "source_path", "source_sha256", "duration",
                 "width", "height", "has_audio", "mode", "segments"}
        unknown = set(data) - known
        _require(not unknown, f"未知のキー: {sorted(unknown)}")
        raw_segments = data.get("segments")
        _require(isinstance(raw_segments, list), "segments は配列")
        assert isinstance(raw_segments, list)
        segments = []
        seg_known = {"start", "end", "action", "telop", "reason", "excite"}
        for i, raw in enumerate(raw_segments):
            _require(isinstance(raw, dict), f"segments[{i}] はオブジェクト")
            seg_unknown = set(raw) - seg_known
            _require(not seg_unknown, f"segments[{i}] の未知のキー: {sorted(seg_unknown)}")
            _require({"start", "end", "action"} <= set(raw),
                     f"segments[{i}] に start / end / action は必須")
            segments.append(PlanSegment(**raw))
        rest = {k: v for k, v in data.items() if k != "segments"}
        return cls(segments=tuple(segments), **rest)  # type: ignore[arg-type]

    @classmethod
    def load(cls, path: Path) -> "CutPlan":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise CutPlanError(f"cutplan がない: {path}(先に analyze を実行)")
        except json.JSONDecodeError as exc:
            raise CutPlanError(f"cutplan が JSON として読めない: {exc}")
        return cls.from_dict(data)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")
