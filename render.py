"""Builds the Discord messages (content + embed dicts). Shared by bot.py (live bot)
and scan_once.py (GitHub Actions + webhook), so both post the exact same thing."""
from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import filters
from checkers.news import NewsItem
from models import Alert, Product

TZ = os.getenv("TZ_NAME", "America/Denver")
COLORS = {"target": 0xCC0000, "bestbuy": 0x0046BE, "shop": 0x2E8B57, "url": 0x7289DA, "news": 0xF1C40F}
RETAILER_LABEL = {"target": "Target", "bestbuy": "Best Buy", "shop": "Local shop", "url": "Online",
                  "manual": "added by hand", "news": "news"}
SKU_WORD = {"target": "TCIN", "bestbuy": "SKU"}


def _tz():
    try:
        return ZoneInfo(TZ)
    except Exception:
        return ZoneInfo("UTC")


def today_iso() -> str:
    return datetime.now(_tz()).date().isoformat()


def games_label(games: list[str]) -> str:
    return " + ".join(filters.GAMES[g]["label"] for g in games if g in filters.GAMES) or "—"


def nice_date(iso: str) -> str:
    try:
        d = datetime.strptime(iso, "%Y-%m-%d").date()
    except Exception:
        return iso
    delta = (d - datetime.now(_tz()).date()).days
    when = ("today" if delta == 0 else "tomorrow" if delta == 1
            else f"in {delta} days" if delta > 1 else f"{-delta}d ago")
    return f"{d.strftime('%a %b %d')} ({when})"


def _spec(game: str) -> dict:
    return filters.GAMES.get(game, {"label": "Trading cards", "emoji": "🃏"})


def _embed(title: str, url: str = "", color: int = 0x999999, description: str = "",
           thumbnail: str = "", footer: str = "") -> dict:
    e: dict = {"title": title[:256], "color": color}
    if url:
        e["url"] = url
    if description:
        e["description"] = description[:4000]
    if thumbnail:
        e["thumbnail"] = {"url": thumbnail}
    if footer:
        e["footer"] = {"text": footer[:2048]}
    return e


# ---------- stock alerts ----------
def alert_message(a: Alert) -> tuple[str, dict]:
    prod = a.product
    spec = _spec(prod.game)
    retailer = RETAILER_LABEL.get(prod.retailer, prod.retailer)
    if prod.retailer == "shop" and a.hits and a.hits[0].store:
        retailer = a.hits[0].store.name
    desc = []
    if prod.price:
        desc.append(f"💲 {prod.price}")
    fields = []
    if a.kind == "local":
        header = f"{spec['emoji']} **{spec['label']}** — **{retailer}** has it in stock near you"
        lines = []
        for h in a.hits[:12]:
            qty = f"{h.qty} left · " if h.qty is not None else ""
            lines.append(f"🏬 **{h.store.label}** — {qty}{h.note}")
        if len(a.hits) > 12:
            lines.append(f"…and {len(a.hits) - 12} more stores")
        fields.append({"name": "Where", "value": "\n".join(lines)[:1024], "inline": False})
        mentions = ", ".join(f"<@{uid}> ({mi} mi)" for uid, mi in a.recipients[:30])
    else:
        site = a.hits[0].note if a.hits else "online"
        header = f"🌐 {spec['emoji']} **{spec['label']}** — {retailer} restock {site}"
        mentions = ", ".join(f"<@{uid}>" for uid, _ in a.recipients[:30])
    desc.append("Go now — stock moves fast. Double-check in the app before you drive.")
    embed = _embed(prod.title, prod.url, COLORS.get(prod.retailer, 0x999999), "\n".join(desc),
                   prod.image, f"{retailer} · {datetime.now(_tz()).strftime('%b %d %I:%M %p')}")
    if fields:
        embed["fields"] = fields
    content = header + (f"\n{mentions}" if mentions else "")
    return content[:1990], embed


