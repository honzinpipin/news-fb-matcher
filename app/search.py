"""Fulltextove vyhledavani (SQLite FTS5) v clancich a prispevcich.

Index search_idx (rowid = items.id) obsahuje titulek, text + text z obrazku a AI data
(shrnuti, entity, klicova slova, odvozene osoby). Tokenizer ignoruje diakritiku a velikost pismen.

Syntaxe dotazu pro uzivatele:
  slovo            hleda slova zacinajici na "slovo" bez bezne koncovky (Babišovi -> Babiš*, najde vse)
  vice slov        musi byt obsazena vsechna
  "presna fraze"   presna fraze
  A NEBO B         kterekoli (funguje i OR)
  -slovo           vylouci
"""
import html
import re
import unicodedata

from markupsafe import Markup

TOKEN_RE = re.compile(r'-?"[^"]*"?|\S+')
# Nejcastejsi ceske koncovky (od nejdelsich); slovo se orizne a hleda se jako prefix,
# takze "Babišovi" najde i "Babiš", "Ukrajina" i "Ukrajině".
SUFFIXES = ("ovi", "ová", "ové", "ého", "ému", "ých", "ými", "ami", "ech", "ách", "em", "ou", "ům", "ím",
            "a", "e", "ě", "i", "í", "u", "y", "ý", "é", "á", "ů", "o")
MIN_STEM = 4
# Zkratky (STAN, ODS, NATO) se hledaji presne i s pady, ne jako prefix ("STAN" != "stanovisko").
ACRONYM_CASES = ("", "u", "em", "ovi", "a", "y", "ů")
WORD_RE = re.compile(r"\w+", re.UNICODE)


def stem(word: str) -> str:
    for suf in SUFFIXES:
        if word.lower().endswith(suf) and len(word) - len(suf) >= MIN_STEM:
            return word[: -len(suf)]
    return word


def rebuild(conn) -> int:
    """Znovu naplni index ze vsech polozek (tisice polozek = zlomek sekundy)."""
    conn.execute("DELETE FROM search_idx")
    conn.execute(
        """INSERT INTO search_idx (rowid, title, body, extra)
           SELECT i.id, COALESCE(i.title, ''),
                  i.text || ' ' || COALESCE(e.image_text, ''),
                  COALESCE(e.summary, '') || ' ' || COALESCE(e.main_claim, '') || ' ' ||
                  COALESCE(e.entities, '') || ' ' || COALESCE(e.keywords, '') || ' ' ||
                  COALESCE(e.inferred_persons, '') || ' ' || COALESCE(e.image_description, '')
           FROM items i LEFT JOIN extractions e ON e.item_id = i.id"""
    )
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM search_idx").fetchone()[0]


def ensure(conn) -> None:
    """Postavi index, pokud je prazdny (prvni spusteni po pridani vyhledavani)."""
    if not conn.execute("SELECT 1 FROM search_idx LIMIT 1").fetchone() and \
            conn.execute("SELECT 1 FROM items LIMIT 1").fetchone():
        rebuild(conn)


def _parse(q: str) -> tuple[list[list[str]], list[str]]:
    """Dotaz -> (skupiny pozitivnich vyrazu spojene AND, uvnitr skupiny OR; negativni vyrazy).

    Vyraz je bud fraze (tuple slov, presne), nebo slovo (prefix).
    """
    groups: list[list] = []
    negatives: list = []
    join_or = False
    for tok in TOKEN_RE.findall(q):
        if tok.upper() in ("NEBO", "OR"):
            join_or = bool(groups)
            continue
        neg = tok.startswith("-") and len(tok) > 1
        tok = tok[1:] if neg else tok
        if tok.startswith('"'):
            words = WORD_RE.findall(tok)
            term = ("phrase", words) if words else None
        else:
            words = WORD_RE.findall(tok)
            if len(words) == 1 and words[0].isupper() and 2 <= len(words[0]) <= 5:
                term = ("acronym", words)
            else:
                if len(words) == 1:
                    words = [stem(words[0])]
                term = ("prefix", words) if words else None
        if not term:
            continue
        if neg:
            negatives.append(term)
        elif join_or:
            groups[-1].append(term)
        else:
            groups.append([term])
        join_or = False
    return groups, negatives


def _fts_term(term) -> str:
    kind, words = term
    if kind == "acronym":
        return "(" + " OR ".join(f'"{words[0]}{c}"' for c in ACRONYM_CASES) + ")"
    phrase = '"' + " ".join(words) + '"'
    return phrase if kind == "phrase" else phrase + "*"


def to_fts(q: str) -> str | None:
    """Uzivatelsky dotaz -> dotaz pro FTS5 MATCH (None = nic k hledani)."""
    groups, negatives = _parse(q or "")
    if not groups:
        return None
    expr = " AND ".join("(" + " OR ".join(_fts_term(t) for t in g) + ")" for g in groups)
    if negatives:
        expr = f"({expr}) NOT (" + " OR ".join(_fts_term(t) for t in negatives) + ")"
    return expr


def fold(s: str) -> str:
    """Bez diakritiky a malymi pismeny, znak po znaku (delka textu se nemeni)."""
    return "".join(unicodedata.normalize("NFD", ch)[0] if ch.strip() else ch for ch in s).lower()


def highlight(text: str | None, q: str | None) -> Markup:
    """Zvyrazni hledana slova (<mark>), bez ohledu na diakritiku; prefixy od zacatku slova."""
    text = text or ""
    groups, _ = _parse(q or "")
    patterns = []
    for g in groups:
        for kind, words in g:
            folded = [re.escape(fold(w)) for w in words]
            p = r"\W+".join(folded)
            if kind == "acronym":
                p += "(?:" + "|".join(re.escape(fold(c)) for c in ACRONYM_CASES if c) + ")?"
            patterns.append(r"\b" + p + (r"\w*" if kind == "prefix" else r"\b"))
    if not patterns:
        return Markup(html.escape(text))
    rx = re.compile("|".join(patterns))
    out, last = [], 0
    for m in rx.finditer(fold(text)):
        out.append(html.escape(text[last:m.start()]))
        out.append("<mark>" + html.escape(text[m.start():m.end()]) + "</mark>")
        last = m.end()
    out.append(html.escape(text[last:]))
    return Markup("".join(out))


def snippet(text: str | None, q: str | None, width: int = 220) -> Markup:
    """Vyrez textu kolem prvniho nalezu se zvyraznenim."""
    text = text or ""
    groups, _ = _parse(q or "")
    folded = fold(text)
    pos = -1
    for g in groups:
        for _, words in g:
            m = re.search(r"\b" + re.escape(fold(words[0])), folded)
            if m and (pos < 0 or m.start() < pos):
                pos = m.start()
    if pos < 0:
        cut = text[:width]
        return highlight(cut + ("…" if len(text) > width else ""), q)
    start = max(0, pos - width // 3)
    cut = text[start:start + width]
    return highlight(("…" if start else "") + cut + ("…" if start + width < len(text) else ""), q)
