"""Tests for rank.py (python -m unittest)."""
import unittest
from datetime import datetime, timedelta, timezone

import rank

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)

CFG = {
    "relevance": r"(?-i:\bAI\b)|\bLLMs?\b|\bagents?\b|\bClaude\b|\bGPT[-\w.]*",
    "noise": r"\bstocks?\b|\bcrypto\b",
    "topics": {
        "launches": r"\blaunch\w*|\breleas\w*|\bintroduc\w*",
        "agents": r"\bagents?\b|\bagentic\b|\bMCP\b",
        "research": r"\bpaper\b|\barxiv\b|\bbenchmark",
        "tools": r"\bframework\b|\blibrary\b|\bSDK\b",
        "open": r"\bopen[- ]weights?\b|\bopen[- ]source\b",
    },
    "platform_topics": {"hf_papers": ["research"], "github": ["tools", "open"]},
    "type_weight": {"official": 1.0, "press": 0.45, "gnews": 0.3},
    "platform_weight": {"hn": 1.0, "reddit": 0.9, "lobsters": 0.7},
    "grace_hours": 12,
    "half_life_hours": 24,
    "cross_platform_boost": 0.5,
}


def row(**kw):
    base = {"id": kw.get("url", "x"), "platform": "hn", "source": "Hacker News", "type": "community",
            "dedicated": 0, "title": "An AI thing", "url": "https://example.com/a", "discussion_url": None,
            "text": None, "published": (NOW - timedelta(hours=3)).isoformat(), "first_seen": NOW.isoformat(),
            "score": None, "comments": None, "rank": None}
    base.update(kw)
    return base


class CanonTest(unittest.TestCase):
    def test_arxiv_variants_collapse(self):
        keys = {rank.canon(u) for u in (
            "https://arxiv.org/abs/2609.18766", "https://arxiv.org/pdf/2609.18766v2",
            "https://huggingface.co/papers/2609.18766")}
        self.assertEqual(keys, {"arxiv:2609.18766"})

    def test_github_subpaths_collapse_to_repo(self):
        self.assertEqual(rank.canon("https://github.com/Foo/Bar/tree/main/src?utm_source=x"), "github.com/foo/bar")

    def test_tracking_params_and_www_dropped(self):
        self.assertEqual(rank.canon("http://www.example.com/post/?utm_source=hn&id=3"),
                         rank.canon("https://example.com/post?id=3"))

    def test_hf_model_but_not_blog(self):
        self.assertEqual(rank.canon("https://huggingface.co/Qwen/Qwen3-8B/tree/main"), "huggingface.co/qwen/qwen3-8b")
        self.assertEqual(rank.canon("https://huggingface.co/blog/some-post"), "huggingface.co/blog/some-post")


class RelevanceTest(unittest.TestCase):
    def setUp(self):
        self.f = rank.Filters(CFG)

    def test_general_source_needs_ai_match(self):
        self.assertTrue(self.f.keep(row(title="Claude gets a new memory feature")))
        self.assertFalse(self.f.keep(row(title="Rust 2.0 is out")))

    def test_ai_is_case_sensitive(self):
        self.assertFalse(self.f.keep(row(title="Visiting Hawaii in winter")))
        self.assertTrue(self.f.keep(row(title="What AI means for search")))

    def test_dedicated_source_passes_without_keyword(self):
        self.assertTrue(self.f.keep(row(title="Scaling sparse attention", dedicated=1, platform="hf_papers")))

    def test_noise_drops_even_dedicated(self):
        self.assertFalse(self.f.keep(row(title="Best AI stocks to buy", dedicated=1)))

    def test_official_ignores_noise(self):
        self.assertTrue(self.f.keep(row(title="Our crypto policy", dedicated=1, type="official")))


class TagTest(unittest.TestCase):
    def test_top_two_by_matches(self):
        f = rank.Filters(CFG)
        tags = f.tags("Introducing an open-source agent framework for MCP agents", "", ["hn"])
        self.assertEqual(tags[0], "agents")
        self.assertEqual(len(tags), 2)

    def test_single_body_mention_is_not_a_tag(self):
        self.assertEqual(rank.Filters(CFG).tags("A new AI thing", "also mentions a paper", ["hn"]), [])

    def test_platform_prior(self):
        self.assertEqual(rank.Filters(CFG).tags("Scaling sparse attention", "", ["hf_papers"]), ["research"])


