# FlyVikingBot

Discord bot for the FlyViking virtual airline, connecting [flyviking.net](https://flyviking.net) (Invision Community), FSAirlines and Discord.

## Features

**Slash commands**
- `/airline_statistics`: airline-wide statistics from FSAirlines
- `/file_lookup <query>`: find a file (paint, configuration, ...) in the website's downloads
- `/clear [amount] [minutes]`: delete the last 1–50 messages and/or those from the last N minutes, up to 24 hours (staff only)
- `/pilot [member] [pilot]`: FSAirlines pilot stats. Shows rank, hours, flights, average rating, distance, passengers, cargo, location, last active and last flight. With no option it shows your own linked pilot. `pilot` accepts a callsign (`FVK003`) or a pilot ID.
- `/sync_rank [member]`: sync FSAirlines rank roles right away (staff only)
- `/weather <airports> [frequencies]`: METAR, TAF, active runway, wind and ATIS for up to 3 airports (e.g. `EKCH ENGM`), from SayIntentions.AI. Results are cached for 5 minutes. Only available when the SayIntentions keys are set.

*Staff* means Manage Roles (administrators have it), or the @Board of Directors role (`SYSTEM_MESSAGES_ROLE_ID`). The same rule applies to the Approve/Reject/Completed buttons. Discord hides staff commands from everyone else, and the bot checks again, so opening them to other roles under Server Settings → Integrations doesn't let non-staff use them. Board members without Manage Roles can press the buttons, but only see the staff commands if you allow the Board role under Server Settings → Integrations.

**FSAirlines → Discord**
- **#system-messages:** every post that needs staff action pings **@Board of Directors** (`SYSTEM_MESSAGES_ROLE_ID`). Informational posts, such as "✅ Linked", don't ping. Action posts without their own Approve/Reject buttons get a **Completed** button. When staff press it, the post records who completed it and when.
- **Flight logs:** every flight completed in FSAirlines is posted to #flight-logs within about 2 minutes. It shows route, aircraft, block time, distance, landing rate and rating, and an @mention if the pilot is linked.
- **Account linking:** members fill in *FSAirlines Pilot ID* (their callsign such as `FVK003`, or the numeric ID) and *Discord username* on their flyviking.net profile, and the bot links them automatically. If the names on the website and in FSAirlines differ, it still links and adds a note to the "✅ Linked" post. An unknown pilot or Discord user goes to #system-messages with the member's profile link. So does a pilot or Discord account that's already linked to someone else, which also gets Approve/Reject buttons. If the website's update was missed, for example because the bot was offline, commands that need the link (`/pilot`, and `/sync_rank member:` for staff) look up the member's website profile and link them right away. This needs the website API key to be allowed to use `GET /core/members`.
- **Rank roles:** linked members get the Discord role named after their FSAirlines rank (or its short name). It syncs hourly and right after linking. Only the roles listed in `FSA_RANK_ROLES` are ever assigned or removed; if it's empty, rank roles are switched off. A pilot on any other rank, such as Board of Directors, loses those rank roles and gets nothing automatically. #system-messages is told once, with their profile link, so staff can assign the Board role by hand.
- **Website rank groups** (on by default; turn off with `WEBSITE_RANK_SYNC=false`): the same rank is mirrored to a website **secondary** member group with the same name. Only those allowlisted groups are touched. Pilots on one of those ranks also get **Members** as their primary group. Administrators and Moderators (`WEBSITE_PROTECTED_GROUPS`) are never changed, and an old primary such as Painter/Modder is kept as a secondary group. Members without a pilot rank, such as external painters, are never touched. This prepares for rank-restricted content later.
- **Rank on the website profile** (optional, `IPS_FIELD_FSA_RANK`): IPS profiles only show the primary group, so the bot writes each linked pilot's full FSA rank, Board ranks included, into a read-only profile field. It's updated with the rank sync and cleared when the member is unlinked.

**Website → Discord** (via Invision Community webhooks)
| IPS event | Discord |
|---|---|
| `galleryImage_create` | The image uploaded in #screenshots in its original quality (IPS's resized "large" version if the original is over 9 MB), with "Uploaded by @member" for linked members (their website profile link otherwise) |
| `blogEntry_create` | `@everyone` announcement embed in #announcements (excerpt, image, blog category); only for the blog in `ANNOUNCEMENT_BLOG_ID` |
| `downloadsFile_create` | "New file available" embed in #announcements (author, description, screenshot, category, version); "· by @member" is added when the author is linked |
| `downloads_new_version` | "New version available" embed in #announcements, the same as a new file plus the changelog |
| `member_create`, `member_registration_complete` | New registrations awaiting approval in #system-messages (see below; also found by a 15-minute poll) |

**Registration approvals** (optional, `REGISTRATION_APPROVALS=true`): new flyviking.net registrations that wait for approval are posted to #system-messages with a @Board of Directors ping.
- **Approve** validates the account on the website.
- **Reject** asks for confirmation, then deletes the account.
- The post shows the username, profile link and registration time. If the member already filled in their FSAirlines Pilot ID or Discord username, those are shown too; a Discord username that matches a server member is tagged.
- If an admin approves or deletes the account in the ACP instead, the post is updated to match.
- If the website refuses an approval or deletion, the post keeps its buttons and staff get a link to the ACP's validation queue.
- New registrations are found through the member webhooks and a poll every 15 minutes, which also catches anything missed while the bot was offline.

**Monitoring:** Uptime Kuma push heartbeat every 60 s.

## Configuration

Copy `.env.example` to `.env` and fill in at least `DISCORD_TOKEN`, `FSA_API_KEY`, `FSA_VA_ID`, `WEBSITE_API_KEY` and `WEBHOOK_SECRET`. Channel and role IDs have no built-in defaults; a feature whose channel is empty is switched off. Every option is documented in `.env.example`.

Generate the webhook secret with:
```sh
python -c "import secrets;print(secrets.token_urlsafe(32))"
```


## Running

### Docker (production)
```sh
docker compose up -d --build
```
- Logs, the bot's database and captured webhook payloads end up in `./data/`. The container runs as UID 1000, so that folder must be owned by UID 1000 (`mkdir data && sudo chown 1000:1000 data`).
- This compose file **creates** the Docker network named by `DOCKER_NETWORK` (default `flyviking-bot`). The website's **cron** container (IPS sends webhooks from its cron task) joins it as an external network, so IPS can reach the bot at `http://flyviking-bot:8080`; nothing is exposed publicly. Start the bot's stack before the website's. In the website's `docker-compose.yml`:
  ```yaml
  services:
    cron:                # your cron service
      networks: [default, flyviking-bot]
  networks:
    flyviking-bot:
      external: true
  ```

### Locally
```sh
python -m venv .venv && .venv/Scripts/activate   # or source .venv/bin/activate
pip install -e ".[dev]"
python -m flyvikingbot
```
Set `GUILD_ID` to make slash commands sync instantly to that server. Run the tests with `pytest` and the linter with `ruff check .`.

## External setup checklist

Everything the bot needs outside this repository, in one place. Details follow in the sections below.

**Discord**
- [ ] Developer Portal → Bot → **Privileged Gateway Intents**: enable **Server Members Intent**. The bot won't start without it.
- [ ] Invite the bot with the `bot` and `applications.commands` scopes.
- [ ] Bot permissions in the relevant channels: **View Channel**, **Send Messages**, **Embed Links** (announcements, flight logs, `/pilot`), **Attach Files** (gallery uploads), **Read Message History** + **Manage Messages** (`/clear`), and **Mention @everyone, @here and All Roles** (the `@everyone` announcement and the @Board of Directors ping). Alternatively, make the Board role mentionable.
- [ ] Server-wide **Manage Roles**, with the bot's role placed **above** the six rank roles.
- [ ] Roles named exactly like the FSA ranks in `FSA_RANK_ROLES`.
- [ ] Channels: #announcements, #screenshots, the flight log channel and #system-messages. Put their IDs (and the server and Board role IDs) in `.env`.

**Website (Invision Community ACP)**
- [ ] REST API key (`WEBSITE_API_KEY`) allowed to use:
  - `GET /core/search` and `GET /downloads/files/{id}` (`/file_lookup`)
  - `GET /downloads/categories` (full category path such as "Paints › FlyViking" in file posts and `/file_lookup`; without it only the subcategory is shown)
  - `GET /core/members` (on-demand linking)
  - `GET /core/groups`, `GET /core/members/{id}` and `POST /core/members/{id}` (website rank groups)
  - `DELETE /core/members/{id}` (registration approvals: Reject)
- [ ] Webhooks (one per event) to `http://flyviking-bot:8080/ips/<WEBHOOK_SECRET>/<event>`: `galleryImage_create`, `blogEntry_create`, `downloadsFile_create`, `downloads_new_version` and `member_edited`. For registration approvals, also add `member_create` and `member_registration_complete`.
- [ ] Two member-editable profile fields: **FSAirlines Pilot ID** (text; regex `/^\s*(?:FVK\d{3,}|\d+)\s*$/i`) and **Discord Username**. Their IDs go in `IPS_FIELD_FSA_PILOT_ID` / `IPS_FIELD_DISCORD_NAME`.
- [ ] Optional: a **FlyViking rank** profile field (text, *not* editable by members, shown on profiles and optionally in posts). Its ID goes in `IPS_FIELD_FSA_RANK`.
- [ ] Secondary member groups named like the rank roles, and a **Members** group.
- [ ] The **cron** container, which sends the webhooks, on the `flyviking-bot` Docker network, permanently in the website's compose file. It also needs a working cron/task setup.

**FSAirlines**
- [ ] API key (VA Overview → Edit VA Settings → Api Key Management) in `FSA_API_KEY`. A *site* key locks to the first IP that uses it 100 times, so only use it from the server.

**SayIntentions.AI** (optional; for `/weather`)
- [ ] Sign in to the SayIntentions pilot portal with FlyViking's account (it needs an active subscription).
- [ ] **Developer Key** page: request a *personal* developer key. It identifies the bot and goes in `SAYINTENTIONS_APP_KEY`.
- [ ] **Account** page: copy the "API Key (ACARS ID)". It identifies the account the bot uses and goes in `SAYINTENTIONS_ACCOUNT_KEY`.
- [ ] Optional: FlyViking's VA-Link key from SayIntentions support in `SAYINTENTIONS_VA_KEY`. Not used yet; reserved for crew personas.
- [ ] Check the response format once: `docker compose exec bot python -m flyvikingbot.tools.si_sample EKCH` saves a real reply to `data/sayintentions_samples/getWX.json`.

**Uptime Kuma** (optional)
- [ ] A **Push** monitor (60 s heartbeat). Its push URL goes in `UPTIME_KUMA_URL`; the monitor must not be paused.

**Server**
- [ ] A `data` folder owned by UID 1000 (`mkdir data && sudo chown 1000:1000 data`), a filled-in `.env` (see `.env.example`), then `docker compose up -d --build`.

## Setting up the Invision Community webhooks

1. **ACP → REST & OAuth → Webhooks → Create new**, using the API key the webhook should run as.
2. Create **one webhook per event**, with the event name at the end of the URL:
   ```
   <BOT_URL>/ips/<WEBHOOK_SECRET>/galleryImage_create
   <BOT_URL>/ips/<WEBHOOK_SECRET>/blogEntry_create
   <BOT_URL>/ips/<WEBHOOK_SECRET>/downloadsFile_create
   <BOT_URL>/ips/<WEBHOOK_SECRET>/downloads_new_version
   ```
   `<BOT_URL>` is `http://flyviking-bot:8080` on the shared Docker network.
   IPS also sends the event name in a `Webhook-Event` header, so a **single webhook** with all four events ticked, pointing at `<BOT_URL>/ips/<WEBHOOK_SECRET>`, works too. If an event is in the URL path, it takes precedence over the header.
3. Set `ANNOUNCEMENT_BLOG_ID` (currently `1`). Only entries from that blog are posted; while it's empty, no blog entries are posted at all, so members' own blogs can never ping @everyone.
4. With `WEBHOOK_CAPTURE=true` (off by default), every request is saved to `data/webhook_samples/`, with auth headers and member emails/IPs redacted. This is useful for checking what IPS actually sends.

> **Delivery runs through IPS's background tasks.** Webhooks are queued and sent by the IPS **cron** task, not when the content is posted. As a result:
> - The **cron container** must be on the `flyviking` network, or it can't resolve `flyviking-bot` and deliveries fail with nothing logged on the bot's side.
> - Discord posts arrive up to one cron interval after publishing.
>
> To troubleshoot, each request the bot receives is logged as `Webhook HTTP <method> <path> from <ip> -> <status>`, with the secret masked. To find out which container sent a request, compare its IP with `docker network inspect flyviking-bot`.

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
3. **Website rank groups:** create secondary member groups named like the rank roles, and allow the website API key to use `GET /core/groups` and `GET` + `POST /core/members/{id}`. Missing groups or permissions are reported to #system-messages. Set `WEBSITE_RANK_SYNC=false` to turn this off.
4. **Rank on the profile (optional):** create a text profile field such as **FlyViking rank**. Members must not be able to edit it. Turn on showing it on the profile and, if you like, next to posts. Put its ID in `IPS_FIELD_FSA_RANK`. It uses the same `GET` + `POST /core/members/{id}` permission. The field fills on the next hourly sync, or right away with `/sync_rank`.
5. Set `CHANNEL_FLIGHT_LOGS` and `CHANNEL_SYSTEM_MESSAGES` in `.env`, then run `docker compose up -d --build`.

The flight log feed starts from the newest existing flight and doesn't post the backlog.

**FSAirlines API key caution:** a *site* API key locks to the first IP that uses it 100 times. Only call the FSAirlines API from the server. To capture real responses, e.g. for test fixtures, run this on the server:
```sh
docker compose exec bot python -m flyvikingbot.tools.fsa_sample [pilot_id]
```
It makes about 5 calls and writes them, with personal data redacted, to `data/fsa_samples/`.

## Previewing posts
To see how the automatic posts look without posting on the website or flying a flight, set `CHANNEL_BOT_DEVELOPMENT` and run on the server:
```sh
docker compose exec bot python -m flyvikingbot.tools.preview_posts             # every post type
docker compose exec bot python -m flyvikingbot.tools.preview_posts gallery     # only some (--list shows the names)
```
It posts sample data to the bot development channel using the bot's real message builders, with all pings switched off. It can run while the bot is online.

**Rule for new post types:** every automatic post gets a `*_message` builder in its cog, and that builder gets an entry with sample data in `PREVIEWS` in `flyvikingbot/tools/preview_posts.py`. A test fails when one is missing.

## Project layout
```
flyvikingbot/
  bot.py              bot class: HTTP session, storage, cogs, slash command sync, webhook server
  config.py           settings from env / .env
  storage.py          SQLite (data/flyviking.db): account links and small bot state
  linking.py          pure link-decision and rank-role logic
  system_messages.py  #system-messages helpers: staff check and the persistent Completed button
  cogs/               one module per feature (airline, website, admin, status, website_events, flight_logs, pilots, weather, registrations)
  services/           async API clients (FSAirlines, Invision Community, SayIntentions)
  webhooks/           webhook HTTP receiver and IPS payload parsing
  tools/              maintenance scripts (fsa_sample, si_sample, preview_posts)
tests/
```
