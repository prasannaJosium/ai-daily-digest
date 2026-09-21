# AI Daily

A daily, Hacker News-style front page for AI: LLMs, agents, launches, research, tools and
new ideas. Every morning it collects posts from many platforms, merges posts about the same
story, filters out anything that isn't about AI, ranks what's left by how much buzz it got and
how widely it spread, and writes a static page to `site/index.html`.

Python 3.10+ standard library only. There is nothing to install.

## Use it

```powershell
python collector.py            # collect now and render site\index.html
python collector.py --render   # re-render from the database without fetching (after editing config.json)
start site\index.html          # open the page
python -m unittest test_rank   # ranking/merging/filter tests
.\install-task.ps1             # run it every day at 07:30 (Windows Task Scheduler, task "AIDaily")
.\install-task.ps1 -At 06:00   # change the time
.\install-task.ps1 -Uninstall
```

The scheduled task runs `run.cmd`, which appends to `logs\YYYY-MM-DD.log` and keeps two weeks of logs.
If the PC is off at 07:30, the task runs as soon as the PC is back on.

## The page

- **top**: the day's front page. Stories from the last 12 hours count as "today" with no decay; after
  that a story's rank halves every 24 hours.
- **new**: newest first.
- **past**: one day at a time, ranked by buzz, with links to the previous and next day (14 days kept).
- **launches · llms · agents · research · tools · open source · industry · policy**: topic tabs.
- Each story's subline shows where it appeared (HN points and comments, its position in Reddit's
  top-of-day, Lobsters, HF upvotes, GitHub stars, which labs or outlets published it). Each of those
  links to that platform's thread. **discuss** expands the top comments from the biggest HN and Reddit threads.
- The footer has search and a per-source health table.

## Where stories come from

All of it lives in `config.json` under `sources`:

| kind | what |
|---|---|
| `hn` | every HN story over `min_points` from the last few days (Algolia) |
| `reddit` | combined top-of-day RSS for the listed subs; the feed has no vote counts, so position stands in |
| `lobsters` | tag JSON feeds with scores and comment counts |
| `hf_papers`, `hf_models` | Hugging Face Daily Papers (upvotes) and newly created trending models |
| `github` | repos created in the last week under AI topics, by stars |
| `feed` | RSS/Atom: lab blogs (OpenAI, Google Research, HF, NVIDIA, AWS, Microsoft Research, Qwen), press, writers, Techmeme |
| `sitemap` | lab blogs without a feed (Anthropic, Mistral): new URLs, dated from the page |
| `gnews` | Google News queries, mostly for labs whose own blogs aren't reachable from here (DeepMind, Meta, xAI) |

Not included: X/Twitter and LinkedIn (no free API). Bluesky search now requires auth.
`deepmind.google`, `blog.google` and VentureBeat time out from this network, which is why Google/DeepMind come
in through Google News.

## How it filters and ranks (`rank.py`)

1. **Relevance.** AI-only sources (lab blogs, r/LocalLLaMA, HF Papers, GitHub AI topics) pass as-is.
   General sources (HN, Techmeme, r/singularity, Microsoft Research, ...) must match the `relevance`
   regex. The `noise` regex drops stock/crypto/deals chatter. Official lab posts always pass.
2. **Merging.** Posts about the same thing become one story: same canonical URL (arXiv abs/pdf and HF
   paper pages collapse, GitHub subpaths collapse to the repo, tracking params are ignored) or
   near-identical titles within 4 days.
3. **Buzz.** Each post's engagement is normalized per platform (log scale against that platform's 90th
   percentile), so 50 Lobsters points and 500 HN points are comparable. Reddit uses feed position.
   Posts without engagement get a weight by type (`type_weight`). Official posts get a floor of 1.0.
4. **Spread.** A story found on several platforms gets `1 + 0.5 × (extra platforms)` (up to 3 extra).
5. **Topics.** Up to two tags per story from the `topics` regexes (title matches count double).

Everything above is tunable in `config.json`. Re-run with `--render` to see the effect without refetching.

Set `GITHUB_TOKEN` in the environment if you hit GitHub's anonymous rate limit.

## Notes

- Data accumulates in `data/ai-daily.db` (SQLite): one row per post per platform, plus top comments.
- The first run backfills 14 days of HN; other sources only have what their feeds currently hold,
  so the **past** view fills in over the following days.
- This started as Jev Daily (a TypeSafe/Jev-only tracker); the old database is left in `data/digest.db`.
