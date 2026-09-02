# 帰国後の実機テスト手順書(所要 30〜60 分)

対象: 社長の Windows / Mac の実機。2026-09 の留守中に準備した機能を、
実物の録画とローカル AI で確かめるための手順。**この手順の中に公開
(YouTube アップロード)は含まれない**(アップロード機能自体が未実装。
着手には判断が要る)。

## 0. 前提のインストール

1. **Python 3.11 以上** — `python --version` で確認
2. **ffmpeg** — Windows: `winget install ffmpeg` / Mac: `brew install ffmpeg`。
   `ffmpeg -version` と `ffprobe -version` が通ること
3. **日本語フォント** — Windows / Mac は標準で入っている(游ゴシック/
   ヒラギノを自動で探す)。見つからないと言われたら環境変数
   `VIDEOYARD_FONT` にフォントファイルのパスを設定
4. このリポジトリを clone して:

```bash
pip install -e ".[dev]"
python -m unittest discover -s tests   # まず全部通ることを確認
```

## 1. 実録画での 分析 → 添削 → カット(15 分)

ゲーム録画(mp4)を 1 本用意して:

```bash
python -m videoyard analyze productions/test1 --source 録画.mp4
```

- 表示されたカット計画の案を眺める。**確認したいこと**:
  - 退屈な区間(メニュー放置・無音)がちゃんと「切る」になっているか
  - 盛り上がり度の高い区間が、体感と合っているか
- 惜しい場合の調整ノブ:
  - 静止画の見逃し → `--near-still-ydif 0.5` など大きく
  - 切りすぎ → `--mode static_and_silent`(静止かつ無音のときだけ切る)
  - BGM が常に鳴っている録画 → `--silence-db -45` など小さく
- `productions/test1/cutplan.sheet.txt` の行頭 ○/× とテロップを直して
  `python -m videoyard apply productions/test1` で反映し(JSON 編集は不要)、

```bash
python -m videoyard cut productions/test1
```

`out/video.mp4` を再生して確認。**この添削は自動で学習用に記録される。**

## 2. 学習を回す(添削が 30 窓を超えたら)

録画 2〜3 本で手順 1 を繰り返してから:

```bash
python -m videoyard learn
python -m videoyard analyze productions/test4 --source 別の録画.mp4
# → 「学習済みの採点基準を使用」と表示され、点数があなた好みに寄る
```

気に入らなければ `~/.videoyard/weights.json` を消せば既定に戻る。

## 3. Ollama(テロップのローカル AI)の実機確認(15 分)

1. https://ollama.com からインストール(Ollama 本体の来歴はダウンロード
   ページで確認。モデルはライセンスを見て選ぶ — 例: qwen2.5 系は
   Apache-2.0)
2. モデルを 1 つ取得して起動:

```bash
ollama pull qwen2.5:7b     # VRAM 8GB 目安。厳しければ qwen2.5:3b
ollama serve                # 常駐している場合は不要
```

3. テロップの下書き付きで分析:

```bash
python -m videoyard analyze productions/test5 --source 録画.mp4 \
  --llm ollama --llm-model qwen2.5:7b \
  --hint "(ここに動画の内容を一文で)"
```

- **確認したいこと**: cutplan.json の telop に日本語の下書きが入り、
  reason に「ローカルAIの下書き」と明記されること。文言の質は --hint と
  モデル次第(AI は映像を見ていない)
- 接続先は localhost 固定。外部 URL を指定するとエラーになるのが正常

## 4. 紹介動画(v0.6)を実ゲームで作る(10 分)

`examples/facts-sample.json` を実在のゲームの内容に書き換えて:

```bash
python -m videoyard intro productions/intro1 --facts my-game-facts.json
python -m videoyard render productions/intro1
```

facts に書いたことだけが動画になる(生成器は文言を発明しない)。

## 5. 帰国後に判断してほしいこと(実装は判断待ち)

| # | 判断 | 材料 |
| --- | --- | --- |
| 1 | 音声認識(Whisper 等の学習済みローカルモデル)を導入するか | 実況の言葉を字幕・盛り上がり判定に使える。モデルの来歴確認と容量(数百MB〜)が必要。sidra-ai のモデル導入手続きに揃えるのが筋 |
| 2 | YouTube アップロード機能に着手するか | Data API の利用登録(Google アカウント連携)が必要。実装しても公開は approval.json 必須の設計のまま |
| 3 | 実地テストの結果、採点の既定重み(動き0.5/音量0.3/立ち上がり0.2)を直すか | 手順 1〜2 の体感。学習で個人化できるので急ぎではない |
