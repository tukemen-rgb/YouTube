# 市場調査 — 競合の自動編集ツールと videoyard のギャップ(2026-09-03)

自律改善ループ・サイクル 21 の調査。行き詰まり打開ではなく「次の大物」
選定のための定点観測。出典は末尾。

## 競合の要点

| ツール | 種類 | videoyard に無い強み |
| --- | --- | --- |
| auto-editor | OSS・CLI | 退屈区間を「切る」以外に**倍速で残す**選択肢。Premiere/Resolve へ書き出し |
| OpusClip | クラウド有料 | 1 本の長尺から**ショート候補を複数本**生成。バイラル可能性スコア。アニメ字幕 |
| CapCut / Vidio.ai | クラウド | スマホ完結・自動ハイライト。テンプレ演出が豊富 |
| Vrew / KIRARI | クラウド(日本語) | **音声認識ベースの字幕**と、発話内容での面白さ判定 |

観測できた競合の弱み(レビューより): OpusClip のスコアは信頼性に
難あり(40 点のクリップが 85 点を上回る事例、複数話者で精度 4 割)。
クラウド型は録画を外部にアップロードする前提で、従量課金。

## videoyard が既に持っているもの(引け目は不要)

- 静止+無音+動き量からのカット案、盛り上がり度、★、尺調整、縦出力
- ○×シートでの人の確定、来歴 manifest、決定的出力 — **クラウド勢に無い
  「ローカル完結・検証可能・学習は自分の添削だけ」**はそのまま差別化点
- 精度ベンチ常設(既定 85% 床/and モード 100%)。競合レビューが示す
  とおり「スコアの信頼性」が業界共通の弱点であり、うちは床をテストで守る

## ギャップと提案(優先順)

1. **倍速で残す(auto-editor 由来)** — 退屈だが文脈として要る区間を
   「切る」のではなく 4 倍速で残す第 3 の選択肢。無音カットの
   「話が飛ぶ」問題への根本対策。ローカル ffmpeg(setpts/atempo)で
   実装可能。→ **次サイクルで実装**(シート記号「≫」、まず通常 cut
   経路のみ、--incremental は明示エラー)
2. **ショート候補を複数本(OpusClip 由来)** — 盛り上がり上位 N 区間から
   60 秒クリップを N 本出し、人が選ぶ。auto --shorts の拡張で実装可能。
   → サイクル 23 候補
3. **字幕(Vrew/OpusClip 由来)** — 発話の字幕は音声認識(Whisper 等)が
   必要。**D1(社長判断待ち)のまま**。この調査は D1 の判断材料を強化:
   日本語圏の競合は字幕が標準装備で、無いと「編集した感」で見劣りする
4. 直接投稿(各社共通)— **やらない**(公開は人間承認の原則。D2 待ち)

## 出典

- https://pypi.org/project/auto-editor/20.52.1 / https://www.opensourceprojects.dev/post/auto-editor
- https://bigvu.tv/blog/opus-clip-tested-2026-where-ai-wins-40-percent-discard/
- https://reap.video/reports/state-of-top-ai-video-clipping-tools-2026
- https://www.ssemble.com/blog/opus-clip-review-2026
- https://digital-gorilla.co.jp/ai-lab/douga-jidou-henshu-ai/
- https://www.kirari.io/blog/ai-clip-tools
- https://www.capcut.com/ja-jp/resource/ai-gaming-video-editor
