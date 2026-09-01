"""添削からの学習 — ラベル付け・訓練・保存・採点への反映。全部オフライン。"""

import json
import tempfile
import unittest
from pathlib import Path

from videoyard.cutplan import CutPlan, PlanSegment
from videoyard.excitement import ScoreWeights, combine_scores
from videoyard.learning import (
    MIN_EXAMPLES,
    Example,
    LearningError,
    extract_examples,
    load_examples,
    load_weights,
    record_feedback,
    save_weights,
    train,
)

_SHA = "0" * 64


def _plan(segments) -> CutPlan:
    return CutPlan(
        source_path="/s.mp4", source_sha256=_SHA, duration=10.0,
        width=320, height=240, has_audio=True, mode="static_or_silent",
        segments=tuple(segments),
    )


def _windows(count=10, window=1.0):
    return {
        "window_seconds": window,
        "duration": count * window,
        "features": {
            "motion": [float(i) for i in range(count)],
            "loudness": [0.5] * count,
            "onset": [0.0] * count,
        },
    }


class Labeling(unittest.TestCase):
    def test_labels_follow_human_decision(self):
        proposal = _plan([
            PlanSegment(start=0.0, end=2.0, action="cut"),
            PlanSegment(start=2.0, end=10.0, action="keep"),
        ])
        final = _plan([
            PlanSegment(start=0.0, end=2.0, action="keep"),   # 人が復活させた
            PlanSegment(start=2.0, end=6.0, action="keep"),
            PlanSegment(start=6.0, end=10.0, action="cut"),   # 人が切った
        ])
        examples = extract_examples(proposal, final, _windows())
        self.assertEqual(len(examples), 10)
        # 0〜2 秒: 案は cut、人は keep → 正解 1
        self.assertTrue(all(e.kept for e in examples[0:2]))
        # 2〜6 秒: 人もそのまま keep → 正解 1
        self.assertTrue(all(e.kept for e in examples[2:6]))
        # 6〜10 秒: 案は keep、人は cut → 正解 0
        self.assertTrue(all(not e.kept for e in examples[6:10]))

    def test_untouched_boring_cuts_excluded(self):
        segments = [
            PlanSegment(start=0.0, end=4.0, action="cut"),
            PlanSegment(start=4.0, end=10.0, action="keep"),
        ]
        examples = extract_examples(_plan(segments), _plan(segments), _windows())
        self.assertEqual(len(examples), 6)  # cut→cut の 4 窓は含めない


class Training(unittest.TestCase):
    def _separable(self, n=40):
        # 動きが大きい窓ほど残された、というきれいなデータ
        out = []
        for i in range(n):
            motion = 1.0 if i % 2 == 0 else -1.0
            out.append(Example(motion=motion, loudness=0.0, onset=0.0,
                               kept=(motion > 0)))
        return out

    def test_learns_positive_motion_weight(self):
        weights, accuracy = train(self._separable())
        self.assertGreater(weights.motion, 0.5)
        self.assertGreaterEqual(accuracy, 0.95)

    def test_deterministic(self):
        self.assertEqual(train(self._separable()), train(self._separable()))

    def test_refuses_small_data(self):
        with self.assertRaises(LearningError):
            train(self._separable(MIN_EXAMPLES - 1))


class Storage(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name) / "data"
        self.prod = Path(self._tmp.name) / "prod"
        self.prod.mkdir()
        proposal = _plan([
            PlanSegment(start=0.0, end=2.0, action="cut"),
            PlanSegment(start=2.0, end=10.0, action="keep"),
        ])
        final = _plan([
            PlanSegment(start=0.0, end=2.0, action="cut"),
            PlanSegment(start=2.0, end=6.0, action="keep"),
            PlanSegment(start=6.0, end=10.0, action="cut"),
        ])
        proposal.save(self.prod / "cutplan.proposed.json")
        final.save(self.prod / "cutplan.json")
        (self.prod / "analysis_windows.json").write_text(
            json.dumps(_windows()), encoding="utf-8"
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_record_and_load(self):
        count = record_feedback(self.prod, self.data_dir)
        self.assertEqual(count, 8)  # keep 案の 8 窓
        self.assertEqual(len(load_examples(self.data_dir)), 8)

    def test_recut_does_not_duplicate(self):
        record_feedback(self.prod, self.data_dir)
        record_feedback(self.prod, self.data_dir)  # cut のやり直し
        self.assertEqual(len(load_examples(self.data_dir)), 8)

    def test_old_production_without_proposal_is_skipped(self):
        (self.prod / "cutplan.proposed.json").unlink()
        self.assertEqual(record_feedback(self.prod, self.data_dir), 0)

    def test_weights_round_trip(self):
        weights = ScoreWeights(motion=1.2, loudness=-0.3, onset=0.4)
        save_weights(weights, examples=42, accuracy=0.9, directory=self.data_dir)
        loaded = load_weights(self.data_dir)
        assert loaded is not None
        self.assertEqual(loaded[0], weights)
        self.assertEqual(loaded[1]["examples"], 42)

    def test_missing_weights_is_none_but_corrupt_is_error(self):
        self.assertIsNone(load_weights(self.data_dir))
        self.data_dir.mkdir(parents=True)
        (self.data_dir / "weights.json").write_text("{broken", encoding="utf-8")
        with self.assertRaises(LearningError):
            load_weights(self.data_dir)


class WeightsChangeScoring(unittest.TestCase):
    def test_learned_weights_move_the_peak(self):
        motion = [9.0, 1.0, 1.0, 1.0]
        loudness = [-40.0, -40.0, -10.0, -40.0]
        by_motion = combine_scores(motion, loudness, ScoreWeights(1.0, 0.0, 0.0))
        by_loudness = combine_scores(motion, loudness, ScoreWeights(0.0, 1.0, 0.0))
        self.assertEqual(max(range(4), key=lambda i: by_motion[i]), 0)
        self.assertEqual(max(range(4), key=lambda i: by_loudness[i]), 2)


if __name__ == "__main__":
    unittest.main()
