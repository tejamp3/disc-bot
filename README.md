# AI News → Discord and Telegram

Posts a daily digest of new AI headlines to a Discord channel, a Telegram chat, or both. It runs for free on GitHub Actions, so no server or PC needs to stay on.

Every morning it collects stories from the last 24 hours and removes duplicates. Then, if you give it an OpenRouter API key, a small team of AI agents edits the digest: it searches for news the feeds missed, picks the stories that matter, groups them by topic and writes a one-line summary of each. Without a key it posts the plain headlines grouped by source.

**Sources:** OpenAI, Anthropic (via a community-maintained mirror, since Anthropic has no official feed), Google DeepMind, Google AI, Hugging Face, NVIDIA (AI posts only), TechCrunch AI, The Verge AI, MIT Technology Review, Ars Technica AI, Hacker News (front-page AI stories) and Simon Willison. You can add or remove sources by editing `FEEDS` in [ai_news.py](ai_news.py).

## Setup

1. **Push this folder to a GitHub repo.** A private repo is fine.
2. **Set up at least one destination** (see below) and add its values as repository secrets under **Settings → Secrets and variables → Actions → New repository secret**.
3. **Test it.** Open the **Actions** tab, choose **Daily AI news digest** and click **Run workflow**. The digest should appear within a minute.

After that, it posts automatically every day at 03:30 UTC. To change the time, edit the `cron` line in [.github/workflows/daily-digest.yml](.github/workflows/daily-digest.yml).

### Discord

In your Discord server, open the channel's settings and go to **Integrations → Webhooks → New Webhook**, then click **Copy Webhook URL**. Add it as the secret `DISCORD_WEBHOOK_URL`.

### Telegram

1. In Telegram, message **@BotFather**, send `/newbot` and follow the prompts. Copy the token it gives you and add it as the secret `TELEGRAM_BOT_TOKEN`.
2. Open a chat with your new bot and send it any message. To post to a group or channel instead, add the bot there (as an admin, for a channel) and send a message in it.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser, with your token in place of `<TOKEN>`. Find `"chat":{"id":...}` in the response and add that number as the secret `TELEGRAM_CHAT_ID`. Group and channel IDs are negative; keep the minus sign.

### AI editor (optional)

1. Create a free account at [openrouter.ai](https://openrouter.ai) and make a key at [openrouter.ai/keys](https://openrouter.ai/keys). Add it as the secret `OPENROUTER_API_KEY`.
2. Free models only work if your OpenRouter [privacy settings](https://openrouter.ai/settings/privacy) allow them. If runs fail with "No endpoints found matching your data policy", enable the free-model options there.

The default model is `nvidia/nemotron-3-super-120b-a12b:free`, which costs nothing. To use another one, add a repository **variable** (not a secret) named `OPENROUTER_MODEL`; the model must support tool calling.

OpenRouter allows 50 free-model requests a day on accounts without credits (1,000 a day once you have bought $10 of credits). One run makes about 8 to 12 requests and never more than 20.

## How the AI editor works

The agents live in [agents.py](agents.py). An orchestrator agent acts as editor-in-chief and directs three sub-agents through tool calls:

| Agent | Job |
|---|---|
| Orchestrator | Reads the feed stories, decides what to research, reviews the draft and asks for revisions |
| Researcher | Searches Google News for stories the feeds missed |
| Curator | Picks up to 12 stories, keeps one per event and groups them into topic sections |
| Writer | Writes a one-sentence summary of each story and an overview of the day |

Agents refer to stories by number, so every link comes from a feed or a search result and is never written by the model. The summaries are written from each story's title and feed snippet only, so treat them as a guide to what to click and not as a substitute for the article.

If the model is unavailable or returns something unusable, the run falls back to the plain headline digest, so you still get a post that day. The agents' steps are printed in the workflow log.

## Run locally

```bash
python -m venv .venv
.venv/Scripts/activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python ai_news.py --dry-run     # preview the digest without posting

# post for real (PowerShell: $env:DISCORD_WEBHOOK_URL="...")
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
python ai_news.py
```

## Settings

| Variable | Default | Purpose |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | (unset) | Discord channel webhook |
| `TELEGRAM_BOT_TOKEN` | (unset) | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | (unset) | Telegram chat, group or channel to post to |
| `OPENROUTER_API_KEY` | (unset) | Enables the AI editor |
| `OPENROUTER_MODEL` | `nvidia/nemotron-3-super-120b-a12b:free` | Model for the AI editor |
| `LOOKBACK_HOURS` | `24` | Only include stories newer than this |
| `MAX_PER_SOURCE` | `5` | Maximum stories collected per source |

At least one destination must be set. A destination whose variables are unset is skipped.

## Notes

- Keep the webhook URL, bot token and API key secret. Anyone who has them can post as your bot or use your OpenRouter account.
- GitHub pauses scheduled workflows in public repos after 60 days without commits. If that happens, re-enable the workflow from the Actions tab.
- Scheduled runs can start a few minutes late when GitHub is busy.
