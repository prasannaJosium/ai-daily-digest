"""Tests for collector.Store eviction (python -m unittest)."""
import tempfile
import unittest
from pathlib import Path

import collector


class EvictTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = collector.Store(Path(self.tmp.name) / "t.db")
        db = self.store.db
        rows = [  # id, published, first_seen, discussion_url
            ("old", "2026-09-01T10:00:00Z", "2026-09-21T00:00:00Z", "https://news.ycombinator.com/item?id=1"),
            ("new", "2026-09-20T10:00:00.000Z", "2026-09-21T00:00:00Z", "https://news.ycombinator.com/item?id=2"),
            ("undated-old", None, "2026-09-02T00:00:00Z", None),
            ("undated-new", None, "2026-09-21T00:00:00Z", None),
        ]
        for i, pub, seen, disc in rows:
            db.execute("""insert into items (id, platform, source, type, title, url, discussion_url, published,
                          first_seen, last_seen) values (?, 'hn', 'HN', 'community', ?, ?, ?, ?, ?, ?)""",
                       (i, i, "https://x.com/" + i, disc, pub, seen, seen))
        for cid, thread in (("c1", rows[0][3]), ("c2", rows[1][3])):
            db.execute("insert into comments values (?, ?, 'a', 'text', 0, null)", (cid, thread))
        db.execute("insert into runs values ('2026-09-01T02:00:00Z', null, '[]')")
        db.execute("insert into runs values ('2026-09-21T02:00:00Z', null, '[]')")
        db.execute("insert into seen_pages values ('https://anthropic.com/news/old', 'Anthropic', '2026-09-01T00:00:00Z')")
        db.commit()

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def ids(self, table, col="id"):
        return {r[0] for r in self.store.db.execute(f"select {col} from {table}")}

    def test_evicts_posts_comments_and_runs_older_than_cutoff(self):
        n = self.store.evict("2026-09-08T00:00:00Z")
        self.assertEqual(self.ids("items"), {"new", "undated-new"})
        self.assertEqual(self.ids("comments"), {"c2"})
        self.assertEqual(self.ids("runs", "started"), {"2026-09-21T02:00:00Z"})
        self.assertEqual(n, {"posts": 2, "comments": 1})

    def test_keeps_seen_pages_so_old_sitemap_posts_are_not_rediscovered(self):
        self.store.evict("2026-09-08T00:00:00Z")
        self.assertEqual(self.ids("seen_pages", "url"), {"https://anthropic.com/news/old"})

    def test_nothing_to_evict(self):
        self.assertEqual(self.store.evict("2026-08-01T00:00:00Z"), {"posts": 0, "comments": 0})
        self.assertEqual(len(self.ids("items")), 4)


if __name__ == "__main__":
    unittest.main()
