"""Story logic for AI Daily: relevance, topic tags, merging and ranking.

Pure functions only (no network, no database): stored rows in, ranked stories out.
Everything tunable lives in config.json; see test_rank.py for the behaviour.
"""
from __future__ import annotations

import hashlib
import math
import re
import urllib.parse
from datetime import datetime, timezone

# --------------------------------------------------------------------------- urls

TRACKING = re.compile(r"^(utm_|ref$|ref_src$|fbclid$|gclid$|mc_|source$|sk$|s$)")
ARXIV_RE = re.compile(r"(?:arxiv\.org/(?:abs|pdf|html)/|huggingface\.co/papers/|alphaxiv\.org/abs/)(\d{4}\.\d{4,5})")
HF_NOT_MODELS = {"blog", "papers", "spaces", "datasets", "docs", "learn", "posts", "collections", "organizations"}


def norm_url(u: str) -> str:
    p = urllib.parse.urlsplit(u.strip())
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if not TRACKING.match(k)]
    path = p.path.rstrip("/") or "/"
    host = p.netloc.lower().removeprefix("www.")
    return urllib.parse.urlunsplit(("https", host, path, urllib.parse.urlencode(q), ""))


def canon(u: str | None) -> str:
    """A merge key: the same paper, repo or article from any platform maps to one key."""
    if not u:
        return ""
    m = ARXIV_RE.search(u)
    if m:
        return "arxiv:" + m.group(1)
    p = urllib.parse.urlsplit(norm_url(u))
    parts = [x for x in p.path.split("/") if x]
    if p.netloc == "github.com" and len(parts) >= 2:
        return f"github.com/{parts[0].lower()}/{parts[1].lower()}"
    if p.netloc == "huggingface.co" and len(parts) >= 2 and parts[0] not in HF_NOT_MODELS:
        return f"huggingface.co/{parts[0].lower()}/{parts[1].lower()}"
    return p.netloc + p.path + ("?" + p.query if p.query else "")


def domain(u: str | None) -> str:
    return urllib.parse.urlsplit(u or "").netloc.lower().removeprefix("www.")


DISCUSSION_HOSTS = ("news.ycombinator.com", "reddit.com", "lobste.rs", "news.google.com")


def is_discussion(u: str | None) -> bool:
    return any(domain(u).endswith(h) for h in DISCUSSION_HOSTS)


# --------------------------------------------------------------------------- dates

def parse_iso(s) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def when(r: dict) -> datetime:
    return parse_iso(r.get("published")) or parse_iso(r.get("first_seen")) or datetime.now(timezone.utc)


# --------------------------------------------------------------------------- relevance and topics

class Filters:
    def __init__(self, cfg: dict):
        self.rel = re.compile(cfg["relevance"], re.I)
        self.noise = re.compile(cfg["noise"], re.I) if cfg.get("noise") else None
        self.topics = {k: re.compile(v, re.I) for k, v in cfg.get("topics", {}).items()}
        self.priors = cfg.get("platform_topics", {})

    def keep(self, r: dict) -> bool:
        text = " ".join(x for x in (r.get("title"), r.get("text")) if x)
        if r.get("type") == "official":
            return True
        if self.noise and self.noise.search(r.get("title") or ""):
            return False
        return bool(r.get("dedicated")) or bool(self.rel.search(text + " " + (r.get("url") or "")))

    def tags(self, title: str, text: str, platforms: list[str], n: int = 2) -> list[str]:
        # Title words count double: body text is long and mentions everything in passing.
        hits = {k: 2 * len(rx.findall(title)) + len(rx.findall(text)) for k, rx in self.topics.items()}
        for p in platforms:
            for k in self.priors.get(p, []):
                hits[k] = hits.get(k, 0) + 2
        order = list(self.topics)
        ranked = sorted((k for k, v in hits.items() if v > 1), key=lambda k: (-hits[k], order.index(k)))
        return ranked[:n]


# --------------------------------------------------------------------------- merging

STOP = set("""a an the and or of to in on for with by from at as is are was were be been this that these those
it its into about over new how why what when who your you we our their they via vs show hn ask tell just now
after before more than up out not no can will has have had do does""".split())


def tokens(title: str | None) -> set[str]:
    words = re.findall(r"[a-z0-9][a-z0-9.+\-]*", (title or "").lower())
    return {w.rstrip(".-") for w in words if w.rstrip(".-") not in STOP and len(w) > 1}


def similar(a: set[str], b: set[str]) -> bool:
    if min(len(a), len(b)) < 4:
        return False
    return len(a & b) / len(a | b) >= 0.7


