"""AI Daily collector.

Pulls AI/LLM/agent stories from the platforms in config.json (Hacker News, Reddit, Lobsters,
Hugging Face, GitHub, lab blogs, press feeds, Google News), keeps every post in SQLite
(data/ai-daily.db), then merges, filters and ranks them (rank.py) into site/index.html.

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

import rank

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
SITE_DIR = ROOT / "site"
DB_PATH = DATA_DIR / "ai-daily.db"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ai-daily/2.0 (+personal news digest)"

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
            if e.code in (401, 403, 404, 410):
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


def clip(s: str | None, n: int) -> str | None:
    s = (s or "").strip()
    if not s:
        return None
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


VISIBLE_DATE = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? (\d{1,2}), (20\d\d)\b")
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def page_published(page: str) -> datetime | None:
    d = meta(page, "article:published_time", "datePublished", "og:published_time", "date", "publish-date")
    if not d:
        m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', page)
        d = m.group(1) if m else None
    if d and parse_date(d):  # some sites ship template placeholders like "YYYY-MM-DD"
        return parse_date(d)
    # Some lab blogs only print the date ("Sep 18, 2026") next to the headline.
    h1 = page.find("<h1")
    m = VISIBLE_DATE.search(strip_html(page[h1:h1 + 6000])) if h1 >= 0 else None
    if m:
        return datetime(int(m.group(3)), MONTHS.index(m.group(1)) + 1, int(m.group(2)), 12, tzinfo=timezone.utc)
    return None


def row_id(platform: str, url: str) -> str:
    return hashlib.sha1(f"{platform}|{rank.norm_url(url)}".encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- feeds

NS = {"a": "http://www.w3.org/2005/Atom", "dc": "http://purl.org/dc/elements/1.1/",
      "content": "http://purl.org/rss/1.0/modules/content/"}


def parse_feed(xml: str) -> list[dict]:
    """RSS 2.0 or Atom -> [{title, url, published, text, author}]."""
    root = ET.fromstring(xml.lstrip("\ufeff"))
    out = []
    for it in root.iter("item"):
        out.append({
            "title": strip_html(it.findtext("title")),
            "url": (it.findtext("link") or "").strip(),
            "published": iso(parse_date(it.findtext("pubDate") or it.findtext("dc:date", None, NS))),
            "text": strip_html(it.findtext("description") or it.findtext("content:encoded", None, NS)),
            "raw": it.findtext("description"),
            "author": it.findtext("dc:creator", None, NS) or it.findtext("author"),
        })
    for e in root.iter(f"{{{NS['a']}}}entry"):
        link = next((l.get("href") for l in e.findall("a:link", NS) if l.get("rel") in (None, "alternate")), None)
        out.append({
            "title": strip_html(e.findtext("a:title", "", NS)),
            "url": (link or "").strip(),
            "published": iso(parse_date(e.findtext("a:published", None, NS) or e.findtext("a:updated", None, NS))),
            "text": strip_html(e.findtext("a:summary", None, NS) or e.findtext("a:content", None, NS)),
            "raw": e.findtext("a:summary", None, NS),
            "author": e.findtext("a:author/a:name", None, NS),
        })
    return [x for x in out if x["title"] and x["url"]]


# --------------------------------------------------------------------------- storage

SCHEMA = """
create table if not exists items (
  id text primary key,
  platform text not null,        -- hn | reddit | lobsters | hf_papers | hf_models | github | web | gnews
  source text not null,          -- display name: Hacker News, r/LocalLLaMA, OpenAI, TechCrunch ...
  type text not null,            -- official | press | writer | aggregator | community | paper | code | model | gnews
  dedicated integer default 0,   -- 1: an AI-only source, so it skips the keyword filter
  title text, url text, discussion_url text, text text, author text,
  published text, first_seen text not null, last_seen text not null,
  score integer, comments integer, rank integer
);
create index if not exists items_published on items(published);
create table if not exists comments (
  id text primary key, thread_url text not null, author text, text text, position integer, published text
);
create table if not exists seen_pages (url text primary key, source text, checked_at text);
create table if not exists runs (started text primary key, finished text, stats text);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def upsert(self, it: dict) -> None:
        it = {"discussion_url": None, "text": None, "author": None, "published": None, "score": None,
              "comments": None, "rank": None, "dedicated": 0, **it}
        it.setdefault("id", row_id(it["platform"], it.get("discussion_url") or it["url"]))
        it["first_seen"] = it["last_seen"] = NOW_ISO
        it["text"] = clip(it["text"], 400)
        self.db.execute(
            """insert into items (id, platform, source, type, dedicated, title, url, discussion_url, text, author,
                 published, first_seen, last_seen, score, comments, rank)
               values (:id, :platform, :source, :type, :dedicated, :title, :url, :discussion_url, :text, :author,
                 :published, :first_seen, :last_seen, :score, :comments, :rank)
               on conflict(id) do update set
                 last_seen = excluded.last_seen,
                 source = excluded.source, dedicated = excluded.dedicated,
                 title = coalesce(excluded.title, items.title),
                 text = coalesce(items.text, excluded.text),
                 published = coalesce(items.published, excluded.published),
                 score = max(coalesce(items.score, 0), coalesce(excluded.score, 0)),
                 comments = max(coalesce(items.comments, 0), coalesce(excluded.comments, 0)),
                 rank = min(coalesce(items.rank, 9999), coalesce(excluded.rank, 9999))""",
            it,
        )
        if it["rank"] is None:
            self.db.execute("update items set rank = null where id = ? and rank = 9999", (it["id"],))

    def add_comment(self, c: dict) -> None:
        self.db.execute(
            """insert into comments (id, thread_url, author, text, position, published)
               values (:id, :thread_url, :author, :text, :position, :published)
               on conflict(id) do update set position = excluded.position, text = excluded.text""", c)

    def seen(self, source: str) -> set[str]:
        return {r[0] for r in self.db.execute("select url from seen_pages where source = ?", (source,))}

    def mark_seen(self, source: str, urls) -> None:
        self.db.executemany("insert or replace into seen_pages values (?, ?, ?)",
                            [(u, source, NOW_ISO) for u in urls])


