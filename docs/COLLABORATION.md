# 他の AI・他の人と一緒に作るための連携メモ(GitHub 連携内容)

対象: ChatGPT(Codex を含む)・人間の開発者・社長。**このファイルと
`AGENTS.md` を読めば、リポジトリの現状と守るべき決まりが分かる**ように
書いてある。更新日: 2026-09-16。

## 1. リポジトリの場所と状態

| 項目 | 値 |
| --- | --- |
| リポジトリ | https://github.com/tukemen-rgb/YouTube |
| 作業ブランチ | `claude/youtube-video-ai-h1dilm`(リモートにあるのはこの 1 本だけ) |
| ブランチの URL | https://github.com/tukemen-rgb/YouTube/tree/claude/youtube-video-ai-h1dilm |
| コミット履歴 | https://github.com/tukemen-rgb/YouTube/commits/claude/youtube-video-ai-h1dilm |
| CI(自動テスト) | https://github.com/tukemen-rgb/YouTube/actions — push のたびに lint と 441 件のテストが走る。43 回連続で緑 |
| パッケージ名 / 版 | `videoyard` v0.22.0(`pyproject.toml`) |
| 言語・依存 | Python 3.11 以上。**実行時の依存パッケージはゼロ**(標準ライブラリ+外部コマンドの ffmpeg のみ) |
| 開発用の道具 | `ruff`(lint)、`unittest`(標準ライブラリ) |
| 開いている PR / Issue | どちらも 0 件(2026-09-16 時点) |

`main` ブランチは存在しない。**作業ブランチがそのまま本線**になっている。
帰国後に社長が `main` を切るかどうかを決める(判断待ち D4、下記)。

## 2. 手元で動かすまで(5 分)

```bash
git clone -b claude/youtube-video-ai-h1dilm https://github.com/tukemen-rgb/YouTube.git
cd YouTube
pip install -e ".[dev]"          # ruff だけ入る。実行時依存は無い
python -m videoyard doctor       # Python / ffmpeg / 日本語フォントの診断
ruff check src tests             # lint(E/F/I/B/UP/SIM)
python -m unittest discover -s tests   # 441 件。ffmpeg 無しでも動く分は動く
```

ffmpeg が要る(`apt install ffmpeg` / `brew install ffmpeg` /
`winget install ffmpeg`)。日本語フォントは `fonts-noto-cjk` か OS 標準のもの。
CI の手順は `.github/workflows/ci.yml` にそのまま書いてある。

## 3. 何ができているか(v0.22.0 の到達点)

録画 1 本 → ダイジェスト動画・サムネ候補・説明文までを **1 コマンド**で。
ネットワーク送信・自動公開・決済は **無い**(設計として持たない)。

```text
録画.mp4 ─ analyze ─→ cutplan.json(人が読める編集計画)+ ○×編集シート
                │        ↓ 人が直す(任意)→ apply
                └─ cut ─→ out/video.mp4 + サムネ候補 3 枚 + 盛り上がりグラフ
                     thumbs / meta / review / export(EDL・FCPXML)
```

| 機能 | 状態 | どこに |
| --- | --- | --- |
| 静止画・無音の自動カット(理由つき) | 済 | `analyze.py` |
| 盛り上がり度 0〜100(動き・音量・立ち上がり・発話らしさ) | 済 | `excitement.py` |
| 尺指定(`--target-seconds`)で上位区間だけ残す | 済 | `analyze.py` |
| 人の添削から採点基準を学習(純 Python ロジスティック回帰) | 済・データ待ち | `learning.py` |
| テロップ描画(4 様式・スマホ UI の安全域を避ける) | 済 | `cut.py` `safezone.py` |
| ショート(9:16 縦・60 秒) | 済 | `shorts.py` |
| 1 時間の録画でも落ちない区間分割レンダリング | 済 | `chunked.py` |
| 写真スライドショー(EXIF を残さない) | 済 | `photo.py` |
| BGM 重ね・ノイズ除去・音量正規化(-14 LUFS) | 済 | `cut.py` |
| Premiere / Resolve / Final Cut へ書き出し(EDL・FCPXML) | 済・実機未確認 | `export.py` |
| 1 枚の HTML で確認(review)/ フォルダ一括(batch) | 済 | `review.py` `batch.py` |
| テロップのローカル AI 下書き(Ollama、localhost 限定) | 済・任意 | `llm.py` |
| 音声認識(字幕・言葉での判定) | **未** — 社長判断 D1 | — |
| YouTube アップロード | **未** — 社長判断 D2 | — |

