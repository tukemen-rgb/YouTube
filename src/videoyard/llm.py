"""テロップ文言のローカル AI 下書き。

sidra-ai と同じ決まりで作る:

* **local LLM first。** 接続先は localhost の Ollama だけ。外部ホストの
  URL は組み立ての時点で拒否する(従量課金 API への依存をコードの形で
  作らない)。
* **明示的に頼まれたときだけ動く。** 既定は「AI なし」で、テンプレートの
  「シーン 1」が付く。--llm ollama と明示されたのに接続できなければ、
  黙ってテンプレートに落ちずにエラーで止まる(頼まれたことができない
  ときに、できたふりをしない)。
* **AI の出力は下書きの DATA。** 生成された文言は検査(1 行化・長さ上限)
  を通して cutplan.json に書かれるだけで、人が直してから cut が実行する。
  描画は textfile + expansion=none 経由なので、文言が描画エンジンへの
  命令になる余地はない。

AI は動画の映像そのものを見ていない。渡るのはシーンの番号・秒数・
盛り上がり候補かどうかと、人が --hint で書いた内容ヒントだけ。だから
文言の「うまさ」はヒントの質に大きく依存する(正直な限界)。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse

#: テロップとして受け入れる最大文字数。これを超えた下書きは切り詰める。
MAX_DRAFT_CHARS = 30

_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


class LlmError(RuntimeError):
    """ローカル AI が使えない・応答が成立していない。"""


@dataclass(frozen=True)
class SceneBrief:
    """AI に渡してよい、と決めたシーン情報のすべて。

    ここに無い情報は AI に渡らない。「AI が何を知っていたか」を
    このデータクラスの形で固定するための入れ物。
    """

    number: int
    start: float
    duration: float
    is_highlight: bool
    hint: str = ""


def build_prompt(brief: SceneBrief) -> str:
    highlight = "この動画の盛り上がり候補(音量が最大)の場面。" if brief.is_highlight else ""
    hint = brief.hint if brief.hint else "(なし)"
    return (
        "あなたはYouTube動画の編集アシスタント。次のシーンに付ける短い日本語テロップを"
        "1つだけ出力すること。\n"
        "条件: 20文字以内。1行だけ。誇張しすぎない。絵文字は使わない。\n"
        f"動画の内容ヒント: {hint}\n"
        f"シーン情報: {brief.number}番目のシーン。開始{brief.start:.0f}秒、"
        f"長さ{brief.duration:.0f}秒。{highlight}\n"
        "テロップの文言だけを出力:"
    )


def sanitize_draft(text: str) -> str | None:
    """AI の出力を「テロップ 1 行」に整える。使えなければ None。"""
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    cleaned = first_line.strip().strip('"「」『』\'')
    if not cleaned:
        return None
    return cleaned[:MAX_DRAFT_CHARS]


def require_local_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in _LOCAL_HOSTS:
        raise LlmError(
            f"ローカル AI の接続先は localhost のみ({url} は使えない)。"
            "外部 API はこの仕組みに登録できない。"
        )
    return url


class OllamaTelopWriter:
    """localhost の Ollama にテロップの下書きを頼む。"""

    def __init__(self, model: str, url: str = "http://127.0.0.1:11434",
                 timeout_seconds: float = 60.0):
        if not model:
            raise LlmError(
                "モデル名が無い(--llm-model か VIDEOYARD_LLM_MODEL で明示する)。"
                "勝手にモデルを推測して選ぶことはしない。"
            )
        self.model = model
        self.base_url = require_local_url(url).rstrip("/")
        self.timeout_seconds = timeout_seconds

    def write(self, brief: SceneBrief) -> str | None:
        payload = json.dumps({
            "model": self.model,
            "prompt": build_prompt(brief),
            "stream": False,
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/api/generate",
            data=payload,
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            raise LlmError(
                f"ローカル AI({self.base_url})に接続できない/応答が読めない: {exc}"
            )
        text = body.get("response")
        if not isinstance(text, str):
            raise LlmError("ローカル AI の応答に response が無い")
        return sanitize_draft(text)