# --------------------------------------------------------------------------- adapters
# Each adapter fetches + parses (thread-safe, no DB access) and returns a list of rows.

class Collector:
    def __init__(self, cfg: dict, store: Store):
        self.cfg = cfg
        self.store = store
        self.window = timedelta(days=cfg.get("window_days", 14))
        self.health: list[dict] = []
        self.pool = ThreadPoolExecutor(max_workers=10)

    def since(self, days: float | None = None) -> datetime:
        return NOW - (timedelta(days=days) if days else self.window)

    def gh_headers(self) -> dict:
        tok = os.environ.get("GITHUB_TOKEN")
        return {"Accept": "application/vnd.github+json", **({"Authorization": f"Bearer {tok}"} if tok else {})}

    # ---- community

    def a_hn(self, s: dict, state: dict) -> list[dict]:
        # Every story over min_points, one Algolia query per day (a query returns at most 1000 hits).
        days = s.get("days", 3) if state["has_rows"] else s.get("backfill_days", self.window.days)
        rows = []
        for d in range(days):
            hi, lo = NOW - timedelta(days=d), NOW - timedelta(days=d + 1)
            q = urllib.parse.urlencode({
                "tags": "story", "hitsPerPage": 1000,
                "numericFilters": f"created_at_i>{int(lo.timestamp())},created_at_i<={int(hi.timestamp())},"
                                  f"points>={s.get('min_points', 15)}"})
            for h in fetch_json(f"https://hn.algolia.com/api/v1/search_by_date?{q}")["hits"]:
                hn = f"https://news.ycombinator.com/item?id={h['objectID']}"
                rows.append({
                    "platform": "hn", "source": "Hacker News", "type": "community",
                    "title": h.get("title"), "url": h.get("url") or hn, "discussion_url": hn,
                    "text": strip_html(h.get("story_text")), "author": h.get("author"),
                    "published": h.get("created_at"), "score": h.get("points"), "comments": h.get("num_comments"),
                })
        return rows

    def a_reddit(self, s: dict, state: dict) -> list[dict]:
        # One combined "top of the day" feed; RSS has no vote counts, so feed position stands in for them.
        u = (f"https://www.reddit.com/r/{'+'.join(s['subs'])}/top/.rss?"
             + urllib.parse.urlencode({"t": "day", "limit": s.get("limit", 100)}))
        root = ET.fromstring(fetch(u, retries=2))
        rows = []
        for pos, e in enumerate(root.findall("a:entry", NS)):
            thread = e.find("a:link", NS).get("href")
            content = e.findtext("a:content", "", NS)
            link = re.search(r'<a href="([^"]+)">\[link\]</a>', content)
            ext = html.unescape(link.group(1)) if link else None
            if ext and ("reddit.com" in ext or "redd.it" in ext):
                ext = None
            cat = e.find("a:category", NS)
            sub = cat.get("term") if cat is not None else ""
            text = strip_html(content)
            text = re.split(r"submitted by\s", text)[0].strip()
            rows.append({
                "platform": "reddit", "source": f"r/{sub}" if sub else "Reddit",
                "type": "community", "dedicated": int(sub in s.get("dedicated_subs", [])),
                "title": strip_html(e.findtext("a:title", "", NS)), "url": ext or thread, "discussion_url": thread,
                "text": text, "author": (e.findtext("a:author/a:name", "", NS) or "").removeprefix("/u/"),
                "published": iso(parse_date(e.findtext("a:published", None, NS) or e.findtext("a:updated", None, NS))),
                "rank": pos,
            })
        return rows

    def a_lobsters(self, s: dict, state: dict) -> list[dict]:
        rows, errors = [], []
        for tag in s.get("tags", ["ai"]):
            try:
                data = fetch_json(f"https://lobste.rs/t/{tag}.json")
            except Exception as e:
                errors.append(str(e))
                continue
            for p in data:
                rows.append({
                    "platform": "lobsters", "source": "Lobsters", "type": "community", "dedicated": 1,
                    "title": p["title"], "url": p.get("url") or p["comments_url"],
                    "discussion_url": p["comments_url"], "text": strip_html(p.get("description")),
                    "author": (p.get("submitter_user") or {}).get("username") if isinstance(p.get("submitter_user"), dict)
                    else p.get("submitter_user"),
                    "published": iso(parse_date(p.get("created_at"))), "score": p.get("score"),
                    "comments": p.get("comment_count"),
                })
        if errors and not rows:
            raise RuntimeError("; ".join(errors))
        return rows

    # ---- papers, models, code

    def a_hf_papers(self, s: dict, state: dict) -> list[dict]:
        rows = []
        for p in fetch_json(f"https://huggingface.co/api/daily_papers?limit={s.get('limit', 50)}"):
            paper = p.get("paper", {})
            pid = paper.get("id")
            if not pid:
                continue
            rows.append({
                "platform": "hf_papers", "source": "HF Papers", "type": "paper", "dedicated": 1,
                "title": p.get("title") or paper.get("title"), "url": f"https://arxiv.org/abs/{pid}",
                "discussion_url": f"https://huggingface.co/papers/{pid}",
                "text": paper.get("ai_summary") or paper.get("summary"),
                "published": iso(parse_date(p.get("publishedAt") or paper.get("publishedAt"))),
                "score": paper.get("upvotes"), "comments": p.get("numComments"),
            })
        return rows

    def a_hf_models(self, s: dict, state: dict) -> list[dict]:
        cutoff = self.since(s.get("max_age_days", 21))
        rows = []
        for m in fetch_json(f"https://huggingface.co/api/models?sort=trendingScore&limit={s.get('limit', 40)}"):
            created = parse_date(m.get("createdAt"))
            if created and created < cutoff:
                continue  # trending but old: not news
            task = (m.get("pipeline_tag") or "").replace("-", " ")
            rows.append({
                "platform": "hf_models", "source": "HF trending", "type": "model", "dedicated": 1,
                "title": f"{m['id']}" + (f" ({task})" if task else ""), "url": f"https://huggingface.co/{m['id']}",
                "published": iso(created), "score": m.get("likes"),
            })
        return rows

    def a_github(self, s: dict, state: dict) -> list[dict]:
        since = self.since(s.get("days", 7))
        seen, rows = set(), []
        for topic in s["topics"]:
            u = "https://api.github.com/search/repositories?" + urllib.parse.urlencode({
                "q": f"topic:{topic} created:>{since:%Y-%m-%d} stars:>={s.get('min_stars', 30)}",
                "sort": "stars", "per_page": s.get("per_topic", 20)})
            for r in fetch_json(u, headers=self.gh_headers()).get("items", []):
                if r["full_name"] in seen:
                    continue
                seen.add(r["full_name"])
                desc = r.get("description") or ""
                rows.append({
                    "platform": "github", "source": "GitHub", "type": "code", "dedicated": 1,
                    "title": r["full_name"] + (f": {clip(desc, 110)}" if desc else ""), "url": r["html_url"],
                    "text": desc, "author": r["owner"]["login"], "published": r.get("created_at"),
                    "score": r.get("stargazers_count"),
                })
        return rows

    # ---- web: lab blogs, press, writers

    def a_feed(self, s: dict, state: dict) -> list[dict]:
        cutoff = self.since()
        rows = []
        for e in parse_feed(fetch(s["url"], timeout=s.get("timeout", 25))):
            pub = parse_date(e["published"])
            if pub and pub < cutoff:
                continue
            if s.get("link_from_description") and e["raw"]:
                # Aggregators (Techmeme) link to their own page; the story is the first outside link.
                ext = [u for u in re.findall(r'(?i)href="(https?://[^"]+)"', e["raw"])
                       if rank.domain(s["url"]) not in rank.domain(u)]
                e["url"] = html.unescape(ext[0]) if ext else e["url"]
                e["title"] = re.sub(r"\s*\([^()]*\)$", "", e["title"])
            e.pop("raw")
            rows.append({"platform": "web", "source": s["name"], "type": s.get("type", "press"),
                         "dedicated": int(s.get("dedicated", True)), **e})
        return rows[: s.get("max", 40)]

    def a_sitemap(self, s: dict, state: dict) -> list[dict]:
        """Blogs without a feed: new URLs under `match` with a recent lastmod, dated from the page itself."""
        xml = fetch(s["url"])
        match = re.compile(s.get("match", "."))
        cutoff = self.since()
        cand = []
        for e in re.findall(r"<url>(.*?)</url>", xml, re.S):
            loc = re.search(r"<loc>\s*([^<\s]+)", e)
            mod = re.search(r"<lastmod>\s*([^<\s]+)", e)
            if not loc or not match.search(loc.group(1)):
                continue
            url = html.unescape(loc.group(1))
            lastmod = parse_date(mod.group(1)) if mod else None
            if url not in state["seen"] and (lastmod is None or lastmod >= cutoff):
                cand.append((lastmod or cutoff, url))
        # Newest lastmod first: a site rebuild can touch every page, and the cap should go to real news.
        state["checked"] = [u for _, u in sorted(cand, reverse=True)[: s.get("max", 20)]]

        def info(u):
            try:
                page = fetch(u, retries=0)
                return u, page_title(page), page_published(page), meta(page, "og:description", "description")
            except Exception:
                return u, None, None, None

        rows = []
        for u, title, pub, desc in self.pool.map(info, state["checked"]):
            if not title or not pub or pub < cutoff:
                continue  # undated or old pages (re-edited evergreen pages) are not news
            title = re.sub(s.get("title_strip", r"$^"), "", title).strip()
            rows.append({"platform": "web", "source": s["name"], "type": s.get("type", "official"),
                         "dedicated": 1, "title": title, "url": u, "text": desc, "published": iso(pub)})
        return rows

    def a_gnews(self, s: dict, state: dict) -> list[dict]:
        rows = []
        for q in s["queries"]:
            u = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
                {"q": f"{q} when:{s.get('days', 3)}d", "hl": "en-US", "gl": "US", "ceid": "US:en"})
            for it in list(ET.fromstring(fetch(u)).iter("item"))[: s.get("per_query", 15)]:
                title, pub = it.findtext("title", ""), it.findtext("source", "")
                if pub and title.endswith(" - " + pub):
                    title = title[: -len(pub) - 3]
                rows.append({"platform": "gnews", "source": pub or "Google News", "type": "gnews",
                             "title": title, "url": it.findtext("link"),
                             "published": iso(parse_date(it.findtext("pubDate")))})
        return rows

    # ---- reactions: top comments from the biggest threads

    def hn_comments(self, per: int, threads: int) -> None:
        rows = self.store.db.execute(
            """select discussion_url from items where platform = 'hn' and published >= ?
               order by score desc limit ?""", (iso(self.since(2)), threads)).fetchall()

        def get(url):
            sid = url.rsplit("=", 1)[-1]
            story = fetch_json(f"https://hacker-news.firebaseio.com/v0/item/{sid}.json") or {}
            kids = story.get("kids", [])[:per + 3]
            out = []
            for c in self.pool.map(lambda k: fetch_json(
                    f"https://hacker-news.firebaseio.com/v0/item/{k}.json", retries=0), kids):
                if c and not c.get("deleted") and not c.get("dead") and c.get("text"):
                    out.append(c)
            return url, out[:per]

        for url, cs in ThreadPoolExecutor(4).map(get, [r[0] for r in rows]):
            for pos, c in enumerate(cs):
                self.store.add_comment({"id": f"hn{c['id']}", "thread_url": url, "author": c.get("by"),
                                        "text": clip(strip_html(c["text"]), 700), "position": pos,
                                        "published": iso(parse_date(c.get("time")))})

    def reddit_comments(self, per: int, threads: int) -> None:
        rows = self.store.db.execute(
            """select discussion_url from items where platform = 'reddit' and last_seen = ? and rank is not null
               order by rank limit ?""", (NOW_ISO, threads)).fetchall()
        for (url,) in rows:
            try:
                root = ET.fromstring(fetch(url.rstrip("/") + f"/.rss?limit={per + 2}&sort=top", retries=0))
            except Exception:
                continue
            finally:
                time.sleep(3)
            for pos, e in enumerate([e for e in root.findall("a:entry", NS)[1:]
                                     if len(strip_html(e.findtext("a:content", "", NS))) >= 40][:per]):
                self.store.add_comment({
                    "id": "rd" + hashlib.sha1(e.findtext("a:id", "", NS).encode()).hexdigest()[:14],
                    "thread_url": url, "author": (e.findtext("a:author/a:name", "", NS) or "").removeprefix("/u/"),
                    "text": clip(strip_html(e.findtext("a:content", "", NS)), 700), "position": pos,
                    "published": iso(parse_date(e.findtext("a:updated", None, NS)))})

    # ----

    def collect(self) -> None:
        adapters = {k[2:]: getattr(self, k) for k in dir(self) if k.startswith("a_")}
        jobs = []
        for s in self.cfg["sources"]:
            state = {"seen": self.store.seen(s["name"]) if s["kind"] == "sitemap" else set(),
                     "has_rows": self.store.db.execute(
                         "select 1 from items where source = ? limit 1", (s["name"],)).fetchone() is not None}
            jobs.append((s, state, self.pool.submit(self._timed, adapters[s["kind"]], s, state)))
        for s, state, fut in jobs:
            rows, err, secs = fut.result()
            for r in rows:
                if r.get("url"):
                    self.store.upsert(r)
            if s["kind"] == "sitemap" and err is None:
                self.store.mark_seen(s["name"], state.get("checked", []))
            self.store.db.commit()
            self.log(s["name"], len(rows), err, secs)
        c = self.cfg.get("comments", {})
        for name, fn in (("HN comments", self.hn_comments), ("Reddit comments", self.reddit_comments)):
            t0 = time.time()
            before = self.store.db.total_changes
            try:
                fn(c.get("per_thread", 4), c.get("hn_threads" if name.startswith("HN") else "reddit_threads", 6))
                err = None
            except Exception as e:
                traceback.print_exc()
                err = clip(str(e), 200)
            self.store.db.commit()
            self.log(name, self.store.db.total_changes - before, err, round(time.time() - t0, 1))

    @staticmethod
    def _timed(fn, s, state):
        t0 = time.time()
        try:
            return fn(s, state), None, round(time.time() - t0, 1)
        except Exception as e:
            traceback.print_exc()
            return [], clip(str(e), 200), round(time.time() - t0, 1)

    def log(self, name, n, err, secs) -> None:
        self.health.append({"name": name, "status": "error" if err else "ok", "error": err, "items": n, "secs": secs})
        print(f"  {'error' if err else 'ok':5} {name:26} {n:4} items  {secs}s" + (f"  {err}" if err else ""))


