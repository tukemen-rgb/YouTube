# YouTube

シドラスタジオの YouTube 動画制作を自動化する AI(構想段階)。

## 目的

企画 → 台本 → 素材 → 音声 → 編集 → メタデータ → 公開 → 分析、という
動画制作の一連の流れを、SIDRA AI と同じ「local first / fail-closed /
外部送信は人間承認」の方針で自動化する。

## 状態

**設計フェーズ。** まだコードはない。既存リポジトリ
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
