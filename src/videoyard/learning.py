"""あなたの添削から学ぶ — 盛り上がり度の採点基準の個人化。

学習の材料は、このパイプラインが自然に生む「先生のお手本」だけ:

    analyze が案を出す → 人が cutplan.json を直す → cut が実行する

案(cutplan.proposed.json)と実行された計画(cutplan.json)の差分が、
「AI はこう思ったが、人はこう直した」というラベル付きデータになる。
窓ごとの測定値(動き・音量・立ち上がり)を入力、人が最終的に残したか
どうかを正解として、ロジスティック回帰(依存ゼロの純 Python 実装)で
採点の重みを学習し直す。

決まりごと:

* データはすべてローカル(既定 ~/.videoyard、環境変数 VIDEOYARD_DATA_DIR
  で変更可)。どこにも送らない。
* 学習に使うのは区間の測定値と keep/cut の判断だけ。動画の中身も
  個人情報も保存しない。
* 学習済みの重みは weights.json という目に見えるファイルで、いつでも
  消せば工場出荷(既定の重み)に戻る。隠れた状態を持たない。
* データが足りないうちは学習しない(少数の例に過剰適合した重みは
  既定より悪いため)。「まだ足りない」と正直に言う。
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from videoyard.cutplan import CutPlan
from videoyard.excitement import ScoreWeights

#: これ未満の添削例では学習しない。3 係数のロジスティック回帰でも、
#: これを下回ると個々の例の偶然を拾いすぎる。
MIN_EXAMPLES = 30

_FEEDBACK_FILE = "feedback.jsonl"
_WEIGHTS_FILE = "weights.json"


class LearningError(RuntimeError):
    """学習・記録が完了しなかった。"""


def data_dir() -> Path:
    override = os.environ.get("VIDEOYARD_DATA_DIR", "")
    return Path(override) if override else Path.home() / ".videoyard"


# ---- 添削の記録 -------------------------------------------------------------

@dataclass(frozen=True)
class Example:
    """窓 1 つぶんの教師データ。"""

    motion: float
    loudness: float
    onset: float
    kept: bool

    def to_dict(self) -> dict[str, object]:
        return {"motion": self.motion, "loudness": self.loudness,
                "onset": self.onset, "kept": self.kept}


def _action_at(plan: CutPlan, time: float) -> str | None:
    for seg in plan.segments:
        if seg.start <= time < seg.end:
            return seg.action
    return None


def extract_examples(proposal: CutPlan, final: CutPlan,
                     windows: dict[str, object]) -> list[Example]:
    """案と最終計画の差分を、窓ごとの教師データにする。

    含めるのは「採点が判断に関わった」窓だけ:

    * 案が keep とした窓(人が残した=正解 1、人が切った=正解 0)
    * 案が cut としたのに人が keep に戻した窓(正解 1)

    案が cut で人もそのまま cut の窓は含めない。それらは静止画/無音の
    検出で決まった部分で、採点の良し悪しの証拠にならない。
    """
    window_seconds = float(windows["window_seconds"])  # type: ignore[arg-type]
    features = windows["features"]
    assert isinstance(features, dict)
    motion = features["motion"]
    loudness = features.get("loudness") or [0.0] * len(motion)
    onset = features.get("onset") or [0.0] * len(motion)

    examples = []
    for i, m in enumerate(motion):
        center = (i + 0.5) * window_seconds
        proposed = _action_at(proposal, center)
        decided = _action_at(final, center)
        if proposed is None or decided is None:
            continue
        if proposed == "cut" and decided == "cut":
            continue
        examples.append(Example(
            motion=float(m), loudness=float(loudness[i]), onset=float(onset[i]),
            kept=(decided == "keep"),
        ))
    return examples


def record_feedback(production_dir: Path, directory: Path | None = None) -> int:
    """cut 実行後に呼ぶ。この production の添削を保存し、件数を返す。

    同じ元動画の記録は最新の 1 回分だけ残す(cut をやり直すたびに
    同じ動画の古い添削が積み上がると、その動画だけ過大に効くため)。
    """
    directory = directory or data_dir()
    proposal_path = production_dir / "cutplan.proposed.json"
    windows_path = production_dir / "analysis_windows.json"
    if not proposal_path.is_file() or not windows_path.is_file():
        return 0  # 旧版の production。記録できるものが無い
    proposal = CutPlan.load(proposal_path)
    final = CutPlan.load(production_dir / "cutplan.json")
    if final.source_sha256 != proposal.source_sha256:
        raise LearningError("cutplan.json と案の元動画が一致しない")
    windows = json.loads(windows_path.read_text(encoding="utf-8"))
    examples = extract_examples(proposal, final, windows)
    if not examples:
        return 0

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / _FEEDBACK_FILE
    kept_lines = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            if json.loads(line).get("source_sha256") != final.source_sha256:
                kept_lines.append(line)
    for example in examples:
        record = {"source_sha256": final.source_sha256,
                  "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  **example.to_dict()}
        kept_lines.append(json.dumps(record, ensure_ascii=False, sort_keys=True))
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(kept_lines) + "\n", encoding="utf-8")
    tmp.replace(path)
    return len(examples)


def load_examples(directory: Path | None = None) -> list[Example]:
    directory = directory or data_dir()
    path = directory / _FEEDBACK_FILE
    if not path.is_file():
        return []
    examples = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        examples.append(Example(
            motion=float(data["motion"]), loudness=float(data["loudness"]),
            onset=float(data["onset"]), kept=bool(data["kept"]),
        ))
    return examples


# ---- 学習(依存ゼロのロジスティック回帰) ----------------------------------

def _sigmoid(x: float) -> float:
    if x < -60:
        return 0.0
    if x > 60:
        return 1.0
    return 1.0 / (1.0 + math.exp(-x))


def train(examples: list[Example], epochs: int = 300, learning_rate: float = 0.1,
          l2: float = 0.01) -> tuple[ScoreWeights, float]:
    """重みを学習し、(重み, 訓練データ上の的中率) を返す。

    初期値ゼロ・固定順・固定回数の勾配降下なので、同じ添削データからは
    同じ重みが出る(乱数を使わない)。
    """
    if len(examples) < MIN_EXAMPLES:
        raise LearningError(
            f"添削の例が {len(examples)} 件で、学習に必要な {MIN_EXAMPLES} 件に"
            "届かない。analyze → 計画を直す → cut を繰り返すと貯まる。"
        )
    w = [0.0, 0.0, 0.0]  # motion, loudness, onset
    b = 0.0
    n = len(examples)
    for _ in range(epochs):
        gw = [0.0, 0.0, 0.0]
        gb = 0.0
        for ex in examples:
            x = (ex.motion, ex.loudness, ex.onset)
            p = _sigmoid(sum(wi * xi for wi, xi in zip(w, x)) + b)
            error = p - (1.0 if ex.kept else 0.0)
            for j in range(3):
                gw[j] += error * x[j]
            gb += error
        for j in range(3):
            w[j] -= learning_rate * (gw[j] / n + l2 * w[j])
        b -= learning_rate * gb / n

    correct = 0
    for ex in examples:
        p = _sigmoid(w[0] * ex.motion + w[1] * ex.loudness + w[2] * ex.onset + b)
        if (p >= 0.5) == ex.kept:
            correct += 1
    weights = ScoreWeights(motion=w[0], loudness=w[1], onset=w[2])
    return weights, correct / n


def save_weights(weights: ScoreWeights, examples: int, accuracy: float,
                 directory: Path | None = None) -> Path:
    directory = directory or data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / _WEIGHTS_FILE
    payload = {
        "format_version": 1,
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "examples": examples,
        "training_accuracy": round(accuracy, 4),
        "weights": {"motion": weights.motion, "loudness": weights.loudness,
                    "onset": weights.onset},
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    tmp.replace(path)
    return path


def load_weights(directory: Path | None = None) -> tuple[ScoreWeights, dict[str, object]] | None:
    """学習済みの重みがあれば読む。壊れていたら黙って使わずエラーにする。"""
    directory = directory or data_dir()
    path = directory / _WEIGHTS_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw = data["weights"]
        weights = ScoreWeights(motion=float(raw["motion"]),
                               loudness=float(raw["loudness"]),
                               onset=float(raw["onset"]))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise LearningError(f"weights.json が読めない({path}): {exc}。"
                            "消せば既定の重みに戻る。")
    return weights, data