# --------------------------------------------------------------------------- render

def render(cfg: dict, store: Store, health: list[dict] | None) -> Path:
    since = iso(NOW - timedelta(days=cfg.get("window_days", 14)))
    rows = [dict(r) for r in store.db.execute(
        "select * from items where coalesce(published, first_seen) >= ?", (since,))]
    for r in rows:  # clamp future-dated posts so they can't pin themselves to the top
        if r["published"] and r["published"] > NOW_ISO:
            r["published"] = NOW_ISO
    stories = rank.build_stories(rows, cfg, NOW)

    # Keep the page light: per local-ish day, only the strongest stories.
    per_day = cfg.get("stories_per_day", 80)
    by_day: dict[str, list[dict]] = {}
    for s in sorted(stories, key=lambda s: -s["heat"]):
        by_day.setdefault(s["published"][:10], []).append(s)
    keep = {s["id"] for day in by_day.values() for s in day[:per_day]}
    stories = [s for s in stories if s["id"] in keep]

    threads = {v["discussion_url"] for s in stories for v in s["via"] if v.get("discussion_url")}
    comments: dict[str, list[dict]] = {}
    for c in store.db.execute("select * from comments order by thread_url, position"):
        if c["thread_url"] in threads:
            comments.setdefault(c["thread_url"], []).append(
                {"author": c["author"], "text": c["text"], "published": c["published"]})
    for s in stories:
        s["comments"] = [dict(c, thread=v["discussion_url"], platform=v["platform"])
                         for v in s["via"] for c in comments.get(v.get("discussion_url"), [])][:6]

    if health is None:
        last = store.db.execute("select stats from runs order by started desc limit 1").fetchone()
        health = json.loads(last["stats"]) if last else []
    payload = {"title": cfg["title"], "tagline": cfg.get("tagline", ""), "generated": NOW_ISO,
               "topics": list(cfg.get("topics", {})), "stories": stories, "health": health}
    tpl = (ROOT / "template.html").read_text(encoding="utf-8")
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    out = tpl.replace("/*__DATA__*/null", blob).replace("__TITLE__", html.escape(cfg["title"]))
    SITE_DIR.mkdir(exist_ok=True)
    (SITE_DIR / "index.html").write_text(out, encoding="utf-8")
    (SITE_DIR / "data.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"rendered {SITE_DIR / 'index.html'}  ({len(stories)} stories from {len(rows)} posts)")
    return SITE_DIR / "index.html"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--render", action="store_true", help="only re-render from the database")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    store = Store(DB_PATH)
    health = None
    if not args.render:
        print(f"[{NOW_ISO}] collecting {cfg['title']}")
        c = Collector(cfg, store)
        c.collect()
        health = c.health
        store.db.execute("insert or replace into runs values (?, ?, ?)",
                         (NOW_ISO, iso(datetime.now(timezone.utc)), json.dumps(health)))
        store.db.commit()
    render(cfg, store, health)
    errors = [h["name"] for h in (health or []) if h["status"] != "ok"]
    if errors:
        print("errors: " + ", ".join(errors))
    return 0


if __name__ == "__main__":
    sys.exit(main())
