"""Obecny zdroj zprav: najde feed webu a stahuje z nej nove clanky.

Nic neni psane na miru konkretnimu webu. Poradi hledani feedu:
  0) sama zadana url, pokud je to feed
  1) news sitemap z robots.txt (obsahuje vsechny clanky za 48 h -> nejlepsi)
  2) RSS/Atom z <link rel="alternate"> na hlavni strance
  3) obvykle cesty RSS (/rss, /feed, /rss.xml, ...)
  4) ostatni sitemapy z robots.txt (nejdriv "article", "post"...; bez obrazku, stitku, kategorii)
     a obvykle cesty sitemap – jen pokud obsahuji cerstve polozky
Zjisteny feed se ulozi do DB. Kdyz prestane fungovat, hleda se znovu.
"""
import gzip
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

import requests
import trafilatura
from bs4 import BeautifulSoup

from ..config import now_iso, parse_iso, to_iso
from ..db import get_state, set_state

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) news-fb-matcher/1.0 (osobni pouziti)"
COMMON_FEED_PATHS = ["/rss", "/feed", "/rss.xml", "/feed.xml", "/atom.xml", "/rss/", "/feed/"]
COMMON_SITEMAP_PATHS = ["/news-sitemap.xml", "/sitemap-news.xml", "/sitemap_index.xml", "/sitemap.xml"]
SITEMAP_SKIP = ("image", "video", "tag", "categor", "author", "list", "page", "static", "product")
SITEMAP_ARTICLE_HINTS = ("article", "post", "clan", "entry", "zprav", "story")


def _get(url: str, timeout: int = 20) -> requests.Response:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    r.raise_for_status()
    return r


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _child(el: ET.Element, name: str) -> ET.Element | None:
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _find_deep(el: ET.Element, name: str) -> ET.Element | None:
    for c in el.iter():
        if _local(c.tag) == name:
            return c
    return None


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def _parse_date(s: str) -> datetime | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None


INVISIBLE_RE = re.compile("[︎️​-‍⁠﻿]")


def clean(s: str) -> str:
    # Variacni selektory a zero-width znaky (zbytky emoji z Telegramu).
    return INVISIBLE_RE.sub("", s or "").strip()


def _html_to_text(s: str) -> str:
    return clean(BeautifulSoup(s or "", "html.parser").get_text(" ", strip=True))


def parse_feed(url: str, _depth: int = 0) -> list[dict]:
    """RSS 2.0, Atom, sitemap (urlset) i sitemap index -> [{url, title, published, summary}]."""
    raw = _get(url, timeout=60).content
    if raw[:2] == b"\x1f\x8b":  # gzip (archivni sitemapy *.xml.gz)
        raw = gzip.decompress(raw)
    root = ET.fromstring(raw)
    kind = _local(root.tag)
    out = []

    if kind == "rss":
        channel = _child(root, "channel")
        for it in channel if channel is not None else []:
            if _local(it.tag) != "item":
                continue
            out.append({
                "url": _text(_child(it, "link")),
                "title": _html_to_text(_text(_child(it, "title"))),
                "published": _parse_date(_text(_child(it, "pubDate"))),
                "summary": _html_to_text(_text(_child(it, "description"))),
            })

    elif kind == "feed":  # Atom
        for e in root:
            if _local(e.tag) != "entry":
                continue
            link = ""
            for c in e:
                if _local(c.tag) == "link" and c.get("rel", "alternate") == "alternate":
                    link = c.get("href", "")
                    break
            out.append({
                "url": link,
                "title": _html_to_text(_text(_child(e, "title"))),
                "published": _parse_date(_text(_child(e, "published")) or _text(_child(e, "updated"))),
                "summary": _html_to_text(_text(_child(e, "summary")) or _text(_child(e, "content"))),
            })

    elif kind == "urlset":
        for u in root:
            if _local(u.tag) != "url":
                continue
            out.append({
                "url": _text(_child(u, "loc")),
                "title": clean(_text(_find_deep(u, "title"))),
                "published": _parse_date(_text(_find_deep(u, "publication_date")) or _text(_child(u, "lastmod"))),
                "summary": "",
            })

    elif kind == "sitemapindex" and _depth == 0:
        # Vezmeme 2 nejcerstvejsi pod-sitemapy.
        subs = [(_parse_date(_text(_child(s, "lastmod"))), _text(_child(s, "loc"))) for s in root]
        subs = [s for s in subs if s[1]]
        subs.sort(key=lambda s: s[0] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        for _, loc in subs[:2]:
            out.extend(parse_feed(loc, _depth + 1))

    return [e for e in out if e["url"]]


def _works(url: str, fresh_days: int | None = None) -> bool:
    """Feed jde precist a ma polozky (s fresh_days: aspon jednu z poslednich N dni)."""
    try:
        entries = parse_feed(url)
    except Exception:
        return False
    if fresh_days is None:
        return len(entries) > 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=fresh_days)
    return any(e["published"] and e["published"] >= cutoff for e in entries)


