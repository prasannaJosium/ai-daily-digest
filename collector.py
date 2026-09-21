"""Jev Daily collector.

Fetches the tracked sources and discovery feeds in config.json, stores every item in
SQLite (data/digest.db) and renders a static dashboard to site/index.html.

    python collector.py            # collect + render
    python collector.py --render   # re-render from the database only
"""
from __future__ import annotations

import argparse
import email.utils
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
SITE_DIR = ROOT / "site"
DB_PATH = DATA_DIR / "digest.db"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) jev-daily/1.0 (+personal news digest)"

NOW = datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


NOW_ISO = iso(NOW)


# --------------------------------------------------------------------------- http

def fetch(url: str, timeout: int = 25, retries: int = 1, headers: dict | None = None) -> str:
    hdrs = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8"}
    hdrs.update(headers or {})
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                charset = resp.headers.get_content_charset() or "utf-8"
                return raw.decode(charset, errors="replace")
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (403, 404, 410):
                break
            if e.code == 429:
                time.sleep(10 * (attempt + 1))
                continue
        except Exception as e:  # timeouts, resets
            last = e
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{url}: {last}")


def fetch_json(url: str, **kw):
    return json.loads(fetch(url, **kw))


# --------------------------------------------------------------------------- text helpers

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def strip_html(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|<p[^>]*>", "\n", s)
    s = TAG_RE.sub(" ", s)
    s = html.unescape(s)
    return "\n".join(WS_RE.sub(" ", line).strip() for line in s.splitlines() if line.strip())


def clip(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: n - 1].rsplit(" ", 1)[0] + "…"


def meta(page: str, *names: str) -> str | None:
    for name in names:
        for pat in (
            rf'<meta[^>]+(?:property|name|itemprop)=["\']{re.escape(name)}["\'][^>]*content=["\']([^"\']*)',
            rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name|itemprop)=["\']{re.escape(name)}["\']',
        ):
            m = re.search(pat, page, re.I)
            if m and m.group(1).strip():
                return html.unescape(m.group(1).strip())
    return None


def page_title(page: str) -> str | None:
    t = meta(page, "og:title", "twitter:title")
    if not t:
        m = re.search(r"(?is)<title[^>]*>(.*?)</title>", page)
        t = html.unescape(WS_RE.sub(" ", m.group(1))).strip() if m else None
    return t or None


def parse_date(v) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, timezone.utc)
    s = str(v).strip()
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        return email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None


def page_published(page: str) -> datetime | None:
    d = meta(page, "article:published_time", "datePublished", "og:published_time", "date", "publish-date")
    if not d:
        m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', page)
        d = m.group(1) if m else None
    return parse_date(d)


TRACKING = re.compile(r"^(utm_|ref$|ref_src$|fbclid$|gclid$|mc_)")


def norm_url(u: str) -> str:
    p = urllib.parse.urlsplit(u.strip())
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if not TRACKING.match(k)]
    path = p.path.rstrip("/") or "/"
    host = p.netloc.lower().removeprefix("www.")
    return urllib.parse.urlunsplit(("https", host, path, urllib.parse.urlencode(q), ""))


