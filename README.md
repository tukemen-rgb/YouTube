# YouTube

シドラスタジオの YouTube 動画制作を自動化する AI(構想段階)。

## 目的

企画 → 台本 → 素材 → 音声 → 編集 → メタデータ → 公開 → 分析、という
動画制作の一連の流れを、SIDRA AI と同じ「local first / fail-closed /
外部送信は人間承認」の方針で自動化する。

## 状態

**v0.1 実装済み。** 骨組み(1 本の動画 = 1 ディレクトリの ProductionJob、
timeline.json → mp4 の決定的レンダリング、オフラインテスト一式)が動く。
AI・音声・ネットワークはまだ使わない。設計の根拠は既存リポジトリ
([sidra-ai](https://github.com/tukemen-rgb/sidra-ai) /
[marketing](https://github.com/tukemen-rgb/marketing) /
[creater-yard](https://github.com/tukemen-rgb/creater-yard))の
分析と、そこから導いた構造の提案が
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) にある。

## 決まっていること(提案)

- ローカル LLM / ローカル TTS / ffmpeg を第一候補にし、従量課金 API 依存を作らない
- YouTube へのアップロードは必ず人間承認を挟む(自動公開はしない)
- 生成物すべてに来歴(provenance)とライセンス記録を付ける
- モデルなし・ネットワークなしで動くオフラインのテスト経路を最初から持つ

## 使い方(v0.2: 元動画の自動カット編集)

録画した動画から退屈な区間(静止画・無音)を検出し、カット計画の
「案」を作る。人が案を直して確定すると、テロップ付きのダイジェストに
編集する。**動画を直接いじる前に必ず人が読める計画ファイルを挟む。**

```bash
# 1. 分析 → カット計画の案と ○×編集シート(cutplan.sheet.txt)ができる
python -m videoyard analyze productions/mygame --source 録画.mp4
#    検出の閾値も選べる: --mode static_and_silent --silence-db -40 など
#    尺指定: --target-seconds 60(盛り上がり度上位でその合計秒数に収める)

# 2. 案を直す(任意): シートの行頭 ○(残す)/×(切る)とテロップを書き換えて
python -m videoyard apply productions/mygame   # シートを計画に反映

# 3. 確定 → 切ってつないだ out/video.mp4 とサムネ候補 3 枚ができる
python -m videoyard cut productions/mygame
#    --vertical   ショート用 9:16(1080x1920、ぼかし背景+中央配置)
#    --fast       速さ優先(2.6 倍速目安。バイト単位の再現性は非保証)
#    --bgm 曲.mp3 手持ち BGM をゲーム音の下に(--bgm-db で音量、既定 -16)
#    --transition dip  つなぎ目に短い暗転(既定はハードカット)

# 4. 文字入りサムネ(任意)
python -m videoyard thumbs productions/mygame --text 'タイトル'
```

- 各 keep 区間に**盛り上がり度 0〜100** が付く(v0.4)。動きの激しさ
  (フレーム差)・音の大きさ・音の急な立ち上がりを 0.5 秒ごとに測って
  合成した、その動画内での相対点。★は最高点の区間。重みや窓幅は
  `excitement.py` の定数がすべてで、隠れた学習モデルはない
- `--target-seconds 60` を付けると、盛り上がり度の高い部分から順に
  合計をその秒数へ収める(時間順は入れ替えない。除外部分は
  「尺調整のため除外」と理由付きで cut になり、計画で復活させられる)
- 元動画の中身が分析時と変わっていたら cut は実行を断る
- テロップ本文は描画エンジンへの命令として解釈されない(記号も安全)
- つなぎ目の音は 0.15 秒フェードしてクリック音を消す(映像はハードカット)

### あなたの添削から学習する(v0.5)

analyze の案を人が直して cut を実行するたび、「AI はこう思ったが、
人はこう直した」という差分が添削データとしてローカルに貯まる
(既定 `~/.videoyard/`、環境変数 VIDEOYARD_DATA_DIR で変更可。
保存されるのは窓ごとの測定値と keep/cut の判断だけで、動画の中身は
保存しない。どこにも送らない)。

```bash
python -m videoyard learn   # 貯まった添削から採点の重みを学習し直す
```

- 学習は依存ゼロの純 Python ロジスティック回帰。乱数を使わず、
  同じ添削からは同じ重みが出る
- 添削が 30 件に満たないうちは学習しない(少数に過剰適合するため)
- 学習結果は `weights.json` という見えるファイル 1 つ。次回の analyze
  から自動で使われ、消せば既定の重みに戻る。隠れた状態は無い

### テロップ文言をローカル AI に書かせる(v0.3・任意)

ローカルで動く Ollama があれば、テンプレート(シーン 1)の代わりに
AI がテロップの下書きを書く。**接続先は localhost のみ**(外部 API は
コード上登録できない)。AI は映像を見ないので、--hint の内容ヒントが
文言の質を決める。下書きは cutplan.json に書かれるだけで、人が直して
から cut が実行する(どの文言が AI 由来かは reason に残る)。

```bash
python -m videoyard analyze productions/mygame --source 録画.mp4 \
  --llm ollama --llm-model qwen2.5:7b --hint "アクションゲームのボス戦"
```

--llm ollama を明示したのに接続できないときは、黙ってテンプレートに
落ちずエラーで止まる(できないことをできたふりしない)。

## 使い方(v0.1: timeline.json からの生成)

必要なもの: Python 3.11+ / ffmpeg / 日本語フォント(例: fonts-noto-cjk)。
実行時の Python 依存パッケージはゼロ。

```bash
pip install -e .
python -m unittest discover -s tests   # オフラインテスト(ffmpeg 無しでも骨格分は走る)

python -m videoyard demo productions/demo     # お手本の制作フォルダを作る
python -m videoyard render productions/demo   # timeline.json → out/video.mp4
python -m videoyard status productions/demo   # 8 段の進み具合を表示
```

- `timeline.json` が編集内容の完全な記述。同じ記述からは**バイト単位で同じ動画**が出る
  (テストで実測している)
- レンダリングのたびに `out/render_manifest.json` に来歴(入力・フォント・出力の
  ダイジェストと実行コマンド)が残る
- `job.json` の publish 段は `approval.json`(人間の承認記録)が無い限り
  完了にできない。承認をスキップするコードパスは存在しない
