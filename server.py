"""AI Daily web server: serves site/ and a small API for saved stories and pinned topics.

Standard library only. Meant to bind to a private (Tailscale) address; see serve.sh.

    python3 server.py --bind 100.x.y.z --port 8420

API (JSON):
    GET  /api/state          -> {"saved": {id: story}, "topics": [{"name", "query"}]}
    POST /api/save   story   -> save a story snapshot (kept even after it leaves the 14-day window)
    POST /api/unsave {"id"}  -> remove it
    PUT  /api/topics [..]    -> replace the pinned custom topics
State lives in data/user.json.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
MAX_BODY = 64 * 1024
MAX_SAVED = 5000
MAX_TOPICS = 50
STORY_FIELDS = {"id", "title", "url", "domain", "text", "published", "official", "tags", "via", "heat"}


class BadRequest(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


class State:
    """The user's saved stories and pinned topics, persisted atomically to one JSON file."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.data = self._load()

    def _load(self) -> dict:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(d, dict):
                raise ValueError("not an object")
        except FileNotFoundError:
            d = {}
        except ValueError:
            # Never serve (or overwrite) a damaged file silently: keep it aside for inspection.
            aside = self.path.with_name(f"{self.path.name}.corrupt-{int(time.time())}")
            self.path.replace(aside)
            print(f"state file unreadable, moved to {aside}", file=sys.stderr)
            d = {}
        return {"saved": dict(d.get("saved") or {}), "topics": list(d.get("topics") or [])}

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.data))

    def save(self, story) -> None:
        if not isinstance(story, dict):
            raise BadRequest(400, "story must be an object")
        sid, title = story.get("id"), story.get("title")
        if not (isinstance(sid, str) and 0 < len(sid) <= 64 and isinstance(title, str) and title):
            raise BadRequest(400, "story needs an id (<= 64 chars) and a title")
        clean = {k: v for k, v in story.items() if k in STORY_FIELDS}
        clean["saved_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.lock:
            if sid not in self.data["saved"] and len(self.data["saved"]) >= MAX_SAVED:
                raise BadRequest(400, "too many saved stories")
            self.data["saved"][sid] = clean
            self._write()

    def unsave(self, body) -> None:
        sid = body.get("id") if isinstance(body, dict) else None
        if not isinstance(sid, str):
            raise BadRequest(400, "need an id")
        with self.lock:
            if self.data["saved"].pop(sid, None) is not None:
                self._write()

    def set_topics(self, topics) -> None:
        if not isinstance(topics, list) or len(topics) > MAX_TOPICS:
            raise BadRequest(400, f"topics must be a list of at most {MAX_TOPICS}")
        clean, names = [], set()
        for t in topics:
            name = t.get("name", "").strip() if isinstance(t, dict) and isinstance(t.get("name"), str) else ""
            query = t.get("query", "").strip() if isinstance(t, dict) and isinstance(t.get("query"), str) else ""
            if not (0 < len(name) <= 40 and 0 < len(query) <= 200):
                raise BadRequest(400, "each topic needs a name (<= 40 chars) and a query (<= 200 chars)")
            if name.lower() in names:
                raise BadRequest(400, f"duplicate topic name: {name}")
            names.add(name.lower())
            clean.append({"name": name, "query": query})
        with self.lock:
            self.data["topics"] = clean
            self._write()


class Handler(SimpleHTTPRequestHandler):
    state: State  # set by make_server

    def end_headers(self):
        # The page is rewritten every morning; always revalidate so nobody sees yesterday's front page.
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, fmt, *args):
        if len(args) > 1 and str(args[1])[:1] in "45":  # only log failed requests
            super().log_message(fmt, *args)

    def send_json(self, code: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        # Cross-site forms can't send application/json without a CORS preflight, which we never answer;
        # the Origin check covers the rest.
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host"):
            raise BadRequest(403, "cross-site request")
        if self.headers.get_content_type() != "application/json":
            raise BadRequest(415, "send application/json")
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise BadRequest(413, "body too large")
        try:
            return json.loads(self.rfile.read(n) or b"null")
        except ValueError:
            raise BadRequest(400, "invalid JSON")

    def api(self, method: str) -> None:
        path = urlsplit(self.path).path
        routes = {("GET", "/api/state"): lambda: self.state.snapshot(),
                  ("POST", "/api/save"): lambda: self.state.save(self.read_json()),
                  ("POST", "/api/unsave"): lambda: self.state.unsave(self.read_json()),
                  ("PUT", "/api/topics"): lambda: self.state.set_topics(self.read_json())}
        if (method, path) not in routes:
            known = {p for _, p in routes}
            self.send_json(405 if path in known else 404, {"error": "not found"})
            return
        try:
            result = routes[(method, path)]()
        except BadRequest as e:
            self.send_json(e.code, {"error": str(e)})
            return
        self.send_json(200, result if method == "GET" else {"ok": True})

    def do_GET(self):
        if self.path.startswith("/api/"):
            self.api("GET")
        else:
            super().do_GET()

    def do_POST(self):
        self.api("POST")

    def do_PUT(self):
        self.api("PUT")

    def do_DELETE(self):
        self.api("DELETE")


def make_server(bind: str, port: int, site: Path, state_path: Path) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"state": State(state_path)})
    return ThreadingHTTPServer((bind, port), partial(handler, directory=str(site)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bind", required=True, help="address to listen on (use the Tailscale IP)")
    ap.add_argument("--port", type=int, default=8420)
    ap.add_argument("--site", default=str(ROOT / "site"))
    ap.add_argument("--state", default=str(ROOT / "data" / "user.json"))
    args = ap.parse_args()
    httpd = make_server(args.bind, args.port, Path(args.site), Path(args.state))
    print(f"serving http://{args.bind}:{args.port}/", flush=True)
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
