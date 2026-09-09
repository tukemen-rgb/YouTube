"""設定ファイルとプリセット — 毎回同じ指定を打たない(S6)。

競合調査で分かったサービス面の弱点その 6: `--shorts --fast --bgm 曲.mp3
--bgm-db -18 --transition dip` を毎回打つのは現実的でない。競合
(Avidemux のプロファイル、Adobe の video profiles)は設定を名前で
保存できる。

videoyard.json はこういう形:

    {
      "defaults": { "fast": true, "bgm": "~/音楽/bgm.mp3" },
      "profiles": {
        "shorts": { "shorts": true, "bgm_db": -18 },
        "配信切り抜き": { "transition": "dip", "mode": "static_and_silent" }
      }
    }

強さの順は **コマンドラインの指定 > プロファイル > defaults > 組み込みの
既定**。コマンドで打ったものが常に勝つ(設定ファイルに黙って上書き
されない)。

探す場所は production ディレクトリから上へ辿る(git のように)。
案件ごとの設定を案件のフォルダに置けるようにするため。見つからなければ
`~/.videoyard/videoyard.json` を見る。どこを読んだかは必ず表示する
(隠れた設定を作らない)。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from videoyard.learning import data_dir
from videoyard.render import RenderError

CONFIG_NAME = "videoyard.json"
#: 上へ辿る段数の上限。ルートまで無限に遡らない。
MAX_PARENTS = 6

#: 設定ファイルで指定できる項目と型。ここに無いキーは受け付けない
#: (打ち間違いを黙って無視しない)。
SETTING_TYPES: dict[str, type | tuple[type, ...]] = {
    "shorts": bool,
    "vertical": bool,
    "fast": bool,
    "incremental": bool,
    "no_loudnorm": bool,
    "bgm": str,
    "bgm_db": (int, float),
    "transition": str,
    "denoise": str,
    "telop_style": str,
    "mode": str,
    "target_seconds": (int, float),
    "silence_db": (int, float),
    "min_silence": (int, float),
    "still_noise": (int, float),
    "min_still": (int, float),
    "near_still_ydif": (int, float),
    "min_cut": (int, float),
    "min_keep": (int, float),
    "seconds": (int, float),
    "count": int,
    "fps": int,
    "hint": str,
    "text": str,
    "title": str,
    "llm": str,
    "llm_model": str,
    "llm_url": str,
}


class ConfigError(RenderError):
    """設定ファイルが読めない・内容が正しくない。"""


def find_config(start: Path) -> Path | None:
    """start から上へ videoyard.json を探す。無ければ利用者の既定を見る。"""
    current = start.resolve() if start.exists() else start.absolute()
    for _ in range(MAX_PARENTS + 1):
        candidate = current / CONFIG_NAME
        if candidate.is_file():
            return candidate
        if current.parent == current:
            break
        current = current.parent
    fallback = data_dir() / CONFIG_NAME
    return fallback if fallback.is_file() else None


def _check_settings(settings: object, where: str) -> dict[str, Any]:
    if not isinstance(settings, dict):
        raise ConfigError(f"{where} はオブジェクトであること")
    unknown = sorted(set(settings) - set(SETTING_TYPES))
    if unknown:
        known = ", ".join(sorted(SETTING_TYPES))
        raise ConfigError(
            f"{where} に知らない項目がある: {unknown}\n"
            f"使えるのは: {known}"
        )
    for key, value in settings.items():
        expected = SETTING_TYPES[key]
        # bool は int の仲間なので、数値の項目に true が入るのを弾く
        if isinstance(value, bool) and expected is not bool:
            raise ConfigError(f"{where}.{key} は真偽値ではなく値を書くこと")
        if not isinstance(value, expected):
            names = (expected.__name__ if isinstance(expected, type)
                     else "/".join(t.__name__ for t in expected))
            raise ConfigError(
                f"{where}.{key} の型が違う({names} のはずが "
                f"{type(value).__name__})")
    return dict(settings)


def load_config(path: Path) -> dict[str, Any]:
    """設定ファイルを読んで検証する。壊れていれば止まる(fail-closed)。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"設定ファイルがない: {path}") from None
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} が JSON として読めない: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} はオブジェクトであること")
    unknown = sorted(set(data) - {"defaults", "profiles"})
    if unknown:
        raise ConfigError(
            f"{path} に知らないキー: {unknown}(使えるのは defaults / profiles)")
    defaults = _check_settings(data.get("defaults", {}), "defaults")
    raw_profiles = data.get("profiles", {})
    if not isinstance(raw_profiles, dict):
        raise ConfigError("profiles はオブジェクトであること")
    profiles = {
        name: _check_settings(body, f"profiles.{name}")
        for name, body in raw_profiles.items()
    }
    return {"defaults": defaults, "profiles": profiles}


def profile_settings(config: dict[str, Any], name: str) -> dict[str, Any]:
    """プロファイル名 → 設定。無い名前は候補を添えて断る。"""
    profiles = config.get("profiles", {})
    if name not in profiles:
        available = ", ".join(sorted(profiles)) or "(1 つも定義されていない)"
        raise ConfigError(f"プロファイル「{name}」が無い。あるのは: {available}")
    return dict(profiles[name])


def expand_paths(settings: dict[str, Any], base: Path) -> dict[str, Any]:
    """設定中のファイルパスを絶対パスにする。~ も展開する。純粋関数。

    設定ファイルからの相対パスとして解決する(どこから実行しても同じ
    ファイルを指す)。
    """
    resolved = dict(settings)
    for key in ("bgm",):
        value = resolved.get(key)
        if isinstance(value, str) and value:
            path = Path(value).expanduser()
            resolved[key] = str(path if path.is_absolute() else base / path)
    return resolved


def merge(*layers: dict[str, Any]) -> dict[str, Any]:
    """後の層ほど強い。None は「指定なし」として無視する。純粋関数。"""
    merged: dict[str, Any] = {}
    for layer in layers:
        for key, value in layer.items():
            if value is not None:
                merged[key] = value
    return merged


def resolve_settings(cli: dict[str, Any], start: Path,
                     profile: str | None = None,
                     config_path: Path | None = None,
                     ) -> tuple[dict[str, Any], list[str]]:
    """設定を決める。返すのは (設定, 何を読んだかの説明行)。

    強さの順: cli > プロファイル > defaults。cli の None は「指定なし」。
    """
    path = config_path or find_config(start)
    notes: list[str] = []
    if path is None:
        if profile:
            raise ConfigError(
                f"プロファイル「{profile}」を指定されたが {CONFIG_NAME} が無い")
        return merge(cli), notes
    config = load_config(path)
    notes.append(f"設定を読んだ: {path}")
    layers = [expand_paths(config["defaults"], path.parent)]
    if profile:
        layers.append(expand_paths(profile_settings(config, profile), path.parent))
        notes.append(f"プロファイル: {profile}")
    layers.append(cli)
    settings = merge(*layers)
    overridden = sorted(k for k, v in cli.items()
                        if v is not None and any(k in layer for layer in layers[:-1]))
    if overridden:
        notes.append("コマンドの指定が優先: " + ", ".join(overridden))
    return settings, notes
