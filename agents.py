"""Multi-agent editor for the AI news digest, running on an OpenRouter model.

An orchestrator agent acts as editor-in-chief. It reads the stories collected from the feeds and directs
three sub-agents through tool calls:

    researcher   searches the news web for stories the feeds missed
    curator      picks the stories that matter, drops duplicates and groups them by topic
    writer       writes a one-sentence summary of each pick and a short overview of the day

Agents refer to stories by id, so every link comes from a feed or a search result and is never written
by the model.

Environment:
    OPENROUTER_API_KEY    OpenRouter API key (required)
    OPENROUTER_MODEL      Model to use (default nvidia/nemotron-3-super-120b-a12b:free)
"""

import calendar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import feedparser

API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"
USER_AGENT = "Mozilla/5.0 (compatible; ai-news-digest/1.0)"

# OpenRouter allows 50 free-model requests a day on accounts without credits, so one run stays well below that.
MAX_LLM_CALLS = 20
MAX_ORCHESTRATOR_TURNS = 6
MAX_RESEARCHER_TURNS = 3
MAX_RESEARCH_RUNS = 2
MAX_SEARCH_RESULTS = 8
MAX_STORIES = 12
MAX_SECTIONS = 5
MAX_PER_SECTION = 5

ORCHESTRATOR_PROMPT = f"""You are the editor-in-chief of a daily AI news digest posted to a chat channel. \
Readers want the handful of stories that matter today: model and product releases, research results, \
major funding and deals, policy and safety news.

You do not write the digest yourself. You direct a team through tools:
- ask_researcher searches the news web. Use it for important AI news from the last day that the feed \
stories seem to be missing. Give it one specific task per call, and call it at most {MAX_RESEARCH_RUNS} times.
- ask_curator chooses the stories for the digest from everything collected so far and groups them by \
topic. Call it once research is finished.
- ask_writer writes the summaries and the overview for the curator's selection and returns the draft.

Work in that order. Read the writer's draft; if something is wrong, call the curator or the writer again \
with specific guidance. When the draft is good, reply with the single word DONE. Use as few calls as you can."""

RESEARCHER_PROMPT = """You are the researcher for a daily AI news digest. Carry out the editor's task with \
search_news. Each search returns recent news stories, which are saved automatically. Use short keyword \
queries of two to five words, run at most three searches, and do not search for stories we already have. \
Then reply with one or two sentences on what you found, citing story ids."""

CURATOR_PROMPT = f"""You are the curator for a daily AI news digest. From the numbered stories, choose the \
{MAX_STORIES} or fewer that matter most to people who follow AI closely.

- Pick one story per event. When several stories cover the same news, keep the primary source or the \
most informative one.
- Skip opinion pieces, listicles, shopping deals, and anything that is not about AI.
- Group the picks into 2 to {MAX_SECTIONS} topic sections with short titles, most important first, with \
at most {MAX_PER_SECTION} stories in a section.

Reply with only JSON in this shape: {{"sections": [{{"title": "Model releases", "story_ids": [3, 17]}}]}}"""

WRITER_PROMPT = """You are the writer for a daily AI news digest. For each numbered story, write one plain \
sentence of at most 30 words that says what happened and why it matters. Use only the title and snippet \
you are given: never add facts, numbers or names that are not there. Also write an overview of one or two \
sentences on the day's main themes.

Reply with only JSON in this shape: {"overview": "...", "summaries": {"3": "...", "17": "..."}}"""


def tool(name: str, description: str, param: str, param_description: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {param: {"type": "string", "description": param_description}},
                "required": [param],
            },
        },
    }


ORCHESTRATOR_TOOLS = [
    tool("ask_researcher", "Have the researcher search the news web.", "task", "What to look for."),
    tool("ask_curator", "Have the curator choose and group the stories.", "guidance", "Editorial guidance."),
    tool("ask_writer", "Have the writer summarize the selection.", "guidance", "Guidance or revisions."),
]
RESEARCHER_TOOLS = [tool("search_news", "Search news from the last day.", "query", "Short keyword query.")]


def log(agent: str, text: str) -> None:
    print(f"  [{agent}] {text}", file=sys.stderr)


def parse_json(text: str | None) -> dict:
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        raise ValueError("reply contained no JSON object")
    return json.loads(match.group())


def clean(text: object, limit: int) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


