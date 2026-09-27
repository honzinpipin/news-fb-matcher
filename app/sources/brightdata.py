"""Prispevky verejne FB stranky pres Bright Data Web Scraper API (bez FB prihlaseni).

Postup: trigger -> cekat na progress 'ready' -> stahnout snapshot -> ulozit jako prispevky.
Kazdou noc se stahuje od data posledniho ulozeneho prispevku (s 1 dnem prekryvu);
uz ulozene prispevky z prekryvu se posilaji v posts_to_not_include, aby se neplatily znovu.
Surova odpoved se uklada do data/fb_raw/ (pro kontrolu poli).
"""
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests

from ..config import ROOT, env, parse_iso
from ..db import add_usage, get_state, get_usage, set_state
from . import facebook

API = "https://api.brightdata.com/datasets/v3"
PROVIDER = "brightdata"
RAW_DIR = ROOT / "data" / "fb_raw"
IMG_DIR = ROOT / "data" / "fb_images"     # odkazy na obrazky z FB po par dnech expiruji
MAX_IMAGES = 4
PENDING = ROOT / "data" / "fb_pending.json"   # jen stary format; nyni v sources.state["pending"]


class BrightDataError(Exception):
    pass


def token() -> str | None:
    return env("BRIGHTDATA_TOKEN")


def enabled(cfg: dict) -> bool:
    return cfg.get("facebook", {}).get("provider") == PROVIDER


def ready(cfg: dict) -> bool:
    return enabled(cfg) and bool(token())


def month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _req(method: str, path: str, **kw) -> requests.Response:
    r = requests.request(method, API + path, headers={"Authorization": f"Bearer {token()}"},
                         timeout=60, **kw)
    if r.status_code >= 400:
        raise BrightDataError(f"HTTP {r.status_code} {path}: {r.text[:300]}")
    return r


def _load_pending(conn, source_id: int) -> list[str]:
    pending = list(get_state(conn, source_id).get("pending", []))
    # Stary globalni soubor (verze s jednim profilem) -> prevzit.
    if PENDING.exists():
        try:
            pending += json.loads(PENDING.read_text(encoding="utf-8"))
        except ValueError:
            pass
        PENDING.unlink()
    return pending


def _save_pending(conn, source_id: int, ids: list[str]) -> None:
    set_state(conn, source_id, pending=ids)


def _window(conn, source_id: int, cfg: dict) -> tuple[datetime, list[str]]:
    """Od kdy stahovat + ID prispevku, ktere uz mame (z prekryvu)."""
    row = conn.execute(
        "SELECT MAX(published_at) FROM items WHERE kind = 'post' AND source_id = ?", (source_id,)
    ).fetchone()
    if row[0]:
        start = parse_iso(row[0]) - timedelta(days=1)
    else:
        start = datetime.now(timezone.utc) - timedelta(days=cfg["facebook"]["backfill_days"])
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    known = [r[0] for r in conn.execute(
        """SELECT external_id FROM items WHERE kind = 'post' AND source_id = ?
           AND external_id IS NOT NULL AND published_at >= ?""",
        (source_id, start.isoformat()),
    )]
    return start, known


def _wait_and_download(snapshot_id: str, cfg: dict, log) -> list[dict] | None:
    """Vraci zaznamy, nebo None, kdyz snapshot jeste nedobehl (zkusi se pri dalsim behu)."""
    fb = cfg["facebook"]
    deadline = time.time() + fb["timeout_min"] * 60
    while True:
        status = _req("GET", f"/progress/{snapshot_id}").json().get("status")
        if status == "ready":
            break
        if status in ("failed", "canceled"):
            raise BrightDataError(f"snapshot {snapshot_id}: {status}")
        if time.time() >= deadline:
            log(f"  snapshot {snapshot_id} po {fb['timeout_min']} min stale bezi ({status}), dokoncim priste")
            return None
        time.sleep(fb["poll_interval_s"])
    for _ in range(10):
        r = _req("GET", f"/snapshot/{snapshot_id}", params={"format": "json"})
        if r.status_code == 200:
            data = r.json()
            return data if isinstance(data, list) else [data]
        time.sleep(fb["poll_interval_s"])  # 202 = snapshot se jeste sestavuje
    return None


