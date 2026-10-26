"""Decide which product titles count as Pokémon / Yu-Gi-Oh! trading-card product."""
from __future__ import annotations

import os
import re

GAMES: dict[str, dict] = {
    "pokemon": {
        "label": "Pokémon",
        "emoji": "🟡",
        "include": ["pokemon", "pokémon"],
        "search": {
            "target": ["pokemon trading cards"],
            "bestbuy": [["pokemon", "cards"]],
        },
    },
    "funko": {
        "label": "Funko Pop",
        "emoji": "📦",
        "include": ["funko"],
        "search": {
            "target": ["funko pop"],
            "bestbuy": [["funko", "pop"]],
        },
    },
    "yugioh": {
        "label": "Yu-Gi-Oh!",
        "emoji": "🟣",
        "include": ["yu-gi-oh", "yugioh", "yu gi oh"],
        "search": {
            "target": ["yu-gi-oh trading cards"],
            "bestbuy": [["yu-gi-oh"], ["yugioh"]],
        },
    },
}

# Words that mean "this is sealed TCG product" — these win over the exclude list.
STRONG_WORDS = [
    "funko", "pop! ", "pop vinyl", "vinyl figure", "soda vinyl", "bitty pop",
    "booster", "elite trainer", "trainer box", "etb", "blister", "collection box",
    "premium collection", "special collection", "ultra-premium", "ultra premium",
    "tin", "bundle", "battle deck", "starter deck", "structure deck", "theme deck",
    "build & battle", "build and battle", "box set", "display box", "booster box",
    "mega tin", "legendary collection", "speed duel", "25th anniversary", "poster collection",
    "surprise box", "pack", "sleeved", "ex box", "v box", "vmax", "vstar", "stacking tin",
    "collector chest", "accessory pouch",
]

# Words that usually mean toys / clothing / supplies rather than cards.
DEFAULT_EXCLUDE = [
    "plush", "figure", "figurine", "binder", "sleeves", "card sleeves", "playmat",
    "play mat", "deck box", "shirt", "pajama", "hoodie", "backpack", "lunch", "puzzle",
    "lego", "card holder", "portfolio", "storage", "poster", "sticker",
    "costume", "blanket", "mug", "squishmallow", "nintendo switch", "video game",
    "monopoly", "uno", "album", "carrying case", "display case", "bedding", "pillow",
    "watch", "socks", "cap ", "beanie", "keychain", "lamp", "clock", "towel",
    "trading card game guide", "handbook", "coloring", "mega construx", "action figure",
    "carry case", "battle figure", "clip 'n' go", "pokeball", "poke ball", "water bottle",
    "tumbler", "notebook", "pencil", "magazine", "coin bank", "nanoblock", "build kit",
    "hot wheels", "card protector", "top loader", "toploader", "swatch", "earbuds", "headphones",
    "pop protector", "protector case", "pop stack", "display shelf",
]


def _exclude_words() -> list[str]:
    extra = os.getenv("EXCLUDE_WORDS", "")
    words = list(DEFAULT_EXCLUDE)
    if extra:
        words += [w.strip().lower() for w in extra.split(",") if w.strip()]
    return words


_WS = re.compile(r"\s+")
_rx_cache: dict[str, re.Pattern] = {}


def _has_word(text: str, phrase: str) -> bool:
    """Whole-word match so 'pack' doesn't hit 'backpack' and 'uno' doesn't hit 'unopened'."""
    rx = _rx_cache.get(phrase)
    if rx is None:
        rx = re.compile(r"(?<![a-z0-9])" + re.escape(phrase.strip()) + r"(?:s|es)?(?![a-z0-9])")
        _rx_cache[phrase] = rx
    return rx.search(text) is not None


def norm(s: str) -> str:
    return _WS.sub(" ", (s or "").lower()).strip()


def which_game(title: str) -> str | None:
    t = norm(title)
    if "funko" in t:          # a Funko of a Pokémon is a Funko
        return "funko"
    for game, spec in GAMES.items():
        if any(w in t for w in spec["include"]):
            return game
    return None


def is_tcg_product(title: str, require_strong: bool = False) -> bool:
    """True if the title looks like sealed card product (not plush / binders / etc.)."""
    t = norm(title)
    if any(_has_word(t, w) for w in ("protector", "protectors", "display shelf", "pop stack", "wall mount")):
        return False   # accessories for Funkos / cards, not the thing itself
    strong = any(_has_word(t, w) for w in STRONG_WORDS) or "trading card" in t or _has_word(t, "tcg")
    if require_strong:
        return strong
    if strong:
        return True
    return not any(_has_word(t, w) for w in _exclude_words())


def match(title: str, games: set[str] | None = None, require_strong: bool = False) -> str | None:
    """Return the game key if the title is wanted, else None."""
    g = which_game(title)
    if g is None:
        return None
    if games and g not in games:
        return None
    return g if is_tcg_product(title, require_strong=require_strong) else None


FUNKO_RETAILERS = ["gamestop", "hot topic", "walmart", "fye", "target", "boxlunch", "amazon", "entertainment earth",
                   "funko shop", "nycc", "sdcc", "comic con", "chalice", "big apple"]


def funko_tags(title: str) -> list[str]:
    """Flags collectors care about: chase, exclusive (and where), glow, flocked, convention stickers."""
    t = norm(title)
    tags = []
    if "chase" in t:
        tags.append("CHASE")
    if "exclusive" in t or "excl" in t or any(w in t for w in ("nycc", "sdcc", "eccc", "wondercon", "comic con")):
        where = next((r for r in FUNKO_RETAILERS if r in t), "")
        tags.append(f"{where.title()} Exclusive" if where else "Exclusive")
    for w, tag in (("glow", "Glow"), ("flocked", "Flocked"), ("diamond", "Diamond"), ("metallic", "Metallic"),
                   ("jumbo", "Jumbo 10\""), ("limited edition", "Limited")):
        if w in t:
            tags.append(tag)
    return tags
