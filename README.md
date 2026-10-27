# TCG Restock Bot

A Discord bot that watches for **Pokémon** and **Yu-Gi-Oh!** card product in stock near you, tells you about **upcoming drops** and their street dates, and spots the **SKU / TCIN numbers** the moment a retailer creates a product page.

Everyone in your server sets their own circle: `/track zip:80202 radius:50`.

---

## What it does

| | How | Status |
|---|---|---|
| **Target** — in-store + order-pickup stock at every Target in your circle | Target's own public store/stock API | 🟢 works (unofficial, could break if Target changes it) |
| **Best Buy** — store pickup stock in your circle + online restocks + street dates | Official Best Buy developer API (free key) | 🟢 works |
| **Local card shops** — new sealed product / back in stock | Shopify's public product feed (most shops use Shopify) | 🟢 works for Shopify shops |
| **Walmart + GameStop** — online stock, found automatically from their search pages | Reads their search results for Pokémon / Yu-Gi-Oh! (marketplace scalpers filtered out) | 🟡 online only, best-effort — both block bots on some runs; shows 🔴 when that happens, resumes by itself |
| **Costco, Pokémon Center, anything else** | Watches product pages you give it (`/watchurl`) | 🔴 online stock only, best-effort |
| **Drop news** — set reveals, release dates, pre-orders | RSS from PokéBeach + YGOrganization (add more with `/addfeed`) | 🟢 works |
| **New listings** — "Target just created a page for X, TCIN 9xxxxxxx" | Compares each scan's product list with the last one | 🟢 works |
| **Drop calendar** — `/drops`, morning digest, "DROPS TODAY" ping | Street dates pulled from Best Buy/Target product data, or added by hand | 🟢 works |

No bot anywhere can see Walmart's or GameStop's shelf counts — if someone says theirs can, it's guessing. For those, watch the product page and go check the aisle on drop day.

---

## Commands (live-bot mode; in GitHub mode the same things are set in config.json)

**Everyone**
- `/track zip:80202 radius:50` — start alerts for a 50-mile circle (max 100). Options: `games`, `dm:true`, `online:false`
- `/stock` — what's in stock in your circle right now
- `/drops` — upcoming release dates
- `/skus` — the product pages (TCIN/SKU numbers) being checked
- `/status` — is every retailer check working? (✅ / ❌)
- `/list` — shops and pages being watched
- `/untrack` — stop
- `/tcg` — give yourself the TCG role (or remove it) to see the TCG News channels

**Admins** (Manage Server permission)
- `/addshop name:"Bobs Cards" url:bobscards.com/collections/pokemon zip:80202` — watch a local shop; the ZIP makes it count as local
- `/watchurl url:<product page> name:"Mega Tin (Walmart)"` — watch one product page for online stock
- `/addsku retailer:Target number:94300123` — pin a TCIN/SKU so it's always checked, even before the search finds it
- `/adddrop name:"Phantasmal Flames ETB" date:2026-11-14 games:Pokémon` — put a release on the calendar
- `/addfeed url:<rss>` — add a news feed
- `/setup` — create the TCG role + private TCG News category with #pokemon and #yugioh (see above)
- `/scan` — run a scan now (`post_everything:true` posts all current stock)
- `/removeshop`, `/unwatchurl`, `/removesku`, `/removedrop`, `/removefeed`

---

## The "TCG News" category and the TCG role

The bot builds its own home in your server: a **TCG** role, a private **TCG News** category, and two channels inside it — **#pokemon** and **#yugioh**. Only the TCG role (and admins) can see the category. Everything Pokémon — stock alerts, new listings, drop dates, news, the morning digest — posts in #pokemon; everything Yu-Gi-Oh! posts in #yugioh.

