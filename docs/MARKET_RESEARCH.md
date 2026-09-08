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


---

# サービス面の競合調査(2026-09-08、社長指示による追加調査)

「同業他社と比較して弱点を調べて克服」の指示で、機能ではなく**サービス面**
(使い始めから仕上げまでの体験)を調べ直した。

## 見つかった弱点(サービス面)

| # | 弱点 | 競合の状況 | 深刻度 |
| --- | --- | --- | --- |
| S1 | **他ソフトへ持ち出せない**(mp4 しか出ない) | auto-editor / AutoCut / Descript / Eddie AI は EDL・FCPXML・XML でタイムラインごと Premiere / Resolve / FCP へ渡せる。プロの本流は「AI で粗く切って仕上げは自分のソフト」 | **最大** |
| S2 | 導入のハードル(Python + ffmpeg + CLI) | 初心者向けは「インストール不要」が主流(Clipchamp / Canva / CapCut)。最低限、環境の不備を自分で診断して直し方を言えるべき | 大 |
| S3 | 複数動画をまとめて処理できない | 実況者は週に何本も録る。1 本ずつ手で回すのは現実的でない | 中 |
| S4 | 出来上がりを確認する手段が mp4 を再生するだけ | 競合はプレビュー画面を持つ。うちはグラフと計画で説明できるが、一覧性が無い | 中 |

## 逆に、うちが競合より強い点(調査で確認)

- OpusClip はクリップ 20 個中使えるのが 2〜3 個、スコアの信頼性に難あり、
  **日本語字幕に非対応**。うちは判定根拠を全部計画に残し、精度の床を
  テストで守っている(既定 85% / and モード 100%)
- クラウド勢は録画を外部にアップロードする前提。うちはローカル完結
- Vrew は字幕が強い(D1 の判断材料。実装は社長判断待ち)

## 出典(サービス面の追加調査)

- https://knightli.com/en/2026/04/23/auto-editor-auto-cut-silence-premiere-resolve-workflow/
- https://www.autocut.com/en/ / https://www.cined.com/autocut-plugin-now-integrates-ai-directly-into-premiere-pro-to-automatically-handle-time-consuming-tasks/
- https://help.heyeddie.ai/en/articles/10328445-one-click-export-of-your-edit-to-resolve-adobe-and-fcp
- https://help.descript.com/hc/en-us/articles/10255813481613-Timeline-exports
- https://cutconvert.com/guides/edl-vs-xml-vs-aaf / https://scriptcut.io/blog/what-is-fcpxml
- https://bigvu.tv/blog/ja/opus-clips%E3%81%A8%E3%81%AF-...(OpusClip 日本語字幕非対応・当たり外れ)
- https://note.com/aidynote/n/nae817c3cfd83(Opus Clip vs Vrew vs CapCut)
- https://www.itreview.jp/products/vrew/reviews


---

# 縦動画のセーフゾーン(2026-09-08 調査、S5 の根拠)

スマホのアプリ UI が映像に重なるため、そこに置いた文字は読めない。
1080×1920 を基準にした各社の目安:

| 領域 | 空ける画素 | 何が重なるか |
| --- | --- | --- |
| 下端 | 300px | TikTok のキャプション欄(3 アプリで最も広い) |
| 右端 | 120px | いいね・コメント・シェアのボタン列 |
| 上端 | 120px | 検索・おすすめの帯 |
| 右下 | 180×80px | YouTube の登録ボタン(2026 年は以前より大きい) |

3 アプリ共通で安全な最大公約数は **中央 900×1160**。YouTube Shorts 単体
なら 888×1500。videoyard は下端 300 / 右端 120 / 上端 120 / 左端 40 を
空ける(共通の最大公約数を含む)。

**実測で見つかった不具合(2026-09-08):** photo --vertical のテロップは
1080×1920 の最下端に描かれ、下端 300px と右端 120px の両方に掛かって
いた。スマホでは完全に隠れる。cut --vertical も、素材が縦の場合は
同じ問題が起きる(16:9 素材は帯が付くのでたまたま安全だった)。
サイクル 29 で safezone.py を作り、両方を直した。

## 出典

- https://kreatli.com/guides/safe-zone-guide
- https://postplanify.com/blog/social-media-safe-zones-2026-complete-guide
- https://syllaby.io/blog/aspect-ratios-safe-zones-shorts-reels-tiktok/
- https://fluxtoolkit.com/blog/tiktok-safe-zone-overlay-template-2026
- https://postplanify.com/tools/youtube-shorts-safe-zone-checker
