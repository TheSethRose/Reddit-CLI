---
name: reddit-cli
description: Use Reddit's public .json endpoints with local SQLite caching and seen-post tracking when asked to inspect subreddits, fetch comments, summarize Reddit posts, or report what is new since last check without repeatedly hitting or re-showing the same data.
version: 1.0.0
author: TheSethRose
license: MIT
metadata:
  hermes:
    tags: [reddit, research, social, json, cache, monitoring]
    related_skills: []
---

# Reddit CLI

## Overview

Use this skill when the user asks about Reddit, a subreddit, Reddit comments, Reddit trends, or "what's new" on one or more subreddits.

This skill uses Reddit's public legacy `.json` endpoints and a local SQLite cache so Hermes does not repeatedly fetch or re-report the same posts. It is read-only. No voting, commenting, saving, moderation, login, OAuth, or PRAW by default.

The bundled script stores:

- raw fetch responses by exact URL and TTL
- post records by Reddit post ID
- seen/reporting state so repeated checks can return only new posts
- rate-limit headers when Reddit sends them


## Installation

This repo is laid out as a Hermes-compatible skill:

```text
SKILL.md
scripts/reddit_cli.py
```

To install as a user-local Hermes skill, copy or clone this repo into a skill directory named `reddit-cli`, then load the `reddit-cli` skill in Hermes.

To use only the CLI, run:

```bash
python3 scripts/reddit_cli.py --help
```

Optional wrapper from the repo root:

```bash
mkdir -p "$HOME/.local/bin"
REDDIT_CLI_DIR="$(pwd)"
cat > "$HOME/.local/bin/reddit-cli" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec python3 "$REDDIT_CLI_DIR/scripts/reddit_cli.py" "\$@"
EOF
chmod +x "$HOME/.local/bin/reddit-cli"
```

## Command

Run the global wrapper command:

```bash
reddit-cli <command> [flags]
```

Preferred command is the `reddit-cli` wrapper. If the wrapper is missing, run the bundled script directly from the skill/repo directory:

```bash
python3 scripts/reddit_cli.py <command> [flags]
```

For recurring use, create a local wrapper named `reddit-cli` that delegates to `scripts/reddit_cli.py`.

## Common Commands

```bash
# Check new hot posts from a subreddit
reddit-cli subreddit selfhosted --listing hot --limit 25 --new-only

# Check all current hot posts, marking them as seen
reddit-cli subreddit LocalLLaMA --listing hot --limit 25 --all

# Check newest posts
reddit-cli subreddit python --listing new --limit 10 --new-only

# Force a fresh fetch. Global flags like --refresh must come before the subcommand.
reddit-cli --refresh subreddit MacOS --listing new --limit 50 --all

# Top posts for a time window
reddit-cli subreddit selfhosted --listing top --t week --limit 25 --all

# Fetch comments for a post
reddit-cli comments python <post_id_or_permalink> --limit 50

# Show seen count for a subreddit
reddit-cli seen selfhosted

# Reset seen state for a subreddit
reddit-cli reset-seen selfhosted
```

## Defaults

- Cache DB: `~/.cache/reddit-cli/reddit.sqlite`
- Cache TTL: 10 minutes
- Listing: `hot`
- Limit: 25
- Output: JSON
- User-Agent: `reddit-cli/0.1`

## When the User Asks

### "What's new on r/X?"

Use:

```bash
reddit-cli subreddit X --listing hot --limit 25 --new-only
```

Then summarize only returned posts. If `new_count` is `0`, say no new posts.

### "What's hot/top/current on r/X?"

Use `--all`, because the user is asking for the current listing, not only unseen posts.

```bash
reddit-cli subreddit X --listing hot --limit 25 --all
```

### "Check comments on this post"

Use `comments` with the post ID, full permalink, or `/comments/<id>/...` path.

### "Track this subreddit"

Use `subreddit <name> --new-only` now. If the user wants recurrence, create a cron job later that loads this skill and runs the same script.

## Output Contract

Subreddit output shape:

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
  "posts": [
    {
      "id": "abc123",
      "fullname": "t3_abc123",
      "title": "Post title",
      "author": "someone",
      "url": "https://...",
      "permalink": "https://www.reddit.com/r/...",
      "score": 42,
      "num_comments": 8,
      "created_utc": 1234567890,
      "over_18": false,
      "stickied": false,
      "is_new": true
    }
  ]
}
```

The script prints machine-readable JSON to stdout. Diagnostics and errors go to stderr.

## Caching and Seen Semantics

- Response cache avoids repeated Reddit hits for the same URL within TTL.
- Seen tracking avoids repeatedly showing the user the same post.
- A post is "new" if its `subreddit + id` pair was not in `seen` before this command processed it.
- Running with `--all` still marks returned posts as seen because the user has now seen them.
- Use `--refresh` to bypass response cache.
- `--refresh` is a global flag, so it must come before the subcommand: `reddit-cli --refresh subreddit MacOS --listing new --limit 50 --all`. Do not put it after subreddit-specific flags.
- Use `reset-seen <subreddit>` when the user wants to start fresh for a subreddit.

## Reddit Endpoint Notes

Supported public endpoints:

```text
https://www.reddit.com/r/<subreddit>.json
https://www.reddit.com/r/<subreddit>/hot.json
https://www.reddit.com/r/<subreddit>/new.json
https://www.reddit.com/r/<subreddit>/top.json?t=day|week|month|year|all
https://www.reddit.com/r/<subreddit>/rising.json
https://www.reddit.com/r/<subreddit>/comments/<post_id>.json
```

Use a real User-Agent. Reddit may rate-limit default Python clients.

## Common Pitfalls

1. Do not scrape Reddit HTML first. Try `.json` first.
2. Do not use titles or URLs for dedupe. Use Reddit post IDs.
3. Do not dump raw JSON to the user. Summarize the `posts` or `comments` arrays.
4. Do not refetch with `--refresh` unless freshness matters.
5. Do not use PRAW/OAuth unless the user needs authenticated Reddit features.
6. Private, banned, quarantined, age-gated, or restricted subreddits may fail or return non-useful data. Report that plainly.
7. If Reddit returns HTTP 429, stop and report the `retry_after` / reset info. Do not hammer it.


## Verification Checklist

- [ ] `reddit-cli --help` works
- [ ] `reddit-cli --refresh subreddit python --limit 1 --all` returns JSON with `ok: true`
- [ ] Running the same `--new-only` command twice returns fewer or zero new posts the second time
- [ ] Cache DB exists at `~/.cache/reddit-cli/reddit.sqlite`
