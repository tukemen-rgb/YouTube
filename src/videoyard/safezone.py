"""セーフゾーン — スマホの UI に隠れない場所に文字を置く(S5)。

縦動画(ショート / リール / TikTok)は、アプリ自身の UI が映像の上に
重なる。下端にはキャプション欄、右端にはいいね・コメントのボタン列、
上端には検索やおすすめの帯。**そこに置いたテロップは読めない。**

実測(2026-09-08): photo --vertical のテロップは 1080×1920 の最下端に
描かれ、下端 300px と右端 120px の両方に掛かっていた。スマホでは
完全に隠れる。ここを直すのがこのモジュール。

数値は 1080×1920 を基準にした各プラットフォームの目安(出典は
docs/MARKET_RESEARCH.md)。3 つのアプリすべてで安全な最大公約数を取る:

* 下端 300px … TikTok のキャプション欄(いちばん広い)
* 右端 120px … TikTok / Reels のボタン列
* 上端 120px … 上部の帯

横動画(16:9)には適用しない。プレーヤーの UI は再生中に消えるうえ、
既存の見た目を勝手に変えないため(必要になったら別途決める)。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 基準になる縦動画の大きさ。比率はここから作る。
REFERENCE_WIDTH = 1080
REFERENCE_HEIGHT = 1920
#: 1080×1920 のときに空けるべき画素数。
BOTTOM_RESERVED = 300
RIGHT_RESERVED = 120
TOP_RESERVED = 120
LEFT_RESERVED = 40


@dataclass(frozen=True)
class Box:
    """安全に文字を置ける矩形(左上が x, y)。"""

    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    def contains(self, x: int, y: int) -> bool:
        return self.x <= x <= self.right and self.y <= y <= self.bottom


def is_vertical(width: int, height: int) -> bool:
    """縦長(ショート向け)の画面か。純粋関数。"""
    return height > width


def _scaled(value: int, actual: int, reference: int) -> int:
    """基準サイズでの画素数を、実際の大きさに比例させる。"""
    return int(round(value * actual / reference))


def reserved_bottom(height: int) -> int:
    return _scaled(BOTTOM_RESERVED, height, REFERENCE_HEIGHT)


def reserved_top(height: int) -> int:
    return _scaled(TOP_RESERVED, height, REFERENCE_HEIGHT)


def reserved_right(width: int) -> int:
    return _scaled(RIGHT_RESERVED, width, REFERENCE_WIDTH)


def reserved_left(width: int) -> int:
    return _scaled(LEFT_RESERVED, width, REFERENCE_WIDTH)


def safe_box(width: int, height: int) -> Box:
    """文字を置いてよい範囲。横動画なら画面全体を返す(制限しない)。"""
    if not is_vertical(width, height):
        return Box(0, 0, width, height)
    left = reserved_left(width)
    top = reserved_top(height)
    return Box(
        x=left,
        y=top,
        width=max(1, width - left - reserved_right(width)),
        height=max(1, height - top - reserved_bottom(height)),
    )


def bottom_text_margin(width: int, height: int, minimum: int = 20) -> int:
    """下寄せの文字を、下端から何 px 上に置くか。純粋関数。"""
    if not is_vertical(width, height):
        return minimum
    return max(minimum, reserved_bottom(height))


def text_wrap_width(width: int, height: int) -> int:
    """折り返しに使ってよい横幅。右のボタン列に食い込ませない。"""
    return safe_box(width, height).width


def letterbox_top(source_width: int, source_height: int,
                  out_width: int = REFERENCE_WIDTH,
                  out_height: int = REFERENCE_HEIGHT) -> float:
    """横動画を縦画面の中央に置いたとき、映像の上端が来る位置。純粋関数。

    cut --vertical は「幅いっぱいに拡大して中央配置」なので、
    拡大率は out_width / source_width で決まる。
    """
    scale = out_width / source_width
    return (out_height - source_height * scale) / 2


def source_bottom_margin(source_width: int, source_height: int,
                         minimum: int = 20,
                         out_width: int = REFERENCE_WIDTH,
                         out_height: int = REFERENCE_HEIGHT) -> int:
    """縦変換**前**の絵に文字を描くときの、下端からの余白。純粋関数。

    cut --vertical はテロップを元の縦横比の絵に描いてから縮めて中央に
    置く。出力で安全な位置に来るよう、必要な余白を元の絵の座標へ
    逆算する。16:9 のように上下に帯が付く場合は余白 0 でも安全なので、
    そのときは minimum のまま(見た目を変えない)。
    """
    scale = out_width / source_width
    top = letterbox_top(source_width, source_height, out_width, out_height)
    allowed_output_bottom = out_height - reserved_bottom(out_height)
    # 出力での文字下端 = top + (source_height - margin) * scale
    needed = source_height - (allowed_output_bottom - top) / scale
    return max(minimum, int(round(needed)))


def source_wrap_width(source_width: int,
                      out_width: int = REFERENCE_WIDTH) -> int:
    """縦変換前の絵での折り返し幅。出力の安全幅から逆算する。純粋関数。"""
    scale = out_width / source_width
    safe_width = out_width - reserved_left(out_width) - reserved_right(out_width)
    return max(1, min(source_width, int(safe_width / scale)))
