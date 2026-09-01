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
import os
import sys
import time
from pathlib import Path

from videoyard.analyze import MODES, AnalyzeError, AnalyzeParams, analyze
from videoyard.cut import (
    BGM_DEFAULT_GAIN_DB,
    SHORTS_RECOMMENDED_SECONDS,
    TRANSITIONS,
    cut,
)
from videoyard.cutplan import CutPlanError
from videoyard.fonts import FontError
from videoyard.intro import GameFacts, IntroError, build_timeline
from videoyard.job import JobError, ProductionJob
from videoyard.learning import (
    LearningError,
    load_examples,
    load_weights,
    record_feedback,
    save_weights,
    train,
)
from videoyard.llm import LlmError, OllamaTelopWriter
from videoyard.render import RenderError, render
from videoyard.thumbs import extract_thumbnails
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
        near_still_ydif=args.near_still_ydif,
        min_cut=args.min_cut,
        min_keep=args.min_keep,
        target_seconds=args.target_seconds,
    )
    llm_choice = args.llm or os.environ.get("VIDEOYARD_LLM", "none")
    writer = None
    if llm_choice == "ollama":
        writer = OllamaTelopWriter(
            model=args.llm_model or os.environ.get("VIDEOYARD_LLM_MODEL", ""),
            url=args.llm_url or os.environ.get("VIDEOYARD_LLM_URL", "http://127.0.0.1:11434"),
        )
    elif llm_choice != "none":
        raise LlmError(f"知らない LLM 指定: {llm_choice}(none / ollama)")
    learned = load_weights()
    weights = None
    if learned is not None:
        weights, meta = learned
        print(f"学習済みの採点基準を使用({meta.get('examples')} 件の添削から "
              f"{meta.get('trained_at')} に学習)")
    started = time.monotonic()

    def show_progress(message: str) -> None:
        print(f"[{time.monotonic() - started:4.0f}秒] {message}", flush=True)

    plan = analyze(directory, args.source, params, writer=writer, hint=args.hint,
                   weights=weights, progress=show_progress)
    print(f"カット計画の案: {directory / 'cutplan.json'}")
    for line in format_plan_report(plan):
        print(line)
    print("\n案を直すなら cutplan.json を編集(action の keep/cut と telop は自由)。")
    print(f"確定したら: python -m videoyard cut {directory}")
    return 0


#: 区間一覧をそのまま全部出す上限。これを超えたら要約表示(U4)。
_REPORT_MAX_ROWS = 12


def format_plan_report(plan, max_rows: int = _REPORT_MAX_ROWS) -> list[str]:
    """カット計画の表示行。長い動画では全行を流さず要約する(U4)。"""
    keeps = plan.keeps
    lines = [
        f"元動画 {plan.duration:.1f} 秒 → 残し {plan.kept_seconds:.1f} 秒"
        f"(keep {len(keeps)} 区間 / 全 {len(plan.segments)} 区間)"
    ]

    def row(seg) -> str:
        mark = "残す" if seg.action == "keep" else "切る"
        excite = f" 盛り上がり度{seg.excite:3d}" if seg.excite is not None else ""
        return f"  {seg.start:7.1f}〜{seg.end:7.1f}  {mark}{excite}  {seg.reason}"

    if len(plan.segments) <= max_rows:
        lines += [row(seg) for seg in plan.segments]
        return lines
    top = sorted(keeps, key=lambda s: (-(s.excite or 0), s.start))[:5]
    lines.append(f"区間が多いので盛り上がり度の上位 {len(top)} 件だけ表示:")
    lines += [row(seg) for seg in sorted(top, key=lambda s: s.start)]
    lines.append(f"  …残り {len(plan.segments) - len(top)} 区間は cutplan.json を参照")
    return lines


def cmd_cut(directory: Path, args: argparse.Namespace) -> int:
    job = ProductionJob.load(directory)
    manifest = cut(directory, normalize_loudness=not args.no_loudnorm,
                   vertical=args.vertical, fast=args.fast,
                   bgm=args.bgm, bgm_gain_db=args.bgm_db,
                   transition=args.transition)
    job.mark_done("assembly", note="videoyard cut")
    print(f"出力: {directory / 'out' / 'video.mp4'}")
    if args.vertical:
        duration = float(manifest["duration_seconds"])  # type: ignore[arg-type]
        if duration > SHORTS_RECOMMENDED_SECONDS:
            print(f"注意: {duration:.0f} 秒はショート推奨({SHORTS_RECOMMENDED_SECONDS:.0f} 秒)"
                  "を超えている。analyze --target-seconds 60 で縮められる。")
    print(f"長さ: {manifest['duration_seconds']:.1f} 秒 / {manifest['output_bytes']} バイト")
    recorded = record_feedback(directory)
    if recorded:
        print(f"添削 {recorded} 窓ぶんを学習用に記録した"
              "(貯まったら python -m videoyard learn)")
    thumbnails = extract_thumbnails(directory)
    if thumbnails:
        print(f"サムネイル候補 {len(thumbnails)} 枚: {thumbnails[0].parent}"
              "(盛り上がり度上位の瞬間。thumbnails.json に選定理由)")
    return 0


