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
- The container joins the external Docker network named by `DOCKER_NETWORK` (default `flyviking`, create it once with `docker network create flyviking`). Connect the website container to the same network so IPS can reach the bot at `http://flyviking-discord-bot:8080`; nothing needs to be public.
- Only if the website can't share that network: set `WEBHOOK_PUBLIC=true` and `WEBHOOK_HOST` to expose `/ips/*` through Traefik (Traefik must be attached to the same network).

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
   IPS sends the bare content object with no event name, so the event **must** be in the URL path.
3. Set `ANNOUNCEMENT_BLOG_ID` (currently `1`) so that only entries from the announcements blog are posted.
4. While `WEBHOOK_CAPTURE=true`, every request is saved to `data/webhook_samples/`, with auth headers and member emails/IPs redacted. This is useful for checking what IPS actually sends.

> **Delivery runs through IPS's background tasks.** Webhooks are queued and sent by the IPS **cron** task, not when the content is posted. As a result:
> - The **cron container** (and the PHP container) must be on the `flyviking` network, or they can't resolve `flyviking-discord-bot` and deliveries fail silently on the bot's side.
> - Discord posts arrive up to one cron interval after publishing.
>
> To troubleshoot, each request the bot receives is logged as `Webhook HTTP <method> <path> from <ip> -> <status>`, with the secret masked. To find out which container sent a request, compare its IP with `docker network inspect flyviking`.

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
