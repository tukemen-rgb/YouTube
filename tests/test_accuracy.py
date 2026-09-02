"""判定精度のリグレッション防止(C19)。

正解の構造を仕込んだ合成動画で analyze を採点し、精度が一定以上で
あることをテストとして常設する。重みや検出の変更で判定品質が落ちたら
ここで止まる。

動画の構造(正解):
  0-3   静止+無音     → 切る
  3-8   動き+ふつうの音 → 残す
  8-11  静止+無音     → 切る
  11-14 動き+小さい音  → 残す(既定モードでは無音扱いになる既知の限界)
  14-17 静止+無音     → 切る
  17-22 動き+大きい音  → 残す(★=一番の見どころ、になるべき)
  22-24 静止+無音     → 切る
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from videoyard.analyze import AnalyzeParams, analyze, diagnose

_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None

#: (種類, 秒数, 音量) の並び。モジュール先頭 docstring の正解表と一致させる。
SPEC = (
    ("still", 3, None),
    ("move", 5, "normal"),
    ("still", 3, None),
    ("move", 3, "quiet"),
    ("still", 3, None),
    ("move", 5, "loud"),
    ("still", 2, None),
)
#: 検出の窓(0.5 秒)による境界のずれは採点しない。
BOUNDARY_TOLERANCE = 0.6


def synth_video(base: Path) -> Path:
    pieces = []
    for i, (kind, dur, vol) in enumerate(SPEC):
        piece = base / f"p{i}.mp4"
        if kind == "still":
            video = f"color=c=blue:s=320x240:d={dur}:r=30"
            audio = "anullsrc=r=44100:cl=stereo"
            af = []
        else:
            video = f"testsrc=s=320x240:d={dur}:r=30"
            audio = "sine=frequency=440:r=44100"
            gain = {"quiet": "volume=0.08", "normal": "volume=0.5",
                    "loud": "volume=2.0"}[vol]
            af = ["-af", gain]
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", video, "-f", "lavfi", "-i", audio,
             "-t", str(dur), *af, "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-ac", "2", "-shortest", str(piece)],
            check=True, capture_output=True)
        pieces.append(piece)
    listfile = base / "list.txt"
    listfile.write_text("".join(f"file '{p}'\n" for p in pieces), encoding="utf-8")
    source = base / "source.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "concat", "-safe", "0", "-i", str(listfile),
         "-c", "copy", str(source)], check=True, capture_output=True)
    return source


def truth_action(t: float) -> str:
    cursor = 0.0
    for kind, dur, _ in SPEC:
        if cursor <= t < cursor + dur:
            return "cut" if kind == "still" else "keep"
        cursor += dur
    return "keep"


def grade(plan) -> dict[str, float]:
    """0.1 秒刻みで答え合わせ(区間境界 ±0.6 秒は不問)。"""

    def plan_action(t: float) -> str:
        for seg in plan.segments:
            if seg.start <= t < seg.end:
                return seg.action
        return "keep"

    boundaries = []
    cursor = 0.0
    for _, dur, _ in SPEC[:-1]:
        cursor += dur
        boundaries.append(cursor)
    total = cursor + SPEC[-1][1]

    step = 0.1
    graded = correct = overcut = undercut = 0
    t = step / 2
    while t < total:
        if not any(abs(t - b) <= BOUNDARY_TOLERANCE for b in boundaries):
            truth, pred = truth_action(t), plan_action(t)
            graded += 1
            if truth == pred:
                correct += 1
            elif pred == "cut":
                overcut += 1
            else:
                undercut += 1
        t += step
    return {
        "match": correct / graded,
        "overcut_seconds": overcut * step,
        "undercut_seconds": undercut * step,
    }


@unittest.skipUnless(_HAS_FFMPEG, "ffmpeg が無い環境ではスキップ")
class AccuracyBenchmark(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.source = synth_video(Path(cls._tmp.name))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _analyze(self, name: str, params: AnalyzeParams):
        directory = Path(self._tmp.name) / name
        directory.mkdir()
        return analyze(directory, self.source, params)

    def test_default_mode_meets_floor(self):
        plan = self._analyze("prod_or", AnalyzeParams())
        score = grade(plan)
        # 既定モードの下限: 一致 85% 以上・退屈の切り残しゼロ。
        # (「小さい音+動き」を無音扱いで切る 1.8 秒は既知の限界 = U15)
        self.assertGreaterEqual(score["match"], 0.85, score)
        self.assertEqual(score["undercut_seconds"], 0.0, score)
        # その既知の限界を診断が利用者に知らせること
        advice = diagnose(plan, AnalyzeParams(), plan.has_audio)
        self.assertTrue(any("static_and_silent" in a for a in advice), advice)

    def test_and_mode_is_perfect_on_benchmark(self):
        plan = self._analyze("prod_and", AnalyzeParams(mode="static_and_silent"))
        score = grade(plan)
        self.assertEqual(score["match"], 1.0, score)

    def test_star_hits_loudest_scene(self):
        plan = self._analyze("prod_star", AnalyzeParams())
        star = [s for s in plan.keeps if "★" in s.reason]
        self.assertEqual(len(star), 1)
        # ★の区間は「大きい音の場面」(17〜22 秒)と重なること
        self.assertLess(star[0].start, 22.0)
        self.assertGreater(star[0].end, 17.0)


if __name__ == "__main__":
    unittest.main()
