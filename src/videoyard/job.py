"""ProductionJob — 動画 1 本の状態機械。

1 本の動画 = 1 ディレクトリで、進み具合は job.json に記録する。
段(stage)は docs/ARCHITECTURE.md の 8 段そのまま。v0.1 で自動化されて
いるのは assembly(編集)だけだが、状態としては最初から 8 段を持つ。
後から段を継ぎ足すと「昔のジョブに publish の記録が無いのは、やって
いないのか記録形式が無かったのか」が区別できなくなるため。

決まりごと:

* 完了の記録は成果物の検証が通ったときだけ書く。書き込みは一時ファイル
  経由で、途中で死んでも job.json が壊れた状態にならない(fail-closed)。
* publish だけは特別で、approval.json(人間の承認記録)が無い限り
  完了にできない。承認をスキップするコードパスはここに存在しない。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

FORMAT_VERSION = 1

#: 制作の 8 段。順序に意味がある(表示・検査はこの順)。
STAGES = (
    "plan",       # 企画
    "script",     # 台本
    "assets",     # 素材
    "voice",      # 音声
    "assembly",   # 編集(v0.1 で自動化済み)
    "metadata",   # タイトル・説明・サムネ
    "publish",    # 公開(人間承認が必須)
    "analytics",  # 実績の取り込み
)


class JobError(RuntimeError):
    """job.json が読めない・決まりに反する操作をした。"""


class ApprovalRequired(JobError):
    """publish を人間の承認記録なしに完了させようとした。"""


class ProductionJob:
    def __init__(self, directory: Path, data: dict[str, object]):
        self.directory = directory
        self._data = data

    # ---- 生成と読み書き ----------------------------------------------

    @classmethod
    def create(cls, directory: Path, title: str) -> "ProductionJob":
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "job.json"
        if path.exists():
            raise JobError(f"job.json が既にある: {path}")
        data: dict[str, object] = {
            "format_version": FORMAT_VERSION,
            "title": title,
            "created_at": _now(),
            "stages": {name: {"status": "pending"} for name in STAGES},
        }
        job = cls(directory, data)
        job._save()
        return job

    @classmethod
    def load(cls, directory: Path) -> "ProductionJob":
        path = directory / "job.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise JobError(f"job.json がない: {path}")
        except json.JSONDecodeError as exc:
            raise JobError(f"job.json が JSON として読めない: {exc}")
        if not isinstance(data, dict) or data.get("format_version") != FORMAT_VERSION:
            raise JobError("job.json の format_version が想定と違う")
        stages = data.get("stages")
        if not isinstance(stages, dict) or set(stages) != set(STAGES):
            raise JobError("job.json の stages が 8 段と一致しない")
        return cls(directory, data)

    def _save(self) -> None:
        path = self.directory / "job.json"
        tmp = self.directory / "job.json.tmp"
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)

    # ---- 状態の参照と前進 --------------------------------------------

    @property
    def title(self) -> str:
        return str(self._data.get("title", ""))

    def stage_status(self, stage: str) -> str:
        self._check_stage(stage)
        stages = self._data["stages"]
        assert isinstance(stages, dict)
        return str(stages[stage]["status"])

    def mark_done(self, stage: str, note: str = "") -> None:
        """検証を済ませた呼び出し側だけが完了を記録できる。

        assembly は out/video.mp4 と render_manifest.json の実在を、
        publish は approval.json の実在をここでも確かめる。呼び出し側の
        言い分だけで記録が進まないようにする最後の関所。
        """
        self._check_stage(stage)
        if stage == "assembly":
            for required in ("out/video.mp4", "out/render_manifest.json"):
                if not (self.directory / required).is_file():
                    raise JobError(f"assembly の成果物がない: {required}")
            # 動画が「今の」計画ファイル(timeline.json / cutplan.json)
            # から作られたものであること。レンダリング後に計画を書き換えた
            # 古い動画を完了と記録できてしまう穴を塞ぐ。
            try:
                manifest = json.loads(
                    (self.directory / "out/render_manifest.json").read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise JobError(f"render_manifest.json が読めない: {exc}")
            plan_file = manifest.get("plan_file")
            plan_path = self.directory / str(plan_file)
            if not isinstance(plan_file, str) or not plan_path.is_file():
                raise JobError(f"来歴が指す計画ファイルが読めない: {plan_file}")
            current = hashlib.sha256(plan_path.read_bytes()).hexdigest()
            if manifest.get("plan_sha256") != current:
                raise JobError(
                    f"out/video.mp4 は今の {plan_file} から作られたものではない。"
                    "作り直すこと。"
                )
        if stage == "publish":
            if not (self.directory / "approval.json").is_file():
                raise ApprovalRequired(
                    "approval.json(人間の承認記録)が無いので publish を"
                    "完了にできない。承認なしの公開はこの仕組みに存在しない。"
                )
        stages = self._data["stages"]
        assert isinstance(stages, dict)
        record: dict[str, object] = {"status": "done", "completed_at": _now()}
        if note:
            record["note"] = note
        stages[stage] = record
        self._save()

    def summary(self) -> str:
        lines = [f"{self.title} ({self.directory})"]
        for name in STAGES:
            status = self.stage_status(name)
            mark = "✔" if status == "done" else "・"
            lines.append(f"  {mark} {name}: {status}")
        return "\n".join(lines)

    def _check_stage(self, stage: str) -> None:
        if stage not in STAGES:
            raise JobError(f"知らない段: {stage}(有効: {', '.join(STAGES)})")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
