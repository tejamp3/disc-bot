"""Collect recent AI news from RSS/Atom feeds and post a digest to a Discord webhook.

Usage:
    python ai_news.py             # post the digest to DISCORD_WEBHOOK_URL
    python ai_news.py --dry-run   # print the digest instead of posting

Environment:
    DISCORD_WEBHOOK_URL   Discord channel webhook (required unless --dry-run)
    LOOKBACK_HOURS        Only include items newer than this (default 24)
    MAX_PER_SOURCE        Cap on items per source (default 5)
"""

import argparse
import calendar
import json
import os
import re
import socket
import sys
import time
import urllib.request
from datetime import datetime, timezone

import feedparser

FEEDS = {
    "OpenAI": "https://openai.com/news/rss.xml",
    # Anthropic publishes no official feed; this is a community-maintained mirror of anthropic.com/news.
    "Anthropic": "https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_anthropic_news.xml",
    "Google DeepMind": "https://deepmind.google/blog/rss.xml",
    "Google AI": "https://blog.google/technology/ai/rss/",
    "Hugging Face": "https://huggingface.co/blog/feed.xml",
    "NVIDIA": "https://blogs.nvidia.com/feed/",
    "TechCrunch AI": "https://techcrunch.com/category/artificial-intelligence/feed/",
    "The Verge AI": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    "MIT Technology Review": "https://www.technologyreview.com/topic/artificial-intelligence/feed",
    "Ars Technica AI": "https://arstechnica.com/ai/feed/",
    "Hacker News": "https://hnrss.org/frontpage?q=AI+OR+LLM+OR+GPT+OR+OpenAI+OR+Anthropic+OR+Gemini",
    "Simon Willison": "https://simonwillison.net/atom/entries/",
}

# Broad feeds that also cover non-AI topics; only items whose title matches AI_KEYWORDS are kept.
FILTERED_SOURCES = {"NVIDIA"}
AI_KEYWORDS = re.compile(
    r"\b(AI|A\.I\.|artificial intelligence|machine learning|LLMs?|GPT|agents?|agentic|"
    r"models?|neural|robot\w*|inference|training|generative|chatbot|OpenAI|Anthropic|Claude|"
    r"Gemini|DeepMind|Llama|Nemotron|NIM|CUDA|GPUs?)\b",
    re.IGNORECASE,
)

USER_AGENT = "Mozilla/5.0 (compatible; ai-news-digest/1.0)"
EMBED_COLOR = 0x5865F2
# Discord limits: 10 embeds and 6000 characters per message, 4096 per embed description.
MAX_EMBEDS_PER_MESSAGE = 10
MAX_CHARS_PER_MESSAGE = 5800
MAX_DESCRIPTION = 4000


def fetch_items(name: str, url: str, since: float, limit: int) -> list[dict]:
    feed = feedparser.parse(url, agent=USER_AGENT)
    if feed.get("status", 200) >= 400 or (feed.bozo and not feed.entries):
        print(f"  ! {name}: failed ({feed.get('status') or feed.get('bozo_exception')})", file=sys.stderr)
        return []
    items = []
    for entry in feed.entries:
        parsed = entry.get("published_parsed") or entry.get("updated_parsed")
        if not parsed or not entry.get("link"):
            continue
        ts = calendar.timegm(parsed)  # feedparser normalizes dates to UTC
        if ts < since:
            continue
        title = re.sub(r"\s+", " ", entry.get("title", "")).strip() or entry.link
        if name in FILTERED_SOURCES and not AI_KEYWORDS.search(title):
            continue
        items.append({"title": title, "link": entry.link, "ts": ts})
    items.sort(key=lambda i: i["ts"], reverse=True)
    return items[:limit]


def collect(lookback_hours: float, max_per_source: int) -> dict[str, list[dict]]:
    socket.setdefaulttimeout(30)
    since = time.time() - lookback_hours * 3600
    seen: set[str] = set()
    digest = {}
    for name, url in FEEDS.items():
        unique = []
        for item in fetch_items(name, url, since, max_per_source):
            key = re.sub(r"\W+", "", item["title"].lower())
            if key in seen or item["link"] in seen:
                continue
            seen.update({key, item["link"]})
            unique.append(item)
        print(f"  {name}: {len(unique)} new", file=sys.stderr)
        if unique:
            digest[name] = unique
    return digest


def escape_md(text: str) -> str:
    return re.sub(r"([\\*_~`|\[\]])", r"\\\1", text)


def build_embeds(digest: dict[str, list[dict]]) -> list[dict]:
    embeds = []
    for source, items in digest.items():
        lines = [f"• [{escape_md(i['title'][:200])}]({i['link']})" for i in items]
        description = ""
        for line in lines:
            if len(description) + len(line) + 1 > MAX_DESCRIPTION:
                break
            description += line + "\n"
        embeds.append({"title": source, "description": description.strip(), "color": EMBED_COLOR})
    return embeds


def build_messages(digest: dict[str, list[dict]]) -> list[dict]:
    total = sum(len(v) for v in digest.values())
    today = datetime.now(timezone.utc).strftime("%A, %d %B %Y")
    header = f"🤖 **AI news digest: {today}**\n{total} new stories from {len(digest)} sources"
    if not digest:
        return [{"content": f"🤖 **AI news digest: {today}**\nNo new stories in the last period."}]

    messages, batch, size = [], [], 0
    for embed in build_embeds(digest):
        embed_size = len(embed["title"]) + len(embed["description"])
        if batch and (len(batch) == MAX_EMBEDS_PER_MESSAGE or size + embed_size > MAX_CHARS_PER_MESSAGE):
            messages.append({"embeds": batch})
            batch, size = [], 0
        batch.append(embed)
        size += embed_size
    messages.append({"embeds": batch})
    messages[0]["content"] = header
    return messages


def post(webhook_url: str, message: dict) -> None:
    body = json.dumps({**message, "allowed_mentions": {"parse": []}}).encode()
    for attempt in range(3):
        req = urllib.request.Request(
            webhook_url, data=body, headers={"Content-Type": "application/json", "User-Agent": USER_AGENT}
        )
        try:
            with urllib.request.urlopen(req, timeout=30):
                return
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:  # rate limited: wait as instructed, then retry
                time.sleep(float(json.loads(e.read() or b"{}").get("retry_after", 2)))
                continue
            raise RuntimeError(f"Discord returned {e.code}: {e.read().decode(errors='replace')[:300]}") from e


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="print the digest instead of posting it")
    args = parser.parse_args()

    lookback = float(os.getenv("LOOKBACK_HOURS") or 24)
    per_source = int(os.getenv("MAX_PER_SOURCE") or 5)
    print(f"Collecting AI news from the last {lookback:g}h...", file=sys.stderr)
    messages = build_messages(collect(lookback, per_source))

    if args.dry_run:
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(messages, indent=2, ensure_ascii=False))
        return

    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        sys.exit("DISCORD_WEBHOOK_URL is not set (use --dry-run to preview without posting)")
    for message in messages:
        post(webhook_url, message)
        time.sleep(1)
    print(f"Posted {len(messages)} message(s) to Discord.", file=sys.stderr)


if __name__ == "__main__":
    main()
