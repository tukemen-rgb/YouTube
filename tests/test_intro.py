"""紹介動画の生成器 — facts の検証と「facts の範囲でしか語らない」こと。"""

import json
import tempfile
import unittest
from pathlib import Path

from videoyard.intro import GameFacts, IntroError, build_timeline


def _facts(**overrides) -> GameFacts:
    fields = dict(
        name="つりゲーム",
        tagline="タイミングを合わせよう",
        features=("ワンボタン", "3 段階のむずかしさ"),
        controls=(("SPACE", "仕掛けを合わせる"),),
        play_url="https://play-game-yard.com",
    )
    fields.update(overrides)
    return GameFacts(**fields)


class FactsValidation(unittest.TestCase):
    def test_valid(self):
        _facts()

    def test_name_required_nonempty(self):
        with self.assertRaises(IntroError):
            _facts(name="  ")

    def test_too_long_line_rejected(self):
        with self.assertRaises(IntroError):
            _facts(tagline="あ" * 61)

    def test_newline_rejected(self):
        with self.assertRaises(IntroError):
            _facts(tagline="二\n行")

    def test_http_url_rejected(self):
        with self.assertRaises(IntroError):
            _facts(play_url="http://play-game-yard.com")

    def test_load_rejects_unknown_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "facts.json"
            path.write_text(json.dumps({"name": "x", "price": 100}), encoding="utf-8")
            with self.assertRaises(IntroError):
                GameFacts.load(path)

    def test_load_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "facts.json"
            path.write_text(json.dumps({
                "name": "つりゲーム",
                "controls": [["SPACE", "合わせる"]],
            }), encoding="utf-8")
            facts = GameFacts.load(path)
            self.assertEqual(facts.name, "つりゲーム")
            self.assertEqual(facts.controls, (("SPACE", "合わせる"),))

    def test_sample_file_is_valid(self):
        sample = Path(__file__).parent.parent / "examples" / "facts-sample.json"
        facts = GameFacts.load(sample)
        self.assertTrue(facts.name)


class TimelineFromFacts(unittest.TestCase):
    #: 生成器が置いてよい定型見出し。これと facts 以外の文言は出ない。
    _FIXED = ("あそびかた", "いますぐ遊べる")

    def test_scene_count_and_order(self):
        timeline = build_timeline(_facts())
        # 名前 + tagline + 特徴 2 + 操作 + URL = 6 シーン
        self.assertEqual(len(timeline.scenes), 6)
        self.assertEqual(timeline.scenes[0].text, "つりゲーム")

    def test_optional_parts_omitted(self):
        timeline = build_timeline(GameFacts(name="ミニマル"))
        self.assertEqual(len(timeline.scenes), 1)

    def test_every_line_comes_from_facts_or_fixed_labels(self):
        facts = _facts()
        allowed = {facts.name, facts.tagline, facts.play_url}
        allowed.update(facts.features)
        for key, action in facts.controls:
            allowed.add(f"{key} … {action}")
        allowed.update(self._FIXED)
        for scene in build_timeline(facts).scenes:
            for line in scene.text.split("\n"):
                self.assertIn(line, allowed,
                              f"facts に無い文言が発明されている: {line!r}")


    def test_vertical_intro_dimensions(self):
        # C15: ショート用の縦(1080x1920)でも同じ facts から作れる
        timeline = build_timeline(_facts(), width=1080, height=1920)
        self.assertEqual((timeline.width, timeline.height), (1080, 1920))

    def test_deterministic(self):
        self.assertEqual(build_timeline(_facts()), build_timeline(_facts()))


if __name__ == "__main__":
    unittest.main()
