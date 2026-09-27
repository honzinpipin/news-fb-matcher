"""Vrstva 1: AI extrakce (shrnuti, teze, entity, klicova slova, cisla, obrazky) + embeddingy."""
import base64
import json
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from . import ai
from .config import ROOT, now_iso
from .db import active_sources, jl

SYSTEM = """Jsi analytik textu. Z textu (zpravodajsky clanek nebo prispevek na socialni siti) vytahni:
- summary: shrnuti v 1–3 vetach, cesky, vecne, bez hodnoceni. U prispevku s obrazkem shrn
  prispevek jako celek (popisek + obrazek).
- main_claim: hlavni tvrzeni nebo myslenka v jedne vete.
- entities: konkretni osoby, organizace, instituce, politicke strany, mista, zakony, udalosti, ktere
  jsou v textu nebo na obrazku vyslovne uvedene. VZDY v 1. padu (zakladnim tvaru) a v plnem zneni,
  napr. "Andrej Babiš" (ne "Babišovi"), "Ministerstvo financí". Max 15.
- keywords: 3–10 vecnych klicovych slov v zakladnim tvaru, malymi pismeny (napr. "daň", "zdražení").
  Nepis obecna slova jako "situace", "člověk".
- numbers: konkretni cisla, castky, procenta a data, jak jsou uvedena (napr. "15 %", "3,2 mld. Kč",
  "1. ledna"). Kdyz zadna nejsou, prazdne pole.
- image_text: doslovny prepis veskereho textu na obrazcich (nadpisy, bubliny, transparenty,
  loga, napisy na obleceni). Bez obrazku prazdny retezec.
- image_description: 1–2 vety, co obrazek ukazuje (karikatura, fotomontaz, snimek clanku, graf...)
  a jaka je jeho pointa. Bez obrazku prazdny retezec.
- inferred_persons: verejne zname osoby, na ktere prispevek MIRI, i kdyz jejich jmeno neni napsane.
  Osobu NEURCUJ podle obliceje ani vzhledu. Odvod ji jen z kontextovych vodítek: prezdivka znama
  z verejneho prostoru, slogan nebo heslo spojene s konkretnim politikem, logo a nazev strany,
  funkce ("pan premier"), narazka v popisku. Uved jen tehdy, kdyz je vodítko dostatecne
  jednoznacne. cue = presne vodítko, podle ktereho jsi osobu urcil. Kdyz si nejsi jisty, vynech.
Nic nevymyslej. Co v textu ani na obrazku neni, neuvadej."""

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "main_claim": {"type": "string"},
        "entities": {"type": "array", "items": {"type": "string"}},
        "keywords": {"type": "array", "items": {"type": "string"}},
        "numbers": {"type": "array", "items": {"type": "string"}},
        "image_text": {"type": "string"},
        "image_description": {"type": "string"},
        "inferred_persons": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "cue": {"type": "string"}},
                "required": ["name", "cue"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "main_claim", "entities", "keywords", "numbers",
                 "image_text", "image_description", "inferred_persons"],
    "additionalProperties": False,
}


def image_parts(item) -> list[dict]:
    """Ulozene obrazky prispevku jako casti zpravy pro model."""
    parts = []
    for rel in jl(item["images"]):
        path = ROOT / rel
        if path.exists():
            b64 = base64.b64encode(path.read_bytes()).decode()
            parts.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    return parts


def _prompt(item, max_chars: int) -> str | list:
    kind = "Zpravodajsky clanek" if item["kind"] == "article" else "Prispevek na Facebooku"
    title = f"Titulek: {item['title']}\n" if item["title"] else ""
    text = f"{kind}\n{title}Text:\n{item['text'][:max_chars]}"
    images = image_parts(item)
    if not images:
        return text
    return [{"type": "text", "text": text + f"\n\nK prispevku patri {len(images)} obrazek/obrazky:"}, *images]


def merged_entities(res: dict) -> list[str]:
    """Vyslovne entity + osoby odvozene z kontextu (bez duplicit)."""
    out, seen = [], set()
    for name in res["entities"] + [p["name"] for p in res["inferred_persons"]]:
        key = name.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(name.strip())
    return out


