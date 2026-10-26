"""TCG Restock Bot — Discord alerts for Pokémon & Yu-Gi-Oh! stock near you.

Run:  python bot.py          (needs DISCORD_TOKEN and ALERT_CHANNEL_ID in .env or the environment)
Test: python selftest.py 80202 50   (checks the stores from the command line, no Discord needed)
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands

import filters
from zoneinfo import ZoneInfo

from checkers import (BestBuyChecker, NewsChecker, PageWatchChecker, SearchPageChecker, ShopifyChecker,
                      TargetChecker)
from checkers.news import NewsItem
from engine import Engine
from geo import lookup_zip, valid_zip
from http_client import AiohttpFetcher
from models import Alert, Product
from render import (RETAILER_LABEL, SKU_WORD, alert_message, digest_text, drop_message, games_label,
                    listing_message, news_message, nice_date, status_lines)
from state import ALL_GAMES, State

try:  # optional: lets you keep secrets in a .env file
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

# ---------- configuration (environment variables) ----------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
ALERT_CHANNEL_ID = int(os.getenv("ALERT_CHANNEL_ID", "0") or 0)
CONFIG_CHANNEL_ID = int(os.getenv("CONFIG_CHANNEL_ID", "0") or 0)   # optional: backups
NEWS_CHANNEL_ID = int(os.getenv("NEWS_CHANNEL_ID", "0") or 0)       # optional: drops/news (else alert channel)
POKEMON_CHANNEL_ID = int(os.getenv("POKEMON_CHANNEL_ID", "0") or 0) # optional: everything Pokémon goes here
YUGIOH_CHANNEL_ID = int(os.getenv("YUGIOH_CHANNEL_ID", "0") or 0)   # optional: everything Yu-Gi-Oh! goes here
DIGEST_HOUR = int(os.getenv("DIGEST_HOUR", "9") or 9)               # local hour for the daily drops digest
TZ = os.getenv("TZ_NAME", "America/Denver")
GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)                     # optional: instant commands
BESTBUY_API_KEY = os.getenv("BESTBUY_API_KEY", "")
TARGET_KEY = os.getenv("TARGET_KEY", "")                            # optional override
SCAN_INTERVAL_MIN = float(os.getenv("SCAN_INTERVAL_MIN", "10"))
REALERT_HOURS = float(os.getenv("REALERT_HOURS", "6"))
MAX_TARGET_STORES = int(os.getenv("MAX_TARGET_STORES", "20"))
DATA_DIR = os.getenv("DATA_DIR", "data")
PORT = int(os.getenv("PORT", "8080") or 8080)                        # keep-alive web page

def ago(ts: float | None) -> str:
    if not ts:
        return "never"
    s = int(time.time() - ts)
    return f"{s}s ago" if s < 90 else f"{s // 60} min ago"


class RestockBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.state = State(os.path.join(DATA_DIR, "state.json"))
        self.fetcher = AiohttpFetcher()
        self.engine: Engine | None = None
        self.scan_lock = asyncio.Lock()
        self._ready_once = False
        self._had_local_state = False

    # ---------- lifecycle ----------
    async def setup_hook(self) -> None:
        self._had_local_state = self.state.load_file()
        checkers = [
            TargetChecker(self.fetcher, key=TARGET_KEY or None, max_stores=MAX_TARGET_STORES),
            BestBuyChecker(self.fetcher, BESTBUY_API_KEY),
            ShopifyChecker(self.fetcher),
            PageWatchChecker(self.fetcher),
            SearchPageChecker(self.fetcher, walmart=os.getenv("WALMART", "1") != "0",
                              gamestop=os.getenv("GAMESTOP", "1") != "0"),
        ]
        self.engine = Engine(self.state, checkers, os.path.join(DATA_DIR, "seen.json"),
                             realert_hours=REALERT_HOURS, news=NewsChecker(self.fetcher))
        register_commands(self)
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("slash commands synced to guild %s", GUILD_ID)
        else:
            await self.tree.sync()
            log.info("slash commands synced globally (can take up to an hour to appear)")
        asyncio.create_task(start_keepalive(self))
        asyncio.create_task(self._scan_forever())
        asyncio.create_task(self._digest_forever())

    async def on_ready(self) -> None:
        if self._ready_once:
            return
        self._ready_once = True
        log.info("logged in as %s", self.user)
        if not self._had_local_state and CONFIG_CHANNEL_ID:
            if await self.restore_state():
                log.info("restored tracker list from the config channel backup")

    async def _scan_forever(self) -> None:
        await self.wait_until_ready()
        await asyncio.sleep(5)
        while not self.is_closed():
            try:
                await self.run_scan()
            except Exception:
                log.exception("scan crashed")
            await asyncio.sleep(SCAN_INTERVAL_MIN * 60)

    # ---------- scanning & alerting ----------
    async def run_scan(self, force_alert: bool = False) -> list[Alert]:
        async with self.scan_lock:
            alerts = await self.engine.scan(force_alert=force_alert)
            news = await self.engine.scan_news()
            self.state.prune_drops()
        for a in alerts:
            try:
                await self.post_alert(a)
            except Exception:
                log.exception("could not post alert")
        for prod in self.engine.new_listings:
            try:
                await self.post_listing(prod)
            except Exception:
                log.exception("could not post listing")
        for d in self.engine.new_drops:
            try:
                await self.post_drop(d)
            except Exception:
                log.exception("could not post drop")
        for n in news:
            try:
                await self.post_news(n)
            except Exception:
                log.exception("could not post news")
        return alerts

    async def _post(self, channel_id: int, content: str, embed: dict) -> None:
        ch = await self.channel(channel_id)
        if ch:
            await ch.send(content=content, embed=discord.Embed.from_dict(embed))

    async def post_listing(self, prod: Product) -> None:
        """A retailer created a product page — usually days/weeks before launch."""
        content, embed = listing_message(prod)
        await self._post(self.news_channel_id(prod.game), content, embed)

    async def post_drop(self, d: dict) -> None:
        content, embed = drop_message(d)
        await self._post(self.news_channel_id(d.get("game", "")), content, embed)

    async def post_news(self, n: NewsItem) -> None:
        content, embed = news_message(n, interactive=True)
        await self._post(self.news_channel_id(n.game), content, embed)

    # ---------- daily drops digest ----------
    async def _digest_forever(self) -> None:
        await self.wait_until_ready()
        tz = ZoneInfo(TZ)
        while not self.is_closed():
            now = datetime.now(tz)
            nxt = now.replace(hour=DIGEST_HOUR, minute=0, second=0, microsecond=0)
            if nxt <= now:
                nxt += timedelta(days=1)
            await asyncio.sleep(max(60, (nxt - now).total_seconds()))
            try:
                await self.post_digest()
            except Exception:
                log.exception("digest failed")

    def digest_text(self, days: int = 14, game: str | None = None) -> str:
        return digest_text(self.state, days, interactive=True, game=game)

    async def post_digest(self) -> None:
        """One digest per game channel when they're split, otherwise one combined digest."""
        if self.game_channel_id("pokemon") or self.game_channel_id("yugioh"):
            for game in ("pokemon", "yugioh"):
                if any(d.get("game") == game for d in self.state.upcoming_drops(14)):
                    ch = await self.channel(self.news_channel_id(game))
                    if ch:
                        await ch.send(self.digest_text(14, game))
            return
        ch = await self.channel(self.news_channel_id())
        if ch and self.state.upcoming_drops(14):
            await ch.send(self.digest_text(14))

    async def post_alert(self, a: Alert) -> None:
        content, embed_dict = alert_message(a)
        embed = discord.Embed.from_dict(embed_dict)
        channel = await self.channel(self.alert_channel_id(a.product.game))
        if channel is not None:
            await channel.send(content=content, embed=embed,
                               allowed_mentions=discord.AllowedMentions(users=True))
        # optional DMs
        plain = content.split("\n")[0]
        for uid, _ in a.recipients:
            t = self.state.trackers.get(str(uid))
            if not t or not t.get("dm"):
                continue
            try:
                user = self.get_user(int(uid)) or await self.fetch_user(int(uid))
                await user.send(content=plain, embed=embed)
            except Exception as e:
                log.info("DM to %s failed: %s", uid, e)

    def game_channel_id(self, game: str) -> int:
        env = {"pokemon": POKEMON_CHANNEL_ID, "yugioh": YUGIOH_CHANNEL_ID}.get(game, 0)
        return int(self.state.settings.get(f"{game}_channel_id") or env or 0)

    def news_channel_id(self, game: str = "") -> int:
        """Where news / listings / drops for a game go: its own channel, else the news/alert channel."""
        return (self.game_channel_id(game) or int(self.state.settings.get("news_channel_id") or NEWS_CHANNEL_ID)
                or self.alert_channel_id(game))

    def alert_channel_id(self, game: str = "") -> int:
        """Where stock alerts for a game go: its own channel, else the alert channel, else any game channel."""
        return (self.game_channel_id(game) or int(self.state.settings.get("alert_channel_id") or ALERT_CHANNEL_ID)
                or self.game_channel_id("pokemon") or self.game_channel_id("yugioh"))

    async def channel(self, cid: int):
        if not cid:
            return None
        ch = self.get_channel(cid)
        if ch is None:
            try:
                ch = await self.fetch_channel(cid)
            except Exception as e:
                log.warning("channel %s unavailable: %s", cid, e)
                return None
        return ch

    # ---------- state backup in Discord (survives disk wipes on free hosts) ----------
    async def backup_state(self) -> None:
        ch = await self.channel(CONFIG_CHANNEL_ID)
        if ch is None:
            return
        try:
            data = self.state.to_json().encode("utf-8")
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            await ch.send(content=f"🗂️ tracker backup · {stamp} — don't delete the newest one",
                          file=discord.File(io.BytesIO(data), filename="state.json"))
            # tidy older backups (newest first; keep the one we just posted)
            old = [m async for m in ch.history(limit=50)
                   if m.author.id == self.user.id and m.content.startswith("🗂️")]
            for m in old[1:]:
                try:
                    await m.delete()
                except Exception:
                    pass
        except Exception as e:
            log.warning("backup failed: %s", e)

    async def restore_state(self) -> bool:
        ch = await self.channel(CONFIG_CHANNEL_ID)
        if ch is None:
            return False
        try:
            async for msg in ch.history(limit=50):
                if msg.author.id != self.user.id:
                    continue
                for att in msg.attachments:
                    if att.filename == "state.json":
                        self.state.load_json((await att.read()).decode("utf-8"))
                        self.state.save()
                        return True
        except Exception as e:
            log.warning("restore failed: %s", e)
        return False