class Newsroom:
    """Shared state for one run: the story pool, the current selection and the current draft."""

    def __init__(self, api_key: str, model: str, feed_digest: dict[str, list[dict]], lookback_hours: float):
        self.api_key = api_key
        self.model = model
        self.lookback_hours = lookback_hours
        self.pool: dict[int, dict] = {}
        self.seen: set[str] = set()
        self.sections: list[tuple[str, list[int]]] = []
        self.summaries: dict[int, str] = {}
        self.overview = ""
        self.calls = 0
        self.research_runs = 0
        for source, items in feed_digest.items():
            for item in items:
                self.add(source, item)

    def add(self, source: str, item: dict) -> int | None:
        key = re.sub(r"\W+", "", item["title"].lower())
        if key in self.seen or item["link"] in self.seen:
            return None
        self.seen.update({key, item["link"]})
        story_id = len(self.pool) + 1
        self.pool[story_id] = {**item, "source": source}
        return story_id

    def listing(self, ids, snippets: bool = False) -> str:
        lines = []
        for story_id in ids:
            story = self.pool[story_id]
            lines.append(f"[{story_id}] {story['source']}: {story['title']}")
            if snippets and story.get("snippet"):
                lines.append(f"    {story['snippet']}")
        return "\n".join(lines) or "(none)"

    def chat(self, messages: list[dict], tools: list[dict] | None = None, json_mode: bool = False) -> dict:
        if self.calls >= MAX_LLM_CALLS:
            raise RuntimeError("LLM call budget for this run is used up")
        self.calls += 1
        payload = {"model": self.model, "messages": messages, "temperature": 0.3}
        if tools:
            payload["tools"] = tools
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        body = json.dumps(payload).encode()
        for attempt in range(4):
            req = urllib.request.Request(
                API_URL,
                data=body,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": USER_AGENT,
                    "X-Title": "AI news digest",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=180) as resp:
                    data = json.load(resp)
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:300]
                if e.code in (429, 502, 503) and attempt < 3:  # free models are often briefly overloaded
                    time.sleep(15 * (attempt + 1))
                    continue
                raise RuntimeError(f"OpenRouter returned {e.code}: {detail}") from e
            if data.get("choices"):
                return data["choices"][0]["message"]
            # OpenRouter reports upstream provider failures inside a 200 response.
            if attempt < 3:
                time.sleep(15 * (attempt + 1))
                continue
            raise RuntimeError(f"OpenRouter error: {json.dumps(data.get('error'))[:300]}")

    def run_agent(self, name: str, system: str, user: str, tools: list[dict], handlers: dict, max_turns: int) -> str:
        """Run a tool-calling loop until the agent replies without calling a tool, and return that reply."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for _ in range(max_turns):
            reply = self.chat(messages, tools)
            calls = reply.get("tool_calls") or []
            turn = {"role": "assistant", "content": reply.get("content") or ""}
            if calls:
                turn["tool_calls"] = calls
            if reply.get("reasoning_details"):  # reasoning models expect these back on the next turn
                turn["reasoning_details"] = reply["reasoning_details"]
            messages.append(turn)
            if not calls:
                return turn["content"]
            for call in calls:
                function = call.get("function", {})
                try:
                    args = json.loads(function.get("arguments") or "{}")
                    log(name, f"{function.get('name')}: {clean(next(iter(args.values()), ''), 150)}")
                    result = handlers[function.get("name")](**args)
                except (KeyError, TypeError, ValueError, AttributeError) as e:
                    result = f"Error: {e!r}"
                messages.append({"role": "tool", "tool_call_id": call.get("id"), "content": result})
        log(name, "ran out of turns")
        return ""

    def search_news(self, query: str) -> str:
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
            {"q": f"{query} when:{max(1, round(self.lookback_hours))}h", "hl": "en-US", "gl": "US", "ceid": "US:en"}
        )
        feed = feedparser.parse(url, agent=USER_AGENT)
        since = time.time() - self.lookback_hours * 3600
        found = []
        for entry in feed.entries:
            parsed = entry.get("published_parsed")
            if not parsed or not entry.get("link") or calendar.timegm(parsed) < since:
                continue
            publisher = entry.get("source", {}).get("title") or "Google News"
            # Google News titles end with " - Publisher".
            title = clean(entry.get("title", "").removesuffix(f" - {publisher}"), 300)
            if not title:
                continue
            story_id = self.add(publisher, {"title": title, "link": entry.link, "ts": calendar.timegm(parsed)})
            if story_id is not None:
                found.append(story_id)
            if len(found) == MAX_SEARCH_RESULTS:
                break
        return self.listing(found) if found else "No new results."

    def research(self, task: str) -> str:
        if self.research_runs >= MAX_RESEARCH_RUNS:
            return "The research budget is used up. Continue with the stories already collected."
        self.research_runs += 1
        before = len(self.pool)
        report = self.run_agent(
            "researcher",
            RESEARCHER_PROMPT,
            f"Task: {task}\n\nStories we already have:\n{self.listing(self.pool)}",
            RESEARCHER_TOOLS,
            {"search_news": self.search_news},
            MAX_RESEARCHER_TURNS,
        )
        new = [story_id for story_id in self.pool if story_id > before]
        return f"{report}\n\nStories added by this research ({len(new)}):\n{self.listing(new)}"

    def curate(self, guidance: str = "") -> str:
        reply = self.chat(
            [
                {"role": "system", "content": CURATOR_PROMPT},
                {"role": "user", "content": f"{guidance}\n\nStories:\n{self.listing(self.pool)}".strip()},
            ],
            json_mode=True,
        )
        sections, used = [], set()
        for section in parse_json(reply.get("content")).get("sections", [])[:MAX_SECTIONS]:
            ids = []
            for raw in section.get("story_ids", []):
                story_id = int(raw)
                if story_id in self.pool and story_id not in used and len(used) < MAX_STORIES:
                    used.add(story_id)
                    ids.append(story_id)
            if ids:
                sections.append((clean(section.get("title"), 80) or "Top stories", ids[:MAX_PER_SECTION]))
        if not sections:
            raise ValueError("the curator selected no known story ids")
        # A new selection makes any earlier draft stale.
        self.sections, self.summaries, self.overview = sections, {}, ""
        log("curator", f"selected {len(used)} of {len(self.pool)} stories in {len(sections)} sections")
        return "\n\n".join(f"{title}\n{self.listing(ids)}" for title, ids in sections)

    def write(self, guidance: str = "") -> str:
        if not self.sections:
            return "Nothing has been selected yet. Ask the curator first."
        selected = [story_id for _, ids in self.sections for story_id in ids]
        reply = self.chat(
            [
                {"role": "system", "content": WRITER_PROMPT},
                {"role": "user", "content": f"{guidance}\n\nStories:\n{self.listing(selected, snippets=True)}".strip()},
            ],
            json_mode=True,
        )
        data = parse_json(reply.get("content"))
        summaries = {int(k): clean(v, 300) for k, v in data.get("summaries", {}).items() if str(k).isdigit()}
        self.summaries = {k: v for k, v in summaries.items() if k in selected and v}
        self.overview = clean(data.get("overview"), 600)
        log("writer", f"wrote {len(self.summaries)} summaries")
        draft = [f"Overview: {self.overview}"]
        for title, ids in self.sections:
            draft.append(f"\n{title}")
            draft += [f"[{i}] {self.pool[i]['title']}\n    {self.summaries.get(i, '(no summary)')}" for i in ids]
        return "\n".join(draft)

    def run(self) -> tuple[dict[str, list[dict]], str]:
        try:
            self.run_agent(
                "orchestrator",
                ORCHESTRATOR_PROMPT,
                f"Stories from today's feeds ({len(self.pool)}):\n{self.listing(self.pool)}\n\nProduce today's digest.",
                ORCHESTRATOR_TOOLS,
                {"ask_researcher": self.research, "ask_curator": self.curate, "ask_writer": self.write},
                MAX_ORCHESTRATOR_TURNS,
            )
        except (RuntimeError, OSError) as e:  # keep whatever the team finished before the failure
            log("orchestrator", f"stopped early: {e}")
        # The orchestrator may stop early or run out of turns, so make sure both editorial steps happened.
        if not self.sections:
            self.curate()
        if not self.summaries:
            self.write()
        digest: dict[str, list[dict]] = {}
        for title, ids in self.sections:
            digest.setdefault(title, []).extend({**self.pool[i], "summary": self.summaries.get(i, "")} for i in ids)
        log("orchestrator", f"done after {self.calls} model calls")
        return digest, self.overview


def run(feed_digest: dict[str, list[dict]], lookback_hours: float) -> tuple[dict[str, list[dict]], str]:
    """Turn the per-source feed digest into an edited digest grouped by topic, plus an overview line."""
    model = os.getenv("OPENROUTER_MODEL") or DEFAULT_MODEL
    log("orchestrator", f"editing with {model}")
    return Newsroom(os.environ["OPENROUTER_API_KEY"], model, feed_digest, lookback_hours).run()