- **Live bot:** type `/setup` once (the bot needs Manage Roles, Manage Channels, Manage Webhooks). Options: `chat:true` lets TCG members talk in the two channels, `general:true` adds a #tcg-chat they can talk in, `category:"Card News"` renames the category. Members then grab or drop the role with `/tcg`. The bot starts posting there right away — no redeploy.
- **GitHub mode (or before deploying):** `python setup_discord.py` does the same from your PC and prints `POKEMON_WEBHOOK` and `YUGIOH_WEBHOOK` to paste into the repo secrets (plus the channel IDs for the live bot). It needs a bot token + server ID in `.env` and the bot invited with those three permissions. Flags: `--chat`, `--general`, `--category "Card News"`.
- **By hand (3 minutes):** Server Settings → Roles → Create Role "TCG" (mentionable). Create a category "TCG News" → Edit Category → Permissions → **Private Category**, add the TCG role → under TCG turn **Send Messages** off (read-only). Create text channels `pokemon` and `yugioh` inside it (they inherit the permissions). In each channel: Integrations → Webhooks → New Webhook → copy the URL → repo secrets `POKEMON_WEBHOOK` / `YUGIOH_WEBHOOK`.

`/setup` and the script are safe to run again — they reuse what exists and re-apply the permissions. With only a single `DISCORD_WEBHOOK` / `ALERT_CHANNEL_ID` set, the bot posts everything in one channel instead.

---

## Two ways to run it

| | **GitHub mode** (recommended) | **Live bot** (Render / Oracle / your PC) |
|---|---|---|
| Where it runs | GitHub Actions — free, nothing to keep awake, nothing to babysit | A server that stays on 24/7 |
| How people join | You edit `config.json` on GitHub (name, zip, radius) | Everyone types `/track zip radius` themselves |
| Alerts | Posted through a Discord webhook, @-mentions still work | Posted by the bot, plus optional DMs |
| Timing | Every 10 minutes (GitHub sometimes runs a few minutes late when it's busy) | Every 10 minutes, on the dot |
| Commands | None — `/stock`, `/drops`, `/skus` aren't available; the digest covers drops | All slash commands |
| Cost | $0 on a **public** repo (unlimited). A private repo gets 2,000 free minutes/month ≈ one scan every 30 min | $0 on Render free (sleeps unless pinged) / Oracle Always Free |

Nothing in the code is secret — tokens and keys live in GitHub's encrypted Secrets, never in the files — so a public repo is fine. If you'd rather keep it private, set the cron in `.github/workflows/scan.yml` to `3-59/30 * * * *`.

---

## GitHub mode setup (15 minutes)

1. **Discord channels + webhooks.** Run `python setup_discord.py` (see the section above) or make them by hand; either way you end up with a webhook URL for #pokemon and one for #yugioh.
2. **Best Buy key.** <https://developer.bestbuy.com> → sign up → the key is emailed to you. Without it, Best Buy is skipped.
3. **Put the code on GitHub.** Create a new repository (public), then **Add file → Upload files** → drag everything from this folder in (including the `.github` folder — on Windows, show hidden files) → **Commit**.
4. **Add the secrets.** Repo → **Settings → Secrets and variables → Actions → New repository secret**:
   - `POKEMON_WEBHOOK` and `YUGIOH_WEBHOOK` = the two webhook URLs from `setup_discord.py` (or one `DISCORD_WEBHOOK` for a single channel)
   - `BESTBUY_API_KEY` = your key (recommended)
5. **Say where you are.** Open `config.json` on GitHub → pencil icon → replace `CHANGE-ME` with your zip, set `discord_user_id` if you want @-mentions (Discord → Settings → Advanced → Developer Mode, then right-click yourself → Copy User ID) → Commit. Add friends as extra entries in `trackers`, local shops under `shops`, Walmart/GameStop pages under `pages`, known TCINs/SKUs under `pinned_skus`, and release dates under `drops`. `config.example.json` shows every field.
6. **Turn it on.** Repo → **Actions** tab → enable workflows if asked → **TCG stock scan → Run workflow**. The first run only takes a snapshot (no spam). From then on it runs every 10 minutes by itself and posts only what newly came in stock. The green ✓ / red ✗ on each run tells you it's healthy; open a run → "Scan stores" to see each retailer's OK/FAIL line.

That's it. The bot remembers what it already alerted by committing a tiny `state/` folder back to the repo after each run (that also keeps GitHub's scheduler from going idle).