class ClusterTest(unittest.TestCase):
    def test_same_canonical_url_merges(self):
        a = row(id="1", url="https://arxiv.org/abs/2609.10001", title="Paper one about AI")
        b = row(id="2", platform="hf_papers", url="https://huggingface.co/papers/2609.10001", title="Different words here")
        self.assertEqual(len(rank.cluster([a, b])), 1)

    def test_near_identical_titles_merge(self):
        a = row(id="1", url="https://a.com/x", title="OpenAI releases GPT-6 with native agents")
        b = row(id="2", url="https://b.com/y", platform="gnews", title="OpenAI releases GPT-6, with native agents")
        self.assertEqual(len(rank.cluster([a, b])), 1)

    def test_different_stories_stay_apart(self):
        a = row(id="1", url="https://a.com/x", title="OpenAI releases GPT-6 with native agents")
        b = row(id="2", url="https://b.com/y", title="Anthropic ships Claude memory for teams")
        self.assertEqual(len(rank.cluster([a, b])), 2)

    def test_short_titles_never_title_merge(self):
        a = row(id="1", url="https://a.com/x", title="AI news")
        b = row(id="2", url="https://b.com/y", title="AI news")
        self.assertEqual(len(rank.cluster([a, b])), 2)


class RankTest(unittest.TestCase):
    def stories(self, rows):
        return {s["title"]: s for s in rank.build_stories(rows, CFG, NOW)}

    def test_cross_platform_beats_single_platform(self):
        rows = [
            row(id="1", url="https://x.com/one", title="AI story one is here", score=100, comments=50),
            row(id="2", url="https://x.com/one", title="AI story one is here", platform="reddit", rank=5),
            row(id="3", url="https://x.com/two", title="AI story two is elsewhere", score=100, comments=50),
        ]
        s = self.stories(rows)
        self.assertGreater(s["AI story one is here"]["heat"], s["AI story two is elsewhere"]["heat"])
        self.assertEqual(len(s["AI story one is here"]["via"]), 2)

    def test_no_decay_within_grace(self):
        rows = [
            row(id="1", url="https://x.com/a", title="Very fresh AI story", score=5,
                published=(NOW - timedelta(hours=1)).isoformat()),
            row(id="2", url="https://x.com/b", title="Big AI story from this morning", score=500,
                published=(NOW - timedelta(hours=10)).isoformat()),
        ]
        s = self.stories(rows)
        self.assertGreater(s["Big AI story from this morning"]["rank"], s["Very fresh AI story"]["rank"])

    def test_older_story_decays(self):
        rows = [
            row(id="1", url="https://x.com/a", title="Fresh AI story today", score=50),
            row(id="2", url="https://x.com/b", title="Old AI story yesterday", score=50,
                published=(NOW - timedelta(hours=30)).isoformat()),
        ]
        s = self.stories(rows)
        self.assertGreater(s["Fresh AI story today"]["rank"], s["Old AI story yesterday"]["rank"])
        self.assertAlmostEqual(s["Fresh AI story today"]["heat"], s["Old AI story yesterday"]["heat"])

    def test_official_post_gets_floor_without_engagement(self):
        rows = [
            row(id="1", platform="web", type="official", dedicated=1, source="OpenAI",
                url="https://openai.com/index/x", title="Introducing a model"),
            row(id="2", url="https://x.com/b", title="Small AI blog post", score=2),
            row(id="3", url="https://x.com/c", title="Big AI blog post", score=400),
        ]
        s = self.stories(rows)
        self.assertGreater(s["Introducing a model"]["heat"], s["Small AI blog post"]["heat"])
        self.assertTrue(s["Introducing a model"]["official"])

    def test_lead_title_prefers_official_and_url_prefers_article(self):
        rows = [
            row(id="1", url="https://openai.com/index/x", title="HN: OpenAI does a thing (AI)", score=10,
                discussion_url="https://news.ycombinator.com/item?id=1"),
            row(id="2", platform="web", type="official", dedicated=1, source="OpenAI",
                url="https://openai.com/index/x", title="Introducing the thing"),
        ]
        (s,) = rank.build_stories(rows, CFG, NOW)
        self.assertEqual(s["title"], "Introducing the thing")
        self.assertEqual(s["url"], "https://openai.com/index/x")
        self.assertEqual(s["domain"], "openai.com")

    def test_irrelevant_rows_dropped(self):
        self.assertEqual(rank.build_stories([row(title="Rust 2.0 is out")], CFG, NOW), [])


if __name__ == "__main__":
    unittest.main()