def cluster(rows: list[dict], days: float = 4) -> list[list[dict]]:
    """Group rows about the same story: same canonical URL, or near-identical titles close in time."""
    parent = list(range(len(rows)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        parent[find(i)] = find(j)

    by_key: dict[str, int] = {}
    for i, r in enumerate(rows):
        for k in {canon(r.get("url")), canon(r.get("discussion_url"))} - {""}:
            if k in by_key:
                union(i, by_key[k])
            else:
                by_key[k] = i
    toks = [tokens(r.get("title")) for r in rows]
    times = [when(r) for r in rows]
    order = sorted(range(len(rows)), key=lambda i: times[i])
    for a_pos, i in enumerate(order):
        for j in order[a_pos + 1:]:
            if (times[j] - times[i]).total_seconds() > days * 86400:
                break
            if find(i) != find(j) and similar(toks[i], toks[j]):
                union(i, j)
    groups: dict[int, list[dict]] = {}
    for i, r in enumerate(rows):
        groups.setdefault(find(i), []).append(r)
    return list(groups.values())


# --------------------------------------------------------------------------- scoring

# Which row names a story: an official post beats press, which beats community titles.
LEAD = {"official": 0, "press": 1, "writer": 1, "aggregator": 2, "community": 3, "paper": 3,
        "code": 4, "model": 4, "gnews": 5}
# Platforms that count separately for the cross-platform boost ("web" covers every feed).
PLATFORM_GROUP = {"gnews": "web"}


def engagement(r: dict) -> int:
    return (r.get("score") or 0) + (r.get("comments") or 0)


def strengths(rows: list[dict], cfg: dict) -> dict[str, float]:
    """Per-row signal on a common scale (~1.0 = a strong post on its platform)."""
    refs: dict[str, float] = {}
    by_platform: dict[str, list[int]] = {}
    for r in rows:
        if engagement(r) > 0:
            by_platform.setdefault(r["platform"], []).append(engagement(r))
    for p, vals in by_platform.items():
        vals.sort()
        refs[p] = max(vals[int(0.9 * (len(vals) - 1))], 20)
    tw, pw = cfg.get("type_weight", {}), cfg.get("platform_weight", {})
    out = {}
    for r in rows:
        if r.get("rank") is not None:  # position in a ranked feed without vote counts (Reddit)
            s = max(0.3, 1.2 - r["rank"] / 60)
        elif engagement(r) > 0:
            s = min(1.6, math.log1p(engagement(r)) / math.log1p(refs[r["platform"]]))
        else:
            s = tw.get(r.get("type"), tw.get(r["platform"], 0.3))
        out[r["id"]] = s * pw.get(r["platform"], 1.0)
    return out


def build_stories(rows: list[dict], cfg: dict, now: datetime) -> list[dict]:
    f = Filters(cfg)
    rows = [r for r in rows if f.keep(r)]
    st = strengths(rows, cfg)
    grace, half = cfg.get("grace_hours", 12), cfg.get("half_life_hours", 24)
    boost = cfg.get("cross_platform_boost", 0.5)
    floor = cfg.get("type_weight", {}).get("official", 1.0)
    stories = []
    for g in cluster(rows):
        g.sort(key=lambda r: (LEAD.get(r.get("type"), LEAD.get(r["platform"], 3)), -st[r["id"]]))
        lead = g[0]
        url = next((r["url"] for r in g if r.get("url") and not is_discussion(r["url"])), None) \
            or lead.get("url") or lead.get("discussion_url")
        published = min(when(r) for r in g)
        platforms = sorted({PLATFORM_GROUP.get(r["platform"], r["platform"]) for r in g})
        base = max(st[r["id"]] for r in g)
        official = any(r.get("type") == "official" for r in g)
        if official:
            base = max(base, floor)
        heat = base * (1 + boost * min(3, len(platforms) - 1))
        age_h = max(0.0, (now - published).total_seconds() / 3600)
        titles = " ".join(r["title"] for r in g if r.get("title"))
        text = " ".join(r["text"] for r in g if r.get("text"))
        stories.append({
            "id": hashlib.sha1(canon(url).encode()).hexdigest()[:12],
            "title": lead["title"], "url": url, "domain": "" if is_discussion(url) else domain(url),
            "text": next((r["text"] for r in g if r.get("text")), None),
            "published": published.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "official": official, "heat": round(heat, 4),
            # A daily page: everything from the last `grace` hours is "today", older stories halve every `half`.
            "rank": round(heat * 0.5 ** (max(0.0, age_h - grace) / half), 6),
            "tags": f.tags(titles, text, [r["platform"] for r in g]),
            "via": [{k: r.get(k) for k in ("platform", "source", "score", "comments", "rank", "discussion_url", "url")}
                    for r in sorted(g, key=lambda r: -st[r["id"]])],
        })
    stories.sort(key=lambda s: -s["rank"])
    return stories
