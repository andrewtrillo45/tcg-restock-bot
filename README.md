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
| **Walmart, GameStop, Costco, Pokémon Center, anything else** | Watches product pages you give it (`/watchurl`) | 🔴 online stock only, best-effort — these sites don't share store-level stock and often block bots |
| **Drop news** — set reveals, release dates, pre-orders | RSS from PokéBeach + YGOrganization (add more with `/addfeed`) | 🟢 works |
| **New listings** — "Target just created a page for X, TCIN 9xxxxxxx" | Compares each scan's product list with the last one | 🟢 works |
| **Drop calendar** — `/drops`, morning digest, "DROPS TODAY" ping | Street dates pulled from Best Buy/Target product data, or added by hand | 🟢 works |

No bot anywhere can see Walmart's or GameStop's shelf counts — if someone says theirs can, it's guessing. For those, watch the product page and go check the aisle on drop day.

---

## Commands

**Everyone**
- `/track zip:80202 radius:50` — start alerts for a 50-mile circle (max 100). Options: `games`, `dm:true`, `online:false`
- `/stock` — what's in stock in your circle right now
- `/drops` — upcoming release dates
- `/skus` — the product pages (TCIN/SKU numbers) being checked
- `/status` — is every retailer check working? (✅ / ❌)
- `/list` — shops and pages being watched
- `/untrack` — stop

**Admins** (Manage Server permission)
- `/addshop name:"Bobs Cards" url:bobscards.com/collections/pokemon zip:80202` — watch a local shop; the ZIP makes it count as local
- `/watchurl url:<product page> name:"Mega Tin (Walmart)"` — watch one product page for online stock
- `/addsku retailer:Target number:94300123` — pin a TCIN/SKU so it's always checked, even before the search finds it
- `/adddrop name:"Phantasmal Flames ETB" date:2026-11-14 games:Pokémon` — put a release on the calendar
- `/addfeed url:<rss>` — add a news feed
- `/scan` — run a scan now (`post_everything:true` posts all current stock)
- `/removeshop`, `/unwatchurl`, `/removesku`, `/removedrop`, `/removefeed`

---

## Setup (about 20 minutes)

### 1. Make the Discord bot
1. Go to <https://discord.com/developers/applications> → **New Application** → name it.
2. Left menu **Bot** → **Reset Token** → copy the token (you only see it once). Keep **Message Content Intent off** — not needed.
3. Left menu **OAuth2** → **URL Generator**: tick scopes `bot` and `applications.commands`; bot permissions: **Send Messages, Embed Links, Attach Files, Read Message History**. Open the generated URL and invite the bot to your server.
4. In Discord: **Settings → Advanced → Developer Mode on**. Right-click your alerts channel → **Copy Channel ID**. Do the same for a private channel the bot can use for backups (e.g. `#bot-config`), and right-click the server icon → **Copy Server ID**.

### 2. Get a free Best Buy API key
<https://developer.bestbuy.com> → sign up → the key is emailed to you (usually within minutes). Without it, Best Buy is skipped.

### 3. Run it somewhere

**Option A — Render (free, no server to manage)**
1. Put this folder on GitHub: create a new repo → **Add file → Upload files** → drag everything in → Commit.
2. <https://render.com> → **New → Blueprint** → connect the repo (it reads `render.yaml`).
3. Fill in the environment variables it asks for: `DISCORD_TOKEN`, `ALERT_CHANNEL_ID`, `CONFIG_CHANNEL_ID`, `GUILD_ID`, `BESTBUY_API_KEY`. Deploy.
4. Render's free tier sleeps after 15 idle minutes, so keep it awake: <https://uptimerobot.com> (free) → **New monitor → HTTP** → paste your Render URL (`https://tcg-restock-bot-xxxx.onrender.com`) → every 5 minutes. That's it — the bot has a tiny web page for exactly this.
5. Free Render wipes its disk on every restart. That's why `CONFIG_CHANNEL_ID` matters: the bot backs up everyone's `/track` settings, shops, SKUs and drops as a file in that channel and restores them on boot. Don't delete the newest backup message.

**Option B — Your own PC / a spare laptop (Windows)**
Install Python 3.11+ from python.org (tick "Add to PATH"). Copy `.env.example` to `.env`, fill it in, double-click `start.bat`. It only runs while the PC is on.

**Option C — Oracle Cloud "Always Free" VM (truly 24/7, more setup)**
Create the free ARM instance (Ubuntu), then: `sudo apt install python3-pip -y`, upload the folder, `pip install -r requirements.txt`, fill in `.env`, and run it with `nohup python3 bot.py &` (or a systemd service). Best option if you don't want the UptimeRobot dance.

### 4. Test the store checks first (recommended)
From the folder, with your `.env` filled in:
```
pip install -r requirements.txt
python selftest.py 80202 50
```
It prints every item in stock in that circle, which retailer checks pass (✅) or fail (❌) and why, upcoming drops, and the latest news. If Target shows ❌ "HTTP 404/410", its web key rotated — see Troubleshooting.

### 5. Use it
In Discord: `/track zip:80202 radius:50`. The first scan only takes a snapshot (no spam); after that you get pinged on every change. Add your local shops with `/addshop`.

---

## Settings (environment variables)

| Variable | Required | What it does |
|---|---|---|
| `DISCORD_TOKEN` | yes | bot token |
| `ALERT_CHANNEL_ID` | yes | where stock alerts go |
| `NEWS_CHANNEL_ID` | no | where drop news / new listings / digest go (default: alert channel) |
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

## Troubleshooting

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

Files: `bot.py` (Discord), `engine.py` (what's new / who to ping), `checkers/` (one file per source), `state.py` (settings), `selftest.py` (command-line test).