def item_id(u: str) -> str:
    return hashlib.sha1(norm_url(u).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- tone (heuristic)

POS = re.compile(
    r"\b(impressive|love|loving|great|amazing|excit\w*|game[- ]?changer|useful|promising|cool|clever|"
    r"brilliant|awesome|fantastic|nice|neat|elegant|works (?:really )?well|blown away|finally|wow|"
    r"incredible|solid|huge|big deal|fast(?:er)?|cheap(?:er)?)\b",
    re.I,
)
NEG = re.compile(
    r"\b(skeptic\w*|sceptic\w*|hype\w*|doubt\w*|marketing|cherry[- ]?pick\w*|misleading|overhyped|"
    r"suspicious|meh|not convinced|snake ?oil|vapou?r\w*|grift\w*|bs|bullshit|fake|worse|disappoint\w*|"
    r"just a (?:classifier|bert)|closed[- ]source|unverified|benchmaxx\w*|red flag|concern\w*|"
    r"not (?:new|novel)|reinvent\w*|confus\w*|why not just|what's the catch|underwhelm\w*|lock-?in)\b",
    re.I,
)


def tone(text: str) -> str:
    p, n = len(POS.findall(text)), len(NEG.findall(text))
    if p == 0 and n == 0:
        return "neutral"
    if p >= 2 * n + 1 and p > n:
        return "positive"
    if n >= 2 * p + 1 and n > p:
        return "skeptical"
    return "mixed"


# --------------------------------------------------------------------------- storage

SCHEMA = """
create table if not exists items (
  id text primary key,
  kind text not null,            -- news | reaction | repo | update
  source text not null,
  platform text not null,        -- web | hn | reddit | github | devto | lobsters | gnews
  title text, url text, text text, author text,
  published text, first_seen text not null, last_seen text not null, updated_at text,
  score integer, comments integer, discussion_url text,
  official integer default 0, tone text, context text
);
create table if not exists pages (
  url text primary key, source text, hash text, title text,
  status text, checked_at text, changed_at text
);
create table if not exists runs (
  started text primary key, finished text, stats text
);
"""


class Store:
    def __init__(self, path: Path, title_strip: str | None = None):
        self.title_strip = re.compile(title_strip) if title_strip else None
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def upsert(self, it: dict) -> None:
        it = {
            "kind": "news", "platform": "web", "title": None, "text": None, "author": None,
            "published": None, "updated_at": None, "score": None, "comments": None,
            "discussion_url": None, "official": 0, "tone": None, "context": None, **it,
        }
        it.setdefault("id", item_id(it["url"]))
        if it["title"] and self.title_strip:
            it["title"] = self.title_strip.sub("", it["title"]).strip()
        it["first_seen"] = it["last_seen"] = NOW_ISO
        self.db.execute(
            """insert into items (id, kind, source, platform, title, url, text, author, published,
                 first_seen, last_seen, updated_at, score, comments, discussion_url, official, tone, context)
               values (:id, :kind, :source, :platform, :title, :url, :text, :author, :published,
                 :first_seen, :last_seen, :updated_at, :score, :comments, :discussion_url, :official, :tone, :context)
               on conflict(id) do update set
                 last_seen = excluded.last_seen,
                 title = coalesce(items.title, excluded.title),
                 text = coalesce(excluded.text, items.text),
                 author = coalesce(items.author, excluded.author),
                 published = coalesce(items.published, excluded.published),
                 updated_at = coalesce(excluded.updated_at, items.updated_at),
                 score = max(coalesce(items.score, 0), coalesce(excluded.score, 0)),
                 comments = max(coalesce(items.comments, 0), coalesce(excluded.comments, 0)),
                 discussion_url = coalesce(excluded.discussion_url, items.discussion_url),
                 official = max(items.official, excluded.official),
                 tone = coalesce(excluded.tone, items.tone),
                 context = coalesce(items.context, excluded.context)""",
            it,
        )

    def page(self, url: str):
        return self.db.execute("select * from pages where url = ?", (url,)).fetchone()

    def save_page(self, url, source, h, title, status, changed):
        self.db.execute(
            """insert into pages (url, source, hash, title, status, checked_at, changed_at)
               values (?, ?, ?, ?, ?, ?, ?)
               on conflict(url) do update set hash = coalesce(excluded.hash, pages.hash),
                 title = coalesce(excluded.title, pages.title), status = excluded.status,
                 checked_at = excluded.checked_at,
                 changed_at = coalesce(excluded.changed_at, pages.changed_at)""",
            (url, source, h, title, status, NOW_ISO, NOW_ISO if changed else None),
        )

    def has_pages_for(self, source: str, prefix: str) -> bool:
        return self.db.execute(
            "select 1 from pages where source = ? and url like ? limit 1", (source, prefix + "%")
        ).fetchone() is not None


# --------------------------------------------------------------------------- collectors

class Collector:
    def __init__(self, cfg: dict, store: Store):
        self.cfg = cfg
        self.store = store
        self.rel = re.compile(cfg["relevance"], re.I)
        self.disc = cfg.get("discovery", {})
        self.since = NOW - timedelta(days=cfg.get("window_days", 30))
        self.health: list[dict] = []
        self.pool = ThreadPoolExecutor(max_workers=8)

    def relevant(self, *parts) -> bool:
        return bool(self.rel.search(" ".join(p for p in parts if p)))

    def run(self, name: str, fn, *args) -> None:
        t0 = time.time()
        before = self.store.db.total_changes
        try:
            fn(*args)
            status, err = "ok", None
        except Exception as e:
            status, err = "error", clip(str(e), 200)
            traceback.print_exc()
        self.store.db.commit()
        rec = {"name": name, "status": status, "error": err,
               "changes": self.store.db.total_changes - before, "secs": round(time.time() - t0, 1)}
        self.health.append(rec)
        print(f"  {status:5} {name:34} {rec['changes']:4} changes  {rec['secs']}s" + (f"  {err}" if err else ""))

    # ---- tracked sources

    def src_page(self, s: dict) -> None:
        url = s["url"]
        try:
            body = fetch(url)
        except Exception:
            # Keep a known source visible even when this network can't reach it today.
            self.store.upsert({"url": url, "source": s["name"], "title": s.get("title") or s["name"],
                               "kind": "reference" if s.get("reference") else "news",
                               "published": s.get("published"), "official": int(bool(s.get("official")))})
            self.attach_hn(url)
            raise
        is_html = "<html" in body[:2000].lower()
        title = s.get("title") or (page_title(body) if is_html else None) or url
        desc = (meta(body, "og:description", "description", "twitter:description") if is_html else None)
        if not desc:
            desc = clip(strip_html(body), 280)
        m = re.search(r"(?is)<(article|main)[^>]*>(.*?)</\1>", body) if is_html else None
        text = strip_html(m.group(2) if m else body)
        h = hashlib.sha1(text.encode()).hexdigest()
        prev = self.store.page(url)
        changed = bool(prev and prev["hash"] and prev["hash"] != h)
        self.store.save_page(url, s["name"], h, title, "ok", changed)
        pub = page_published(body) if is_html else None
        self.store.upsert({
            "url": url, "source": s["name"], "title": title, "text": clip(desc, 400),
            "kind": "reference" if s.get("reference") else "news",
            "published": iso(pub), "official": int(bool(s.get("official"))),
            "updated_at": NOW_ISO if changed else None,
        })
        self.attach_hn(url)

    def src_sitemap(self, s: dict) -> None:
        xml = fetch(s["url"])
        entries = re.findall(r"<url>(.*?)</url>", xml, re.S)
        locs = []
        for e in entries:
            loc = re.search(r"<loc>\s*([^<\s]+)", e)
            mod = re.search(r"<lastmod>\s*([^<\s]+)", e)
            if loc and s.get("match", "") in loc.group(1):
                locs.append((html.unescape(loc.group(1)), mod.group(1) if mod else None))
        host = urllib.parse.urlsplit(s["url"]).scheme + "://" + urllib.parse.urlsplit(s["url"]).netloc
        bootstrap = not self.store.has_pages_for(s["name"], host)
        is_blog = "/blog/" in s.get("match", "")
        todo = []
        for loc, mod in locs:
            prev = self.store.page(loc)
            if prev is None:
                todo.append((loc, mod, "new"))
            elif mod and prev["hash"] and prev["hash"] != mod:
                todo.append((loc, mod, "updated"))
            else:
                self.store.save_page(loc, s["name"], mod, None, "ok", False)
        if bootstrap and not is_blog:
            # First sight of a docs sitemap: record the baseline, only report diffs from now on.
            for loc, mod, _ in todo:
                self.store.save_page(loc, s["name"], mod, None, "ok", False)
            return
        info = dict(zip([t[0] for t in todo[:25]], self.pool.map(self._title_of, [t[0] for t in todo[:25]])))
        for loc, mod, what in todo:
            page_t, page_pub = info.get(loc) or (None, None)
            self.store.save_page(loc, s["name"], mod, page_t, "ok", what == "updated")
            title = page_t or loc.rsplit("/", 1)[-1].replace("-", " ")
            if is_blog:
                self.store.upsert({
                    "url": loc, "source": s["name"], "title": title, "official": 1,
                    "published": iso(page_pub or parse_date(mod)),
                    "updated_at": NOW_ISO if what == "updated" else None,
                })
                self.attach_hn(loc)
            else:
                self.store.upsert({
                    "id": item_id(loc + "#" + (mod or NOW_ISO)), "url": loc, "kind": "update",
                    "source": s["name"], "official": 1, "published": iso(parse_date(mod)) or NOW_ISO,
                    "title": f"{'New' if what == 'new' else 'Updated'} doc page: {title}",
                })

    def _title_of(self, url: str) -> tuple[str | None, datetime | None]:
        try:
            page = fetch(url, retries=0)
            return page_title(page), page_published(page)
        except Exception:
            return None, None

    def src_links(self, s: dict) -> None:
        body = fetch(s["url"])
        hrefs = {urllib.parse.urljoin(s["url"], h) for h in re.findall(r'href="([^"#]+)"', body)}
        cand = sorted(h for h in hrefs if s.get("match", "") in h and h.rstrip("/") != s["url"].rstrip("/"))
        new = [h for h in cand if self.store.page(h) is None][:30]

        def check(u):
            try:
                return u, fetch(u, retries=0)
            except Exception:
                return u, None

        for u, page in self.pool.map(check, new):
            if page is None:
                continue
            title = page_title(page)
            ok = self.relevant(strip_html(page))
            self.store.save_page(u, s["name"], None, title, "relevant" if ok else "irrelevant", False)
            if ok:
                self.store.upsert({
                    "url": u, "source": s["name"], "title": title,
                    "text": clip(meta(page, "og:description", "description") or "", 400) or None,
                    "published": iso(page_published(page)),
                })
                self.attach_hn(u)

    def src_github(self, s: dict) -> None:
        for repo in s["repos"]:
            self.github_repo(fetch_json(f"https://api.github.com/repos/{repo}", headers=self.gh_headers()), s["name"])

    def gh_headers(self) -> dict:
        tok = os.environ.get("GITHUB_TOKEN")
        return {"Accept": "application/vnd.github+json", **({"Authorization": f"Bearer {tok}"} if tok else {})}

    def github_repo(self, r: dict, source: str) -> None:
        self.store.upsert({
            "url": r["html_url"], "kind": "repo", "platform": "github", "source": source,
            "title": r["full_name"], "text": clip(r.get("description") or "", 300) or None,
            "author": r["owner"]["login"], "published": r.get("created_at"),
            "updated_at": r.get("pushed_at"), "score": r.get("stargazers_count"),
            "comments": r.get("open_issues_count"),
        })

    def src_search(self, s: dict) -> None:
        # Sources without a known URL: find them by name via Google News and HN.
        n = self.gnews(s["query"], source=s["name"], require=s["name"])
        n += self.hn_stories(s["query"], source=s["name"], require=s["name"])
        if n == 0:
            print(f"        (no matches yet for {s['name']!r})")

    # ---- discovery

    def attach_hn(self, url: str) -> None:
        """Find HN discussions of a specific URL and fold them into that item."""
        q = urllib.parse.urlencode({"query": norm_url(url).split("://", 1)[1], "tags": "story",
                                    "restrictSearchableAttributes": "url", "hitsPerPage": 5})
        try:
            hits = fetch_json(f"https://hn.algolia.com/api/v1/search?{q}", retries=0)["hits"]
        except Exception:
            return
        hits = [h for h in hits if h.get("url") and norm_url(h["url"]) == norm_url(url)]
        if hits:
            top = max(hits, key=lambda h: h.get("points") or 0)
            self.store.upsert({
                "url": url, "source": "?", "score": top.get("points"), "comments": top.get("num_comments"),
                "discussion_url": f"https://news.ycombinator.com/item?id={top['objectID']}",
            })

    def hn_stories(self, query: str, source: str = "Hacker News", require: str | None = None) -> int:
        q = urllib.parse.urlencode({
            "query": query, "tags": "story", "hitsPerPage": 50,
            "numericFilters": f"created_at_i>{int(self.since.timestamp())}",
        })
        n = 0
        for h in fetch_json(f"https://hn.algolia.com/api/v1/search?{q}")["hits"]:
            if not self.relevant(h.get("title"), h.get("url"), h.get("story_text")):
                continue
            if require and require.lower() not in f"{h.get('title')} {h.get('url')}".lower():
                continue
            hn = f"https://news.ycombinator.com/item?id={h['objectID']}"
            self.store.upsert({
                "url": h.get("url") or hn, "platform": "hn", "source": source if source != "Hacker News" else (
                    urllib.parse.urlsplit(h["url"]).netloc.removeprefix("www.") if h.get("url") else "Hacker News"),
                "title": h.get("title"), "author": h.get("author"), "published": h.get("created_at"),
                "score": h.get("points"), "comments": h.get("num_comments"), "discussion_url": hn,
                "text": clip(strip_html(h.get("story_text")), 400) or None,
            })
            n += 1
        return n

    def disc_hn(self) -> None:
        for q in self.disc.get("hn_queries", []):
            self.hn_stories(q)
        # Reactions: comments anywhere on HN that mention the topic.
        for q in self.disc.get("hn_queries", []):
            p = urllib.parse.urlencode({
                "query": q, "tags": "comment", "hitsPerPage": 60,
                "numericFilters": f"created_at_i>{int(self.since.timestamp())}",
            })
            for h in fetch_json(f"https://hn.algolia.com/api/v1/search_by_date?{p}")["hits"]:
                text = strip_html(h.get("comment_text"))
                if self.relevant(text, h.get("story_title")):
                    self.hn_reaction(h["objectID"], h.get("author"), text, h.get("created_at"), h.get("story_title"))

    def disc_hn_threads(self) -> None:
        # Top-ranked comments from the biggest threads (HN's own ranking via the Firebase API).
        rows = self.store.db.execute(
            """select discussion_url, title from items
               where kind != 'reaction' and discussion_url like '%news.ycombinator.com/item?id=%'
               order by coalesce(comments, 0) desc limit ?""",
            (self.disc.get("hn_threads_with_comments", 5),),
        ).fetchall()
        per = self.disc.get("hn_comments_per_thread", 8)
        for row in rows:
            sid = row["discussion_url"].rsplit("=", 1)[-1]
            story = fetch_json(f"https://hacker-news.firebaseio.com/v0/item/{sid}.json")
            kids = (story or {}).get("kids", [])[:per]
            for rank, c in enumerate(self.pool.map(
                    lambda k: fetch_json(f"https://hacker-news.firebaseio.com/v0/item/{k}.json", retries=0), kids)):
                if not c or c.get("deleted") or c.get("dead"):
                    continue
                self.hn_reaction(str(c["id"]), c.get("by"), strip_html(c.get("text")), c.get("time"),
                                 row["title"], score=per - rank)

    def hn_reaction(self, cid, author, text, created, story_title, score=None) -> None:
        if not text:
            return
        self.store.upsert({
            "url": f"https://news.ycombinator.com/item?id={cid}", "kind": "reaction", "platform": "hn",
            "source": "Hacker News", "title": story_title, "text": clip(text, 900), "author": author,
            "published": iso(parse_date(created)), "tone": tone(text), "context": story_title, "score": score,
        })

    def disc_reddit(self) -> None:
        ns = {"a": "http://www.w3.org/2005/Atom"}
        posts = []
        failed = []
        for q in self.disc.get("reddit_queries", []):
            u = "https://www.reddit.com/search.rss?" + urllib.parse.urlencode({"q": q, "sort": "new", "t": "month"})
            try:
                root = ET.fromstring(fetch(u, retries=2))
            except Exception as e:
                failed.append(str(e))
                continue
            for e in root.findall("a:entry", ns):
                title = e.findtext("a:title", "", ns)
                content = strip_html(e.findtext("a:content", "", ns))
                link = e.find("a:link", ns).get("href")
                if not self.relevant(title, content):
                    continue
                cat = e.find("a:category", ns)
                sub = cat.get("label") if cat is not None else "reddit"
                self.store.upsert({
                    "url": link, "kind": "reaction", "platform": "reddit", "source": sub,
                    "title": title, "text": clip(content.replace("submitted by", "").strip(), 900) or None,
                    "author": (e.findtext("a:author/a:name", "", ns) or "").removeprefix("/u/"),
                    "published": e.findtext("a:updated", None, ns), "tone": tone(title + " " + content),
                    "context": title,
                })
                posts.append((link, title, sub))
            time.sleep(6)
        # A few top comments from the newest threads (Reddit throttles RSS, so keep it small).
        for link, title, sub in posts[:4]:
            try:
                root = ET.fromstring(fetch(link.rstrip("/") + "/.rss?limit=6", retries=0))
            except Exception:
                continue
            finally:
                time.sleep(6)
            for e in root.findall("a:entry", ns)[1:6]:  # entry 0 is the post itself
                text = strip_html(e.findtext("a:content", "", ns))
                if len(text) < 40:
                    continue
                self.store.upsert({
                    "url": e.find("a:link", ns).get("href"), "kind": "reaction", "platform": "reddit",
                    "source": sub, "title": title, "text": clip(text, 900), "context": title,
                    "author": (e.findtext("a:author/a:name", "", ns) or "").removeprefix("/u/"),
                    "published": e.findtext("a:updated", None, ns), "tone": tone(text),
                })
        if failed and not posts:
            raise RuntimeError("; ".join(failed))

    def gnews(self, query: str, source: str | None = None, require: str | None = None) -> int:
        u = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
            {"q": f"{query} when:{self.cfg.get('window_days', 30)}d", "hl": "en-US", "gl": "US", "ceid": "US:en"})
        root = ET.fromstring(fetch(u))
        n = 0
        for it in root.iter("item"):
            title = it.findtext("title", "")
            pub = it.findtext("source", "")
            desc = strip_html(it.findtext("description", ""))
            if pub and title.endswith(" - " + pub):
                title = title[: -len(pub) - 3]
            if not self.relevant(title, desc):
                continue
            if require and require.lower() not in (title + " " + pub + " " + desc).lower():
                continue
            self.store.upsert({
                "url": it.findtext("link"), "platform": "gnews", "source": source or pub or "Google News",
                "title": title, "published": iso(parse_date(it.findtext("pubDate"))), "context": pub or None,
            })
            n += 1
        return n

    def disc_gnews(self) -> None:
        for q in self.disc.get("google_news_queries", []):
            self.gnews(q)

    def disc_github(self) -> None:
        for q in self.disc.get("github_queries", []):
            u = "https://api.github.com/search/repositories?" + urllib.parse.urlencode(
                {"q": f"{q} created:>{self.since:%Y-%m-%d}", "sort": "stars", "per_page": 30})
            for r in fetch_json(u, headers=self.gh_headers()).get("items", []):
                if self.relevant(r.get("name"), r.get("description"), " ".join(r.get("topics", []))):
                    self.github_repo(r, "GitHub")

    def disc_devto(self) -> None:
        for tag in self.disc.get("devto_tags", []):
            for a in fetch_json(f"https://dev.to/api/articles?tag={tag}&per_page=30&top=30"):
                if not self.relevant(a.get("title"), a.get("description"), " ".join(a.get("tag_list", []))):
                    continue
                self.store.upsert({
                    "url": a["url"], "platform": "devto", "source": "DEV", "title": a["title"],
                    "text": clip(a.get("description") or "", 300) or None, "author": a["user"]["username"],
                    "published": a.get("published_at"), "score": a.get("positive_reactions_count"),
                    "comments": a.get("comments_count"), "discussion_url": a["url"] + "#comments",
                })

    def disc_lobsters(self) -> None:
        for tag in self.disc.get("lobsters_tags", []):
            root = ET.fromstring(fetch(f"https://lobste.rs/t/{tag}.rss"))
            for it in root.iter("item"):
                title, link = it.findtext("title", ""), it.findtext("link", "")
                if not self.relevant(title, link):
                    continue
                self.store.upsert({
                    "url": link, "platform": "lobsters", "source": "Lobsters", "title": title,
                    "author": (it.findtext("author") or "").rsplit(" via ", 1)[-1] or None,
                    "published": iso(parse_date(it.findtext("pubDate"))),
                    "discussion_url": it.findtext("comments"),
                })

    # ----

    def collect(self) -> None:
        kinds = {"page": self.src_page, "sitemap": self.src_sitemap, "links": self.src_links,
                 "github": self.src_github, "search": self.src_search}
        for s in self.cfg["sources"]:
            label = s["name"] + ("" if s["kind"] == "page" else f" ({s['kind']})")
            self.run(label, kinds[s["kind"]], s)
        self.run("Hacker News", self.disc_hn)
        self.run("Google News", self.disc_gnews)
        self.run("Reddit", self.disc_reddit)
        self.run("GitHub search", self.disc_github)
        self.run("DEV", self.disc_devto)
        self.run("Lobsters", self.disc_lobsters)
        self.run("HN top comments", self.disc_hn_threads)
        # attach_hn upserts with source "?" only when the item already exists; drop any orphan.
        self.store.db.execute("delete from items where source = '?'")
        self.store.db.commit()


