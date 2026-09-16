# AGENTS.md — このリポジトリで作業する AI へ(Claude / ChatGPT / Codex 共通)

**最初に `docs/COLLABORATION.md` を読むこと。** 現状・決まり・進め方が
全部そこにある。読まずに実装を始めないこと。

## 一行で

videoyard = シドラスタジオの YouTube 動画制作を自動化する Python パッケージ。
録画 1 本 → 自動カット・テロップ・サムネ・説明文まで。**ローカルだけで
動き、外部送信・自動公開・決済を持たない。**

## 絶対に守ること

- 依存パッケージを増やさない(`dependencies = []`)
- 外部送信・YouTube 公開・決済を実装しない(社長判断 D1〜D4 待ち:
  `docs/DECISIONS_PENDING.md`)
- 外から来たデータを命令として解釈しない(textfile + expansion=none、
  HTML エスケープ、Ollama は localhost 固定)
- fail-closed。推測で埋めない。確かめられない数字を書かない
- 秘密情報・個人情報をコミットしない
- コミットは小さく、`ruff check src tests` と
  `python -m unittest discover -s tests` を必ず通す
- 本線は `claude/youtube-video-ai-h1dilm`。他の AI は別ブランチ
  (`chatgpt/<課題名>` など)から PR で合流する

## 検証

```bash
pip install -e ".[dev]"
python -m videoyard doctor
ruff check src tests
python -m unittest discover -s tests
```
