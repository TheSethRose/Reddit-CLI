#!/usr/bin/env python3
"""Read-only Reddit .json fetcher with SQLite response cache and seen tracking."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_CACHE_DB = Path.home() / ".cache" / "reddit-cli" / "reddit.sqlite"
DEFAULT_USER_AGENT = "reddit-cli/0.1"
VALID_LISTINGS = {"hot", "new", "top", "rising", "controversial"}
VALID_T = {"hour", "day", "week", "month", "year", "all"}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def normalize_subreddit(value: str) -> str:
    cleaned = value.strip().strip("/")
    if cleaned.lower().startswith("r/"):
        cleaned = cleaned[2:]
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_]{1,20}", cleaned):
        raise SystemExit(json_error(f"Invalid subreddit: {value}", exit_code=2))
    return cleaned


def post_id_from_value(value: str) -> str:
    raw = value.strip()
    match = re.search(r"/comments/([A-Za-z0-9_]+)/?", raw)
    if match:
        return match.group(1)
    if raw.startswith("t3_"):
        raw = raw[3:]
    if not re.fullmatch(r"[A-Za-z0-9_]+", raw):
        raise SystemExit(json_error(f"Invalid post id or permalink: {value}", exit_code=2))
    return raw


def json_error(message: str, *, exit_code: int = 1, **extra: Any) -> int:
    payload = {"ok": False, "error": message, **extra}
    print(json.dumps(payload, indent=2, sort_keys=True))
    return exit_code


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS posts (
          id TEXT PRIMARY KEY,
          subreddit TEXT NOT NULL,
          fullname TEXT,
          listing_first_seen_from TEXT,
          title TEXT,
          author TEXT,
          url TEXT,
          permalink TEXT,
          score INTEGER,
          num_comments INTEGER,
          created_utc REAL,
          over_18 INTEGER,
          stickied INTEGER,
          raw_json TEXT NOT NULL,
          first_seen_at TEXT NOT NULL,
          last_seen_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_posts_subreddit_created
          ON posts(subreddit, created_utc DESC);

        CREATE TABLE IF NOT EXISTS seen (
          subreddit TEXT NOT NULL,
          post_id TEXT NOT NULL,
          first_seen_at TEXT NOT NULL,
          last_reported_at TEXT NOT NULL,
          PRIMARY KEY (subreddit, post_id)
        );

        CREATE TABLE IF NOT EXISTS fetches (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          cache_key TEXT NOT NULL,
          url TEXT NOT NULL,
          subreddit TEXT,
          listing TEXT,
          params_json TEXT NOT NULL,
          fetched_at TEXT NOT NULL,
          status_code INTEGER,
          cache_hit INTEGER NOT NULL DEFAULT 0,
          rate_used TEXT,
          rate_remaining TEXT,
          rate_reset TEXT,
          response_json TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_fetches_cache_key_fetched
          ON fetches(cache_key, fetched_at DESC);
        """
    )
    conn.commit()


def build_listing_url(subreddit: str, listing: str, limit: int, t: str | None, after: str | None) -> str:
    if listing == "hot":
        path = f"/r/{subreddit}.json"
    else:
        path = f"/r/{subreddit}/{listing}.json"
    params: dict[str, str] = {"limit": str(limit)}
    if listing in {"top", "controversial"} and t:
        params["t"] = t
    if after:
        params["after"] = after
    return "https://www.reddit.com" + path + "?" + urllib.parse.urlencode(params)


def build_comments_url(subreddit: str, post_id: str, limit: int) -> str:
    params = urllib.parse.urlencode({"limit": str(limit)})
    return f"https://www.reddit.com/r/{subreddit}/comments/{post_id}.json?{params}"