# --------------------------------------------------------------------------- render

def render(cfg: dict, store: Store, health: list[dict] | None) -> Path:
    since = iso(NOW - timedelta(days=cfg.get("window_days", 30)))
    rows = store.db.execute(
        """select * from items where official = 1 or kind = 'repo'
             or coalesce(published, first_seen) >= ? or first_seen >= ? or updated_at >= ?""",
        (since, since, since),
    ).fetchall()
    items = [dict(r) for r in rows]

    # Google News links are redirects, so they can't merge by URL. Drop them when the same
    # headline already came in from a direct source.
    def key(t):
        return re.sub(r"[^a-z0-9]", "", (t or "").lower())[:48]

    direct = {key(i["title"]) for i in items if i["platform"] != "gnews"}
    items = [i for i in items if i["platform"] != "gnews" or key(i["title"]) not in direct]

    # Cross-posted reactions (same text in several subreddits) collapse into the earliest copy.
    seen: dict[str, dict] = {}
    deduped = []
    for i in sorted(items, key=lambda i: i["published"] or i["first_seen"]):
        if i["kind"] == "reaction" and i["text"]:
            k = re.sub(r"[^a-z0-9]", "", i["text"].lower())[:160]
            if k in seen:
                seen[k]["crossposts"] = seen[k].get("crossposts", 1) + 1
                continue
            seen[k] = i
        deduped.append(i)
    items = deduped

    if health is None:
        last = store.db.execute("select stats from runs order by started desc limit 1").fetchone()
        health = json.loads(last["stats"]) if last else []
    pages = [dict(r) for r in store.db.execute(
        "select url, source, title, status, checked_at, changed_at from pages where source in (%s)"
        % ",".join("?" * len(cfg["sources"])), [s["name"] for s in cfg["sources"]]).fetchall()]

    sources = []
    for s in cfg["sources"]:
        label = s["name"] + ("" if s["kind"] == "page" else f" ({s['kind']})")
        h = next((x for x in health if x["name"] == label), None)
        pg = next((p for p in pages if p["url"] == s.get("url")), None)
        n = sum(1 for i in items if i["source"] == s["name"])
        sources.append({"name": s["name"], "kind": s["kind"], "url": s.get("url") or s.get("query"),
                        "official": bool(s.get("official")), "status": h["status"] if h else None,
                        "error": h["error"] if h else None, "items": n,
                        "changed_at": pg["changed_at"] if pg else None})

    first = store.db.execute("select min(started) from runs").fetchone()[0]
    payload = {
        "title": cfg["title"], "topic": cfg["topic"], "generated": NOW_ISO, "first_run": first or NOW_ISO,
        "items": items, "sources": sources, "health": health,
    }
    tpl = (ROOT / "template.html").read_text(encoding="utf-8")
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    out = tpl.replace("/*__DATA__*/null", blob).replace("__TITLE__", html.escape(cfg["title"]))
    SITE_DIR.mkdir(exist_ok=True)
    (SITE_DIR / "index.html").write_text(out, encoding="utf-8")
    (SITE_DIR / "data.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return SITE_DIR / "index.html"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--render", action="store_true", help="only re-render from the database")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    store = Store(DB_PATH, cfg.get("title_strip"))
    health = None
    if not args.render:
        print(f"[{NOW_ISO}] collecting {cfg['topic']}")
        c = Collector(cfg, store)
        c.collect()
        health = c.health
        store.db.execute("insert or replace into runs values (?, ?, ?)",
                         (NOW_ISO, iso(datetime.now(timezone.utc)), json.dumps(health)))
        store.db.commit()
    out = render(cfg, store, health)
    total = store.db.execute("select count(*) from items").fetchone()[0]
    errors = [h["name"] for h in (health or []) if h["status"] != "ok"]
    print(f"rendered {out}  ({total} items in db" + (f"; errors: {', '.join(errors)}" if errors else "") + ")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
