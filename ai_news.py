"""Collect recent AI news from RSS/Atom feeds and post a digest to Discord and/or Telegram.

With OPENROUTER_API_KEY set, a team of AI agents (see agents.py) researches, curates and summarizes the
stories first. Without it, or if the agents fail, the digest is the plain headlines grouped by source.

Usage:
    python ai_news.py             # post the digest to every configured destination
    python ai_news.py --dry-run   # print the digest instead of posting

Environment (at least one destination is required unless --dry-run):
    DISCORD_WEBHOOK_URL   Discord channel webhook
    TELEGRAM_BOT_TOKEN    Telegram bot token from @BotFather
    TELEGRAM_CHAT_ID      Telegram chat, group or channel to post to
    OPENROUTER_API_KEY    Enables the AI editor (optional)
    OPENROUTER_MODEL      Model for the AI editor (default: a free NVIDIA Nemotron model)
    LOOKBACK_HOURS        Only include items newer than this (default 24)
    MAX_PER_SOURCE        Cap on items per source (default 5)
"""

import argparse
import calendar
import html
import json
import os
import re
import socket
import sys
import time
import urllib.request
from datetime import datetime, timezone

import feedparser

import agents

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
# Telegram limit: 4096 characters per message.
MAX_TELEGRAM_CHARS = 4000


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
        snippet = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", entry.get("summary", "")))).strip()
        items.append({"title": title, "link": entry.link, "ts": ts, "snippet": snippet[:300]})
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
        lines = []
        for i in items:
            line = f"• [{escape_md(i['title'][:200])}]({i['link']})"
            # Only AI-edited digests carry a source and summary per story.
            if i.get("source"):
                line += f" · *{escape_md(i['source'])}*"
            if i.get("summary"):
                line += f"\n{escape_md(i['summary'])}"
            lines.append(line)
        description = ""
        for line in lines:
            if len(description) + len(line) + 1 > MAX_DESCRIPTION:
                break
            description += line + "\n"
        embeds.append({"title": source, "description": description.strip(), "color": EMBED_COLOR})
    return embeds


def digest_header(digest: dict[str, list[dict]], overview: str) -> tuple[str, str]:
    today = datetime.now(timezone.utc).strftime("%A, %d %B %Y")
    total = sum(len(v) for v in digest.values())
    summary = f"{total} new stories from {len(digest)} sources" if digest else "No new stories in the last period."
    return f"AI news digest: {today}", overview or summary


def build_messages(digest: dict[str, list[dict]], overview: str = "") -> list[dict]:
    title, summary = digest_header(digest, overview)
    header = f"🤖 **{title}**\n{escape_md(summary)}"
    if not digest:
        return [{"content": header}]

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


def build_telegram_messages(digest: dict[str, list[dict]], overview: str = "") -> list[str]:
    title, summary = digest_header(digest, overview)
    lines = [f"🤖 <b>{title}</b>", html.escape(summary)]
    for section, items in digest.items():
        lines += ["", f"<b>{html.escape(section)}</b>"]
        for i in items:
            line = f'• <a href="{html.escape(i["link"])}">{html.escape(i["title"][:200])}</a>'
            if i.get("source"):
                line += f" · <i>{html.escape(i['source'])}</i>"
            if i.get("summary"):
                line += f"\n{html.escape(i['summary'])}"
            lines.append(line)

    messages, current = [], ""
    for line in lines:
        if current and len(current) + len(line) + 1 > MAX_TELEGRAM_CHARS:
            messages.append(current.strip())
            current = ""
        current += line + "\n"
    messages.append(current.strip())
    return messages


def post_json(service: str, url: str, payload: dict) -> None:
    body = json.dumps(payload).encode()
    for attempt in range(3):
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json", "User-Agent": USER_AGENT}
        )
        try:
            with urllib.request.urlopen(req, timeout=30):
                return
        except urllib.error.HTTPError as e:
            detail = e.read()
            if e.code == 429 and attempt < 2:  # rate limited: wait as instructed, then retry
                info = json.loads(detail or b"{}")
                # Discord puts retry_after at the top level, Telegram under "parameters".
                time.sleep(float(info.get("retry_after") or info.get("parameters", {}).get("retry_after", 2)))
                continue
            raise RuntimeError(f"{service} returned {e.code}: {detail.decode(errors='replace')[:300]}") from e


def post_discord(webhook_url: str, digest: dict[str, list[dict]], overview: str) -> int:
    messages = build_messages(digest, overview)
    for message in messages:
        post_json("Discord", webhook_url, {**message, "allowed_mentions": {"parse": []}})
        time.sleep(1)
    return len(messages)


def post_telegram(token: str, chat_id: str, digest: dict[str, list[dict]], overview: str) -> int:
    messages = build_telegram_messages(digest, overview)
    for text in messages:
        post_json(
            "Telegram",
            f"https://api.telegram.org/bot{token}/sendMessage",
            {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}},
        )
        time.sleep(1)
    return len(messages)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="print the digest instead of posting it")
    args = parser.parse_args()

    lookback = float(os.getenv("LOOKBACK_HOURS") or 24)
    per_source = int(os.getenv("MAX_PER_SOURCE") or 5)
    print(f"Collecting AI news from the last {lookback:g}h...", file=sys.stderr)
    digest = collect(lookback, per_source)
    overview = ""
    if os.getenv("OPENROUTER_API_KEY"):
        try:
            digest, overview = agents.run(digest, lookback)
        except Exception as e:  # plain headlines are better than no digest at all
            print(f"  ! AI editor failed, falling back to plain headlines: {e!r}", file=sys.stderr)

    if args.dry_run:
        sys.stdout.reconfigure(encoding="utf-8")
        preview = {
            "discord": build_messages(digest, overview),
            "telegram": build_telegram_messages(digest, overview),
        }
        print(json.dumps(preview, indent=2, ensure_ascii=False))
        return

    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    telegram_token, telegram_chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    destinations = {}
    if webhook_url:
        destinations["Discord"] = lambda: post_discord(webhook_url, digest, overview)
    if telegram_token and telegram_chat:
        destinations["Telegram"] = lambda: post_telegram(telegram_token, telegram_chat, digest, overview)
    if not destinations:
        sys.exit(
            "No destination configured: set DISCORD_WEBHOOK_URL, or TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID "
            "(use --dry-run to preview without posting)"
        )

    # A failure in one destination should not stop the digest reaching the other.
    failed = False
    for name, send in destinations.items():
        try:
            print(f"Posted {send()} message(s) to {name}.", file=sys.stderr)
        except (RuntimeError, OSError) as e:
            failed = True
            print(f"  ! {name}: {e}", file=sys.stderr)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
