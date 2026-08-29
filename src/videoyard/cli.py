"""コマンドライン入口。

python -m videoyard demo <dir>     お手本の production を作る
python -m videoyard render <dir>   timeline.json → out/video.mp4
python -m videoyard status <dir>   段ごとの進み具合を表示
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="videoyard", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, handler in (("demo", cmd_demo), ("render", cmd_render), ("status", cmd_status)):
        cmd = sub.add_parser(name)
        cmd.add_argument("directory", type=Path)
        cmd.set_defaults(handler=handler)
    args = parser.parse_args(argv)
    try:
        return args.handler(args.directory)
    except (TimelineError, RenderError, JobError, FontError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
