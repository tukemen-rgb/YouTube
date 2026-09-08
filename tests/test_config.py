"""設定ファイルとプロファイル(S6)— 強さの順と、間違いを黙って飲まないこと。"""

import json
import tempfile
import unittest
from pathlib import Path

from videoyard.config import (
    CONFIG_NAME,
    ConfigError,
    expand_paths,
    find_config,
    load_config,
    merge,
    profile_settings,
    resolve_settings,
)


def _write(directory: Path, data: dict) -> Path:
    path = directory / CONFIG_NAME
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


class Loading(unittest.TestCase):
    def test_reads_defaults_and_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), {
                "defaults": {"fast": True, "bgm_db": -18},
                "profiles": {"ショート": {"shorts": True}},
            })
            config = load_config(path)
            self.assertEqual(config["defaults"], {"fast": True, "bgm_db": -18})
            self.assertEqual(config["profiles"]["ショート"], {"shorts": True})

    def test_unknown_top_level_key_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), {"default": {"fast": True}})  # 打ち間違い
            with self.assertRaises(ConfigError) as ctx:
                load_config(path)
            self.assertIn("defaults", str(ctx.exception))

    def test_unknown_setting_lists_valid_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), {"defaults": {"quick": True}})
            with self.assertRaises(ConfigError) as ctx:
                load_config(path)
            self.assertIn("quick", str(ctx.exception))
            self.assertIn("fast", str(ctx.exception))  # 候補を出す

    def test_wrong_type_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), {"defaults": {"bgm_db": "大きめ"}})
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_bool_not_accepted_for_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), {"defaults": {"bgm_db": True}})
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_broken_json_reports_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / CONFIG_NAME
            path.write_text("{壊れている", encoding="utf-8")
            with self.assertRaises(ConfigError) as ctx:
                load_config(path)
            self.assertIn(CONFIG_NAME, str(ctx.exception))

    def test_missing_file(self):
        with self.assertRaises(ConfigError):
            load_config(Path("/no/such/videoyard.json"))


class Finding(unittest.TestCase):
    def test_walks_up_to_parent_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, {"defaults": {"fast": True}})
            deep = root / "productions" / "案件A"
            deep.mkdir(parents=True)
            self.assertEqual(find_config(deep), root / CONFIG_NAME)

    def test_nearest_config_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, {"defaults": {"fast": True}})
            inner = root / "案件A"
            inner.mkdir()
            _write(inner, {"defaults": {"fast": False}})
            self.assertEqual(find_config(inner), inner / CONFIG_NAME)


class Profiles(unittest.TestCase):
    def test_unknown_profile_lists_available(self):
        config = {"defaults": {}, "profiles": {"ショート": {}, "配信": {}}}
        with self.assertRaises(ConfigError) as ctx:
            profile_settings(config, "しょーと")
        self.assertIn("ショート", str(ctx.exception))
        self.assertIn("配信", str(ctx.exception))

    def test_no_profiles_defined(self):
        with self.assertRaises(ConfigError) as ctx:
            profile_settings({"defaults": {}, "profiles": {}}, "x")
        self.assertIn("定義されていない", str(ctx.exception))


class Precedence(unittest.TestCase):
    def test_merge_ignores_none(self):
        self.assertEqual(merge({"a": 1, "b": 2}, {"b": None, "c": 3}),
                         {"a": 1, "b": 2, "c": 3})

    def test_cli_beats_profile_beats_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, {
                "defaults": {"fast": True, "bgm_db": -16, "transition": "none"},
                "profiles": {"ショート": {"shorts": True, "bgm_db": -18}},
            })
            settings, notes = resolve_settings(
                {"bgm_db": -22, "shorts": None, "fast": None,
                 "transition": None},
                root, profile="ショート")
            self.assertEqual(settings["bgm_db"], -22)      # コマンドが最強
            self.assertTrue(settings["shorts"])            # プロファイル
            self.assertTrue(settings["fast"])              # defaults
            self.assertEqual(settings["transition"], "none")
            self.assertTrue(any("ショート" in n for n in notes))
            self.assertTrue(any("bgm_db" in n for n in notes))  # 上書きを告げる

    def test_no_config_returns_cli_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings, notes = resolve_settings({"fast": True}, Path(tmp) / "x",
                                               config_path=None)
            # 利用者の既定(~/.videoyard)を拾わない環境でも壊れない
            self.assertTrue(settings.get("fast"))
            self.assertIsInstance(notes, list)

    def test_profile_without_config_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            deep = Path(tmp) / "a" / "b"
            deep.mkdir(parents=True)
            # 上に設定が無いことを保証するため config_path を明示しない経路で、
            # 利用者既定も無い前提のときはエラーになること
            from videoyard import config as config_module
            original = config_module.find_config
            config_module.find_config = lambda _s: None
            try:
                with self.assertRaises(ConfigError):
                    resolve_settings({}, deep, profile="ショート")
            finally:
                config_module.find_config = original


class Paths(unittest.TestCase):
    def test_relative_bgm_resolved_against_the_config_file(self):
        settings = expand_paths({"bgm": "音楽/bgm.mp3"}, Path("/案件"))
        self.assertEqual(settings["bgm"], "/案件/音楽/bgm.mp3")

    def test_absolute_and_tilde_paths(self):
        self.assertEqual(expand_paths({"bgm": "/a/b.mp3"}, Path("/x"))["bgm"],
                         "/a/b.mp3")
        expanded = expand_paths({"bgm": "~/b.mp3"}, Path("/x"))["bgm"]
        self.assertTrue(expanded.startswith("/"))
        self.assertNotIn("~", expanded)

    def test_other_settings_untouched(self):
        self.assertEqual(expand_paths({"fast": True}, Path("/x")), {"fast": True})


if __name__ == "__main__":
    unittest.main()
