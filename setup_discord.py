"""One-time Discord setup: creates the TCG role, a private #tcg-news channel that only
that role (and admins) can see, and a webhook for the bot to post through.

    python setup_discord.py                      # role "TCG" + #tcg-news + webhook
    python setup_discord.py --alerts-channel     # also make #tcg-alerts with the same perms
    python setup_discord.py --category "TCG"     # put the channel(s) under a category
    python setup_discord.py --chat               # let TCG members talk in the channel (default: read-only)

Needs DISCORD_TOKEN and GUILD_ID in .env (or the environment). The bot must be in the server
with these permissions: Manage Roles, Manage Channels, Manage Webhooks.
It prints the channel IDs and webhook URLs you need for the bot's settings / GitHub secrets.
Safe to run again: existing role/channel/webhook are reused, permissions are re-applied.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

API = "https://discord.com/api/v10"

# permission bits (https://discord.com/developers/docs/topics/permissions)
ADD_REACTIONS = 1 << 6
VIEW_CHANNEL = 1 << 10
SEND_MESSAGES = 1 << 11
EMBED_LINKS = 1 << 14
ATTACH_FILES = 1 << 15
READ_MESSAGE_HISTORY = 1 << 16
MENTION_EVERYONE = 1 << 17
USE_EXTERNAL_EMOJIS = 1 << 18
MANAGE_WEBHOOKS = 1 << 29
CREATE_PUBLIC_THREADS = 1 << 35
SEND_MESSAGES_IN_THREADS = 1 << 38

ROLE_NAME = "TCG"
ROLE_COLOR = 0xF1C40F          # gold
NEWS_CHANNEL = "tcg-news"
ALERTS_CHANNEL = "tcg-alerts"
WEBHOOK_NAME = "TCG Restock Bot"
TOPIC = ("Pokémon & Yu-Gi-Oh! restocks near you, new listings, drop dates and news — posted by the TCG bot. "
         "Only the TCG role can see this channel.")


class SetupError(SystemExit):
    pass


class DiscordREST:
    def __init__(self, token: str):
        import aiohttp
        self._aiohttp = aiohttp
        self.headers = {"Authorization": f"Bot {token}", "User-Agent": "tcg-restock-bot setup (https://github.com, 1.0)"}
        self.session = None

    async def __aenter__(self):
        self.session = self._aiohttp.ClientSession(headers=self.headers,
                                                   timeout=self._aiohttp.ClientTimeout(total=30))
        return self

    async def __aexit__(self, *a):
        await self.session.close()

    async def request(self, method: str, path: str, json: dict | None = None):
        for _ in range(5):
            async with self.session.request(method, API + path, json=json) as r:
                if r.status == 429:
                    data = await r.json(content_type=None)
                    await asyncio.sleep(float((data or {}).get("retry_after", 1)) + 0.2)
                    continue
                if r.status == 204:
                    return {}
                data = await r.json(content_type=None)
                if r.status >= 400:
                    msg = (data or {}).get("message", str(data))
                    if r.status == 401:
                        raise SetupError("Discord says the token is invalid (401). Reset it in the Developer Portal → Bot.")
                    if r.status == 403:
                        raise SetupError(f"Discord refused ({msg}). The bot needs Manage Roles, Manage Channels and "
                                         f"Manage Webhooks in this server — re-invite it with those ticked, or give its role "
                                         f"those permissions in Server Settings → Roles.")
                    raise SetupError(f"Discord error {r.status} on {method} {path}: {msg}")
                return data
        raise SetupError("Discord kept rate-limiting us; try again in a minute.")


def overwrites(guild_id: str, role_id: str, bot_id: str, chat: bool) -> list[dict]:
    member_allow = VIEW_CHANNEL | READ_MESSAGE_HISTORY | ADD_REACTIONS | USE_EXTERNAL_EMOJIS
    member_deny = CREATE_PUBLIC_THREADS | SEND_MESSAGES_IN_THREADS
    if chat:
        member_allow |= SEND_MESSAGES
    else:
        member_deny |= SEND_MESSAGES
    return [
        {"id": guild_id, "type": 0, "allow": "0", "deny": str(VIEW_CHANNEL)},            # @everyone: hidden
        {"id": role_id, "type": 0, "allow": str(member_allow), "deny": str(member_deny)},  # TCG role
        {"id": bot_id, "type": 1,                                                           # the bot itself
         "allow": str(VIEW_CHANNEL | SEND_MESSAGES | EMBED_LINKS | ATTACH_FILES | READ_MESSAGE_HISTORY
                      | MENTION_EVERYONE | MANAGE_WEBHOOKS), "deny": "0"},
    ]


async def ensure_role(api: DiscordREST, guild_id: str) -> tuple[dict, bool]:
    roles = await api.request("GET", f"/guilds/{guild_id}/roles")
    for r in roles:
        if r["name"].lower() == ROLE_NAME.lower():
            return r, False
    role = await api.request("POST", f"/guilds/{guild_id}/roles",
                             {"name": ROLE_NAME, "color": ROLE_COLOR, "mentionable": True, "hoist": False,
                              "permissions": "0"})   # a tag role: grants nothing by itself
    return role, True


async def ensure_category(api: DiscordREST, guild_id: str, name: str, ow: list[dict]) -> str:
    chans = await api.request("GET", f"/guilds/{guild_id}/channels")
    for c in chans:
        if c["type"] == 4 and c["name"].lower() == name.lower():
            return c["id"]
    cat = await api.request("POST", f"/guilds/{guild_id}/channels",
                            {"name": name, "type": 4, "permission_overwrites": ow})
    return cat["id"]


async def ensure_channel(api: DiscordREST, guild_id: str, name: str, ow: list[dict],
                         parent_id: str | None) -> tuple[dict, bool]:
    chans = await api.request("GET", f"/guilds/{guild_id}/channels")
    for c in chans:
        if c["type"] == 0 and c["name"].lower() == name.lower():
            body = {"permission_overwrites": ow, "topic": TOPIC}
            if parent_id:
                body["parent_id"] = parent_id
            ch = await api.request("PATCH", f"/channels/{c['id']}", body)
            return ch, False
    body = {"name": name, "type": 0, "topic": TOPIC, "permission_overwrites": ow}
    if parent_id:
        body["parent_id"] = parent_id
    ch = await api.request("POST", f"/guilds/{guild_id}/channels", body)
    return ch, True


async def ensure_webhook(api: DiscordREST, channel_id: str) -> str:
    hooks = await api.request("GET", f"/channels/{channel_id}/webhooks")
    for h in hooks:
        if h.get("name") == WEBHOOK_NAME and h.get("token"):
            return f"https://discord.com/api/webhooks/{h['id']}/{h['token']}"
    h = await api.request("POST", f"/channels/{channel_id}/webhooks", {"name": WEBHOOK_NAME})
    return f"https://discord.com/api/webhooks/{h['id']}/{h['token']}"


async def run(args, api_factory=DiscordREST) -> dict:
    token = os.getenv("DISCORD_TOKEN", "").strip()
    guild_id = os.getenv("GUILD_ID", "").strip()
    if not token or not guild_id:
        raise SetupError("Set DISCORD_TOKEN and GUILD_ID in .env first (GUILD_ID = right-click server → Copy Server ID).")
    out: dict = {}
    async with api_factory(token) as api:
        me = await api.request("GET", "/users/@me")
        bot_id = me["id"]
        role, new_role = await ensure_role(api, guild_id)
        ow = overwrites(guild_id, role["id"], bot_id, args.chat)
        parent = await ensure_category(api, guild_id, args.category, ow) if args.category else None
        news, new_news = await ensure_channel(api, guild_id, NEWS_CHANNEL, ow, parent)
        out["role_id"] = role["id"]
        out["news_channel_id"] = news["id"]
        out["news_webhook"] = await ensure_webhook(api, news["id"])
        print(f"{'✅ created' if new_role else '✅ found'} role @{ROLE_NAME}  (id {role['id']})")
        print(f"{'✅ created' if new_news else '✅ updated'} #{NEWS_CHANNEL}  (id {news['id']}) — hidden from @everyone, "
              f"visible to @{ROLE_NAME}{' (can chat)' if args.chat else ' (read-only)'}")
        if args.alerts_channel:
            alerts, new_alerts = await ensure_channel(api, guild_id, ALERTS_CHANNEL, ow, parent)
            out["alerts_channel_id"] = alerts["id"]
            out["alerts_webhook"] = await ensure_webhook(api, alerts["id"])
            print(f"{'✅ created' if new_alerts else '✅ updated'} #{ALERTS_CHANNEL}  (id {alerts['id']})")
    print("\nNext, paste these where the bot reads them:")
    print(f"  Live bot (.env / Render):   NEWS_CHANNEL_ID={out['news_channel_id']}"
          + (f"   ALERT_CHANNEL_ID={out['alerts_channel_id']}" if "alerts_channel_id" in out else
             f"   (and ALERT_CHANNEL_ID={out['news_channel_id']} if you want everything in one channel)"))
    print(f"  GitHub mode (repo secrets): NEWS_WEBHOOK={out['news_webhook']}")
    if "alerts_webhook" in out:
        print(f"                              DISCORD_WEBHOOK={out['alerts_webhook']}")
    else:
        print(f"                              DISCORD_WEBHOOK={out['news_webhook']}   (same channel for alerts)")
    print(f"\nGive people the @{ROLE_NAME} role to let them see the channel (Server Settings → Members, "
          f"or the live bot's /tcg command lets them grab it themselves).")
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--alerts-channel", action="store_true", help=f"also create #{ALERTS_CHANNEL}")
    p.add_argument("--category", default="", help="category name to put the channel(s) under")
    p.add_argument("--chat", action="store_true", help="let TCG members post in the channel")
    args = p.parse_args()
    try:
        asyncio.run(run(args))
    except SetupError as e:
        print(f"\n❌ {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
