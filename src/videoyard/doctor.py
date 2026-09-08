"""環境診断 — 動かない理由と直し方を、動かす前に言う(S2)。

競合調査で分かったサービス面の弱点その 2: クラウド勢は「インストール
不要」で始まるのに、videoyard は Python + ffmpeg + 日本語フォントが
要る。揃っていないと、長い ffmpeg のエラーを読まされる羽目になる。

doctor は実際に動かす前に一つずつ確かめ、足りないものには**その環境で
打つコマンド**を出す。診断は事実だけを言い、勝手に何かを入れたりは
しない(インストールは人が決める)。

各項目は「調べる関数」に分かれていて、ffmpeg 無しの環境でもテスト
できる(結果の組み立ては純粋関数)。
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from videoyard.fonts import FontError, resolve_font
from videoyard.learning import data_dir

#: videoyard が要求する Python の最低版。pyproject と揃える。
MIN_PYTHON = (3, 11)
#: 使っている ffmpeg のフィルタ。1 つでも欠けると特定の機能が動かない。
REQUIRED_FILTERS = {
    "drawtext": "テロップとサムネの文字(--enable-libfreetype 付きの ffmpeg が要る)",
    "freezedetect": "静止画の検出(analyze)",
    "silencedetect": "無音の検出(analyze)",
    "loudnorm": "書き出し音量の統一(cut)",
    "atempo": "倍速で残す ≫(cut)",
    "zoompan": "写真スライドショーのズーム(photo)",
    "boxblur": "ショートのぼかし背景(cut --vertical / photo)",
}

_OK = "OK"
_WARN = "注意"
_FAIL = "不足"


@dataclass(frozen=True)
class Check:
    """診断 1 項目。level は OK / 注意 / 不足。"""

    name: str
    level: str
    detail: str
    fix: str = ""

    @property
    def failed(self) -> bool:
        return self.level == _FAIL


def _package_hint() -> str:
    """この OS でのインストール手順の目安。推測は「調べること」と書く。"""
    system = platform.system()
    if system == "Darwin":
        return "brew install ffmpeg"
    if system == "Windows":
        return "winget install Gyan.FFmpeg(または https://ffmpeg.org から入れて PATH を通す)"
    if system == "Linux":
        return "sudo apt install ffmpeg(Debian/Ubuntu の場合)"
    return "お使いの OS の手順で ffmpeg を入れること"


def _font_hint() -> str:
    system = platform.system()
    if system == "Linux":
        return ("sudo apt install fonts-noto-cjk "
                "(または環境変数 VIDEOYARD_FONT にフォントのパスを指定)")
    return ("日本語フォントを入れるか、環境変数 VIDEOYARD_FONT に "
            "フォントファイルのパスを指定する")


def check_python(version: tuple[int, ...] | None = None) -> Check:
    """Python の版。純粋関数(version を渡せば任意の版で試せる)。"""
    current = version if version is not None else sys.version_info[:2]
    text = ".".join(str(n) for n in current[:2])
    if tuple(current[:2]) >= MIN_PYTHON:
        return Check("Python", _OK, f"{text}")
    need = ".".join(str(n) for n in MIN_PYTHON)
    return Check("Python", _FAIL, f"{text}(必要: {need} 以上)",
                 f"Python {need} 以上を入れて、そちらで実行する")


def check_command(name: str, which=shutil.which) -> Check:
    """ffmpeg / ffprobe が PATH にあるか。"""
    path = which(name)
    if path:
        return Check(name, _OK, str(path))
    return Check(name, _FAIL, "PATH に見つからない", _package_hint())


def check_font(resolver=resolve_font) -> Check:
    """日本語の出るフォントがあるか。無ければ文字を描く機能が全滅する。"""
    try:
        path = resolver()
    except FontError as exc:
        return Check("日本語フォント", _FAIL, str(exc), _font_hint())
    if "DejaVu" in path.name:
        return Check(
            "日本語フォント", _WARN,
            f"{path}(ラテン文字のみ。日本語が豆腐■になる)", _font_hint())
    return Check("日本語フォント", _OK, str(path))


def parse_filters(text: str) -> set[str]:
    """`ffmpeg -filters` の出力からフィルタ名を集める。純粋関数。

    行の形は「 T.. name  in->out  説明」。2 列目がフィルタ名。
    """
    names = set()
    for line in text.splitlines():
        parts = line.split()
        # フィルタ行は 3 列目が "A->A" のような入出力表記。凡例の行
        # (「T.. = Timeline support」など)はこれを持たないので弾ける。
        if len(parts) >= 3 and "->" in parts[2]:
            names.add(parts[1])
    return names


def check_filters(available: set[str]) -> list[Check]:
    """必要なフィルタが揃っているか。純粋関数。"""
    checks = []
    for name, purpose in sorted(REQUIRED_FILTERS.items()):
        if name in available:
            checks.append(Check(f"filter:{name}", _OK, purpose))
        else:
            checks.append(Check(
                f"filter:{name}", _FAIL, f"{purpose} が使えない",
                "この機能が入った ffmpeg を入れ直す。"
                + _package_hint()))
    return checks


def check_data_dir(directory: Path | None = None) -> Check:
    """学習データの置き場に書けるか(書けないと学習が黙って消える)。"""
    directory = directory or data_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".videoyard-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Check(
            "学習データの置き場", _FAIL, f"{directory} に書けない: {exc}",
            "環境変数 VIDEOYARD_DATA_DIR に書ける場所を指定する")
    return Check("学習データの置き場", _OK, str(directory))


def check_disk(directory: Path | None = None, need_mb: int = 500) -> Check:
    """作業用の空き容量。動画は大きいので、足りないと途中で失敗する。"""
    directory = directory or Path.cwd()
    try:
        free_mb = shutil.disk_usage(directory).free // (1024 * 1024)
    except OSError as exc:
        return Check("空き容量", _WARN, f"調べられない: {exc}")
    if free_mb < need_mb:
        return Check("空き容量", _WARN,
                     f"{free_mb} MB(動画 1 本に {need_mb} MB は見ておきたい)",
                     "不要なファイルを消すか、別のディスクで作業する")
    return Check("空き容量", _OK, f"{free_mb} MB")


def run_checks(ffmpeg: str = "ffmpeg") -> list[Check]:
    """全項目を実行する。ffmpeg が無ければフィルタ検査は飛ばす。"""
    checks = [check_python(), check_command(ffmpeg), check_command("ffprobe")]
    if shutil.which(ffmpeg):
        try:
            result = subprocess.run([ffmpeg, "-hide_banner", "-filters"],
                                    capture_output=True, text=True, timeout=30)
            checks += check_filters(parse_filters(result.stdout))
        except (OSError, subprocess.SubprocessError) as exc:
            checks.append(Check("filter 一覧", _WARN,
                                f"ffmpeg -filters が実行できない: {exc}"))
    checks.append(check_font())
    checks.append(check_data_dir())
    checks.append(check_disk())
    return checks


def format_report(checks: list[Check]) -> list[str]:
    """診断結果の表示行。純粋関数。不足があれば直し方を必ず添える。"""
    width = max((len(c.name) for c in checks), default=10)
    lines = []
    for check in checks:
        lines.append(f"  [{check.level:<2}] {check.name:<{width}}  {check.detail}")
        if check.fix:
            lines.append(f"        → 直し方: {check.fix}")
    failures = [c for c in checks if c.failed]
    warns = [c for c in checks if c.level == _WARN]
    lines.append("")
    if failures:
        lines.append(f"{len(failures)} 件足りない。上の「直し方」を実行してから"
                     "もう一度 doctor を実行すること。")
    elif warns:
        lines.append(f"動かせる状態。ただし {len(warns)} 件の注意あり。")
    else:
        lines.append("すべて揃っている。python -m videoyard auto <dir> "
                     "--source 録画.mp4 から始められる。")
    return lines


def environment_summary() -> list[str]:
    """環境の要約(不具合報告に貼れる情報)。個人情報は含めない。"""
    from videoyard import __version__
    return [
        f"videoyard {__version__}",
        f"Python {platform.python_version()} / {platform.system()} "
        f"{platform.machine()}",
        f"VIDEOYARD_FONT={os.environ.get('VIDEOYARD_FONT', '(未設定)')}",
        f"VIDEOYARD_DATA_DIR={os.environ.get('VIDEOYARD_DATA_DIR', '(未設定)')}",
    ]
