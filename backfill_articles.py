"""Dotahne starsi clanky propojenych webu za obdobi a zpracuje je (extrakce, pary, AI posouzeni).

python backfill_articles.py --from 2026-09-20 [--to 2026-09-27] [--source 3] [--no-process]
Clanky hleda ve vsech sitemapach z robots.txt vcetne archivnich (*.xml.gz).
"""
import argparse
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from app import pipeline
from app.config import load_config
from app.db import active_sources, connect
from app.sources import news

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="d_from", required=True)
    ap.add_argument("--to", dest="d_to")
    ap.add_argument("--source", type=int)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--no-process", action="store_true")
    a = ap.parse_args()
    cfg = load_config()
    tz = ZoneInfo(cfg["app"]["timezone"])
    since = datetime.combine(date.fromisoformat(a.d_from), time.min, tz)
    until = datetime.combine(date.fromisoformat(a.d_to), time.max, tz) if a.d_to else datetime.now(tz)
    conn = connect(cfg)
    known = {r[0] for r in conn.execute("SELECT url FROM items WHERE kind = 'article'")}
    for src in active_sources(conn, "news"):
        if a.source and src["id"] != a.source:
            continue
        entries = [e for e in news.archive_entries(src["url"], since, until) if e["url"] not in known]
        print(f"{src['name']}: v obdobi chybi {len(entries)} clanku, stahuji…", flush=True)
        added = news.store_entries(conn, src, entries, cfg, lambda m: print(m, flush=True), workers=a.workers)
        print(f"{src['name']}: pridano {added}", flush=True)
    conn.close()
    if not a.no_process:
        # Zpracovani: beh opakujeme, dokud neco ceka (extrakce ma limit na beh).
        for i in range(5):
            res = pipeline.run(f"backfill-{i + 1}", cfg)
            print(res, flush=True)
            c = connect(cfg)
            ids = [s["id"] for s in active_sources(c, "news") + active_sources(c, "facebook")]
            pending = c.execute(
                f"SELECT COUNT(*) FROM items WHERE status IN ('new', 'extracted') AND source_id IN ({','.join('?' * len(ids)) or 'NULL'})",
                ids,
            ).fetchone()[0]
            c.close()
            if res.get("status") == "busy" or not pending:
                break
