"""videoyard — YouTube 動画制作パイプライン v0.1。

v0.1 が持つのは「骨格」だけ:

* ProductionJob — 1 本の動画 = 1 ディレクトリ。段(stage)ごとの完了を
  job.json に記録し、完了は成果物の検証が通ったときだけ前進する(fail-closed)。
* Timeline — 編集内容の完全な記述(timeline.json)。検証は構築時に行う。
* render — timeline.json だけから ffmpeg コマンドを組み立てる決定的レンダリング。
  同じ入力なら同じ mp4 が出る。

モデルも TTS もネットワークも要らない。docs/ARCHITECTURE.md の段階計画 v0.1。
"""

__version__ = "0.4.0"
