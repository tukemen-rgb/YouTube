"""まとめて処理(S3)— 割り当て・失敗しても止まらないこと・再開。"""

import json
import tempfile
import unittest
from pathlib import Path

from videoyard.batch import (
    MAX_ITEMS,
    BatchError,
    Item,
    Result,
    collect_videos,
    format_summary,
    is_done,
    plan_items,
    run_batch,
    safe_name,
    write_report,
)


class Collecting(unittest.TestCase):
    def test_video_files_sorted(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for name in ("b.mp4", "a.MOV", "note.txt", "c.mkv"):
                (base / name).write_bytes(b"x")
            self.assertEqual([p.name for p in collect_videos(base)],
                             ["a.MOV", "b.mp4", "c.mkv"])

    def test_missing_dir_fails_closed(self):
        with self.assertRaises(BatchError):
            collect_videos(Path("/no/such/place"))

    def test_no_videos_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(BatchError):
            collect_videos(Path(tmp))

    def test_too_many_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for i in range(MAX_ITEMS + 1):
                (base / f"v{i:03d}.mp4").write_bytes(b"x")
            with self.assertRaises(BatchError) as ctx:
                collect_videos(base)
            self.assertIn("上限", str(ctx.exception))


class Naming(unittest.TestCase):
    def test_path_separators_and_spaces_replaced(self):
        self.assertEqual(safe_name(Path("/x/ボス戦 その1.mp4")), "ボス戦_その1")
        self.assertEqual(safe_name(Path("/x/a:b?c.mp4")), "a_b_c")

    def test_empty_name_gets_placeholder(self):
        self.assertEqual(safe_name(Path("/x/...mp4")), "untitled")

    def test_duplicate_names_get_numbered(self):
        items = plan_items(
            [Path("/a/戦闘.mp4"), Path("/b/戦闘.mp4"), Path("/c/戦闘.mp4")],
            Path("/out"))
        self.assertEqual([i.directory.name for i in items],
                         ["戦闘", "戦闘_2", "戦闘_3"])

    def test_each_item_keeps_its_source(self):
        items = plan_items([Path("/a/x.mp4"), Path("/b/y.mp4")], Path("/out"))
        self.assertEqual(items[0], Item(Path("/a/x.mp4"), Path("/out/x")))
        self.assertEqual(items[1].source, Path("/b/y.mp4"))


class Running(unittest.TestCase):
    def _items(self, root: Path, count: int = 3) -> list[Item]:
        return plan_items([Path(f"/src/v{i}.mp4") for i in range(count)], root)

    def test_all_succeed(self):
        with tempfile.TemporaryDirectory() as tmp:
            items = self._items(Path(tmp))
            results = run_batch(items, lambda _i: 12.5)
            self.assertEqual([r.status for r in results], ["done"] * 3)
            self.assertEqual(results[0].seconds, 12.5)

    def test_one_failure_does_not_stop_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            items = self._items(Path(tmp))

            def process(item: Item) -> float:
                if item.source.name == "v1.mp4":
                    raise RuntimeError("ffmpeg がこけた")
                return 5.0

            results = run_batch(items, process)
            self.assertEqual([r.status for r in results],
                             ["done", "failed", "done"])
            self.assertIn("ffmpeg がこけた", results[1].detail)
            self.assertIn("RuntimeError", results[1].detail)

    def test_finished_productions_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            items = self._items(root)
            out = items[1].directory / "out"
            out.mkdir(parents=True)
            (out / "video.mp4").write_bytes(b"x")
            self.assertTrue(is_done(items[1].directory))
            calls = []

            def process(item: Item) -> float:
                calls.append(item.source.name)
                return 1.0

            results = run_batch(items, process)
            self.assertEqual([r.status for r in results],
                             ["done", "skipped", "done"])
            self.assertNotIn("v1.mp4", calls)  # 出来ている分は触らない

    def test_force_reprocesses_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            items = self._items(root)
            out = items[1].directory / "out"
            out.mkdir(parents=True)
            (out / "video.mp4").write_bytes(b"x")
            calls = []
            run_batch(items, lambda i: calls.append(i.source.name) or 1.0,
                      force=True)
            self.assertEqual(len(calls), 3)

    def test_progress_is_reported_per_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            messages: list[str] = []
            run_batch(self._items(Path(tmp), 2), lambda _i: 1.0,
                      progress=messages.append)
            self.assertTrue(any("[1/2]" in m for m in messages))
            self.assertTrue(any("[2/2]" in m for m in messages))


class Summary(unittest.TestCase):
    def test_counts_and_named_failures(self):
        results = [
            Result(Path("/s/a.mp4"), Path("/o/a"), "done", seconds=60.0),
            Result(Path("/s/b.mp4"), Path("/o/b"), "failed", "CutError: 音が無い"),
            Result(Path("/s/c.mp4"), Path("/o/c"), "skipped", "既にある"),
        ]
        text = "\n".join(format_summary(results))
        self.assertIn("完了 1 本", text)
        self.assertIn("失敗 1 本", text)
        self.assertIn("b.mp4", text)       # 失敗は名指しする
        self.assertIn("音が無い", text)
        self.assertIn("続きから", text)     # やり直し方を案内する

    def test_multiline_error_collapsed_to_one_line(self):
        from videoyard.batch import one_line
        results = [Result(Path("/s/b.mp4"), Path("/o/b"), "failed",
                          "AnalyzeError: ffprobe が失敗:\n  moov atom not found\n"
                          "  Invalid data")]
        line = next(ln for ln in format_summary(results) if "失敗:" in ln)
        self.assertNotIn("\n", line)
        self.assertIn("moov atom not found", line)
        # 長すぎる理由は切り詰め、全文の在処を示す
        long = one_line("x" * 500)
        self.assertIn("batch_report.json", long)
        self.assertLess(len(long), 500)

    def test_no_failures_no_retry_hint(self):
        results = [Result(Path("/s/a.mp4"), Path("/o/a"), "done", seconds=30.0)]
        self.assertNotIn("続きから", "\n".join(format_summary(results)))


class Report(unittest.TestCase):
    def test_report_records_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            results = [
                Result(Path("/s/a.mp4"), root / "a", "done", seconds=61.234),
                Result(Path("/s/b.mp4"), root / "b", "failed", "CutError: x"),
            ]
            path = write_report(root, results)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual((data["total"], data["done"], data["failed"]),
                             (2, 1, 1))
            self.assertEqual(data["items"][0]["output_seconds"], 61.234)
            self.assertEqual(data["items"][1]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