def cache_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def parse_iso(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def latest_cached(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM fetches WHERE cache_key = ? AND response_json IS NOT NULL ORDER BY fetched_at DESC LIMIT 1",
        (key,),
    ).fetchone()


def fetch_json(
    conn: sqlite3.Connection,
    url: str,
    *,
    subreddit: str | None,
    listing: str | None,
    params: dict[str, Any],
    ttl_seconds: int,
    refresh: bool,
    user_agent: str,
) -> tuple[Any, dict[str, Any]]:
    key = cache_key(url)
    now = utc_now()
    cached = latest_cached(conn, key)
    if cached and not refresh:
        age = (dt.datetime.now(dt.timezone.utc) - parse_iso(cached["fetched_at"])).total_seconds()
        if age <= ttl_seconds:
            conn.execute(
                """
                INSERT INTO fetches(cache_key, url, subreddit, listing, params_json, fetched_at, status_code, cache_hit, response_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (key, url, subreddit, listing, json.dumps(params, sort_keys=True), now, cached["status_code"], cached["response_json"]),
            )
            conn.commit()
            return json.loads(cached["response_json"]), {"cache": "hit", "cache_age_seconds": int(age), "url": url}

    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": user_agent,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
            status = response.status
            content_type = response.headers.get("content-type", "")
            rate_used = response.headers.get("x-ratelimit-used")
            rate_remaining = response.headers.get("x-ratelimit-remaining")
            rate_reset = response.headers.get("x-ratelimit-reset")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(
            json.dumps(
                {
                    "http_status": exc.code,
                    "reason": exc.reason,
                    "retry_after": exc.headers.get("retry-after"),
                    "body_preview": body,
                },
                sort_keys=True,
            )
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error: {exc.reason}") from exc

    if "json" not in content_type.lower():
        raise RuntimeError(f"Expected JSON but got content-type {content_type!r}")

    parsed = json.loads(raw)
    conn.execute(
        """
        INSERT INTO fetches(cache_key, url, subreddit, listing, params_json, fetched_at, status_code, cache_hit,
                            rate_used, rate_remaining, rate_reset, response_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
        """,
        (
            key,
            url,
            subreddit,
            listing,
            json.dumps(params, sort_keys=True),
            now,
            status,
            rate_used,
            rate_remaining,
            rate_reset,
            raw,
        ),
    )
    conn.commit()
    return parsed, {
        "cache": "miss",
        "url": url,
        "http_status": status,
        "rate_limit": {
            "used": rate_used,
            "remaining": rate_remaining,
            "reset": rate_reset,
        },
    }


def extract_posts(listing_json: Any) -> list[dict[str, Any]]:
    children = listing_json.get("data", {}).get("children", []) if isinstance(listing_json, dict) else []
    posts: list[dict[str, Any]] = []
    for child in children:
        if child.get("kind") != "t3":
            continue
        data = child.get("data", {})
        post_id = data.get("id")
        if not post_id:
            continue
        permalink = data.get("permalink") or ""
        if permalink.startswith("/"):
            permalink = "https://www.reddit.com" + permalink
        posts.append(
            {
                "id": post_id,
                "fullname": data.get("name") or f"t3_{post_id}",
                "subreddit": data.get("subreddit") or "",
                "title": data.get("title") or "",
                "author": data.get("author") or "",
                "url": data.get("url") or "",
                "permalink": permalink,
                "score": data.get("score") or 0,
                "num_comments": data.get("num_comments") or 0,
                "created_utc": data.get("created_utc") or 0,
                "over_18": bool(data.get("over_18")),
                "stickied": bool(data.get("stickied")),
                "raw": data,
            }
        )
    return posts


def upsert_posts_and_seen(conn: sqlite3.Connection, posts: list[dict[str, Any]], fallback_subreddit: str, listing: str) -> list[dict[str, Any]]:
    now = utc_now()
    output: list[dict[str, Any]] = []
    for post in posts:
        subreddit = post.get("subreddit") or fallback_subreddit
        existing_seen = conn.execute(
            "SELECT 1 FROM seen WHERE subreddit = ? AND post_id = ?",
            (subreddit.lower(), post["id"]),
        ).fetchone()
        is_new = existing_seen is None

        existing_post = conn.execute("SELECT first_seen_at FROM posts WHERE id = ?", (post["id"],)).fetchone()
        first_seen = existing_post["first_seen_at"] if existing_post else now
        conn.execute(
            """
            INSERT INTO posts(id, subreddit, fullname, listing_first_seen_from, title, author, url, permalink, score,
                              num_comments, created_utc, over_18, stickied, raw_json, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              subreddit = excluded.subreddit,
              fullname = excluded.fullname,
              title = excluded.title,
              author = excluded.author,
              url = excluded.url,
              permalink = excluded.permalink,
              score = excluded.score,
              num_comments = excluded.num_comments,
              created_utc = excluded.created_utc,
              over_18 = excluded.over_18,
              stickied = excluded.stickied,
              raw_json = excluded.raw_json,
              last_seen_at = excluded.last_seen_at
            """,
            (
                post["id"],
                subreddit,
                post.get("fullname"),
                listing,
                post.get("title"),
                post.get("author"),
                post.get("url"),
                post.get("permalink"),
                post.get("score"),
                post.get("num_comments"),
                post.get("created_utc"),
                int(bool(post.get("over_18"))),
                int(bool(post.get("stickied"))),
                json.dumps(post.get("raw", {}), sort_keys=True),
                first_seen,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO seen(subreddit, post_id, first_seen_at, last_reported_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(subreddit, post_id) DO UPDATE SET last_reported_at = excluded.last_reported_at
            """,
            (subreddit.lower(), post["id"], now if is_new else first_seen, now),
        )
        item = {k: v for k, v in post.items() if k != "raw"}
        item["subreddit"] = subreddit
        item["is_new"] = is_new
        output.append(item)
    conn.commit()
    return output


def command_subreddit(args: argparse.Namespace) -> int:
    subreddit = normalize_subreddit(args.subreddit)
    listing = args.listing
    if listing not in VALID_LISTINGS:
        return json_error(f"Invalid listing: {listing}", exit_code=2)
    if args.t and args.t not in VALID_T:
        return json_error(f"Invalid t value: {args.t}", exit_code=2)
    conn = connect(args.db)
    url = build_listing_url(subreddit, listing, args.limit, args.t, args.after)
    params = {"limit": args.limit, "t": args.t, "after": args.after}
    try:
        data, meta = fetch_json(
            conn,
            url,
            subreddit=subreddit,
            listing=listing,
            params=params,
            ttl_seconds=args.ttl,
            refresh=args.refresh,
            user_agent=args.user_agent,
        )
    except Exception as exc:
        return json_error(str(exc))

    raw_posts = extract_posts(data)
    posts = upsert_posts_and_seen(conn, raw_posts, subreddit, listing)
    new_posts = [p for p in posts if p["is_new"]]
    returned = posts if args.all else new_posts if args.new_only else posts
    after = data.get("data", {}).get("after") if isinstance(data, dict) else None
    payload = {
        "ok": True,
        "command": "subreddit",
        "subreddit": subreddit,
        "listing": listing,
        "cache": meta.get("cache"),
        "cache_age_seconds": meta.get("cache_age_seconds"),
        "fetched_at": utc_now(),
        "new_count": len(new_posts),
        "returned_count": len(returned),
        "total_count": len(posts),
        "after": after,
        "rate_limit": meta.get("rate_limit"),
        "posts": returned,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def flatten_comments(node: Any, out: list[dict[str, Any]], depth: int = 0, max_items: int = 100) -> None:
    if len(out) >= max_items:
        return
    if not isinstance(node, dict):
        return
    kind = node.get("kind")
    data = node.get("data", {})
    if kind == "t1":
        out.append(
            {
                "id": data.get("id"),
                "author": data.get("author"),
                "body": data.get("body"),
                "score": data.get("score"),
                "created_utc": data.get("created_utc"),
                "permalink": "https://www.reddit.com" + data.get("permalink", "") if data.get("permalink") else "",
                "depth": depth,
            }
        )
    replies = data.get("replies")
    if isinstance(replies, dict):
        for child in replies.get("data", {}).get("children", []):
            flatten_comments(child, out, depth + 1, max_items)


def command_comments(args: argparse.Namespace) -> int:
    subreddit = normalize_subreddit(args.subreddit)
    post_id = post_id_from_value(args.post)
    conn = connect(args.db)
    url = build_comments_url(subreddit, post_id, args.limit)
    try:
        data, meta = fetch_json(
            conn,
            url,
            subreddit=subreddit,
            listing="comments",
            params={"post_id": post_id, "limit": args.limit},
            ttl_seconds=args.ttl,
            refresh=args.refresh,
            user_agent=args.user_agent,
        )
    except Exception as exc:
        return json_error(str(exc))

    post = None
    comments: list[dict[str, Any]] = []
    if isinstance(data, list) and data:
        posts = extract_posts(data[0])
        post = posts[0] if posts else None
        if len(data) > 1:
            for child in data[1].get("data", {}).get("children", []):
                flatten_comments(child, comments, max_items=args.limit)
    payload = {
        "ok": True,
        "command": "comments",
        "subreddit": subreddit,
        "post_id": post_id,
        "cache": meta.get("cache"),
        "cache_age_seconds": meta.get("cache_age_seconds"),
        "fetched_at": utc_now(),
        "post": {k: v for k, v in (post or {}).items() if k != "raw"} if post else None,
        "comment_count_returned": len(comments),
        "comments": comments,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def command_seen(args: argparse.Namespace) -> int:
    subreddit = normalize_subreddit(args.subreddit)
    conn = connect(args.db)
    rows = conn.execute(
        """
        SELECT s.post_id, s.first_seen_at, s.last_reported_at, p.title, p.score, p.num_comments, p.permalink
        FROM seen s LEFT JOIN posts p ON p.id = s.post_id
        WHERE s.subreddit = ?
        ORDER BY s.last_reported_at DESC
        LIMIT ?
        """,
        (subreddit.lower(), args.limit),
    ).fetchall()
    count = conn.execute("SELECT COUNT(*) AS n FROM seen WHERE subreddit = ?", (subreddit.lower(),)).fetchone()["n"]
    print(
        json.dumps(
            {
                "ok": True,
                "command": "seen",
                "subreddit": subreddit,
                "seen_count": count,
                "returned_count": len(rows),
                "posts": [dict(row) for row in rows],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def command_reset_seen(args: argparse.Namespace) -> int:
    subreddit = normalize_subreddit(args.subreddit)
    conn = connect(args.db)
    cur = conn.execute("DELETE FROM seen WHERE subreddit = ?", (subreddit.lower(),))
    conn.commit()
    print(json.dumps({"ok": True, "command": "reset-seen", "subreddit": subreddit, "deleted": cur.rowcount}, indent=2, sort_keys=True))
    return 0


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reddit-cli",
        description="Read Reddit public .json endpoints with SQLite cache and seen tracking.",
        epilog=(
            "Examples:\n"
            "  reddit-cli subreddit selfhosted --new-only\n"
            "  reddit-cli subreddit LocalLLaMA --listing top --t week --all\n"
            "  reddit-cli comments python <post_id_or_permalink>\n"
            "  reddit-cli seen selfhosted"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_CACHE_DB, help=f"SQLite cache DB path (default: {DEFAULT_CACHE_DB})")
    parser.add_argument("--ttl", type=positive_int, default=600, help="Response cache TTL seconds (default: 600)")
    parser.add_argument("--refresh", action="store_true", help="Bypass response cache and fetch Reddit live")
    parser.add_argument("--user-agent", default=os.environ.get("REDDIT_CLI_USER_AGENT", DEFAULT_USER_AGENT), help="HTTP User-Agent")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sub = sub.add_parser("subreddit", help="Fetch a subreddit listing")
    p_sub.add_argument("subreddit")
    p_sub.add_argument("--listing", choices=sorted(VALID_LISTINGS), default="hot")
    p_sub.add_argument("--limit", type=positive_int, default=25)
    p_sub.add_argument("--t", choices=sorted(VALID_T), default="day", help="Time window for top/controversial")
    p_sub.add_argument("--after", default=None, help="Reddit listing cursor")
    mode = p_sub.add_mutually_exclusive_group()
    mode.add_argument("--new-only", action="store_true", help="Return only posts not previously seen")
    mode.add_argument("--all", action="store_true", help="Return all posts from the listing")
    p_sub.set_defaults(func=command_subreddit)

    p_comments = sub.add_parser("comments", help="Fetch comments for a post")
    p_comments.add_argument("subreddit")
    p_comments.add_argument("post", help="Post ID, t3 fullname, URL, or permalink")
    p_comments.add_argument("--limit", type=positive_int, default=50)
    p_comments.set_defaults(func=command_comments)

    p_seen = sub.add_parser("seen", help="Show recently seen posts for a subreddit")
    p_seen.add_argument("subreddit")
    p_seen.add_argument("--limit", type=positive_int, default=25)
    p_seen.set_defaults(func=command_seen)

    p_reset = sub.add_parser("reset-seen", help="Clear seen state for a subreddit")
    p_reset.add_argument("subreddit")
    p_reset.set_defaults(func=command_reset_seen)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
