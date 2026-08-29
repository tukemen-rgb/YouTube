"""フォントの解決。見つからなければ落ちる(fail-closed)。

文字を描く以上フォントは必須で、環境ごとに場所が違う。ここでは

1. 環境変数 ``VIDEOYARD_FONT``(明示指定が常に勝つ)
2. 日本語が出る既知のフォント(Noto CJK)
3. 最後の手段としてラテン文字のみの DejaVu

の順で探し、どれも無ければ例外にする。「それらしいフォントで黙って
代用して文字化けした動画を出す」ことはしない。
"""

from __future__ import annotations

import os
from pathlib import Path


class FontError(RuntimeError):
    """使えるフォントが見つからない。"""


#: 日本語グリフを持つ候補を先に。順序が優先順位。
_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Regular.otf",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W4.ttc",
    "C:/Windows/Fonts/YuGothM.ttc",
    "C:/Windows/Fonts/msgothic.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def resolve_font() -> Path:
    override = os.environ.get("VIDEOYARD_FONT", "")
    if override:
        path = Path(override)
        if not path.is_file():
            raise FontError(f"VIDEOYARD_FONT が指すファイルがない: {path}")
        return path
    for candidate in _CANDIDATES:
        path = Path(candidate)
        if path.is_file():
            return path
    raise FontError(
        "フォントが見つからない。日本語対応フォント(例: fonts-noto-cjk)を"
        "インストールするか、環境変数 VIDEOYARD_FONT にフォントファイルの"
        "パスを設定すること。"
    )
