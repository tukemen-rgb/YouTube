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

## 使い方(v0.1)

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
