"""Informace o Telegram kanalech, ze kterych pochazeji clanky (items.origin).

- z verejne stranky t.me/<jmeno>: nazev, pocet odberatelu, popis (zdarma, bez prihlaseni)
- z Bot API getChat (@jmeno): ciselne ID kanalu – jen s TELEGRAM_BOT_TOKEN
Vysledky se cachuji v tabulce telegram_channels (obnova po 7 dnech).
"""
import re
from datetime import timedelta
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from .config import env, now_iso, parse_iso

REFRESH = timedelta(days=7)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def bot_token() -> str | None:
    return env("TELEGRAM_BOT_TOKEN")


def username(origin: str | None) -> str | None:
    """https://t.me/selskyrozum -> selskyrozum; soukrome pozvanky (t.me/+..., joinchat) -> None."""
    if not origin:
        return None
    p = urlparse(origin)
    if p.netloc.lower() not in ("t.me", "telegram.me", "www.t.me"):
        return None
    parts = [x for x in p.path.split("/") if x]
    if parts and parts[0] == "s":
        parts = parts[1:]
    if not parts or parts[0].startswith("+") or parts[0] == "joinchat":
        return None
    name = parts[0]
    return name if re.fullmatch(r"[A-Za-z0-9_]{4,64}", name) else None


def _public_info(name: str) -> dict:
    html = requests.get(f"https://t.me/{name}", headers=UA, timeout=20).text
    s = BeautifulSoup(html, "html.parser")
    title = s.select_one(".tgme_page_title")
    extra = s.select_one(".tgme_page_extra")
    desc = s.select_one(".tgme_page_description")
    subs = None
    if extra:
        m = re.search(r"([\d\s ]+)\s*(subscribers|members|odběratel)", extra.get_text(" ", strip=True))
        if m:
            subs = int(re.sub(r"\D", "", m.group(1)))
    return {"title": title.get_text(strip=True) if title else None, "subscribers": subs,
            "description": desc.get_text(" ", strip=True)[:300] if desc else None}


def _chat_id(name: str) -> int | None:
    token = bot_token()
    if not token:
        return None
    r = requests.get(f"https://api.telegram.org/bot{token}/getChat", params={"chat_id": f"@{name}"}, timeout=20)
    data = r.json()
    return data["result"]["id"] if data.get("ok") else None


def channel_info(conn, name: str) -> dict:
    """Info o kanalu z cache, pripadne nactene znovu (po 7 dnech nebo kdyz chybi ID a uz je token)."""
    row = conn.execute("SELECT * FROM telegram_channels WHERE username = ?", (name,)).fetchone()
    fresh = row and parse_iso(now_iso()) - parse_iso(row["fetched_at"]) < REFRESH
    if fresh and (row["chat_id"] or not bot_token()):
        return dict(row)
    info = {"title": None, "subscribers": None, "description": None}
    try:
        info = _public_info(name)
    except requests.RequestException:
        pass
    chat_id = None
    try:
        chat_id = _chat_id(name)
    except (requests.RequestException, ValueError, KeyError):
        pass
    conn.execute(
        """INSERT OR REPLACE INTO telegram_channels (username, title, subscribers, description, chat_id, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (name, info["title"], info["subscribers"], info["description"],
         chat_id or (row["chat_id"] if row else None), now_iso()),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM telegram_channels WHERE username = ?", (name,)).fetchone())
