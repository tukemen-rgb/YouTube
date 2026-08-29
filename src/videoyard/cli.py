"""コマンドライン入口。

python -m videoyard demo <dir>       お手本の production を作る
python -m videoyard render <dir>     timeline.json → out/video.mp4
python -m videoyard analyze <dir> --source 元動画.mp4
                                     退屈な区間を検出してカット計画の案を作る
python -m videoyard cut <dir>        cutplan.json のとおりに切ってつなぐ
python -m videoyard status <dir>     段ごとの進み具合を表示
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from videoyard.analyze import MODES, AnalyzeError, AnalyzeParams, analyze
from videoyard.cut import cut
from videoyard.cutplan import CutPlanError
from videoyard.fonts import FontError
from videoyard.job import JobError, ProductionJob
from videoyard.render import RenderError, render
from videoyard.timeline import Scene, Timeline, TimelineError

_DEMO_SCENES = (
    Scene(text="VIDEOYARD v0.1", duration_seconds=2.5),
    Scene(
        text="timeline.json だけから\n同じ動画が何度でも作れる",
        duration_seconds=3.5,
        background="#1d3557",
    ),
    Scene(
        text="公開は人間が承認したときだけ",
        duration_seconds=3.0,
        background="#2a2a2a",
        text_color="#ffd166",
    ),
)


def cmd_demo(directory: Path) -> int:
    job = ProductionJob.create(directory, title="videoyard デモ動画")
    Timeline(scenes=_DEMO_SCENES, width=1280, height=720, fps=30).save(
        directory / "timeline.json"
    )
    print(job.summary())
    print(f"\n次: python -m videoyard render {directory}")
    return 0


def cmd_render(directory: Path) -> int:
    job = ProductionJob.load(directory)
    manifest = render(directory)
    job.mark_done("assembly", note="videoyard render")
    print(f"出力: {directory / 'out' / 'video.mp4'}")
    print(f"長さ: {manifest['duration_seconds']} 秒 / {manifest['output_bytes']} バイト")
    print(f"来歴: {directory / 'out' / 'render_manifest.json'}")
    return 0


def cmd_status(directory: Path) -> int:
    print(ProductionJob.load(directory).summary())
    return 0


def cmd_analyze(directory: Path, args: argparse.Namespace) -> int:
    directory.mkdir(parents=True, exist_ok=True)
    if not (directory / "job.json").is_file():
        ProductionJob.create(directory, title=f"{args.source.name} のダイジェスト")
    params = AnalyzeParams(
        mode=args.mode,
        silence_db=args.silence_db,
        min_silence=args.min_silence,
        still_noise=args.still_noise,
        min_still=args.min_still,
        min_cut=args.min_cut,
        min_keep=args.min_keep,
    )
    plan = analyze(directory, args.source, params)
    keeps = plan.keeps
    print(f"カット計画の案: {directory / 'cutplan.json'}")
    print(f"元動画 {plan.duration:.1f} 秒 → 残し {plan.kept_seconds:.1f} 秒"
          f"(keep {len(keeps)} 区間 / 全 {len(plan.segments)} 区間)")
    for seg in plan.segments:
        mark = "残す" if seg.action == "keep" else "切る"
        print(f"  {seg.start:7.1f}〜{seg.end:7.1f}  {mark}  {seg.reason}")
    print("\n案を直すなら cutplan.json を編集(action の keep/cut と telop は自由)。")
    print(f"確定したら: python -m videoyard cut {directory}")
    return 0


def cmd_cut(directory: Path, _args: argparse.Namespace) -> int:
    job = ProductionJob.load(directory)
    manifest = cut(directory)
    job.mark_done("assembly", note="videoyard cut")
    print(f"出力: {directory / 'out' / 'video.mp4'}")
    print(f"長さ: {manifest['duration_seconds']:.1f} 秒 / {manifest['output_bytes']} バイト")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="videoyard", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, handler in (("demo", cmd_demo), ("render", cmd_render), ("status", cmd_status)):
        cmd = sub.add_parser(name)
        cmd.add_argument("directory", type=Path)
        cmd.set_defaults(handler=lambda a, h=handler: h(a.directory))

    analyze_cmd = sub.add_parser("analyze", help="退屈な区間を検出しカット計画の案を作る")
    analyze_cmd.add_argument("directory", type=Path)
    analyze_cmd.add_argument("--source", type=Path, required=True, help="元動画ファイル")
    analyze_cmd.add_argument("--mode", choices=MODES, default="static_or_silent")
    analyze_cmd.add_argument("--silence-db", type=float, default=-35.0,
                             help="これより静かなら無音(dB)")
    analyze_cmd.add_argument("--min-silence", type=float, default=0.8)
    analyze_cmd.add_argument("--still-noise", type=float, default=0.003)
    analyze_cmd.add_argument("--min-still", type=float, default=1.0)
    analyze_cmd.add_argument("--min-cut", type=float, default=1.0)
    analyze_cmd.add_argument("--min-keep", type=float, default=0.6)
    analyze_cmd.set_defaults(handler=lambda a: cmd_analyze(a.directory, a))

    cut_cmd = sub.add_parser("cut", help="cutplan.json のとおりに切ってつなぐ")
    cut_cmd.add_argument("directory", type=Path)
    cut_cmd.set_defaults(handler=lambda a: cmd_cut(a.directory, a))

    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (TimelineError, RenderError, JobError, FontError,
            AnalyzeError, CutPlanError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
