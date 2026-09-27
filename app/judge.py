"""Vrstva 3: AI posouzeni vsech paru z vrstvy 2 se skore nad prahem."""
import json
from concurrent.futures import ThreadPoolExecutor

from . import ai
from .config import now_iso, parse_iso
from .db import jl
from .extract import image_parts

VERDICTS = ["shoda", "souvisejici_tema", "nesouvisi"]
MATCH_TYPES = ["sdileni_clanku", "citace", "stejne_tvrzeni", "rozpor", "reakce", "stejne_tema", "zadny"]

SYSTEM = """Porovnavas zpravodajsky clanek s prispevkem na Facebooku a rozhodujes, jestli spolu souvisi.

verdict:
- "shoda": oba texty jsou o stejne konkretni udalosti, tvrzeni nebo informaci.
- "souvisejici_tema": stejne sirsi tema, ale ne stejna konkretni vec.
- "nesouvisi": podobnost je jen povrchni (spolecne slovo, obecne tema).

match_type (nejpresnejsi jeden):
- "sdileni_clanku": prispevek clanek primo sdili nebo na nej odkazuje.
- "citace": prispevek doslova preklada nebo cituje text clanku (nebo naopak).
- "stejne_tvrzeni": oba tvrdi totez (napr. stejna cisla, stejne oznameni).
- "rozpor": tykaji se stejne veci, ale tvrdi opak nebo si odporuji.
- "reakce": prispevek na udalost z clanku reaguje, komentuje ji nebo hodnoti.
- "stejne_tema": jen spolecne tema.
- "zadny": nesouvisi.

explanation: 1–3 vecne vety cesky, proc. shared_points: konkretni spolecne body (fakta, jmena,
cisla), max 5, prazdne pole kdyz zadne nejsou. Hodnot jen podle textu a obrazku, nic nedomyslej.
U prispevku s obrazkem (mem, karikatura) ber v uvahu i obsah obrazku. Osoby neurcuj podle obliceje,
jen podle napisu a kontextu."""

SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": VERDICTS},
        "match_type": {"type": "string", "enum": MATCH_TYPES},
        "explanation": {"type": "string"},
        "shared_points": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "match_type", "explanation", "shared_points"],
    "additionalProperties": False,
}


def select_candidates(conn, cfg: dict) -> tuple[list[tuple[int, int]], int]:
    """Vsechny dosud neposouzene pary se skore >= prah (nejlepsi prvni).

    Vraci (pary k posouzeni v tomto behu, kolik jich ceka celkem).
    """
    j = cfg["judge"]
    rows = conn.execute(
        """SELECT m.article_id, m.post_id FROM matches m
           JOIN items a ON a.id = m.article_id
           JOIN items p ON p.id = m.post_id
           JOIN source_links l ON l.news_id = a.source_id AND l.fb_id = p.source_id
           JOIN sources sn ON sn.id = l.news_id AND sn.enabled = 1
           JOIN sources sf ON sf.id = l.fb_id AND sf.enabled = 1
           LEFT JOIN judgements jd ON jd.article_id = m.article_id AND jd.post_id = m.post_id
           WHERE m.score >= ? AND jd.verdict IS NULL
           ORDER BY m.score DESC""",
        (j["min_score"],),
    ).fetchall()
    pairs = [(r["article_id"], r["post_id"]) for r in rows]
    return pairs[: j["max_per_run"]], len(pairs)


def _prompt(conn, cfg: dict, a_id: int, p_id: int) -> list:
    a = conn.execute("SELECT * FROM items WHERE id = ?", (a_id,)).fetchone()
    p = conn.execute("SELECT * FROM items WHERE id = ?", (p_id,)).fetchone()
    pe = conn.execute("SELECT * FROM extractions WHERE item_id = ?", (p_id,)).fetchone()
    m = conn.execute("SELECT rule FROM matches WHERE article_id = ? AND post_id = ?", (a_id, p_id)).fetchone()
    first = "prispevek" if parse_iso(p["published_at"]) < parse_iso(a["published_at"]) else "clanek"
    hint = {"link": "Prispevek obsahuje odkaz na tento clanek.",
            "citation": "Texty maji spolecne doslovne useky."}.get(m["rule"], "")
    post_extra = ""
    if pe and (pe["image_text"] or pe["image_description"]):
        persons = "; ".join(f"{x['name']} (vodítko: {x['cue']})" for x in jl(pe["inferred_persons"]))
        post_extra = (f"\nText na obrazku: {pe['image_text']}\nPopis obrazku: {pe['image_description']}"
                      + (f"\nOsoby odvozene z kontextu: {persons}" if persons else ""))
    text = (
        f"CLANEK (publikovan {a['published_at']}):\nTitulek: {a['title']}\n"
        f"{a['text'][: cfg['judge']['max_article_chars']]}\n\n"
        f"PRISPEVEK NA FACEBOOKU (publikovan {p['published_at']}):\n{p['text'][:4000]}{post_extra}\n\n"
        f"[Metadata, neni soucast textu – nezminuj v odpovedi: drive vysel {first}. {hint}]"
    )
    # Obrazky prispevku dostane model i primo (memy nesou hlavni obsah v obrazku).
    return [{"type": "text", "text": text}, *image_parts(p)]


def judge_new(conn, cfg: dict, log) -> int:
    pairs, waiting = select_candidates(conn, cfg)
    if not pairs:
        return 0
    if waiting > len(pairs):
        log(f"  ceka {waiting} paru, posuzuji {len(pairs)} (pojistka max_per_run), zbytek priste")
    prompts = [(a, p, _prompt(conn, cfg, a, p)) for a, p in pairs]

    def work(t):
        a, p, prompt = t
        try:
            return a, p, ai.chat_json(cfg, SYSTEM, prompt, SCHEMA, "posouzeni"), None
        except ai.AIError as e:
            return a, p, None, str(e)

    done = 0
    with ThreadPoolExecutor(max_workers=cfg["judge"]["workers"]) as pool:
        for a, p, res, err in pool.map(work, prompts):
            if err:
                log(f"  posouzeni {a}x{p} selhalo: {err}")
                continue
            conn.execute(
                """INSERT OR REPLACE INTO judgements
                   (article_id, post_id, verdict, match_type, explanation, shared_points, model, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (a, p, res["verdict"], res["match_type"], res["explanation"],
                 json.dumps(res["shared_points"], ensure_ascii=False), cfg["openai"]["model"], now_iso()),
            )
            conn.commit()
            done += 1
    return done
