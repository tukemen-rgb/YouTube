"""盛り上がりグラフ(U12)— SVG の構造検証。"""

import unittest
import xml.etree.ElementTree as ET

from videoyard.cutplan import CutPlan, PlanSegment
from videoyard.graph import excitement_svg


def _plan() -> CutPlan:
    return CutPlan(
        source_path="/s.mp4", source_sha256="0" * 64, duration=10.0,
        width=320, height=240, has_audio=True, mode="static_or_silent",
        segments=(
            PlanSegment(start=0.0, end=2.0, action="cut"),
            PlanSegment(start=2.0, end=5.0, action="keep", telop="シーン 1",
                        excite=80, reason="★盛り上がり候補"),
            PlanSegment(start=5.0, end=7.0, action="cut"),
            PlanSegment(start=7.0, end=10.0, action="keep", telop="シーン 2", excite=40),
        ),
    )


class Svg(unittest.TestCase):
    def _render(self):
        scores = [float((i * 13) % 101) for i in range(20)]  # 窓 0.5s x 20 = 10s
        return excitement_svg(scores, 0.5, _plan())

    def test_valid_xml_with_line_and_shading(self):
        svg = self._render()
        root = ET.fromstring(svg)  # 壊れた SVG ならここで落ちる
        text = svg
        self.assertIn("<polyline", text)                 # 点数の折れ線
        self.assertEqual(text.count('fill-opacity="0.45"'), 2)  # cut 2 区間の網掛け
        self.assertIn("★", text)                        # 最高点の印
        self.assertIn("シーン 1", text)                  # テロップの札
        self.assertEqual(root.tag.split("}")[-1], "svg")

    def test_deterministic(self):
        self.assertEqual(self._render(), self._render())


if __name__ == "__main__":
    unittest.main()
