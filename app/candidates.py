"""Vrstva 2: skorovani paru clanek x prispevek bez AI.

skore = w_sem*S1 + w_ent*S2 + w_kw*S3 + w_num*S4 + w_time*S5
  S1 vyznamova podobnost (embeddingy, preskalovana kosinova podobnost)
  S2 shoda entit (IDF vahy, vuci mensi mnozine; obecne entity jako Rusko se nepocitaji)
  S3 shoda klicovych slov (totez)
  S4 shoda konkretnich cisel
  S5 casova blizkost exp(-|dni|/tau)
  S6 doslovna shoda (spolecne n-tice slov) -> pravidlo "citation"
Pevna pravidla: odkaz na clanek v prispevku -> 1.0; citace -> min. 0.8.
"""
import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import parse_qs, unquote, urlparse

import numpy as np

from .config import now_iso, parse_iso
from .db import jl, links

WORD_RE = re.compile(r"\w+", re.UNICODE)


@dataclass
class Item:
    id: int
    source_id: int
    kind: str
    url: str
    published: object
    is_new: bool
    entities: set
    keywords: set
    numbers: set
    links: set
    shingles: set
    v_summary: np.ndarray
    v_text: np.ndarray


def norm_term(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().strip(".,;:!?\"'„“()").lower())


def norm_number(s: str) -> str:
    s = s.lower().replace("\xa0", "").replace(" ", "").replace(",", ".")
    s = s.replace("procent", "%").replace("korun", "kč")
    return s.rstrip(".")


def norm_url(u: str) -> str:
    p = urlparse(u.strip())
    # Presmerovani FB: l.facebook.com/l.php?u=<skutecna adresa>
    if p.netloc.endswith("facebook.com") and p.path.startswith("/l.php"):
        target = parse_qs(p.query).get("u")
        if target:
            p = urlparse(unquote(target[0]))
    host = p.netloc.lower().removeprefix("www.")
    return f"{host}{p.path.rstrip('/')}"


def shingles(text: str, n: int) -> set:
    words = WORD_RE.findall(text.lower())
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def idf_table(conn, column: str, norm) -> tuple[dict, float, int]:
    df = Counter()
    n = 0
    for (raw,) in conn.execute(f"SELECT {column} FROM extractions"):
        n += 1
        df.update({norm(t) for t in jl(raw) if t.strip()})
    idf = {t: math.log((n + 1) / (c + 1)) + 1 for t, c in df.items()}
    return idf, math.log(n + 1) + 1, n  # druha hodnota = idf pro neznamy termin


def specificity(idf: dict, n: int, cfg: dict) -> dict:
    """0 = obecna entita (Rusko, NATO...), 1 = specificka (konkretni jmeno). Linearne podle IDF."""
    m = cfg["matching"]
    common = math.log((n + 1) / (m["entity_common_share"] * n + 1)) + 1
    rare = math.log((n + 1) / (m["entity_rare_df"] + 1)) + 1
    if rare <= common:  # malo dat
        return {}
    return {t: max(0.0, min(1.0, (v - common) / (rare - common))) for t, v in idf.items()}


def weighted_overlap(a: set, b: set, idf: dict, default: float, spec: dict | None = None) -> float:
    """Vahovany prunik vuci mensi mnozine. Se spec se obecne terminy v pruniku skoro nepocitaji."""
    if not a or not b:
        return 0.0
    w = lambda s: sum(idf.get(t, default) for t in s)
    common = sum(idf.get(t, default) * (spec.get(t, 1.0) if spec is not None else 1.0) for t in a & b)
    return common / min(w(a), w(b))


def rescale(x: float, low: float, high: float) -> float:
    return max(0.0, min(1.0, (x - low) / (high - low)))


