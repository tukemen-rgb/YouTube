"""job.json の状態機械 — 特に「承認なしの publish は存在しない」こと。"""

import json
import tempfile
import unittest
from pathlib import Path

from videoyard.job import STAGES, ApprovalRequired, JobError, ProductionJob


class JobLifecycle(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "prod"
        self.job = ProductionJob.create(self.dir, title="テスト動画")

    def tearDown(self):
        self._tmp.cleanup()

    def test_all_stages_start_pending(self):
        for stage in STAGES:
            self.assertEqual(self.job.stage_status(stage), "pending")

    def test_create_twice_fails(self):
        with self.assertRaises(JobError):
            ProductionJob.create(self.dir, title="二重作成")

    def test_mark_done_persists(self):
        self.job.mark_done("plan", note="企画書を書いた")
        reloaded = ProductionJob.load(self.dir)
        self.assertEqual(reloaded.stage_status("plan"), "done")

    def test_unknown_stage_rejected(self):
        with self.assertRaises(JobError):
            self.job.mark_done("upload")

    def test_assembly_requires_artifacts(self):
        with self.assertRaises(JobError):
            self.job.mark_done("assembly")

    def test_publish_requires_approval_file(self):
        with self.assertRaises(ApprovalRequired):
            self.job.mark_done("publish")
        # 承認記録を人間が置いたあとだけ完了にできる
        (self.dir / "approval.json").write_text(
            json.dumps({"approved_by": "社長", "at": "2026-08-29T00:00:00+00:00"}),
            encoding="utf-8",
        )
        self.job.mark_done("publish")
        self.assertEqual(self.job.stage_status("publish"), "done")

    def test_assembly_rejects_stale_video(self):
        # 動画を作った後に timeline.json を書き換えたら、その動画は
        # もう「今の記述の成果物」ではないので完了にできない。
        out = self.dir / "out"
        out.mkdir()
        (self.dir / "timeline.json").write_text("{}", encoding="utf-8")
        (out / "video.mp4").write_bytes(b"fake")
        (out / "render_manifest.json").write_text(
            json.dumps({"timeline_sha256": "0" * 64}), encoding="utf-8"
        )
        with self.assertRaises(JobError):
            self.job.mark_done("assembly")

    def test_load_rejects_tampered_stages(self):
        data = json.loads((self.dir / "job.json").read_text(encoding="utf-8"))
        del data["stages"]["publish"]
        (self.dir / "job.json").write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(JobError):
            ProductionJob.load(self.dir)


if __name__ == "__main__":
    unittest.main()