# ---------- drops / listings / news ----------
def listing_message(prod: Product) -> tuple[str, dict]:
    spec = _spec(prod.game)
    retailer = RETAILER_LABEL.get(prod.retailer, prod.retailer)
    desc = f"{SKU_WORD.get(prod.retailer, 'ID')} `{prod.id}`"
    if prod.price:
        desc += f" · 💲 {prod.price}"
    if prod.release_date:
        desc += f"\n📅 Street date: **{nice_date(prod.release_date)}**"
    embed = _embed(prod.title, prod.url, COLORS.get(prod.retailer, 0x999999), desc, prod.image,
                   f"{retailer} · new listing spotted · stock checks start now")
    return f"👀 {spec['emoji']} **New {spec['label']} listing at {retailer}**", embed


def drop_message(d: dict) -> tuple[str, dict]:
    spec = _spec(d.get("game", ""))
    src = RETAILER_LABEL.get(d.get("source", ""), d.get("source", ""))
    desc = f"📅 **{nice_date(d['date'])}**"
    if d.get("sku"):
        desc += f" · {SKU_WORD.get(d.get('retailer', ''), 'ID')} `{d['sku']}`"
    embed = _embed(d["name"], d.get("url", ""), COLORS["news"], desc,
                   footer=f"release date from {src} · /drops for the full list")
    return f"🗓️ {spec['emoji']} **{spec['label']} drop date confirmed**", embed


def news_message(n: NewsItem, interactive: bool = True) -> tuple[str, dict]:
    spec = _spec(n.game)
    desc = n.summary[:220] + ("…" if len(n.summary) > 220 else "")
    if n.release_date:
        how = "pin it with `/adddrop`" if interactive else "add it to config.json → drops"
        desc = f"📅 Mentions **{nice_date(n.release_date)}** — if that's a drop, {how}\n{desc}"
    embed = _embed(n.title, n.link, COLORS["news"], desc,
                   footer=f"{n.source} · {datetime.fromtimestamp(n.published).strftime('%b %d')}")
    return f"📰 {spec['emoji']} **{spec['label']} news**", embed


def digest_text(state, days: int = 14, interactive: bool = True, game: str | None = None) -> str:
    today = today_iso()
    ups = state.upcoming_drops(days)
    if game:
        ups = [d for d in ups if d.get("game") == game]
    lines = []
    todays = [d for d in ups if d["date"] == today]
    if todays:
        lines.append("🚨 **DROPS TODAY** — check Target/Best Buy/Walmart/GameStop this morning:")
        for d in todays:
            line = f"• {_spec(d['game'])['emoji']} **{d['name']}**"
            if d.get("sku"):
                line += (f" — {RETAILER_LABEL.get(d.get('retailer', ''), '')} "
                         f"{SKU_WORD.get(d.get('retailer', ''), '')} `{d['sku']}`")
            lines.append(line)
        lines.append("")
    later = [d for d in ups if d["date"] != today]
    how = "`/adddrop`" if interactive else "config.json"
    lines.append(f"📅 **Coming up (next {days} days)**" if later
                 else f"📅 Nothing else on the calendar for the next {days} days — add drops with {how}.")
    for d in later[:20]:
        line = f"• {nice_date(d['date'])} — {_spec(d['game'])['emoji']} {d['name']}"
        if d.get("retailer"):
            line += f" ({RETAILER_LABEL.get(d.get('retailer', ''), d.get('source', ''))})"
        lines.append(line)
    return "\n".join(lines)[:1990]


def status_lines(engine, state, interval_min: float, ago_fn) -> list[str]:
    e = engine
    lines = [f"Last scan: {ago_fn(e.last_scan_at)} (took {int(e.last_scan_secs)}s) · "
             f"scans: {e.scans} · every {interval_min:g} min"]
    for ch in list(e.checkers) + ([e.news] if e.news else []):
        st, detail = e.health.get(ch.name, ("idle", "not run yet"))
        icon = "✅" if st == "ok" else ("❌" if st == "error" else "⏳")
        lines.append(f"{icon} **{ch.label}** — {detail}")
    lines.append(f"Trackers: {len(state.trackers)} · Shops: {len(state.shops)} · "
                 f"Watched pages: {len(state.urls)} · Pinned SKUs: {len(state.skus)} · "
                 f"Upcoming drops: {len(state.upcoming_drops(365))}")
    return lines
