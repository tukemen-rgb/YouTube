"""書き出しの機械検証(サイクル 38)。

EDL / FCPXML は「仕様どおりに書いたつもり」で終わっていて、実際に
編集ソフトが受け取れる形かを機械で確かめていなかった。Resolve や
Premiere の実機は帰国後にしか触れないので、**形式として成立して
いること**を機械で縛る:

* EDL … CMX3600 の列位置・タイムコードの整合を独自パーサで読み直し、
  「書いた計画」と「読み戻した計画」が一致すること(往復検証)
* FCPXML … XML として妥当・DOCTYPE と version・参照(ref)の解決・
  時刻がフレーム境界に乗ること・区間が隙間なく並ぶこと

往復で一致すれば、少なくとも「自分で読み直せない EDL」は出せない。
"""

import re
import unittest
import xml.etree.ElementTree as ET
from fractions import Fraction

from videoyard.cutplan import SPEED_FACTOR, CutPlan, PlanSegment
from videoyard.export import to_edl, to_fcpxml, to_frames

#: CMX3600 のイベント行。番号 リール トラック 編集種別 と 4 つの時刻。
_EVENT = re.compile(
    r"^(?P<num>\d{3})\s+(?P<reel>\S+)\s+(?P<track>\S+)\s+(?P<kind>C)\s+"
    r"(?P<src_in>[\d:]{11})\s+(?P<src_out>[\d:]{11})\s+"
    r"(?P<rec_in>[\d:]{11})\s+(?P<rec_out>[\d:]{11})\s*$")
_MOTION = re.compile(r"^M2\s+\S+\s+(?P<speed>-?[\d.]+)\s+(?P<at>[\d:]{11})\s*$")


def _seconds(timecode: str, fps: int) -> float:
    hours, minutes, secs, frames = (int(p) for p in timecode.split(":"))
    return hours * 3600 + minutes * 60 + secs + frames / fps


def parse_edl(text: str, fps: int) -> list[dict]:
    """自分で書いた EDL を読み直す。読めない行があれば気づける。"""
    events = []
    pending: dict | None = None
    for line in text.splitlines():
        if m := _EVENT.match(line):
            pending = {
                "number": int(m.group("num")),
                "track": m.group("track"),
                "source_in": _seconds(m.group("src_in"), fps),
                "source_out": _seconds(m.group("src_out"), fps),
                "record_in": _seconds(m.group("rec_in"), fps),
                "record_out": _seconds(m.group("rec_out"), fps),
                "speed": 1.0,
                "comments": [],
            }
            events.append(pending)
        elif (m := _MOTION.match(line)) and pending is not None:
            pending["speed"] = float(m.group("speed")) / fps
        elif line.startswith("* COMMENT:") and pending is not None:
            pending["comments"].append(line.split(":", 1)[1].strip())
    return events


def _plan(**overrides) -> CutPlan:
    fields = dict(
        source_path="/videos/source.mp4", source_sha256="0" * 64,
        duration=300.0, width=1920, height=1080, has_audio=True,
        mode="static_or_silent",
        segments=(
            PlanSegment(start=0.0, end=10.0, action="cut"),
            PlanSegment(start=10.0, end=40.0, action="keep", telop="開幕"),
            PlanSegment(start=40.0, end=60.0, action="speed"),
            PlanSegment(start=60.0, end=120.0, action="keep", telop="決着"),
            PlanSegment(start=120.0, end=300.0, action="cut"),
        ),
    )
    fields.update(overrides)
    return CutPlan(**fields)


class EdlRoundTrip(unittest.TestCase):
    """書いた EDL を読み直して、元の計画と一致すること。"""

    def setUp(self):
        self.fps = 30
        self.plan = _plan()
        self.events = parse_edl(to_edl(self.plan, fps=self.fps), self.fps)

    def test_every_rendered_segment_became_an_event(self):
        self.assertEqual(len(self.events), len(self.plan.renders))

    def test_all_lines_are_parseable(self):
        """読めない行が 1 つでもあれば、イベント数が合わなくなる。"""
        text = to_edl(self.plan, fps=self.fps)
        numbered = [ln for ln in text.splitlines() if re.match(r"^\d{3}\s", ln)]
        self.assertEqual(len(numbered), len(self.events))

    def test_source_times_round_trip(self):
        for event, seg in zip(self.events, self.plan.renders, strict=True):
            self.assertAlmostEqual(event["source_in"], seg.start, delta=1 / self.fps)
            self.assertAlmostEqual(event["source_out"], seg.end, delta=1 / self.fps)

    def test_record_times_are_contiguous_and_match_output(self):
        cursor = 0.0
        for event in self.events:
            self.assertAlmostEqual(event["record_in"], cursor, delta=1 / self.fps)
            cursor = event["record_out"]
        self.assertAlmostEqual(cursor, self.plan.output_seconds,
                               delta=2 / self.fps)

    def test_event_numbers_are_sequential_from_one(self):
        self.assertEqual([e["number"] for e in self.events],
                         list(range(1, len(self.events) + 1)))

    def test_speed_segment_carries_its_rate(self):
        speeds = [e["speed"] for e in self.events]
        self.assertIn(SPEED_FACTOR, speeds)
        fast = self.events[speeds.index(SPEED_FACTOR)]
        source_length = fast["source_out"] - fast["source_in"]
        record_length = fast["record_out"] - fast["record_in"]
        self.assertAlmostEqual(source_length / record_length, SPEED_FACTOR,
                               delta=0.1)

    def test_telops_survive_as_comments(self):
        comments = [c for e in self.events for c in e["comments"]]
        self.assertIn("開幕", comments)
        self.assertIn("決着", comments)

    def test_video_only_uses_v_track(self):
        events = parse_edl(to_edl(_plan(has_audio=False), fps=30), 30)
        self.assertTrue(all(e["track"] == "V" for e in events))

    def test_header_is_present(self):
        text = to_edl(self.plan)
        self.assertTrue(text.startswith("TITLE: "))
        self.assertIn("FCM: NON-DROP FRAME", text)


