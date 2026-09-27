"""Ukladani prispevku z Facebooku do DB (data dodava Bright Data, viz brightdata.py).

Zaznam: {"text", "published_at" (ISO), "url", "post_id", "links"}.
"""
import json
import re
from datetime import datetime

from ..config import now_iso, to_iso

URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")


def _normalize(rec: dict) -> dict | None:
    if not rec.get("published_at") or not rec.get("url"):
        return None
    text = (rec.get("text") or "").strip()
    published = datetime.fromisoformat(str(rec["published_at"]).replace("Z", "+00:00"))
    links = list(rec.get("links") or []) + URL_RE.findall(text)
    ext_id = rec.get("post_id")
    return {"text": text, "published": published, "url": rec["url"].strip(), "links": sorted(set(links)),
            "external_id": str(ext_id) if ext_id else None}


def add_post(conn, source_id: int, rec: dict) -> bool:
    n = _normalize(rec)
    if not n:
        return False
    cur = conn.execute(
        """INSERT OR IGNORE INTO items
               (source_id, kind, url, external_id, title, text, links, published_at, fetched_at)
           VALUES (?, 'post', ?, ?, NULL, ?, ?, ?, ?)""",
        (source_id, n["url"], n["external_id"], n["text"], json.dumps(n["links"]),
         to_iso(n["published"]), now_iso()),
    )
    return cur.rowcount > 0
