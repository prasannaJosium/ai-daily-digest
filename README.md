# Jev Daily

A daily, filtered Hacker News for one topic: TypeSafe AI's Jev and System One models.
Every morning it collects the latest coverage and what people are saying about it, then
writes a static page to `site/index.html`.

Python 3.10+ standard library only. There is nothing to install.

## Use it

```powershell
python collector.py            # collect now and render site\index.html
python collector.py --render   # re-render from the database without fetching
start site\index.html          # open the dashboard
.\install-task.ps1             # run it every day at 07:30 (Windows Task Scheduler)
.\install-task.ps1 -At 06:00   # change the time
.\install-task.ps1 -Uninstall
```

The scheduled task runs `run.cmd`, which appends to `logs\YYYY-MM-DD.log` and keeps two weeks of logs.
If the PC is off at 07:30, the task runs as soon as the PC is back on.

## What it tracks

Everything lives in `config.json`:

- **`sources`**: the key sources, each watched in a specific way.
  - `page`: a known article. Pulls its title, summary and date, notices when the text changes, and attaches its HN discussion (points and comments).
  - `sitemap`: the TypeSafe blog and docs. A new URL means a new post or doc page, and a changed `lastmod` means an updated page.
  - `links`: a blog index (backnotprop). New posts are fetched and kept only if they mention Jev or TypeSafe.
  - `github`: repos such as mini-jev and Von. Tracks stars, last push and open issues.
  - `search`: a source with no known URL. Found by name through Google News and HN.
  - `reference: true` marks a page that is only watched for changes (docs index, evals). It shows on the TypeSafe tab and stays off the front page.
- **`discovery`**: the wider net.
  - HN stories and comments (Algolia), plus the top-ranked comments from the biggest threads (the official HN API)
  - Reddit search RSS, plus a few top comments per thread
  - Google News RSS
  - GitHub repository search
  - DEV tags
  - Lobsters tag RSS
- **`relevance`**: a regex that every discovered item has to match.

Set `GITHUB_TOKEN` in the environment if you hit GitHub's anonymous rate limit (60 requests per hour).

## Notes

- Data accumulates in `data/digest.db` (SQLite), so an item keeps its first-seen date across runs.
  The page shows the last 30 days (`window_days`), plus every official and repo item.
- "New" means published in the last day. For undated items, it means first spotted in the last day.
  The very first run doesn't count, because it sees the whole backlog at once.
- Reaction tone (positive, skeptical, mixed, neutral) comes from a keyword heuristic.
  Treat it as a rough sort key, not as analysis.
- Reddit rate-limits anonymous RSS hard, so it runs one combined query with pauses between requests.
- Lobsters search sits behind a bot challenge, so the collector reads its tag RSS feeds instead.