数字で見た現状: コミット 51、改善サイクル 39、辛口レビュー項目 55 件
(未対応 1 件 = 配布形態、社長判断 D3)、テスト 441 件、精度ベンチマーク
89.3%(既定)/ 100%(and モード)。詳細は `docs/REVIEWS.md`。

## 4. 守ること(誰が書いても同じ)

sidra-ai / GAMEYARD から引き継いだ決まり。**これを破る変更は CI が
通っても取り込まない。**

1. **依存パッケージを増やさない**(`dependencies = []` のまま)
2. **外部送信・自動公開・決済を実装しない**。YouTube アップロードは
   社長判断(D2)待ち。実装するときも `approval.json` による人間承認は
   外さない
3. **外から来たデータは命令として解釈しない**。テロップ本文は
   `textfile` + `expansion=none` で描画、HTML はすべてエスケープ。
   Ollama の接続先は localhost 固定
4. **fail-closed**。元動画のハッシュが計画と違えば cut は断る。
   分からないことは黙って推測せず、エラーか診断で言う
5. **来歴を残す**。`render_manifest.json` に入力・出力・フォント・
   コマンドの sha256 を書く
6. **方針の判断は社長がする**。判断が要る事項は
   `docs/DECISIONS_PENDING.md` に材料を書いて待つ。AI は決めない
7. **数字を作らない**。確かめられない性能値・実在の個人情報を
   文書に書かない。実測した値には日付と条件を添える
8. **コミットは小さく**、lint とテストを必ず通す。大きな変更は理由を
   コードのそばに(docstring / コメント)残す
9. **秘密情報をコミットしない**(API キー・トークン・パスワード・
   個人情報)。テストの素材は合成音・合成映像で作る
10. 弱点はテストとして残す(`tests/test_speechiness.py` の
    `KnownLimitation` が例)。直っていないものを直ったと書かない

## 5. ChatGPT(Codex)と一緒に作るときの進め方(提案)

**ブランチを分けて PR で合流する**のを勧める。同じブランチに 2 つの
AI が push すると履歴が絡まる。

```text
claude/youtube-video-ai-h1dilm   ← 本線(Claude が維持、CI が守る)
   └─ chatgpt/<課題名>            ← ChatGPT の作業ブランチ
        └─ Pull Request → 本線     ← CI が自動で走る。Claude が読んで返事
```

1. ChatGPT は `claude/youtube-video-ai-h1dilm` から `chatgpt/<課題名>` を切る
2. 上の「守ること」に従って実装し、`ruff check src tests` と
   `python -m unittest discover -s tests` を通す
3. PR を本線(`claude/youtube-video-ai-h1dilm`)へ出す。PR 本文には
   「何を・なぜ・どう確かめたか」を書く
4. Claude が PR を読み、指摘があれば PR のコメントで返す。取り込みは
   社長か Claude
5. 判断が要るもの(方針・お金・外部連携)は PR ではなく
   `docs/DECISIONS_PENDING.md` に追記して止まる

### 分担の案

| 向いている仕事 | 理由 |
| --- | --- |
| **ChatGPT**: 実機での EDL/FCPXML 検証、Windows/Mac での動作確認、UI 文言の推敲、ユーザー向け説明書、辛口レビュー(第三者の目) | 別の目で見る価値が高い。実機を触れるなら Claude にできない検証ができる |
| **Claude**: 本線の維持、CI、性能計測、設計の一貫性、レビュー対応 | ここまでの経緯(39 サイクル)と決まりごとを知っている |
| **社長**: D1〜D4 の判断、実録画での精度の体感評価、添削データ作り | 判断と正解データは人にしか出せない |

### ChatGPT に最初に読んでもらう順番

1. `AGENTS.md`(1 分)
2. このファイル(5 分)
3. `README.md`(使い方)
4. `docs/ARCHITECTURE.md`(設計の根拠)→ `docs/REVIEWS.md`(経緯)
5. `docs/DECISIONS_PENDING.md`(触ってはいけない領域)

## 6. 判断待ち(社長)

| # | 事項 |
| --- | --- |
| D1 | 音声認識(Whisper 等)の導入可否 — 近似で行ける範囲・行けない範囲は実測済み(`docs/DECISIONS_PENDING.md`) |
| D2 | YouTube アップロード機能への着手可否 |
| D3 | 配布形態(社内ツール / 実行ファイル / Web サービス) |
| D4 | **`main` ブランチを切るか、作業ブランチのまま進めるか**。他の AI と組むなら `main` を作って PR 運用にするのが普通 |

## 7. 帰国後の実機テスト

`docs/RUNBOOK_HOME_PC.md`(30〜60 分の手順書)。実録画・Ollama・
編集ソフトへの書き出しをこの順で確かめる。