**Tip:** if Target shows `FAIL` with "HTTP 403/blocked" in the Actions log, GitHub's runner IPs are being refused by Target that day — Best Buy and shops still work, and the live-bot mode on Oracle avoids it.

---

## Live-bot setup (the full slash-command version)

### 1. Make the Discord bot
1. Go to <https://discord.com/developers/applications> → **New Application** → name it.
2. Left menu **Bot** → **Reset Token** → copy the token (you only see it once). Keep **Message Content Intent off** — not needed.
3. Left menu **OAuth2** → **URL Generator**: tick scopes `bot` and `applications.commands`; bot permissions: **Send Messages, Embed Links, Attach Files, Read Message History**. Open the generated URL and invite the bot to your server.
4. In Discord: **Settings → Advanced → Developer Mode on**. Right-click your alerts channel → **Copy Channel ID**. Do the same for a private channel the bot can use for backups (e.g. `#bot-config`), and right-click the server icon → **Copy Server ID**.

### 2. Run it somewhere

**Render (free, deploys straight from your GitHub repo)**
1. <https://render.com> → **New → Blueprint** → connect the repo (it reads `render.yaml`).
2. Fill in: `DISCORD_TOKEN`, `ALERT_CHANNEL_ID`, `CONFIG_CHANNEL_ID`, `GUILD_ID`, `BESTBUY_API_KEY`. Deploy.
3. Render's free tier sleeps after 15 idle minutes, so keep it awake: <https://uptimerobot.com> (free) → **New monitor → HTTP** → your Render URL → every 5 minutes. The bot serves a tiny web page for exactly this.
4. Free Render wipes its disk on restart; `CONFIG_CHANNEL_ID` is how it survives — settings are backed up as a file in that channel and restored on boot.

**Your own Windows PC** — install Python 3.11+ (tick "Add to PATH"), copy `.env.example` to `.env`, fill it in, double-click `start.bat`. Runs while the PC is on.

**Oracle Cloud "Always Free" VM** — truly 24/7 and the least blocked by Target, but it's Linux setup: create the free ARM instance (Ubuntu), `sudo apt install python3-pip -y`, upload the folder, `pip install -r requirements.txt`, fill in `.env`, run `nohup python3 bot.py &`.

### 3. Test the store checks first (recommended for either mode)
From the folder, with `.env` filled in (`BESTBUY_API_KEY` at least):
```
pip install -r requirements.txt
python selftest.py 80202 50
```
It prints every item in stock in that circle, which retailer checks pass (✅) or fail (❌) and why, upcoming drops, and the latest news.

---

## Settings (environment variables)

GitHub mode reads `config.json` plus these secrets: `POKEMON_WEBHOOK` + `YUGIOH_WEBHOOK` (or a single `DISCORD_WEBHOOK`), `BESTBUY_API_KEY`, `NEWS_WEBHOOK`, `TARGET_KEY`. Live-bot mode uses the environment variables below.

