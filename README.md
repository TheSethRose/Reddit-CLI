# Reddit CLI

Read-only Reddit `.json` fetcher with SQLite caching and seen-post tracking.

This is a small CLI for checking subreddit listings and comments without OAuth, PRAW, scraping Reddit HTML, voting, commenting, or logging in. It uses Reddit's public legacy `.json` endpoints and stores a local SQLite cache so repeated checks do not keep hitting Reddit or re-reporting the same posts.

## Features

- Fetch subreddit listings: `hot`, `new`, `top`, `rising`, `controversial`
- Fetch comments for a post ID or permalink
- Track seen posts per subreddit
- Return only new/unseen posts when you want monitoring-style output
- Cache Reddit responses with a configurable TTL
- Emit machine-readable JSON for agents, scripts, cron jobs, and dashboards

## Install

Clone the repo:

```bash
git clone https://github.com/TheSethRose/Reddit-CLI.git
cd Reddit-CLI
```

Run directly:

```bash
python3 scripts/reddit_cli.py --help
```

Optional local wrapper from the cloned repo directory:

```bash
mkdir -p "$HOME/.local/bin"
REDDIT_CLI_DIR="$(pwd)"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  "exec python3 \"$REDDIT_CLI_DIR/scripts/reddit_cli.py\" \"\$@\"" \
  > "$HOME/.local/bin/reddit-cli"
chmod +x "$HOME/.local/bin/reddit-cli"
```

## Usage

Check new hot posts from a subreddit:

```bash
reddit-cli subreddit selfhosted --listing hot --limit 25 --new-only
```

Check all current hot posts and mark them seen:

```bash
reddit-cli subreddit LocalLLaMA --listing hot --limit 25 --all
```

Check newest posts:

```bash
reddit-cli subreddit python --listing new --limit 10 --new-only
```

Force a fresh fetch:

```bash
reddit-cli --refresh subreddit MacOS --listing new --limit 50 --all
```

Important: `--refresh` is a global flag, so it must come before the subcommand.

Fetch comments for a post:

```bash
reddit-cli comments python <post_id_or_permalink> --limit 50
```

Show seen posts:

```bash
reddit-cli seen selfhosted
```

Reset seen state for a subreddit:

```bash
reddit-cli reset-seen selfhosted
```

## Output

The CLI prints JSON to stdout. Errors also print JSON and exit non-zero.

Example shape:

```json
{
  "ok": true,
  "command": "subreddit",
  "subreddit": "selfhosted",
  "listing": "hot",
  "cache": "miss",
  "new_count": 3,
  "returned_count": 3,
  "total_count": 25,
  "posts": []
}
```

## Cache and privacy

Default cache path:

```text
~/.cache/reddit-cli/reddit.sqlite
```

The cache contains fetched Reddit post/comment JSON and seen-post state. It is local runtime data and should not be committed.

Use a custom database path:

```bash
reddit-cli --db /tmp/reddit.sqlite subreddit MacOS --listing new --limit 10 --all
```

Use a custom User-Agent:

```bash
REDDIT_CLI_USER_AGENT="your-tool/0.1" reddit-cli subreddit python --limit 5 --all
```

## Notes

- Read-only by design
- No OAuth required
- No HTML scraping
- Reddit may rate limit public requests, so do not hammer it
- Private, banned, quarantined, age-gated, or restricted subreddits may fail or return non-useful data
