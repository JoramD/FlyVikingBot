# FlyVikingBot

Discord bot for the FlyViking virtual airline, connecting [flyviking.net](https://flyviking.net) (Invision Community), FSAirlines and Discord.

## Features

**Slash commands**
- `/airline_statistics`: airline-wide statistics from FSAirlines
- `/paint_lookup <query>`: find an aircraft paint in the website's downloads
- `/clear <amount>`: delete the last 1–50 messages (administrators only)
- `/pilot [member] [pilot_id]`: FSAirlines pilot stats. Shows rank, hours, flights, average rating, distance, location, last active and last flight. With no option it shows your own linked pilot.
- `/sync_rank [member]`: sync FSAirlines rank roles right away (staff with Manage Roles only)

**FSAirlines → Discord**
- **Flight logs:** every flight completed in FSAirlines is posted to #flight-logs within about 2 minutes. It shows route, aircraft, block time, distance, landing rate and rating, and an @mention if the pilot is linked.
- **Account linking:** members fill in *FSAirlines Pilot ID* and *Discord username* on their flyviking.net profile, and the bot links them automatically. Anything it can't verify, such as an unknown user, a name mismatch or an account that's already linked, goes to #system-messages with the member's profile link and Approve/Reject buttons.
- **Rank roles:** linked members get the Discord role named after their FSAirlines rank (or its short name). It syncs hourly and right after linking. Only the roles listed in `FSA_RANK_ROLES` are ever assigned or removed. A pilot on any other rank, such as Board of Directors, loses those rank roles and gets nothing automatically. #system-messages is told once, with their profile link, so staff can assign the Board role by hand.
- **Website rank groups** (optional, `WEBSITE_RANK_SYNC=true`): the same rank is mirrored to a website **secondary** member group with the same name. Only those allowlisted groups are touched. Pilots on one of those ranks also get **Members** as their primary group. Administrators and Moderators (`WEBSITE_PROTECTED_GROUPS`) are never changed, and an old primary such as Painter/Modder is kept as a secondary group. Members without a pilot rank, such as external painters, are never touched. This prepares for rank-restricted content later.

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
   IPS also sends the event name in a `Webhook-Event` header, so a **single webhook** with all four events ticked, pointing at `<BOT_URL>/ips/<WEBHOOK_SECRET>`, works too. If an event is in the URL path, it takes precedence over the header.
3. Set `ANNOUNCEMENT_BLOG_ID` (currently `1`) so that only entries from the announcements blog are posted.
4. While `WEBHOOK_CAPTURE=true`, every request is saved to `data/webhook_samples/`, with auth headers and member emails/IPs redacted. This is useful for checking what IPS actually sends.

> **Delivery runs through IPS's background tasks.** Webhooks are queued and sent by the IPS **cron** task, not when the content is posted. As a result:
> - The **cron container** (and the PHP container) must be on the `flyviking` network, or they can't resolve `flyviking-discord-bot` and deliveries fail silently on the bot's side.
> - Discord posts arrive up to one cron interval after publishing.
>
> To troubleshoot, each request the bot receives is logged as `Webhook HTTP <method> <path> from <ip> -> <status>`, with the secret masked. To find out which container sent a request, compare its IP with `docker network inspect flyviking`.

Health check: `GET /health`.

## Setting up the FSAirlines integration

1. **Website (ACP):**
   - Create two custom profile fields, both editable by members: **FSAirlines Pilot ID** and **Discord username**. Put their IDs in `IPS_FIELD_FSA_PILOT_ID` and `IPS_FIELD_DISCORD_NAME`.
   - Add a webhook for the `member_edited` event (optionally also `member_registration_complete`): `<BOT_URL>/ips/<WEBHOOK_SECRET>/member_edited`.
2. **Discord:**
   - Enable the **Server Members Intent** for the bot in the Discord Developer Portal (Bot → Privileged Gateway Intents). The bot won't start without it.
   - Create #flight-logs, and pick a staff channel for system messages.
   - Create a role for each FSAirlines rank that should be assigned automatically, named exactly after the rank (or its short name), and list them in `FSA_RANK_ROLES`.
   - Move the bot's role **above** those rank roles, and give it Manage Roles.
3. *(Optional) Website rank groups:* create secondary member groups named like the rank roles. Allow the website API key to use `GET /core/groups` and `GET` + `POST /core/members/{id}`, then set `WEBSITE_RANK_SYNC=true`.
4. Set `CHANNEL_FLIGHT_LOGS` and `CHANNEL_SYSTEM_MESSAGES` in `.env`, then run `docker compose up -d --build`.

The flight log feed starts from the newest existing flight and doesn't post the backlog.

**FSAirlines API key caution:** a *site* API key locks to the first IP that uses it 100 times. Only call the FSAirlines API from the server. To capture real responses, e.g. for test fixtures, run this on the server:
```sh
docker compose exec discord-bot python -m flyvikingbot.tools.fsa_sample [pilot_id]
```
It makes about 5 calls and writes them, with personal data redacted, to `data/fsa_samples/`.

## Project layout
```
flyvikingbot/
  bot.py              bot class: HTTP session, storage, cogs, slash command sync, webhook server
  config.py           settings from env / .env
  storage.py          SQLite (data/flyviking.db): account links and small bot state
  linking.py          pure link-decision and rank-role logic
  cogs/               one module per feature (airline, website, admin, status, website_events, flight_logs, pilots)
  services/           async API clients (FSAirlines, Invision Community)
  webhooks/           webhook HTTP receiver and IPS payload parsing
  tools/              maintenance scripts (fsa_sample)
tests/
```
