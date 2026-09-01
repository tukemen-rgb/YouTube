"""ローカル AI 下書き — 接続先の制限・出力の検査・通信の形式。

本物の Ollama は要らない。localhost に偽サーバーを立てて、送っている
内容と受け取りの処理を検証する。
"""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from videoyard.analyze import AnalyzeParams, draft_telops, propose_segments
from videoyard.llm import (
    LlmError,
    OllamaTelopWriter,
    SceneBrief,
    build_prompt,
    require_local_url,
    sanitize_draft,
)


class UrlPolicy(unittest.TestCase):
    def test_localhost_allowed(self):
        require_local_url("http://127.0.0.1:11434")
        require_local_url("http://localhost:11434")

    def test_external_hosts_rejected(self):
        for url in ("https://api.example.com", "http://192.168.1.5:11434",
                    "ftp://127.0.0.1"):
            with self.assertRaises(LlmError):
                require_local_url(url)

    def test_model_name_required(self):
        with self.assertRaises(LlmError):
            OllamaTelopWriter(model="")


class Sanitizing(unittest.TestCase):
    def test_first_line_only(self):
        self.assertEqual(sanitize_draft("神回避!\n(解説: この場面は…)"), "神回避!")

    def test_quotes_stripped(self):
        self.assertEqual(sanitize_draft("「ここが見どころ」"), "ここが見どころ")

    def test_too_long_truncated(self):
        self.assertEqual(len(sanitize_draft("あ" * 100)), 30)

    def test_empty_is_none(self):
        self.assertIsNone(sanitize_draft("   \n\n"))


class Prompting(unittest.TestCase):
    def test_hint_and_highlight_included(self):
        prompt = build_prompt(SceneBrief(
            number=2, start=12.0, duration=5.0, is_highlight=True, hint="ボス戦"
        ))
        self.assertIn("ボス戦", prompt)
        self.assertIn("盛り上がり候補", prompt)
        self.assertIn("2番目のシーン", prompt)


class _FakeOllama(BaseHTTPRequestHandler):
    received: list[dict] = []
    response_text = "ここが見どころ!"

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        type(self).received.append({"path": self.path, "body": body})
        payload = json.dumps({"response": type(self).response_text}).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class RoundTrip(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _FakeOllama)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_write_and_payload_shape(self):
        _FakeOllama.received.clear()
        writer = OllamaTelopWriter(model="test-model", url=self.url)
        draft = writer.write(SceneBrief(number=1, start=0.0, duration=3.0,
                                        is_highlight=False, hint="釣りゲーム"))
        self.assertEqual(draft, "ここが見どころ!")
        sent = _FakeOllama.received[0]
        self.assertEqual(sent["path"], "/api/generate")
        self.assertEqual(sent["body"]["model"], "test-model")
        self.assertFalse(sent["body"]["stream"])
        self.assertIn("釣りゲーム", sent["body"]["prompt"])

    def test_connection_failure_is_error_not_silence(self):
        writer = OllamaTelopWriter(model="m", url="http://127.0.0.1:1")
        with self.assertRaises(LlmError):
            writer.write(SceneBrief(number=1, start=0.0, duration=3.0,
                                    is_highlight=False))


class _FakeWriter:
    """analyze 統合用: 決まった下書きを返す偽ライター。"""

    def __init__(self, drafts):
        self.drafts = list(drafts)
        self.briefs = []

    def write(self, brief):
        self.briefs.append(brief)
        return self.drafts.pop(0) if self.drafts else None


class DraftingIntoPlan(unittest.TestCase):
    def _segments(self):
        return propose_segments(10.0, static=[(4, 6)], silent=[], params=AnalyzeParams())

    def test_keep_telops_replaced_and_marked(self):
        writer = _FakeWriter(["開幕から神プレイ", None])
        segments = draft_telops(self._segments(), writer, hint="アクション")
        keeps = [s for s in segments if s.action == "keep"]
        self.assertEqual(keeps[0].telop, "開幕から神プレイ")
        self.assertIn("ローカルAIの下書き", keeps[0].reason)
        # 下書きが得られなかった区間はテンプレートのまま・AI 由来の印なし
        self.assertEqual(keeps[1].telop, "シーン 2")
        self.assertNotIn("ローカルAIの下書き", keeps[1].reason)

    def test_cut_segments_not_sent_to_llm(self):
        writer = _FakeWriter(["a", "b"])
        draft_telops(self._segments(), writer, hint="")
        self.assertEqual(len(writer.briefs), 2)  # keep は 2 区間だけ

    def test_hint_passed_through(self):
        writer = _FakeWriter(["a", "b"])
        draft_telops(self._segments(), writer, hint="ボス戦")
        self.assertTrue(all(b.hint == "ボス戦" for b in writer.briefs))


if __name__ == "__main__":
    unittest.main()