def score_pair(a: Item, p: Item, cfg: dict, idf_e, idf_k) -> dict | None:
    m = cfg["matching"]
    w = m["weights"]

    cos_raw = float(max(a.v_summary @ p.v_summary, a.v_summary @ p.v_text))
    s1 = rescale(cos_raw, m["cos_low"], m["cos_high"])
    s2 = weighted_overlap(a.entities, p.entities, *idf_e)
    s3 = weighted_overlap(a.keywords, p.keywords, *idf_k)
    s4 = (len(a.numbers & p.numbers) / min(len(a.numbers), len(p.numbers))
          if a.numbers and p.numbers else 0.0)
    days = abs((a.published - p.published).total_seconds()) / 86400
    s5 = math.exp(-days / m["time_tau_days"])
    common = len(a.shingles & p.shingles)

    rule = None
    if norm_url(a.url) in p.links:
        rule = "link"
    elif common >= m["citation_min_shingles"]:
        rule = "citation"

    if rule is None and s1 < m["drop_semantic_below"] and s2 == 0:
        return None

    score = (w["semantic"] * s1 + w["entities"] * s2 + w["keywords"] * s3
             + w["numbers"] * s4 + w["time"] * s5) / sum(w.values())
    if rule == "link":
        score = 1.0
    elif rule == "citation":
        score = max(score, m["citation_min_score"])

    if rule is None and score < m["store_min_score"]:
        return None
    return {"score": round(score, 4), "s_sem": s1, "s_ent": s2, "s_kw": s3, "s_num": s4,
            "s_time": s5, "shingles": common, "cos_raw": cos_raw, "rule": rule}


def _load_items(conn, cfg: dict, since: str, until: str) -> list[Item]:
    n = cfg["matching"]["shingle_size"]
    rows = conn.execute(
        """SELECT i.id, i.source_id, i.kind, i.url, i.text, i.links, i.published_at, i.scored_at,
                  e.entities, e.keywords, e.numbers, e.image_text, vs.vector AS vs, vt.vector AS vt
           FROM items i
           JOIN extractions e ON e.item_id = i.id
           JOIN embeddings vs ON vs.item_id = i.id AND vs.field = 'summary'
           JOIN embeddings vt ON vt.item_id = i.id AND vt.field = 'text'
           WHERE i.status = 'ready' AND i.published_at BETWEEN ? AND ?""",
        (since, until),
    ).fetchall()
    out = []
    for r in rows:
        out.append(Item(
            id=r["id"], source_id=r["source_id"], kind=r["kind"], url=r["url"], published=parse_iso(r["published_at"]),
            is_new=r["scored_at"] is None,
            entities={norm_term(t) for t in jl(r["entities"]) if t.strip()},
            keywords={norm_term(t) for t in jl(r["keywords"]) if t.strip()},
            numbers={norm_number(t) for t in jl(r["numbers"]) if t.strip()},
            links={norm_url(u) for u in jl(r["links"])},
            shingles=shingles(r["text"] + "\n" + r["image_text"], n),
            v_summary=np.frombuffer(r["vs"], dtype=np.float32),
            v_text=np.frombuffer(r["vt"], dtype=np.float32),
        ))
    return out


def score_new(conn, cfg: dict, log) -> int:
    """Inkrementalne: paruje jen dvojice, kde je aspon jedna polozka nova."""
    new = conn.execute(
        "SELECT MIN(published_at), MAX(published_at), COUNT(*) FROM items WHERE status = 'ready' AND scored_at IS NULL"
    ).fetchone()
    if not new[2]:
        return 0
    window = timedelta(days=cfg["matching"]["window_days"])
    since = (parse_iso(new[0]) - window).isoformat()
    until = (parse_iso(new[1]) + window).isoformat()

    items = _load_items(conn, cfg, since, until)
    articles = [i for i in items if i.kind == "article"]
    posts = [i for i in items if i.kind == "post"]
    idf, default, n = idf_table(conn, "entities", norm_term)
    idf_e = (idf, default, specificity(idf, n, cfg))
    idf_k = idf_table(conn, "keywords", norm_term)[:2]

    linked = links(conn)
    stored = 0
    for a in articles:
        for p in posts:
            if (a.source_id, p.source_id) not in linked:
                continue
            if not (a.is_new or p.is_new) or abs(a.published - p.published) > window:
                continue
            s = score_pair(a, p, cfg, idf_e, idf_k)
            if not s:
                continue
            conn.execute(
                """INSERT OR REPLACE INTO matches
                   (article_id, post_id, score, s_sem, s_ent, s_kw, s_num, s_time, shingles, cos_raw, rule, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (a.id, p.id, s["score"], s["s_sem"], s["s_ent"], s["s_kw"], s["s_num"], s["s_time"],
                 s["shingles"], s["cos_raw"], s["rule"], now_iso()),
            )
            stored += 1

    ts = now_iso()
    conn.executemany("UPDATE items SET scored_at = ? WHERE id = ?", [(ts, i.id) for i in items if i.is_new])
    conn.commit()
    log(f"  porovnano {len(articles)} clanku x {len(posts)} prispevku, ulozeno {stored} paru")
    return stored
