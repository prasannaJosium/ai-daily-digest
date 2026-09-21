"""Tests for server.py (python -m unittest)."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import server


def story(i="abc123", **kw):
    s = {"id": i, "title": f"Story {i}", "url": "https://example.com/" + i, "domain": "example.com",
         "published": "2026-09-21T00:00:00Z", "tags": ["agents"], "via": [{"platform": "hn", "score": 5}]}
    s.update(kw)
    return s


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "site").mkdir()
        (root / "site" / "index.html").write_text("<title>AI Daily</title>", encoding="utf-8")
        self.state_path = root / "data" / "user.json"
        self.start(root)

    def start(self, root):
        self.httpd = server.make_server("127.0.0.1", 0, root / "site", self.state_path)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, headers=None):
        h = {"Content-Type": "application/json"} if body is not None else {}
        h.update(headers or {})
        data = json.dumps(body).encode() if body is not None and not isinstance(body, bytes) else body
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"null") if "json" in r.headers.get("Content-Type", "") else r.read()
        except urllib.error.HTTPError as e:
            return e.code, None

    def test_serves_the_page_without_caching(self):
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn(b"AI Daily", r.read())
            self.assertEqual(r.headers["Cache-Control"], "no-cache")

    def test_empty_state(self):
        self.assertEqual(self.call("GET", "/api/state"), (200, {"saved": {}, "topics": []}))

    def test_save_and_unsave(self):
        self.assertEqual(self.call("POST", "/api/save", story())[0], 200)
        _, st = self.call("GET", "/api/state")
        self.assertEqual(st["saved"]["abc123"]["title"], "Story abc123")
        self.assertIn("saved_at", st["saved"]["abc123"])
        self.assertEqual(self.call("POST", "/api/unsave", {"id": "abc123"})[0], 200)
        self.assertEqual(self.call("GET", "/api/state")[1]["saved"], {})

    def test_unknown_story_fields_are_dropped(self):
        self.call("POST", "/api/save", story(evil="x" * 50))
        self.assertNotIn("evil", self.call("GET", "/api/state")[1]["saved"]["abc123"])

    def test_invalid_story_rejected(self):
        self.assertEqual(self.call("POST", "/api/save", {"title": "no id"})[0], 400)
        self.assertEqual(self.call("POST", "/api/save", story(i="x" * 100))[0], 400)
        self.assertEqual(self.call("POST", "/api/save", b"not json")[0], 400)

    def test_topics_replace_and_validate(self):
        topics = [{"name": "MCP", "query": "mcp|model context protocol"}, {"name": "Qwen", "query": "qwen"}]
        self.assertEqual(self.call("PUT", "/api/topics", topics)[0], 200)
        self.assertEqual(self.call("GET", "/api/state")[1]["topics"], topics)
        self.assertEqual(self.call("PUT", "/api/topics", [{"name": "", "query": "x"}])[0], 400)
        self.assertEqual(self.call("PUT", "/api/topics", {"name": "MCP"})[0], 400)

    def test_state_survives_restart(self):
        self.call("POST", "/api/save", story())
        self.call("PUT", "/api/topics", [{"name": "MCP", "query": "mcp"}])
        self.httpd.shutdown()
        self.httpd.server_close()
        self.start(Path(self.tmp.name))
        _, st = self.call("GET", "/api/state")
        self.assertIn("abc123", st["saved"])
        self.assertEqual(st["topics"][0]["name"], "MCP")

    def test_cross_site_writes_rejected(self):
        code, _ = self.call("POST", "/api/save", story(), headers={"Origin": "https://evil.example"})
        self.assertEqual(code, 403)
        self.assertEqual(self.call("GET", "/api/state")[1]["saved"], {})

    def test_same_origin_writes_allowed(self):
        code, _ = self.call("POST", "/api/save", story(), headers={"Origin": self.base})
        self.assertEqual(code, 200)

    def test_writes_require_json(self):
        code, _ = self.call("POST", "/api/save", json.dumps(story()).encode(),
                            headers={"Content-Type": "text/plain"})
        self.assertEqual(code, 415)

    def test_body_size_limit(self):
        self.assertEqual(self.call("POST", "/api/save", story(text="x" * 200_000))[0], 413)

    def test_corrupt_state_file_is_set_aside(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.state_path.parent.mkdir(exist_ok=True)
        self.state_path.write_text("{not json", encoding="utf-8")
        self.start(Path(self.tmp.name))
        self.assertEqual(self.call("GET", "/api/state")[1], {"saved": {}, "topics": []})
        self.assertTrue(list(self.state_path.parent.glob("user.json.corrupt-*")))

    def test_unknown_api_path(self):
        self.assertEqual(self.call("GET", "/api/nope")[0], 404)
        self.assertEqual(self.call("DELETE", "/api/state")[0], 405)


if __name__ == "__main__":
    unittest.main()
