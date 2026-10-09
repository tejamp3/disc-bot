# AI News → Discord

Posts a daily digest of new AI headlines to a Discord channel. It runs for free on GitHub Actions, so no server or PC needs to stay on.

Every morning it collects stories from the last 24 hours, removes duplicates, groups them by source and posts them as Discord embeds with links.

**Sources:** OpenAI, Anthropic (via a community-maintained mirror, since Anthropic has no official feed), Google DeepMind, Google AI, Hugging Face, NVIDIA (AI posts only), TechCrunch AI, The Verge AI, MIT Technology Review, Ars Technica AI, Hacker News (front-page AI stories) and Simon Willison. You can add or remove sources by editing `FEEDS` in [ai_news.py](ai_news.py).

## Setup

1. **Create a Discord webhook.** In your Discord server, open the channel's settings and go to **Integrations → Webhooks → New Webhook**, then click **Copy Webhook URL**.
2. **Push this folder to a GitHub repo.** A private repo is fine.
3. **Add the webhook as a secret.** In the repo, go to **Settings → Secrets and variables → Actions → New repository secret**. Name it `DISCORD_WEBHOOK_URL` and paste the URL as the value.
4. **Test it.** Open the **Actions** tab, choose **Daily AI news digest** and click **Run workflow**. The digest should appear in your channel within a minute.

After that, it posts automatically every day at 03:30 UTC. To change the time, edit the `cron` line in [.github/workflows/daily-digest.yml](.github/workflows/daily-digest.yml).

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
| `DISCORD_WEBHOOK_URL` | (required) | Discord channel webhook |
| `LOOKBACK_HOURS` | `24` | Only include stories newer than this |
| `MAX_PER_SOURCE` | `5` | Maximum stories per source |

## Notes

- Keep the webhook URL secret. Anyone who has it can post in your channel.
- GitHub pauses scheduled workflows in public repos after 60 days without commits. If that happens, re-enable the workflow from the Actions tab.
- Scheduled runs can start a few minutes late when GitHub is busy.