# ---------- keep-alive web page (Render/UptimeRobot) ----------
async def start_keepalive(bot: RestockBot) -> None:
    try:
        from aiohttp import web
    except Exception:
        return

    async def index(_):
        e = bot.engine
        body = (f"tcg-restock-bot ok\nlast scan: {ago(e.last_scan_at) if e else 'n/a'}\n"
                f"scans: {e.scans if e else 0}\ntrackers: {len(bot.state.trackers)}\n")
        return web.Response(text=body)

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/health", index)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("keep-alive page listening on port %s", PORT)


# ---------- slash commands ----------
def register_commands(bot: RestockBot) -> None:
    tree = bot.tree
    GAME_CHOICES = [
        app_commands.Choice(name="Pokémon + Yu-Gi-Oh!", value="both"),
        app_commands.Choice(name="Pokémon only", value="pokemon"),
        app_commands.Choice(name="Yu-Gi-Oh! only", value="yugioh"),
    ]

    @tree.command(name="track", description="Alert me when Pokémon / Yu-Gi-Oh! product is in stock near a ZIP")
    @app_commands.describe(zip_code="5-digit ZIP code to center on", radius="Miles around it (default 50, max 100)",
                           games="Which games (default both)", dm="Also DM me each alert",
                           online="Also tell me about online restocks")
    @app_commands.rename(zip_code="zip")
    @app_commands.choices(games=GAME_CHOICES)
    async def track(inter: discord.Interaction, zip_code: str,
                    radius: app_commands.Range[int, 5, 100] = 50,
                    games: app_commands.Choice[str] | None = None,
                    dm: bool = False, online: bool = True):
        await inter.response.defer(ephemeral=True)
        if not valid_zip(zip_code):
            return await inter.followup.send("That doesn't look like a 5-digit US ZIP code.", ephemeral=True)
        info = await lookup_zip(bot.fetcher, zip_code)
        if not info:
            return await inter.followup.send(f"I couldn't find ZIP {zip_code}. Try a nearby one.", ephemeral=True)
        g = (games.value if games else "both")
        glist = list(ALL_GAMES) if g == "both" else [g]
        bot.state.set_tracker(inter.user.id, inter.user.display_name, info, radius, glist, dm, online)
        await bot.backup_state()
        await inter.followup.send(
            f"✅ Tracking **{games_label(glist)}** within **{radius} mi** of "
            f"**{info['city']}, {info['state']} {info['zip']}**.\n"
            f"Alerts post in <#{bot.alert_channel_id()}>{' and your DMs' if dm else ''}. "
            f"Use `/stock` any time to see what's in stock right now.", ephemeral=True)

    @tree.command(name="untrack", description="Stop my stock alerts")
    async def untrack(inter: discord.Interaction):
        gone = bot.state.remove_tracker(inter.user.id)
        if gone:
            await bot.backup_state()
        await inter.response.send_message("Alerts off." if gone else "You weren't tracking anything.",
                                          ephemeral=True)

    @tree.command(name="stock", description="What's in stock near me right now?")
    async def stock(inter: discord.Interaction):
        t = bot.state.trackers.get(str(inter.user.id))
        if not t:
            return await inter.response.send_message("Set your area first with `/track`.", ephemeral=True)
        e = bot.engine
        if e.last_scan_at is None:
            return await inter.response.send_message("First scan hasn't finished yet — give it a minute.", ephemeral=True)
        local, online = e.stock_for(t)
        lines = [f"**Within {t['radius']} mi of {t['city']}, {t['state']}** (scanned {ago(e.last_scan_at)})"]
        if local:
            for h, d in local[:15]:
                qty = f" · {h.qty} left" if h.qty is not None else ""
                lines.append(f"• [{h.product.title[:70]}]({h.product.url}) — {h.store.label} · {d} mi{qty}")
            if len(local) > 15:
                lines.append(f"…and {len(local) - 15} more")
        else:
            lines.append("• Nothing in local stock right now.")
        if online and t.get("online", True):
            lines.append("\n**Online**")
            for h in online[:10]:
                lines.append(f"• [{h.product.title[:70]}]({h.product.url}) — {h.note}")
        await inter.response.send_message("\n".join(lines)[:1990], ephemeral=True)

    @tree.command(name="status", description="Is the bot healthy? What is it watching?")
    async def status(inter: discord.Interaction):
        lines = status_lines(bot.engine, bot.state, SCAN_INTERVAL_MIN, ago)
        t = bot.state.trackers.get(str(inter.user.id))
        if t:
            lines.append(f"Your circle: {t['zip']} ({t['city']}, {t['state']}) · {t['radius']} mi · "
                         f"{games_label(t['games'])} · DM {'on' if t['dm'] else 'off'} · "
                         f"online alerts {'on' if t.get('online', True) else 'off'}")
        await inter.response.send_message("\n".join(lines)[:1990], ephemeral=True)

    @tree.command(name="list", description="Show the local shops and product pages being watched")
    async def list_cmd(inter: discord.Interaction):
        lines = ["**Local card shops**"]
        lines += [f"• {s['name']} — <{s['url']}>" + (f" · {s.get('city','')}, {s.get('state','')}" if s.get('zip') else " · online only")
                  for s in bot.state.shops] or ["• none yet (`/addshop`)"]
        lines.append("\n**Watched product pages**")
        lines += [f"• {u.get('name') or u['site']} — <{u['url']}>" for u in bot.state.urls] or ["• none yet (`/watchurl`)"]
        await inter.response.send_message("\n".join(lines)[:1990], ephemeral=True)

    @tree.command(name="addshop", description="Watch a local card shop's website (Shopify stores work best)")
    @app_commands.describe(name="Shop name", url="Shop URL, or a collection URL like /collections/pokemon-sealed",
                           zip_code="Shop's ZIP (so it counts as local)", all_products="Include singles, not just sealed")
    @app_commands.rename(zip_code="zip")
    @app_commands.default_permissions(manage_guild=True)
    async def addshop(inter: discord.Interaction, name: str, url: str, zip_code: str | None = None,
                      all_products: bool = False):
        await inter.response.defer(ephemeral=True)
        info = None
        if zip_code:
            info = await lookup_zip(bot.fetcher, zip_code)
            if not info:
                return await inter.followup.send(f"Couldn't find ZIP {zip_code}.", ephemeral=True)
        if not url.lower().startswith("http"):
            url = "https://" + url
        shop = bot.state.add_shop(name, url, info, all_products)
        await bot.backup_state()
        where = f"{info['city']}, {info['state']}" if info else "online only"
        await inter.followup.send(f"🛒 Watching **{shop['name']}** ({where}). It's checked on every scan; "
                                  f"run `/status` after the next scan to confirm it loads.", ephemeral=True)

    @tree.command(name="removeshop", description="Stop watching a local shop")
    @app_commands.default_permissions(manage_guild=True)
    async def removeshop(inter: discord.Interaction, name: str):
        ok = bot.state.remove_shop(name)
        if ok:
            await bot.backup_state()
        await inter.response.send_message("Removed." if ok else "No shop by that name.", ephemeral=True)

    @tree.command(name="watchurl", description="Watch one product page (Walmart, GameStop, Costco, Pokémon Center…) for online stock")
    @app_commands.describe(url="Product page link", name="What to call it in alerts")
    @app_commands.default_permissions(manage_guild=True)
    async def watchurl(inter: discord.Interaction, url: str, name: str | None = None):
        if not url.lower().startswith("http"):
            url = "https://" + url
        w = bot.state.add_url(url, name)
        await bot.backup_state()
        await inter.response.send_message(f"👀 Watching **{w['name'] or w['site']}** — online stock only; "
                                          f"these sites don't share store-level stock.", ephemeral=True)

    @tree.command(name="unwatchurl", description="Stop watching a product page")
    @app_commands.default_permissions(manage_guild=True)
    async def unwatchurl(inter: discord.Interaction, url_or_name: str):
        ok = bot.state.remove_url(url_or_name)
        if ok:
            await bot.backup_state()
        await inter.response.send_message("Removed." if ok else "Nothing matched.", ephemeral=True)

    # ----- drops / SKUs / news -----
    @tree.command(name="drops", description="Upcoming Pokémon & Yu-Gi-Oh! release dates")
    @app_commands.describe(days="How far ahead to look (default 60)")
    async def drops(inter: discord.Interaction, days: app_commands.Range[int, 1, 365] = 60):
        ups = bot.state.upcoming_drops(days)
        if not ups:
            return await inter.response.send_message(
                f"No drops on the calendar for the next {days} days yet. Dates get added automatically when "
                f"Best Buy/Target publish a street date, or by hand with `/adddrop`.", ephemeral=True)
        lines = [f"📅 **Next {days} days** ({len(ups)} drops)"]
        for d in ups[:30]:
            emoji = filters.GAMES.get(d["game"], {}).get("emoji", "🃏")
            src = RETAILER_LABEL.get(d.get("retailer") or d.get("source", ""), d.get("source", ""))
            link = f"[{d['name']}]({d['url']})" if d.get("url") else d["name"]
            sku = f" · {SKU_WORD.get(d.get('retailer', ''), 'ID')} `{d['sku']}`" if d.get("sku") else ""
            lines.append(f"• **{nice_date(d['date'])}** — {emoji} {link}{sku} _({src})_")
        await inter.response.send_message("\n".join(lines)[:1990], ephemeral=True)

    @tree.command(name="adddrop", description="Add an upcoming release by hand (admin)")
    @app_commands.describe(name="Product / set name", date="Release date, YYYY-MM-DD", games="Which game",
                           url="Link (optional)")
    @app_commands.choices(games=GAME_CHOICES[1:])
    @app_commands.default_permissions(manage_guild=True)
    async def adddrop(inter: discord.Interaction, name: str, date: str, games: app_commands.Choice[str],
                      url: str | None = None):
        try:
            iso = datetime.strptime(date.strip(), "%Y-%m-%d").date().isoformat()
        except ValueError:
            return await inter.response.send_message("Date must look like `2026-11-14`.", ephemeral=True)
        rec, _ = bot.state.upsert_drop(name.strip(), iso, games.value, source="manual", url=url or "")
        await bot.backup_state()
        await inter.response.send_message(f"🗓️ **{rec['name']}** — {nice_date(iso)}. It'll show in `/drops` "
                                          f"and the morning digest.", ephemeral=True)

    @tree.command(name="removedrop", description="Remove a release from the calendar (admin)")
    @app_commands.default_permissions(manage_guild=True)
    async def removedrop(inter: discord.Interaction, name: str):
        ok = bot.state.remove_drop(name)
        if ok:
            await bot.backup_state()
        await inter.response.send_message("Removed." if ok else "No drop by that name.", ephemeral=True)

    @tree.command(name="skus", description="Which product pages (TCIN / SKU numbers) the bot is checking")
    @app_commands.describe(games="Filter by game", search="Only titles containing this text")
    @app_commands.choices(games=GAME_CHOICES)
    async def skus(inter: discord.Interaction, games: app_commands.Choice[str] | None = None,
                   search: str | None = None):
        want = None if (games is None or games.value == "both") else games.value
        rows = []
        for ch in bot.engine.checkers:
            for prod in getattr(ch, "known_products", {}).values():
                if want and prod.game != want:
                    continue
                if search and search.lower() not in prod.title.lower():
                    continue
                rows.append(prod)
        pinned = {(s["retailer"], s["id"]) for s in bot.state.skus}
        if not rows:
            return await inter.response.send_message(
                "Nothing tracked yet — the first scan hasn't run, or nothing matched.", ephemeral=True)
        rows.sort(key=lambda p: (p.retailer, p.game, p.title))
        lines = [f"🔎 **{len(rows)} product pages being checked** (📌 = pinned with /addsku)"]
        for p in rows[:40]:
            pin = "📌 " if (p.retailer, p.id) in pinned else ""
            rd = f" · 📅 {p.release_date}" if p.release_date else ""
            lines.append(f"• {pin}{RETAILER_LABEL.get(p.retailer, p.retailer)} `{p.id}` — "
                         f"[{p.title[:60]}]({p.url}){rd}")
        if len(rows) > 40:
            lines.append(f"…and {len(rows) - 40} more (narrow it with `search:`)")
        await inter.response.send_message("\n".join(lines)[:1990], ephemeral=True)

    @tree.command(name="addsku", description="Pin a Target TCIN or Best Buy SKU so it's always checked (admin)")
    @app_commands.describe(retailer="Where the number is from", number="TCIN (Target, in the URL after A-) or SKU (Best Buy)",
                           name="Optional label")
    @app_commands.choices(retailer=[app_commands.Choice(name="Target (TCIN)", value="target"),
                                    app_commands.Choice(name="Best Buy (SKU)", value="bestbuy")])
    @app_commands.default_permissions(manage_guild=True)
    async def addsku(inter: discord.Interaction, retailer: app_commands.Choice[str], number: str,
                     name: str | None = None):
        rec = bot.state.add_sku(retailer.value, number, name or "")
        await bot.backup_state()
        await inter.response.send_message(
            f"📌 Pinned {RETAILER_LABEL[rec['retailer']]} {SKU_WORD[rec['retailer']]} `{rec['id']}`. "
            f"It's checked on every scan from now on (first result after the next scan).", ephemeral=True)

    @tree.command(name="removesku", description="Un-pin a TCIN / SKU (admin)")
    @app_commands.default_permissions(manage_guild=True)
    async def removesku(inter: discord.Interaction, number: str):
        ok = bot.state.remove_sku(number)
        if ok:
            await bot.backup_state()
        await inter.response.send_message("Un-pinned." if ok else "That number wasn't pinned.", ephemeral=True)

    @tree.command(name="addfeed", description="Add an RSS/Atom news feed to watch for drop news (admin)")
    @app_commands.default_permissions(manage_guild=True)
    async def addfeed(inter: discord.Interaction, url: str):
        if not url.lower().startswith("http"):
            url = "https://" + url
        ok = bot.state.add_feed(url)
        if ok:
            await bot.backup_state()
        await inter.response.send_message(("📰 Added. " if ok else "Already watching that feed. ")
                                          + f"Feeds now: {len(bot.engine.feeds())}", ephemeral=True)

    @tree.command(name="removefeed", description="Stop watching a news feed (admin)")
    @app_commands.default_permissions(manage_guild=True)
    async def removefeed(inter: discord.Interaction, url: str):
        ok = bot.state.remove_feed(url)
        if ok:
            await bot.backup_state()
        await inter.response.send_message("Removed." if ok else "That feed isn't in your added list "
                                          "(the two built-in feeds can't be removed; they're filtered by keyword).",
                                          ephemeral=True)

    # ----- server setup -----
    @tree.command(name="setup", description="Create the TCG role + a private \"TCG News\" category with #pokemon and #yugioh (admin)")
    @app_commands.describe(chat="Let TCG members talk in #pokemon / #yugioh (default: read-only)",
                           general="Also make #tcg-chat where TCG members can talk",
                           category="Category name (default: TCG News)")
    @app_commands.default_permissions(manage_guild=True)
    async def setup(inter: discord.Interaction, chat: bool = False, general: bool = False,
                    category: str = "TCG News"):
        await inter.response.defer(ephemeral=True)
        guild = inter.guild
        if guild is None:
            return await inter.followup.send("Run this inside your server.", ephemeral=True)
        me = guild.me
        try:
            role = discord.utils.get(guild.roles, name="TCG")
            made_role = False
            if role is None:
                role = await guild.create_role(name="TCG", colour=discord.Colour(0xF1C40F), mentionable=True,
                                               permissions=discord.Permissions.none(), reason="TCG bot setup")
                made_role = True

            def perms(can_chat: bool) -> dict:
                return {
                    guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    role: discord.PermissionOverwrite(view_channel=True, read_message_history=True,
                                                      add_reactions=True, use_external_emojis=True,
                                                      send_messages=can_chat, create_public_threads=False,
                                                      send_messages_in_threads=False),
                    me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True,
                                                    attach_files=True, read_message_history=True,
                                                    mention_everyone=True, manage_webhooks=True),
                }
            ow = perms(chat)
            parent = discord.utils.get(guild.categories, name=category)
            made_cat = False
            if parent is None:
                parent = await guild.create_category(category, overwrites=ow, reason="TCG bot setup")
                made_cat = True
            else:
                await parent.edit(overwrites=ow)

            async def ensure(name: str, topic: str, o: dict):
                ch = discord.utils.get(parent.text_channels, name=name)
                if ch is None:
                    ch = await guild.create_text_channel(name, overwrites=o, topic=topic, category=parent,
                                                         reason="TCG bot setup")
                    return ch, True
                await ch.edit(overwrites=o, topic=topic)
                return ch, False

            topics = {
                "pokemon": "Pokémon TCG: restocks near you, new listings, drop dates and news — posted by the bot. "
                           "Grab the TCG role to see this.",
                "yugioh": "Yu-Gi-Oh! TCG: restocks near you, new listings, drop dates and news — posted by the bot. "
                          "Grab the TCG role to see this.",
            }
            lines = [f"{'✅ created' if made_role else '✅ found'} role {role.mention}",
                     f"{'✅ created' if made_cat else '✅ updated'} category **{category}** — hidden from everyone else, "
                     f"{'TCG members can chat' if chat else 'read-only for TCG members'}"]
            for game, topic in topics.items():
                ch, new = await ensure(game, topic, ow)
                bot.state.settings[f"{game}_channel_id"] = ch.id
                lines.append(f"{'✅ created' if new else '✅ updated'} {ch.mention} — all {filters.GAMES[game]['label']} "
                             f"stock alerts, listings, drops and news post here")
            if general:
                ch, new = await ensure("tcg-chat", "Talk cards. Pulls, trades, what's in stock near you.", perms(True))
                lines.append(f"{'✅ created' if new else '✅ updated'} {ch.mention} — members can talk here")
            bot.state.save()
            await bot.backup_state()
            lines.append("Members grab or drop the role with `/tcg`. Admins see everything regardless.")
            await inter.followup.send("\n".join(lines), ephemeral=True)
        except discord.Forbidden:
            await inter.followup.send("❌ I'm missing permissions. Give the bot **Manage Roles**, **Manage Channels** "
                                      "and **Manage Webhooks** (Server Settings → Roles → the bot's role), then run "
                                      "`/setup` again.", ephemeral=True)

    @tree.command(name="tcg", description="Give yourself the TCG role (or remove it) to see #tcg-news")
    async def tcg(inter: discord.Interaction):
        guild = inter.guild
        role = discord.utils.get(guild.roles, name="TCG") if guild else None
        if role is None:
            return await inter.response.send_message("There's no TCG role yet — an admin needs to run `/setup`.",
                                                     ephemeral=True)
        member = inter.user
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="/tcg toggle")
                msg = "Removed the TCG role — you won't see #tcg-news anymore."
            else:
                await member.add_roles(role, reason="/tcg toggle")
                chans = " and ".join(f"<#{bot.game_channel_id(g)}>" for g in ("pokemon", "yugioh") if bot.game_channel_id(g))
                msg = f"You've got the {role.mention} role — {chans or 'the TCG News category'} is open to you now."
        except discord.Forbidden:
            msg = "❌ I can't change roles. The bot needs **Manage Roles** and its role must sit above TCG in the role list."
        await inter.response.send_message(msg, ephemeral=True)

    @tree.command(name="scan", description="Run a scan right now (admin)")
    @app_commands.describe(post_everything="Post everything currently in stock, not just new changes")
    @app_commands.default_permissions(manage_guild=True)
    async def scan(inter: discord.Interaction, post_everything: bool = False):
        if bot.scan_lock.locked():
            return await inter.response.send_message("A scan is already running.", ephemeral=True)
        await inter.response.send_message("Scanning… results post in the alert channel.", ephemeral=True)
        alerts = await bot.run_scan(force_alert=post_everything)
        e = bot.engine
        await inter.followup.send(f"Done in {int(e.last_scan_secs)}s — {len(e.last_hits)} items in stock, "
                                  f"{len(alerts)} alert(s) posted. `/status` for details.", ephemeral=True)


def main() -> None:
    if not DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    if not ALERT_CHANNEL_ID:
        log.warning("ALERT_CHANNEL_ID is not set — run /setup in your server and the bot will create the "
                    "TCG News category (#pokemon + #yugioh) and post there.")
    os.makedirs(DATA_DIR, exist_ok=True)
    RestockBot().run(DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
