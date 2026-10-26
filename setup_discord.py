"""One-time Discord setup: creates the TCG role and a private "TCG News" category with
#pokemon and #yugioh text channels that only that role (and admins) can see, plus a
webhook in each so the bot can post Pokémon news in one and Yu-Gi-Oh! news in the other.

    python setup_discord.py                 # role "TCG" + category "TCG News" + #pokemon + #yugioh + webhooks
    python setup_discord.py --chat          # let TCG members talk in the channels (default: read-only)
    python setup_discord.py --general       # also add #tcg-chat where TCG members can talk
    python setup_discord.py --category "Card News"   # use a different category name

Needs DISCORD_TOKEN and GUILD_ID in .env (or the environment). The bot must be in the server
with these permissions: Manage Roles, Manage Channels, Manage Webhooks.
It prints the channel IDs and webhook URLs you need for the bot's settings / GitHub secrets.
Safe to run again: existing role/category/channels/webhooks are reused, permissions re-applied.
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
CATEGORY_NAME = "TCG News"
WEBHOOK_NAME = "TCG Restock Bot"
GAME_CHANNELS = {               # channel name → (env/secret prefix, topic)
    "pokemon": ("POKEMON", "Pokémon TCG: restocks near you, new listings, drop dates and news — posted by the bot. "
                           "Grab the TCG role to see this."),
    "yugioh": ("YUGIOH", "Yu-Gi-Oh! TCG: restocks near you, new listings, drop dates and news — posted by the bot. "
                         "Grab the TCG role to see this."),
    "funko-pops": ("FUNKO", "Funko Pops: drops, chases and exclusives at GameStop, Hot Topic, Walmart, FYE, Target, "
                            "Best Buy — posted by the bot. Grab the TCG role to see this."),
}
CHAT_CHANNEL = "tcg-chat"


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


async def ensure_category(api: DiscordREST, guild_id: str, name: str, ow: list[dict]) -> tuple[dict, bool]:
    chans = await api.request("GET", f"/guilds/{guild_id}/channels")
    for c in chans:
        if c["type"] == 4 and c["name"].lower() == name.lower():
            cat = await api.request("PATCH", f"/channels/{c['id']}", {"permission_overwrites": ow})
            return cat, False
    cat = await api.request("POST", f"/guilds/{guild_id}/channels",
                            {"name": name, "type": 4, "permission_overwrites": ow})
    return cat, True


async def ensure_channel(api: DiscordREST, guild_id: str, name: str, topic: str, ow: list[dict],
                         parent_id: str) -> tuple[dict, bool]:
    chans = await api.request("GET", f"/guilds/{guild_id}/channels")
    for c in chans:
        if c["type"] == 0 and c["name"].lower() == name.lower() and c.get("parent_id") == parent_id:
            ch = await api.request("PATCH", f"/channels/{c['id']}", {"permission_overwrites": ow, "topic": topic})
            return ch, False
    ch = await api.request("POST", f"/guilds/{guild_id}/channels",
                           {"name": name, "type": 0, "topic": topic, "permission_overwrites": ow,
                            "parent_id": parent_id})
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
    category_name = args.category or CATEGORY_NAME
    async with api_factory(token) as api:
        me = await api.request("GET", "/users/@me")
        bot_id = me["id"]
        role, new_role = await ensure_role(api, guild_id)
        ow = overwrites(guild_id, role["id"], bot_id, args.chat)
        cat, new_cat = await ensure_category(api, guild_id, category_name, ow)
        out["role_id"] = role["id"]
        out["category_id"] = cat["id"]
        print(f"{'✅ created' if new_role else '✅ found'} role @{ROLE_NAME}  (id {role['id']})")
        print(f"{'✅ created' if new_cat else '✅ updated'} category \"{category_name}\" — hidden from @everyone, "
              f"visible to @{ROLE_NAME}{' (members can chat)' if args.chat else ' (read-only)'}")
        for name, (prefix, topic) in GAME_CHANNELS.items():
            ch, new_ch = await ensure_channel(api, guild_id, name, topic, ow, cat["id"])
            out[f"{prefix}_CHANNEL_ID"] = ch["id"]
            out[f"{prefix}_WEBHOOK"] = await ensure_webhook(api, ch["id"])
            print(f"{'✅ created' if new_ch else '✅ updated'} #{name}  (id {ch['id']})")
        if args.general:
            chat_ow = overwrites(guild_id, role["id"], bot_id, chat=True)
            ch, new_ch = await ensure_channel(api, guild_id, CHAT_CHANNEL,
                                              "Talk cards. Pulls, trades, what's in stock near you.", chat_ow, cat["id"])
            out["CHAT_CHANNEL_ID"] = ch["id"]
            print(f"{'✅ created' if new_ch else '✅ updated'} #{CHAT_CHANNEL}  (members can chat)")
    print("\nNext, paste these where the bot reads them:")
    print(f"  Live bot (.env / Render):   POKEMON_CHANNEL_ID={out['POKEMON_CHANNEL_ID']}   "
          f"YUGIOH_CHANNEL_ID={out['YUGIOH_CHANNEL_ID']}   FUNKO_CHANNEL_ID={out['FUNKO_CHANNEL_ID']}")
    print(f"  GitHub mode (repo secrets): POKEMON_WEBHOOK={out['POKEMON_WEBHOOK']}")
    print(f"                              YUGIOH_WEBHOOK={out['YUGIOH_WEBHOOK']}")
    print(f"                              FUNKO_WEBHOOK={out['FUNKO_WEBHOOK']}")
    print(f"\nGive people the @{ROLE_NAME} role to let them see the category (Server Settings → Members, "
          f"or the live bot's /tcg command lets them grab it themselves).")
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--chat", action="store_true", help="let TCG members post in #pokemon and #yugioh")
    p.add_argument("--general", action="store_true", help=f"also create #{CHAT_CHANNEL} for members to talk")
    p.add_argument("--category", default="", help=f'category name (default "{CATEGORY_NAME}")')
    args = p.parse_args()
    try:
        asyncio.run(run(args))
    except SetupError as e:
        print(f"\n❌ {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