class FcpxmlValidity(unittest.TestCase):
    """FCPXML が形式として成立していること。"""

    def setUp(self):
        self.fps = 30
        self.plan = _plan()
        self.text = to_fcpxml(self.plan, fps=self.fps)
        self.root = ET.fromstring(self.text)

    def test_doctype_and_version(self):
        self.assertIn("<!DOCTYPE fcpxml>", self.text)
        self.assertEqual(self.root.get("version"), "1.9")

    def test_required_structure_exists(self):
        for path in ("./resources/format", "./resources/asset",
                     "./library/event/project/sequence/spine"):
            self.assertIsNotNone(self.root.find(path), path)

    def test_every_reference_resolves(self):
        """ref で指した id が resources に実在すること。"""
        ids = {el.get("id") for el in self.root.findall("./resources/*")}
        refs = {el.get("ref") for el in self.root.iter() if el.get("ref")}
        self.assertTrue(refs)
        self.assertTrue(refs <= ids, f"未解決の参照: {refs - ids}")

    def test_every_format_reference_resolves(self):
        ids = {el.get("id") for el in self.root.findall("./resources/*")}
        formats = {el.get("format") for el in self.root.iter()
                   if el.get("format")}
        self.assertTrue(formats <= ids, f"未解決の format: {formats - ids}")

    def test_all_times_land_on_frame_boundaries(self):
        """有理数時刻の分母が fps。編集ソフトが丸め直さずに済む。"""
        pattern = re.compile(r"^(\d+)/(\d+)s$")
        checked = 0
        for el in self.root.iter():
            for key in ("offset", "start", "duration", "time", "value"):
                value = el.get(key)
                if value in (None, "0s"):
                    continue
                m = pattern.match(value)
                self.assertIsNotNone(m, f"{key}={value} が有理数表記でない")
                self.assertEqual(int(m.group(2)), self.fps,
                                 f"{key}={value} の分母が fps でない")
                checked += 1
        self.assertGreater(checked, 0)

    def test_clips_are_contiguous_on_the_timeline(self):
        """クリップが隙間なく並ぶこと(タイムラインに穴が空かない)。"""
        cursor = Fraction(0)
        for clip in self.root.findall(".//spine/asset-clip"):
            offset = Fraction(clip.get("offset").rstrip("s"))
            duration = Fraction(clip.get("duration").rstrip("s"))
            self.assertEqual(offset, cursor, "クリップの間に隙間がある")
            cursor += duration
        self.assertEqual(
            cursor, Fraction(to_frames(self.plan.output_seconds, self.fps),
                             self.fps))

    def test_sequence_duration_matches_the_clips(self):
        sequence = self.root.find(".//sequence")
        total = sum(Fraction(c.get("duration").rstrip("s"))
                    for c in self.root.findall(".//spine/asset-clip"))
        self.assertEqual(Fraction(sequence.get("duration").rstrip("s")), total)

    def test_clip_source_times_are_inside_the_asset(self):
        asset_duration = Fraction(
            self.root.find("./resources/asset").get("duration").rstrip("s"))
        for clip in self.root.findall(".//spine/asset-clip"):
            start = Fraction(clip.get("start").rstrip("s"))
            self.assertGreaterEqual(start, 0)
            self.assertLessEqual(start, asset_duration)

    def test_speed_segment_time_map_is_monotonic(self):
        for clip in self.root.findall(".//spine/asset-clip"):
            points = clip.findall("./timeMap/timept")
            if not points:
                continue
            times = [Fraction(p.get("time").rstrip("s")) for p in points]
            values = [Fraction(p.get("value").rstrip("s")) for p in points]
            self.assertEqual(times, sorted(times))
            self.assertEqual(values, sorted(values))
            # 出力より素材のほうが長い = 早送りになっている
            self.assertGreater(values[-1], times[-1])

    def test_media_path_is_a_file_uri(self):
        src = self.root.find("./resources/asset/media-rep").get("src")
        self.assertTrue(src.startswith("file://"), src)

    def test_60fps_project_uses_60_denominator(self):
        root = ET.fromstring(to_fcpxml(self.plan, fps=60))
        self.assertEqual(root.find("./resources/format").get("frameDuration"),
                         "1/60s")
        clip = root.find(".//spine/asset-clip")
        self.assertTrue(clip.get("start").endswith("/60s"))


class BothFormatsAgree(unittest.TestCase):
    """EDL と FCPXML が同じ編集を表していること。"""

    def test_same_number_of_clips_and_same_output_length(self):
        plan = _plan()
        fps = 30
        events = parse_edl(to_edl(plan, fps=fps), fps)
        root = ET.fromstring(to_fcpxml(plan, fps=fps))
        clips = root.findall(".//spine/asset-clip")
        self.assertEqual(len(events), len(clips))
        edl_end = events[-1]["record_out"]
        xml_end = float(Fraction(clips[-1].get("offset").rstrip("s"))
                        + Fraction(clips[-1].get("duration").rstrip("s")))
        self.assertAlmostEqual(edl_end, xml_end, delta=2 / fps)


if __name__ == "__main__":
    unittest.main()