def _collect_links(obj, out: set) -> None:
    """Vsechny externi odkazy v zaznamu (obrazky a odkazy primo na FB vynechava)."""
    if isinstance(obj, dict):
        for v in obj.values():
            _collect_links(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_links(v, out)
    elif isinstance(obj, str) and obj.startswith(("http://", "https://")):
        host = urlparse(obj).netloc.lower()
        is_fb = host.endswith(("facebook.com", "fbcdn.net", "fbsbx.com", "fb.com", "fb.watch"))
        if not is_fb or host.startswith("l.facebook.com"):
            out.add(obj)


# Pole zaznamu, ktera popisuji stranku (ne prispevek) – jejich odkazy nechceme.
PAGE_KEYS = {"about", "privacy_and_legal_info", "avatar_image_url", "header_image",
             "active_ads_urls", "user_url", "input", "posts_to_not_include"}


def to_post(rec: dict) -> dict | None:
    if not rec.get("date_posted") or not rec.get("url"):
        return None
    links: set = set()
    _collect_links({k: v for k, v in rec.items() if k not in PAGE_KEYS and not k.startswith("page_")}, links)
    # U sdileneho odkazu pridame i jeho popisek (titulek clanku), pomaha parovani.
    text = "\n".join(x.strip() for x in (rec.get("content"), rec.get("link_description_text")) if x and x.strip())
    images = [a["url"] for a in rec.get("attachments") or []
              if isinstance(a, dict) and a.get("type") == "Photo" and a.get("url")]
    if rec.get("post_image") and rec["post_image"] not in images:
        images.append(rec["post_image"])
    return {
        "text": text,
        "image_urls": images[:MAX_IMAGES],
        "published_at": rec["date_posted"],
        "url": rec["url"],
        "post_id": rec.get("post_id"),
        "links": sorted(links),
    }


def _save_images(conn, post: dict, log) -> None:
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, url in enumerate(post["image_urls"]):
        name = f"{post['post_id'] or abs(hash(post['url']))}_{i}.jpg"
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            (IMG_DIR / name).write_bytes(r.content)
            paths.append(f"data/fb_images/{name}")
        except requests.RequestException as e:
            log(f"  obrazek {name} se nepodarilo stahnout: {e}")
    if paths:
        conn.execute("UPDATE items SET images = ? WHERE kind = 'post' AND url = ?", (json.dumps(paths), post["url"]))


def _store(conn, source_id: int, records: list[dict], snapshot_id: str, log) -> int:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    (RAW_DIR / f"{snapshot_id}.json").write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
    errors = [r for r in records if r.get("error") or r.get("error_code")]
    valid = [r for r in records if r not in errors]
    for e in errors[:3]:
        log(f"  Bright Data chyba: {e.get('error_code')} {str(e.get('error'))[:200]}")
    add_usage(conn, PROVIDER, len(valid), month_key())
    added = 0
    for rec in valid:
        post = to_post(rec)
        if post and facebook.add_post(conn, source_id, post):
            added += 1
            _save_images(conn, post, log)
    conn.commit()
    log(f"  snapshot {snapshot_id}: {len(valid)} zaznamu, {len(errors)} chyb, novych prispevku {added}")
    return added


def fetch(conn, source, cfg: dict, log) -> int:
    fb = cfg["facebook"]
    added = 0

    # 1) dokoncit snapshoty z minula
    still_pending = []
    for sid in _load_pending(conn, source["id"]):
        try:
            recs = _wait_and_download(sid, cfg, log)
        except BrightDataError as e:
            log(f"  {e}")
            continue
        if recs is None:
            still_pending.append(sid)
        else:
            added += _store(conn, source["id"], recs, sid, log)
    _save_pending(conn, source["id"], still_pending)
    if still_pending:
        return added

    state = get_state(conn, source["id"])
    first = conn.execute(
        "SELECT COUNT(*) FROM items WHERE kind = 'post' AND source_id = ? AND external_id IS NOT NULL", (source["id"],)
    ).fetchone()[0] == 0

    # 2) nove prispevky (pri uplne prvnim behu cele uvodni obdobi s vetsim limitem)
    limit = _limit(conn, cfg, fb["backfill_max_posts"] if first else fb["max_posts_per_run"], log)
    if not limit:
        return added
    start, known = _window(conn, source["id"], cfg)
    if first:
        set_state(conn, source["id"], backfill_start=start.date().isoformat())
    res = _run_job(conn, source, cfg, log, start, None, limit, known)
    if res is None:
        return added
    added += res[0]
    if res[1] >= limit:
        log(f"  !! vraceno {res[1]} = limit {limit}; nektere prispevky mohou chybet (zvys max_posts_per_run)")
    elif first:
        # Uvodni stazeni se veslo do limitu -> obdobi je kompletni, neni co dotahovat.
        set_state(conn, source["id"], backfill_done=True)
        state["backfill_done"] = True

    # 3) jednorazove dotazeni mezery v uvodnim obdobi (napr. kdyz prvni stazeni narazilo na limit)
    if not state.get("backfill_done"):
        default = (datetime.now(timezone.utc) - timedelta(days=fb["backfill_days"])).date().isoformat()
        bstart = datetime.fromisoformat(state.get("backfill_start") or default).replace(tzinfo=timezone.utc)
        oldest = parse_iso(conn.execute(
            "SELECT MIN(published_at) FROM items WHERE kind = 'post' AND source_id = ?", (source["id"],)
        ).fetchone()[0])
        if oldest.date() <= bstart.date():
            set_state(conn, source["id"], backfill_done=True)
        else:
            limit = _limit(conn, cfg, fb["backfill_max_posts"], log)
            if not limit:
                return added
            end_day = oldest.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            known = [r[0] for r in conn.execute(
                """SELECT external_id FROM items WHERE kind = 'post' AND source_id = ?
                   AND external_id IS NOT NULL AND published_at < ?""",
                (source["id"], end_day.isoformat()),
            )]
            log("  dotahuji starsi prispevky uvodniho obdobi")
            res = _run_job(conn, source, cfg, log, bstart, oldest, limit, known)
            if res is not None:
                added += res[0]
                set_state(conn, source["id"], backfill_done=True)
    return added


def _limit(conn, cfg: dict, wanted: int, log) -> int:
    """Pozadovany pocet omezeny zbyvajicim free kreditem (0 = nestahovat)."""
    fb = cfg["facebook"]
    if not fb.get("stop_at_free_limit", True):
        return wanted
    used = get_usage(conn, PROVIDER, month_key())
    remaining = fb["monthly_free_records"] - used
    if remaining <= 0:
        log(f"  vycerpan mesicni free kredit ({used} zaznamu), stahovani se preskakuje")
        return 0
    return min(wanted, remaining)


def _run_job(conn, source, cfg: dict, log, start: datetime, end: datetime | None,
             limit: int, known: list[str]) -> tuple[int, int] | None:
    """Spusti ulohu a ulozi vysledek. Vraci (novych prispevku, vracenych zaznamu), None = ceka se."""
    body = [{
        "url": source["url"],
        "num_of_posts": limit,
        "start_date": start.strftime("%m-%d-%Y"),
        "end_date": end.strftime("%m-%d-%Y") if end else "",
        "posts_to_not_include": known,
    }]
    rng = f"{start.date()} – {end.date() if end else 'dnes'}"
    log(f"  Bright Data: prispevky {rng} (max {limit}, vynechano {len(known)} znamych)")
    sid = _req("POST", "/trigger", params={
        "dataset_id": cfg["facebook"]["dataset_id"], "include_errors": "true", "format": "json",
    }, json=body).json().get("snapshot_id")
    if not sid:
        raise BrightDataError("odpoved bez snapshot_id")
    recs = _wait_and_download(sid, cfg, log)
    if recs is None:
        _save_pending(conn, source["id"], _load_pending(conn, source["id"]) + [sid])
        return None
    return _store(conn, source["id"], recs, sid, log), len(recs)


def verify(conn, url: str, cfg: dict) -> str:
    """Overeni noveho profilu: stahne 1 prispevek (1 zaznam). Vraci nazev stranky, jinak vyjimka.

    Prispevek se neuklada – uvodni 14denni stazeni probehne pri prvnim behu.
    """
    body = [{"url": url, "num_of_posts": 1, "start_date": "", "end_date": "", "posts_to_not_include": []}]
    sid = _req("POST", "/trigger", params={
        "dataset_id": cfg["facebook"]["dataset_id"], "include_errors": "true", "format": "json",
    }, json=body).json().get("snapshot_id")
    if not sid:
        raise BrightDataError("odpoved bez snapshot_id")
    recs = _wait_and_download(sid, cfg, lambda m: None)
    if recs is None:
        raise BrightDataError("overeni nestihlo dobehnout, zkus to znovu")
    add_usage(conn, PROVIDER, len([r for r in recs if not (r.get("error") or r.get("error_code"))]), month_key())
    for r in recs:
        if r.get("error") or r.get("error_code"):
            raise BrightDataError(f"{r.get('error_code')}: {r.get('error')}")
        name = r.get("page_name") or r.get("user_username_raw")
        if name:
            return name
    raise BrightDataError("profil nevratil zadny prispevek (neni verejny, nebo je prazdny)")