def cmd_intro(directory: Path, args: argparse.Namespace) -> int:
    facts = GameFacts.load(args.facts)
    directory.mkdir(parents=True, exist_ok=True)
    if not (directory / "job.json").is_file():
        ProductionJob.create(directory, title=f"{facts.name} の紹介動画")
    timeline = build_timeline(facts)
    timeline.save(directory / "timeline.json")
    print(f"紹介動画のタイムライン: {directory / 'timeline.json'}")
    print(f"シーン {len(timeline.scenes)} 個 / 合計 {timeline.total_seconds:.1f} 秒")
    print("動画に出る文言はすべて facts と定型見出しのみ(生成器は発明しない)。")
    print(f"次: python -m videoyard render {directory}")
    return 0


def cmd_learn(_args: argparse.Namespace) -> int:
    examples = load_examples()
    weights, accuracy = train(examples)
    path = save_weights(weights, len(examples), accuracy)
    print(f"学習完了: {len(examples)} 件の添削 → {path}")
    print(f"訓練データ上の的中率: {accuracy:.0%}")
    print(f"新しい重み: 動き={weights.motion:+.2f} 音量={weights.loudness:+.2f} "
          f"立ち上がり={weights.onset:+.2f}")
    print("次回の analyze から自動で使われる。weights.json を消せば既定に戻る。")
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
    analyze_cmd.add_argument("--near-still-ydif", type=float, default=0.2,
                             help="動き量がこれ未満なら「ほぼ静止」とみなす"
                                  "(ノイズの多い録画向け。0 で無効)")
    analyze_cmd.add_argument("--min-cut", type=float, default=1.0)
    analyze_cmd.add_argument("--min-keep", type=float, default=0.6)
    analyze_cmd.add_argument("--target-seconds", type=float, default=None,
                             help="指定すると盛り上がり度の高い部分から順に"
                                  "この合計秒数へ収める(例: 60)")
    analyze_cmd.add_argument("--llm", choices=("none", "ollama"), default=None,
                             help="テロップ文言の下書きに使うローカルAI"
                                  "(既定: 環境変数 VIDEOYARD_LLM か none)")
    analyze_cmd.add_argument("--llm-model", default=None,
                             help="ローカルAIのモデル名(例: qwen2.5:7b)")
    analyze_cmd.add_argument("--llm-url", default=None,
                             help="Ollama の URL(localhost のみ許可)")
    analyze_cmd.add_argument("--hint", default="",
                             help="動画の内容ヒント(例: 'アクションゲームのボス戦')。"
                                  "AIは映像を見ないので文言の質はこれで決まる")
    analyze_cmd.set_defaults(handler=lambda a: cmd_analyze(a.directory, a))

    cut_cmd = sub.add_parser("cut", help="cutplan.json のとおりに切ってつなぐ")
    cut_cmd.add_argument("directory", type=Path)
    cut_cmd.add_argument("--no-loudnorm", action="store_true",
                         help="音量の正規化(YouTube 基準 -14 LUFS)を行わない")
    cut_cmd.add_argument("--vertical", action="store_true",
                         help="ショート用の縦動画(1080x1920、ぼかし背景+中央配置)で出力")
    cut_cmd.add_argument("--fast", action="store_true",
                         help="速さ優先(全コア+高速プリセット)。バイト単位の"
                              "再現性は保証されない")
    cut_cmd.add_argument("--bgm", type=Path, default=None,
                         help="手持ちの BGM ファイルをゲーム音の下に重ねる"
                              "(短ければループ、末尾フェードアウト)")
    cut_cmd.add_argument("--bgm-db", type=float, default=BGM_DEFAULT_GAIN_DB,
                         help="BGM の音量(dB、既定 -16)")
    cut_cmd.add_argument("--transition", choices=TRANSITIONS, default="none",
                         help="場面転換: none=ハードカット(既定) / dip=短い暗転")
    cut_cmd.set_defaults(handler=lambda a: cmd_cut(a.directory, a))

    intro_cmd = sub.add_parser("intro", help="ゲームの facts から紹介動画のタイムラインを作る")
    intro_cmd.add_argument("directory", type=Path)
    intro_cmd.add_argument("--facts", type=Path, required=True,
                           help="facts の JSON(見本: examples/facts-sample.json)")
    intro_cmd.set_defaults(handler=lambda a: cmd_intro(a.directory, a))

    learn_cmd = sub.add_parser("learn", help="貯まった添削から採点基準を学習し直す")
    learn_cmd.set_defaults(handler=cmd_learn)

    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (TimelineError, RenderError, JobError, FontError,
            AnalyzeError, CutPlanError, LlmError, LearningError, IntroError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
