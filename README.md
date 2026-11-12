# FlyVikingBot

Discord bot for the FlyViking virtual airline, connecting [flyviking.net](https://flyviking.net) (Invision Community), FSAirlines and Discord.

## Features

**Slash commands**
- `/airline_statistics`: airline-wide statistics from FSAirlines
- `/paint_lookup <query>`: find an aircraft paint in the website's downloads
- `/clear <amount>`: delete the last 1–50 messages (administrators only)

**Website → Discord** (via Invision Community webhooks)
| IPS event | Discord |
|---|---|
| `galleryImage_create` | Image posted in #screenshots |
| `blogEntry_create` | `@everyone` announcement embed in #announcements |
| `downloadsFile_create` | "New file available!" in #announcements |
| `downloads_new_version` | "New file version available!" in #announcements |

**Monitoring:** Uptime Kuma push heartbeat every 60 s.

## Configuration

Copy `.env.example` to `.env` and fill in at least `DISCORD_TOKEN`, `FSA_API_KEY`, `WEBSITE_API_KEY` and `WEBHOOK_SECRET`. Every option is documented in `.env.example`.

Generate the webhook secret with:
```sh
python -c "import secrets;print(secrets.token_urlsafe(32))"
```

> A legacy `credentials.json` in the working directory is still read as a fallback for the token and API keys.

## Running

### Docker (production)
```sh
docker compose up -d --build
```
- Logs, and captured webhook payloads, end up in `./data/`.
- The container joins the external Docker network named by `DOCKER_NETWORK` (default `traefik`).
- If the website is on the same network, IPS can reach the bot directly at `http://flyviking-discord-bot:8080`. Otherwise set `WEBHOOK_PUBLIC=true` and `WEBHOOK_HOST` to expose `/ips/*` through Traefik.

### Locally
```sh
python -m venv .venv && .venv/Scripts/activate   # or source .venv/bin/activate
pip install -e ".[dev]"
python -m flyvikingbot
```
Set `GUILD_ID` to make slash commands sync instantly to that server. Run the tests with `pytest` and the linter with `ruff check .`.

## Setting up the Invision Community webhooks

1. **ACP → REST & OAuth → Webhooks → Create new**, using the API key the webhook should run as.
2. Create **one webhook per event**, with the event name at the end of the URL:
   ```
   <BOT_URL>/ips/<WEBHOOK_SECRET>/galleryImage_create
   <BOT_URL>/ips/<WEBHOOK_SECRET>/blogEntry_create
   <BOT_URL>/ips/<WEBHOOK_SECRET>/downloadsFile_create
   <BOT_URL>/ips/<WEBHOOK_SECRET>/downloads_new_version
   ```
   `<BOT_URL>` is `http://flyviking-discord-bot:8080` on the shared Docker network, or `https://<WEBHOOK_HOST>` through Traefik.
   Putting the event in the URL makes routing independent of how IPS labels its requests. A single webhook pointing at `/ips/<WEBHOOK_SECRET>` also works, but only if IPS sends the event name in a header or the body.
3. Optionally set `ANNOUNCEMENT_BLOG_ID` so that only entries from the announcements blog are posted.
4. While `WEBHOOK_CAPTURE=true`, every request is saved to `data/webhook_samples/` with the auth headers redacted. This is useful for checking what IPS actually sends.

Health check: `GET /health`.

## Project layout
```
flyvikingbot/
  bot.py              bot class: HTTP session, cogs, slash command sync, webhook server
  config.py           settings from env / .env
  cogs/               one module per feature (airline, website, admin, status, website_events)
  services/           async API clients (FSAirlines, Invision Community)
  webhooks/           webhook HTTP receiver and IPS payload parsing
tests/
```