def _path(url: str) -> str:
    """Jen cesta adresy (bez domeny) – "news" v domene news-pravda.com neznamena news sitemapu."""
    return urlparse(url).path.lower()


def _try(url: str, errors: list, fresh_days: int | None = None) -> bool:
    """Jako _works, ale duvod neuspechu zapise do errors (pro srozumitelnou hlasku)."""
    try:
        entries = parse_feed(url)
    except Exception as e:
        errors.append(f"{url}: {type(e).__name__}: {str(e)[:120]}")
        return False
    if not entries:
        errors.append(f"{url}: zadne polozky")
        return False
    if fresh_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=fresh_days)
        if not any(e["published"] and e["published"] >= cutoff for e in entries):
            errors.append(f"{url}: zadne polozky za poslednich {fresh_days} dni")
            return False
    return True


def discover_feed(site_url: str, errors: list | None = None) -> str | None:
    errors = [] if errors is None else errors
    if _try(site_url, []):  # zadana adresa uz je feed (chybu neukladame – bezne je to HTML)
        return site_url

    parsed = urlparse(site_url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    # 1) news sitemap z robots.txt
    sitemaps: list[str] = []
    try:
        robots = _get(base + "/robots.txt").text
        sitemaps = [l.split(":", 1)[1].strip() for l in robots.splitlines()
                    if l.lower().startswith("sitemap:")]
    except Exception as e:
        errors.append(f"{base}/robots.txt: {type(e).__name__}: {str(e)[:120]}")
    for sm in sitemaps:
        if "news" in _path(sm) and _try(sm, errors):
            return sm

    # 2) <link rel="alternate" type="application/rss+xml"> na hlavni strance
    try:
        soup = BeautifulSoup(_get(site_url).text, "html.parser")
        for link in soup.find_all("link", rel="alternate"):
            t = (link.get("type") or "").lower()
            if ("rss" in t or "atom" in t) and link.get("href"):
                href = urljoin(site_url, link["href"])
                if _try(href, errors):
                    return href
    except Exception as e:
        errors.append(f"{site_url}: {type(e).__name__}: {str(e)[:120]}")

    # 3) obvykle cesty RSS
    for p in COMMON_FEED_PATHS:
        if _try(base + p, []):  # vetsinou neexistuji – do hlasky nepatri
            return base + p

    # 4) ostatni sitemapy (clanky) – jen s cerstvymi polozkami, aby to nebyl archiv/kategorie
    others = [sm for sm in sitemaps if "news" not in _path(sm) and not any(k in _path(sm) for k in SITEMAP_SKIP)]
    others.sort(key=lambda sm: not any(k in _path(sm) for k in SITEMAP_ARTICLE_HINTS))
    for sm in others:
        if _try(sm, errors, fresh_days=60):
            return sm
    for p in COMMON_SITEMAP_PATHS:
        if _try(base + p, [], fresh_days=60):
            return base + p
    return None


def fetch_article(url: str, origin_attr: str | None = None,
                  content_selector: str | None = None) -> tuple[str | None, str, str | None]:
    """Vraci (titulek, text, puvodni zdroj).

    content_selector = CSS selektor bloku s textem clanku (nepovinne). Kdyz na strance je,
    pouzije se jen on – obecna extrakce totiz u kratkych clanku sahne po postrannich blocich.
    Zdroj = hodnota origin_attr u prvniho prvku, ktery ho ma.
    """
    html = _get(url, timeout=30).text
    soup = BeautifulSoup(html, "html.parser") if (content_selector or origin_attr) else None
    body = soup.select_one(content_selector) if content_selector else None
    if body is not None:
        text = body.get_text("\n", strip=True)
    else:
        text = trafilatura.extract(html, include_comments=False, include_tables=False, favor_precision=True) or ""
    meta = trafilatura.extract_metadata(html)
    origin = None
    if origin_attr:
        el = soup.find(attrs={origin_attr: True})
        value = (el.get(origin_attr) or "").strip() if el is not None else ""
        origin = value if value.startswith(("http://", "https://")) else None
    return (clean(meta.title) if meta and meta.title else None), clean(text), origin


def backfill(conn, source, cfg: dict, log) -> int:
    """Dotazeni obdobi po nove vazbe (sources.state["backfill_from"]) z archivnich sitemap webu.

    Po dokonceni se ulozi covered_from = od kdy ma zdroj kompletni clanky.
    """
    state = get_state(conn, source["id"])
    if not state.get("backfill_from"):
        return 0
    ncfg = cfg["news"]
    since = parse_iso(state["backfill_from"])
    day = since.astimezone(ZoneInfo(cfg["app"]["timezone"])).date()
    known = {r[0] for r in conn.execute("SELECT url FROM items WHERE kind = 'article'")}
    try:
        entries = [e for e in archive_entries(source["url"], since, datetime.now(timezone.utc))
                   if e["url"] not in known]
    except Exception as e:
        log(f"  dotazeni od {day} selhalo ({e}), zkusim pri dalsim behu")
        return 0
    entries = entries[: ncfg.get("backfill_max_articles", 5000)]
    log(f"  dotahuji clanky od {day}: {len(entries)} chybejicich")
    added = store_entries(conn, source, entries, cfg, log, workers=ncfg.get("backfill_workers", 4)) if entries else 0
    covered = min(filter(None, [state.get("covered_from"), state["backfill_from"]]))
    set_state(conn, source["id"], backfill_from=None, covered_from=covered)
    return added


def ingest(conn, source, cfg: dict, log) -> int:
    """Stahne nove clanky jednoho zdroje (vcetne pripadneho dotazeni po nove vazbe). Vraci pocet pridanych."""
    ncfg = cfg["news"]
    added = backfill(conn, source, cfg, log)
    feed = source["feed_url"]
    entries = None
    if feed:
        try:
            entries = parse_feed(feed)
        except Exception as e:
            log(f"  feed {feed} selhal ({e}), hledam znovu")
            entries = None
    if not entries:
        feed = discover_feed(source["url"])
        if not feed:
            log(f"  {source['name']}: nenasel jsem zadny feed na {source['url']}")
            return added
        log(f"  {source['name']}: pouzivam feed {feed}")
        conn.execute("UPDATE sources SET feed_url = ? WHERE id = ?", (feed, source["id"]))
        conn.commit()
        entries = parse_feed(feed)

    # U noveho webu uvodni obdobi (jako u FB), jinak jen posledni dny.
    has_items = conn.execute("SELECT 1 FROM items WHERE source_id = ? LIMIT 1", (source["id"],)).fetchone()
    days = ncfg["lookback_days"] if has_items else ncfg.get("first_lookback_days", 7)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    known = {r[0] for r in conn.execute("SELECT url FROM items WHERE kind = 'article'")}
    # Polozky bez data bereme jen z kratkych feedu (RSS); u velke sitemapy by to byl cely archiv.
    allow_undated = len(entries) <= 100
    fresh = [e for e in entries if e["url"] not in known
             and (e["published"] >= cutoff if e["published"] else allow_undated)]
    return added + store_entries(conn, source, fresh[: ncfg["max_new_per_run"]], cfg, log)


def store_entries(conn, source, entries: list[dict], cfg: dict, log, workers: int = 1) -> int:
    """Stahne texty clanku ze seznamu polozek feedu a ulozi je. Vraci pocet pridanych."""
    ncfg = cfg["news"]

    def work(e):
        try:
            res = fetch_article(e["url"], source["origin_attr"], source["content_selector"])
        except Exception as ex:
            return e, None, str(ex)
        time.sleep(ncfg["request_delay_s"])
        return e, res, None

    added = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for n, (e, res, err) in enumerate(pool.map(work, entries), 1):
            if err:
                log(f"  nelze stahnout {e['url']}: {err}")
                continue
            title, text, origin = res
            if workers > 1 and n % 200 == 0:
                log(f"  {n}/{len(entries)}")
            if len(text) < ncfg["min_text_chars"]:
                text = "\n".join(x for x in (text, e["summary"]) if x)
            published = e["published"] or datetime.now(timezone.utc)
            cur = conn.execute(
                """INSERT OR IGNORE INTO items (source_id, kind, url, title, text, origin, published_at, fetched_at)
                   VALUES (?, 'article', ?, ?, ?, ?, ?, ?)""",
                (source["id"], e["url"], e["title"] or title or e["url"], text, origin, to_iso(published), now_iso()),
            )
            added += cur.rowcount
            conn.commit()
    return added


def archive_entries(source_url: str, since: datetime, until: datetime) -> list[dict]:
    """Polozky ze vsech sitemap uvedenych v robots.txt (vcetne archivnich .gz) v obdobi since–until."""
    p = urlparse(source_url)
    robots = _get(f"{p.scheme}://{p.netloc}/robots.txt").text
    sitemaps = [l.split(":", 1)[1].strip() for l in robots.splitlines() if l.lower().startswith("sitemap:")]
    seen, out = set(), []
    for sm in sitemaps:
        if any(k in _path(sm) for k in SITEMAP_SKIP) or _path(sm).startswith("/en/"):
            continue
        try:
            entries = parse_feed(sm)
        except Exception:
            continue
        for e in entries:
            if e["published"] and since <= e["published"] <= until and e["url"] not in seen:
                seen.add(e["url"])
                out.append(e)
    return sorted(out, key=lambda e: e["published"])


def site_name(url: str) -> str:
    """Nazev webu z <title> hlavni stranky, jinak domena."""
    try:
        title = BeautifulSoup(_get(url).text, "html.parser").title
        if title and title.get_text(strip=True):
            name = clean(title.get_text(strip=True))
            # "42TČen - hlavní dnešní události…" -> "42TČen"
            for sep in (" | ", " - ", " – ", " — "):
                if sep in name and len(name.split(sep)[0]) >= 3:
                    name = name.split(sep)[0]
                    break
            return name[:80]
    except Exception:
        pass
    return urlparse(url).netloc.removeprefix("www.")


def check_site(url: str, content_selector: str | None = None, origin_attr: str | None = None) -> dict:
    """Kontrola pri pridavani webu: najde feed a stahne 1 clanek na ukazku.

    Vraci {feed, entries, sample: {url, title, text, origin}} nebo vyhodi ValueError.
    """
    try:
        _get(url)
    except Exception as e:
        if "ACCESS_DENIED" in str(e) or "TLSV1_ALERT" in str(e):
            raise ValueError(f"Spojení se stránkou {url} zablokoval antivirus nebo síťový filtr (např. ESET – "
                             "ochrana přístupu na web). Povol tuto doménu v jeho seznamu povolených adres, viz NAVOD.md.")
        raise ValueError(f"Stránku {url} nejde vůbec načíst ({type(e).__name__}: {str(e)[:150]}). "
                         "Zkontroluj adresu a připojení; web může blokovat i antivir nebo síť.")
    errors: list[str] = []
    feed = discover_feed(url, errors)
    if not feed:
        detail = "; ".join(errors[:4]) or "robots.txt neuvádí sitemapu a stránka neodkazuje na RSS"
        raise ValueError("Na webu jsem nenašel žádný feed (news sitemap, RSS, Atom ani sitemapu článků). "
                         f"Zkoušel jsem: {detail}. Můžeš zadat přímo adresu feedu.")
    entries = parse_feed(feed)
    sample = None
    for e in entries[:3]:
        try:
            title, text, origin = fetch_article(e["url"], origin_attr, content_selector)
            selector_found = None
            if content_selector:
                selector_found = BeautifulSoup(_get(e["url"], timeout=30).text, "html.parser").select_one(
                    content_selector) is not None
        except Exception:
            continue
        sample = {"url": e["url"], "title": e["title"] or title, "text": text, "origin": origin,
                  "selector_found": selector_found}
        break
    return {"feed": feed, "entries": len(entries), "sample": sample}