| Variable | Required | What it does |
|---|---|---|
| `DISCORD_TOKEN` | yes | bot token |
| `ALERT_CHANNEL_ID` | yes* | where stock alerts go (*or run `/setup` and the bot picks its own channel) |
| `POKEMON_CHANNEL_ID` / `YUGIOH_CHANNEL_ID` | no | per-game channels (`/setup` sets these for you) |
| `NEWS_CHANNEL_ID` | no | fallback for news when the game channels aren't set |
| `CONFIG_CHANNEL_ID` | on Render | private channel for backups |
| `GUILD_ID` | no | your server ID → slash commands appear instantly instead of ~1 hour |
| `BESTBUY_API_KEY` | recommended | free key, enables Best Buy |
| `SCAN_INTERVAL_MIN` | no | minutes between scans (default 10; don't go under 5 — Target will rate-limit) |
| `REALERT_HOURS` | no | don't re-ping the same item at the same store within N hours (default 6) |
| `MAX_TARGET_STORES` | no | cap on Target stores per scan (default 20) |
| `DIGEST_HOUR` / `TZ_NAME` | no | morning drops digest hour (default 9) and time zone (default America/Denver) |
| `TARGET_KEY` | no | only if Target rotates its web key |
| `EXCLUDE_WORDS` | no | extra title words to ignore, comma-separated (`plush` etc. are built in) |

---

## Helping the bot with Walmart

Walmart blocks most server IPs (that's why GitHub runs often show Walmart 🔴). A home internet connection gets through far more often, so you can run **just the Walmart check** from your PC while GitHub does everything else:

1. On your PC, in the bot folder: install Python 3.11+ (tick "Add to PATH"), make sure `.env` has your webhooks, then double-click `walmart-helper.bat` once to test — it prints the scan summary.
2. Double-click `install-walmart-helper.bat` — Windows will run the Walmart check every 10 minutes while you're logged in (hidden; log in `state-walmart\walmart-helper.log`).
3. On GitHub: Settings → Secrets and variables → Actions → **Variables** → New variable `WALMART_ON_PC` = `true`, so the GitHub runs stop hitting Walmart.

Still online stock only. Other options: apply for Walmart's free affiliate API at walmart.io (clean online data, approval takes a while), a residential proxy service (what BrickSeek uses, paid), or Walmart's own "Get in-stock alert" button on an item page.

## Troubleshooting

- **GitHub run is red ✗** — open the run → "Scan stores" step. A `config.json` error says exactly which line; "every source failed" usually means the secrets weren't saved.
- **GitHub runs stopped after a couple of months** — GitHub pauses schedules on idle repos; the state commits normally prevent that, but if it happens: Actions tab → the workflow → **Enable**.
- **Slash commands don't show up** — set `GUILD_ID`; without it Discord can take an hour. Also make sure you invited the bot with the `applications.commands` scope.
- **`/status` shows Target ❌ "HTTP 404" or "410"** — Target rotated its web key. Open target.com in Chrome → F12 → **Network** tab → load any product page → click a request whose name starts with `product_summary` or `pdp_client` → copy the `key=` value from its URL → set it as `TARGET_KEY`. Takes two minutes.
- **Target ❌ "every availability call failed"** — your host's IP is being blocked. Raise `SCAN_INTERVAL_MIN` to 15 and lower `MAX_TARGET_STORES`. Oracle VMs get blocked less than Render.
- **Best Buy ❌ "HTTP 403"** — the key isn't active yet or was pasted with a space. New keys can take a few minutes.
- **Watched pages ❌ "blocked"** — Walmart/GameStop/Pokémon Center block automated visits. Expected; keep those as drop-day reminders via `/drops` rather than relying on page alerts.
- **A shop ❌ "HTTP 404 (not Shopify)"** — that shop isn't on Shopify. Use `/watchurl` on a specific product page instead.
- **Too many / too few products** — tune `EXCLUDE_WORDS`, or pin exact items with `/addsku`.
- **Bot forgot everyone's settings after a redeploy** — `CONFIG_CHANNEL_ID` isn't set, or the backup message was deleted.

---

## How it works (short version)
Every scan: look up Target stores within each tracker's radius → search Target/Best Buy for Pokémon & Yu-Gi-Oh! product (titles filtered so plush, binders and sleeves don't count) → check every store for every product → compare with the last scan → post only what newly flipped to in stock, @-mentioning each person whose circle contains that store, with the distance. New product pages and street dates feed the drop calendar; RSS feeds feed the news channel; a digest posts each morning when something is coming up.

Files: `setup_discord.py` (one-time channel/role setup) · `scan_once.py` (GitHub mode entry point) · `bot.py` (live bot) · `engine.py` (what's new / who to ping) · `checkers/` (one file per source) · `render.py` (the Discord messages) · `state.py` (settings) · `selftest.py` (command-line test) · `.github/workflows/scan.yml` (the schedule).
