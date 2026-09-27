"""Vrati vsechny prispevky ke znovuzpracovani (napr. po zmene extrakce).

Smaze jejich extrakce, embeddingy, pary a AI posouzeni; hodnoceni palcem (feedback) zustava.
Pak staci spustit beh (run_nightly.py nebo tlacitko ve webu).
"""
from app.config import load_config
from app.db import connect

if __name__ == "__main__":
    conn = connect(load_config())
    ids = "SELECT id FROM items WHERE kind = 'post'"
    for table, col in (("judgements", "post_id"), ("matches", "post_id"), ("embeddings", "item_id"), ("extractions", "item_id")):
        conn.execute(f"DELETE FROM {table} WHERE {col} IN ({ids})")
    n = conn.execute("UPDATE items SET status = 'new', error = NULL, scored_at = NULL WHERE kind = 'post'").rowcount
    conn.commit()
    print(f"Pripraveno k novemu zpracovani: {n} prispevku")
