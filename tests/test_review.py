"""確認ページ(S4)— 中身の正しさと、文字列がページへの命令にならないこと。"""

import tempfile
import unittest
from pathlib import Path

from videoyard.cutplan import CutPlan, PlanSegment
from videoyard.review import (
    ReviewError,
    build_review_html,
    plan_rows,
    sanitize_svg,
    write_review,
)


def _plan(**overrides) -> CutPlan:
    fields = dict(
        source_path="/videos/ボス戦.mp4",
        source_sha256="0" * 64,
        duration=100.0,
        width=1920,
        height=1080,
        has_audio=True,
        mode="static_or_silent",
        segments=(
            PlanSegment(start=0.0, end=10.0, action="cut",
                        reason="退屈な区間(静止画+無音)"),
            PlanSegment(start=10.0, end=30.0, action="keep", telop="開幕",
                        excite=60),
            PlanSegment(start=30.0, end=50.0, action="speed", reason="移動中"),
            PlanSegment(start=50.0, end=70.0, action="keep", telop="決着",
                        excite=98, reason="★盛り上がり候補"),
        ),
    )
    fields.update(overrides)
    return CutPlan(**fields)


def _html(plan=None, **kwargs) -> str:
    plan = plan or _plan()
    defaults = dict(
        title="テスト", facts=["元動画 100.0 秒"], rows=plan_rows(plan),
        chapters=[(0.0, "開幕")], description="【チャプター】\n0:00 開幕\n",
    )
    defaults.update(kwargs)
    return build_review_html(plan, **defaults)


class Rows(unittest.TestCase):
    def test_all_segments_listed_with_labels(self):
        rows = plan_rows(_plan())
        self.assertEqual([r["label"] for r in rows],
                         ["切る", "残す", "≫ 倍速", "残す"])

    def test_output_position_accounts_for_cuts_and_speed(self):
        rows = plan_rows(_plan())
        self.assertEqual(rows[0]["output_at"], "—")      # 切った区間は出ない
        self.assertEqual(rows[1]["output_at"], "0:00")   # 1 本目
        self.assertEqual(rows[2]["output_at"], "0:20")   # 20 秒使ったあと
        # 倍速の 20 秒は出力では 5 秒しか占めない → 次は 0:25
        self.assertEqual(rows[3]["output_at"], "0:25")

    def test_excite_blank_when_absent(self):
        rows = plan_rows(_plan())
        self.assertEqual(rows[0]["excite"], "")
        self.assertEqual(rows[1]["excite"], "60")


class Html(unittest.TestCase):
    def test_contains_the_five_sections(self):
        html = _html(video_src="video.mp4", svg_text="<svg><rect/></svg>",
                     thumbnails=["thumbnails/thumb_1.png"])
        for heading in ("出来た動画", "盛り上がり", "サムネ候補",
                        "カット計画", "説明文の下書き"):
            self.assertIn(heading, html)

    def test_missing_parts_are_omitted_not_broken(self):
        html = _html()  # 動画もサムネもグラフも無い
        self.assertNotIn("<video", html)
        self.assertNotIn("<img", html)
        self.assertIn("カット計画", html)  # あるものは出る

    def test_no_external_resources(self):
        html = _html(video_src="video.mp4", svg_text="<svg/>",
                     thumbnails=["thumbnails/thumb_1.png"])
        # ネットに繋がなくても開ける = 外部の読み込みが 1 つも無い
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        self.assertNotIn("<script", html.lower())

    def test_relative_paths_so_the_folder_is_portable(self):
        html = _html(video_src="video.mp4",
                     thumbnails=["thumbnails/thumb_1.png"])
        self.assertIn('src="video.mp4"', html)
        self.assertIn('src="thumbnails/thumb_1.png"', html)

    def test_star_marked_segment_highlighted(self):
        self.assertIn("★", _html())

    def test_titled_thumbnails_are_labelled(self):
        from videoyard.review import thumbnail_caption
        self.assertEqual(thumbnail_caption("thumbnails/thumb_2.png", 2), "候補 2")
        self.assertEqual(thumbnail_caption("thumbnails/thumb_2_titled.png", 2),
                         "候補 2(文字入り)")
        html = _html(thumbnails=["thumbnails/thumb_1.png",
                                 "thumbnails/thumb_1_titled.png"])
        self.assertIn("候補 1(文字入り)", html)


class Escaping(unittest.TestCase):
    """テロップは「データ」であって、ページへの命令ではない。"""

    def test_telop_with_html_is_escaped(self):
        plan = _plan(segments=(
            PlanSegment(start=0.0, end=10.0, action="keep",
                        telop='<script>alert("やられた")</script>'),
        ))
        html = _html(plan, rows=plan_rows(plan))
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;", html)

    def test_reason_and_title_escaped(self):
        plan = _plan(segments=(
            PlanSegment(start=0.0, end=10.0, action="keep",
                        reason='<img src=x onerror="bad()">'),
        ))
        html = _html(plan, title='<b>タイトル</b>', rows=plan_rows(plan))
        self.assertNotIn("<img src=x", html)
        self.assertNotIn("<b>タイトル</b>", html)

    def test_paths_escaped_in_attributes(self):
        html = _html(video_src='x" onerror="bad()')
        self.assertNotIn('onerror="bad()"', html)

    def test_svg_script_is_stripped(self):
        svg = ('<?xml version="1.0"?><svg><script>bad()</script>'
               "<rect width='5'/></svg>")
        clean = sanitize_svg(svg)
        self.assertNotIn("<script", clean)
        self.assertNotIn("<?xml", clean)
        self.assertIn("<rect", clean)  # 中身は残る

    def test_svg_script_stripped_in_page(self):
        html = _html(svg_text="<svg><script>bad()</script></svg>")
        self.assertNotIn("bad()", html)


class WriteReview(unittest.TestCase):
    def test_writes_page_from_plan_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _plan().save(directory / "cutplan.json")
            path = write_review(directory)
            self.assertEqual(path.name, "review.html")
            text = path.read_text(encoding="utf-8")
            self.assertIn("ボス戦", text)
            self.assertIn("カット計画", text)

    def test_includes_video_and_thumbnails_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _plan().save(directory / "cutplan.json")
            out = directory / "out"
            (out / "thumbnails").mkdir(parents=True)
            (out / "video.mp4").write_bytes(b"x")
            (out / "thumbnails" / "thumb_1.png").write_bytes(b"x")
            (directory / "excitement.svg").write_text("<svg><rect/></svg>",
                                                      encoding="utf-8")
            text = write_review(directory).read_text(encoding="utf-8")
            self.assertIn("<video", text)
            self.assertIn("thumb_1.png", text)
            self.assertIn("<rect/>", text)

    def test_missing_plan_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ReviewError):
            write_review(Path(tmp))

    def test_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _plan().save(directory / "cutplan.json")
            first = write_review(directory).read_text(encoding="utf-8")
            second = write_review(directory).read_text(encoding="utf-8")
            self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
