"""ショート候補の複数生成 — 1 本の長尺から N 本の縦クリップ案(C21)。

競合(OpusClip 等)は長尺から候補を複数出して人に選ばせる。同じ発想を
videoyard の分担で行う: 盛り上がり度の高い keep 区間から順に、それぞれ
60 秒以内のクリップ計画(cutplan)を作り、shorts/clip_n/ という
子 production に保存して縦動画に書き出す。どの区間がなぜ選ばれたかは
各クリップの cutplan.json に全部残る(人が選ぶ・直すのはいつもどおり)。
"""

from __future__ import annotations

from videoyard.cut import SHORTS_RECOMMENDED_SECONDS, CutError
from videoyard.cutplan import CutPlan, PlanSegment

#: 候補数の既定と上限。多すぎても選べない。
DEFAULT_COUNT = 3
MAX_COUNT = 10


class ShortsError(CutError):
    """ショート候補が作れない。"""


def _trimmed_to_target(seg: PlanSegment, target: float) -> PlanSegment:
    """長すぎる keep を、中央を核に target 秒へ縮める。"""
    length = seg.end - seg.start
    if length <= target:
        return seg
    center = (seg.start + seg.end) / 2
    start = round(center - target / 2, 3)
    return seg.replaced(
        start=start, end=round(start + target, 3),
        reason=(seg.reason + " " if seg.reason else "")
        + f"(ショート用に中央 {target:.0f} 秒へ短縮)",
    )


def propose_short_plans(plan: CutPlan, count: int = DEFAULT_COUNT,
                        target_seconds: float = SHORTS_RECOMMENDED_SECONDS,
                        ) -> list[CutPlan]:
    """盛り上がり上位の keep から、クリップごとの計画を作る。純粋関数。

    候補 1 本 = keep 1 区間(target 秒以内に短縮)。他の区間はすべて
    「ショート候補外」の cut にする。候補同士は重ならない。
    """
    if not 1 <= count <= MAX_COUNT:
        raise ShortsError(f"count は 1〜{MAX_COUNT}: {count}")
    scored = [s for s in plan.keeps if s.excite is not None]
    if not scored:
        raise ShortsError(
            "盛り上がり度が付いた keep 区間が無い(先に analyze を実行)")
    ranked = sorted(scored, key=lambda s: (-(s.excite or 0), s.start))[:count]

    plans = []
    for chosen in sorted(ranked, key=lambda s: s.start):
        clip_keep = _trimmed_to_target(chosen, target_seconds)
        segments = []
        for seg in plan.segments:
            if seg is chosen:
                segments.append(clip_keep)
            elif seg.action in ("keep", "speed"):
                segments.append(seg.replaced(
                    action="cut", telop="", reason="ショート候補外"))
            else:
                segments.append(seg)
        plans.append(CutPlan(
            source_path=plan.source_path,
            source_sha256=plan.source_sha256,
            duration=plan.duration,
            width=plan.width,
            height=plan.height,
            has_audio=plan.has_audio,
            mode=plan.mode,
            segments=tuple(segments),
        ))
    return plans
