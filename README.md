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

## Hosting on a Linux VM (private, over Tailscale)

The intended setup: a small Linux VM on your Tailscale network runs the collector daily and serves the
page to the tailnet only. No sudo, Docker or open ports needed.

```sh
# on the VM
git clone https://github.com/prasannaJosium/ai-daily-digest.git ~/apps/ai-daily
cd ~/apps/ai-daily
./run.sh             # first collection (backfills 14 days of HN, ~1 minute)
./install-cron.sh    # daily run at 02:00 UTC (07:30 IST) + keep the web server up
./serve.sh           # start the web server now
```

- Page: `http://<vm-host>:8420/` from any device on the tailnet. `serve.sh` runs `server.py` (the page plus the
  saved-stories API) bound to the VM's Tailscale
  address only, so the page is not reachable from the internet. Port: `AIDAILY_PORT=9090 ./serve.sh`.
- Schedule (user crontab): `run.sh` daily at 02:00 UTC (change it with `AIDAILY_CRON="0 1 * * *" ./install-cron.sh`);
  `serve.sh` at boot and every 5 minutes, which starts the web server only if it isn't already running.
  `./install-cron.sh --uninstall` removes both.
- Logs: `logs/YYYY-MM-DD.log` (collector) and `logs/serve.log` (web server).
- Deploy a change: push, then on the VM `git pull --ff-only && python3 collector.py --render`.

The Windows scheduled task (`install-task.ps1`, above) is the alternative for running it on a Windows PC.

## The page

- **top**: the day's front page. Stories from the last 12 hours count as "today" with no decay; after
  that a story's rank halves every 24 hours.
- **new**: newest first.
- **past**: one day at a time, ranked by buzz, with links to the previous and next day (14 days kept).
- **launches · llms · agents · research · tools · open source · industry · policy**: topic tabs.
- Each story's subline shows where it appeared (HN points and comments, its position in Reddit's
  top-of-day, Lobsters, HF upvotes, GitHub stars, which labs or outlets published it). Each of those
  links to that platform's thread. **discuss** expands the top comments from the biggest HN and Reddit threads.
- **save** under any story keeps it in the **saved** tab, even after it drops out of the 14-day window.
- **Search** (the box in the orange bar, or press `/`) filters whatever view you're on and is part of the URL, so every
  search can be bookmarked: `#q/mcp`, `#t/agents/q/mcp`, `#past/2026-09-20/q/qwen`. Words must all match
  (at the start of a word, so `rag` doesn't match "storage"), `"quoted phrases"` match exactly, and
  `qwen|deepseek` matches either. It looks at titles, summaries, sites, sources and tags.
- **pin as topic** turns the current search into your own topic tab in the orange bar (`#u/<name>`),
  ranked like the built-in topics, with **edit** and **unpin**.
- Saved stories and pinned topics are stored on the server in `data/user.json` (`server.py`), so they're the
  same on every device. When the page is opened as a plain file instead, they're kept in that browser only.
- The footer also has a per-source health table.

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
