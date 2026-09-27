"""Znovu stahne text clanku aktivnich zdroju (napr. po zmene content_selector).

Clanky, jejichz text se zmenil, se vrati ke zpracovani (extrakce, embeddingy, pary).
"""
import time

from app.config import load_config
from app.db import active_sources, connect
from app.sources.news import fetch_article

if __name__ == "__main__":
    cfg = load_config()
    conn = connect(cfg)
    srcs = {s["id"]: s for s in active_sources(conn, "news")}
    rows = [r for r in conn.execute("SELECT id, source_id, url, text FROM items WHERE kind = 'article'")
            if r["source_id"] in srcs]
    changed = failed = 0
    for n, r in enumerate(rows, 1):
        sc = srcs[r["source_id"]]
        try:
            _, text, origin = fetch_article(r["url"], sc["origin_attr"], sc["content_selector"])
        except Exception as e:
            failed += 1
            print(f"  {r['url']}: {e}")
            continue
        # Porovnani bez ohledu na bile znaky (samotne formatovani neni duvod k nove AI extrakci).
        if text and " ".join(text.split()) != " ".join(r["text"].split()):
            for table, col in (("judgements", "article_id"), ("matches", "article_id"),
                               ("embeddings", "item_id"), ("extractions", "item_id")):
                conn.execute(f"DELETE FROM {table} WHERE {col} = ?", (r["id"],))
            conn.execute(
                "UPDATE items SET text = ?, origin = COALESCE(?, origin), status = 'new', error = NULL, scored_at = NULL WHERE id = ?",
                (text, origin, r["id"]),
            )
            changed += 1
        conn.commit()
        if n % 100 == 0:
            print(f"  {n}/{len(rows)}, zmeneno {changed}")
        time.sleep(cfg["news"]["request_delay_s"])
    print(f"Hotovo: {len(rows)} clanku, zmeneno {changed}, chyb {failed}")