def extract_new(conn, cfg: dict, log) -> int:
    ecfg = cfg["extract"]
    # Jen polozky zdroju, ktere jsou zapnute a v nejake vazbe (ostatni nic nestoji).
    ids = [r["id"] for r in active_sources(conn, "news") + active_sources(conn, "facebook")]
    items = conn.execute(
        f"""SELECT * FROM items WHERE status = 'new' AND source_id IN ({",".join("?" * len(ids)) or "NULL"})
            ORDER BY published_at DESC LIMIT ?""",
        (*ids, ecfg["max_per_run"]),
    ).fetchall()

    todo = []
    for it in items:
        if len(it["text"].strip()) < ecfg["min_chars"] and not jl(it["images"]):
            conn.execute("UPDATE items SET status = 'skipped', error = 'prilis kratky text' WHERE id = ?", (it["id"],))
        else:
            todo.append(it)
    conn.commit()

    def work(it):
        try:
            return it, ai.chat_json(cfg, SYSTEM, _prompt(it, ecfg["max_input_chars"]), SCHEMA, "extrakce"), None
        except ai.AIError as e:
            return it, None, str(e)

    done = 0
    with ThreadPoolExecutor(max_workers=ecfg["workers"]) as pool:
        for it, res, err in pool.map(work, todo):
            if err:
                conn.execute("UPDATE items SET status = 'error', error = ? WHERE id = ?", (err, it["id"]))
                log(f"  extrakce #{it['id']} selhala: {err}")
            else:
                conn.execute(
                    """INSERT OR REPLACE INTO extractions
                       (item_id, summary, main_claim, entities, keywords, numbers,
                        image_text, image_description, inferred_persons, model, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (it["id"], res["summary"], res["main_claim"],
                     json.dumps(merged_entities(res), ensure_ascii=False),
                     json.dumps(res["keywords"], ensure_ascii=False),
                     json.dumps(res["numbers"], ensure_ascii=False),
                     res["image_text"], res["image_description"],
                     json.dumps(res["inferred_persons"], ensure_ascii=False),
                     cfg["openai"]["model"], now_iso()),
                )
                conn.execute("UPDATE items SET status = 'extracted', error = NULL WHERE id = ?", (it["id"],))
                done += 1
            conn.commit()
    return done


def _unit(v) -> bytes:
    a = np.asarray(v, dtype=np.float32)
    n = np.linalg.norm(a)
    return (a / n if n else a).tobytes()


def embed_new(conn, cfg: dict, log, batch: int = 64) -> int:
    rows = conn.execute(
        """SELECT i.id, i.title, i.text, e.summary, e.main_claim, e.image_text, e.image_description
           FROM items i JOIN extractions e ON e.item_id = i.id
           WHERE i.status = 'extracted'"""
    ).fetchall()
    model = cfg["openai"]["embedding_model"]
    done = 0
    for start in range(0, len(rows), batch):
        chunk = rows[start:start + batch]
        # Dva vektory na polozku: shrnuti+teze a surovy text (zacatek) vcetne textu z obrazku.
        texts = []
        for r in chunk:
            texts.append(f"{r['summary']} {r['main_claim']}")
            raw = "\n".join(x for x in (r["title"], r["text"], r["image_text"], r["image_description"]) if x)
            texts.append(raw[:6000] or r["summary"])
        try:
            vecs = ai.embed(cfg, texts)
        except ai.AIError as e:
            log(f"  embeddingy selhaly: {e}")
            break
        for i, r in enumerate(chunk):
            for field, v in (("summary", vecs[2 * i]), ("text", vecs[2 * i + 1])):
                conn.execute(
                    "INSERT OR REPLACE INTO embeddings (item_id, field, model, vector) VALUES (?, ?, ?, ?)",
                    (r["id"], field, model, _unit(v)),
                )
            conn.execute("UPDATE items SET status = 'ready' WHERE id = ?", (r["id"],))
            done += 1
        conn.commit()
    return done
