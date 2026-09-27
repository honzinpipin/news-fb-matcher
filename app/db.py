import json
import sqlite3

from .config import ROOT, db_path, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id        INTEGER PRIMARY KEY,
    kind      TEXT NOT NULL CHECK (kind IN ('news', 'facebook')),
    name      TEXT NOT NULL,
    url       TEXT NOT NULL,
    feed_url  TEXT,               -- zadany nebo automaticky zjisteny feed
    enabled   INTEGER NOT NULL DEFAULT 1,
    UNIQUE (kind, url)
);

-- Clanky i prispevky. status: new -> extracted -> ready | skipped | error
CREATE TABLE IF NOT EXISTS items (
    id           INTEGER PRIMARY KEY,
    source_id    INTEGER NOT NULL REFERENCES sources (id),
    kind         TEXT NOT NULL CHECK (kind IN ('article', 'post')),
    url          TEXT NOT NULL,
    title        TEXT,
    text         TEXT NOT NULL DEFAULT '',
    links        TEXT NOT NULL DEFAULT '[]',   -- JSON, odkazy v prispevku
    published_at TEXT NOT NULL,                -- ISO UTC
    fetched_at   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'new',
    error        TEXT,
    scored_at    TEXT,                         -- NULL = jeste neparovano
    UNIQUE (kind, url)
);
CREATE INDEX IF NOT EXISTS ix_items_status ON items (status);
CREATE INDEX IF NOT EXISTS ix_items_pub ON items (kind, published_at);

-- Vrstva 1: AI extrakce
CREATE TABLE IF NOT EXISTS extractions (
    item_id    INTEGER PRIMARY KEY REFERENCES items (id) ON DELETE CASCADE,
    summary    TEXT NOT NULL,
    main_claim TEXT NOT NULL,
    entities   TEXT NOT NULL,   -- JSON
    keywords   TEXT NOT NULL,   -- JSON
    numbers    TEXT NOT NULL,   -- JSON
    model      TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- field: 'summary' (shrnuti + teze) | 'text' (surovy text)
CREATE TABLE IF NOT EXISTS embeddings (
    item_id INTEGER NOT NULL REFERENCES items (id) ON DELETE CASCADE,
    field   TEXT NOT NULL,
    model   TEXT NOT NULL,
    vector  BLOB NOT NULL,      -- float32, normalizovany na delku 1
    PRIMARY KEY (item_id, field)
);

-- Vrstva 2: skore paru
CREATE TABLE IF NOT EXISTS matches (
    article_id INTEGER NOT NULL REFERENCES items (id) ON DELETE CASCADE,
    post_id    INTEGER NOT NULL REFERENCES items (id) ON DELETE CASCADE,
    score      REAL NOT NULL,
    s_sem      REAL NOT NULL,
    s_ent      REAL NOT NULL,
    s_kw       REAL NOT NULL,
    s_num      REAL NOT NULL,
    s_time     REAL NOT NULL,
    shingles   INTEGER NOT NULL,
    cos_raw    REAL NOT NULL,
    rule       TEXT,            -- 'link' | 'citation' | NULL
    created_at TEXT NOT NULL,
    PRIMARY KEY (article_id, post_id)
);
CREATE INDEX IF NOT EXISTS ix_matches_score ON matches (score);

-- Vrstva 3: AI posouzeni
CREATE TABLE IF NOT EXISTS judgements (
    article_id    INTEGER NOT NULL,
    post_id       INTEGER NOT NULL,
    verdict       TEXT NOT NULL,
    match_type    TEXT NOT NULL,
    explanation   TEXT NOT NULL,
    shared_points TEXT NOT NULL,  -- JSON
    model         TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (article_id, post_id)
);

-- N9: hodnoceni uzivatele (stitky pro pozdejsi uceni vah)
CREATE TABLE IF NOT EXISTS feedback (
    article_id INTEGER NOT NULL,
    post_id    INTEGER NOT NULL,
    rating     INTEGER NOT NULL CHECK (rating IN (-1, 1)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (article_id, post_id)
);

-- Spotreba placenych sluzeb po mesicich (napr. zaznamy Bright Data)
CREATE TABLE IF NOT EXISTS provider_usage (
    month    TEXT NOT NULL,     -- YYYY-MM
    provider TEXT NOT NULL,
    records  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (month, provider)
);

-- Vazby: ktery zpravodajsky web se porovnava s kterym FB profilem
CREATE TABLE IF NOT EXISTS source_links (
    news_id    INTEGER NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
    fb_id      INTEGER NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (news_id, fb_id)
);

-- Fulltext (rowid = items.id); bez diakritiky a velikosti pismen. Plni ho search.rebuild().
CREATE VIRTUAL TABLE IF NOT EXISTS search_idx USING fts5(
    title, body, extra, tokenize = "unicode61 remove_diacritics 2"
);

-- Vytvorene PDF reporty (shrnuti za obdobi)
CREATE TABLE IF NOT EXISTS reports (
    id           INTEGER PRIMARY KEY,
    period_start TEXT NOT NULL,   -- ISO UTC
    period_end   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    trigger      TEXT NOT NULL,   -- auto | manual
    path         TEXT NOT NULL,   -- relativne k ROOT
    stats        TEXT NOT NULL DEFAULT '{}'
);

-- Cache informaci o Telegram kanalech (zdroje clanku)
CREATE TABLE IF NOT EXISTS telegram_channels (
    username    TEXT PRIMARY KEY,
    title       TEXT,
    subscribers INTEGER,
    description TEXT,
    chat_id     INTEGER,
    fetched_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    trigger     TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,   -- running | ok | error
    summary     TEXT,
    log         TEXT NOT NULL DEFAULT ''
);
"""


def connect(cfg: dict) -> sqlite3.Connection:
    path = db_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    _import_config_sources(conn, cfg)
    return conn


# Sloupce pridane po prvni verzi schematu: (tabulka, sloupec, definice)
MIGRATIONS = [
    ("items", "external_id", "TEXT"),   # ID prispevku u poskytovatele (Bright Data post_id)
    ("items", "origin", "TEXT"),        # puvodni zdroj clanku (napr. Telegram kanal)
    ("items", "images", "TEXT NOT NULL DEFAULT '[]'"),  # JSON, lokalni cesty k obrazkum prispevku
    ("extractions", "image_text", "TEXT NOT NULL DEFAULT ''"),
    ("extractions", "image_description", "TEXT NOT NULL DEFAULT ''"),
    ("extractions", "inferred_persons", "TEXT NOT NULL DEFAULT '[]'"),  # JSON [{name, cue}]
    ("sources", "origin_attr", "TEXT"),        # HTML atribut s puvodnim zdrojem clanku
    ("sources", "content_selector", "TEXT"),   # CSS selektor bloku s textem clanku
    ("sources", "state", "TEXT NOT NULL DEFAULT '{}'"),  # JSON: stav zdroje (backfill FB, overeni...)
    ("sources", "created_at", "TEXT"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, col, decl in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.commit()


def add_usage(conn: sqlite3.Connection, provider: str, records: int, month: str) -> None:
    conn.execute(
        """INSERT INTO provider_usage (month, provider, records) VALUES (?, ?, ?)
           ON CONFLICT (month, provider) DO UPDATE SET records = records + excluded.records""",
        (month, provider, records),
    )
    conn.commit()


def get_usage(conn: sqlite3.Connection, provider: str, month: str) -> int:
    row = conn.execute(
        "SELECT records FROM provider_usage WHERE month = ? AND provider = ?", (month, provider)
    ).fetchone()
    return row["records"] if row else 0


def _import_config_sources(conn: sqlite3.Connection, cfg: dict) -> None:
    """Jednorazovy prevod zdroju ze stareho config.toml ([[sources]]) do DB a jejich propojeni."""
    if conn.execute("SELECT 1 FROM meta WHERE key = 'sources_from_config'").fetchone():
        return
    ids = {"news": [], "facebook": []}
    for s in cfg.get("sources", []):
        kind, url = s["kind"], s["url"].strip()
        conn.execute(
            """INSERT INTO sources (kind, name, url, feed_url, enabled, origin_attr, content_selector, created_at)
               VALUES (?, ?, ?, ?, 1, ?, ?, ?)
               ON CONFLICT (kind, url) DO UPDATE SET name = excluded.name, enabled = 1,
                   origin_attr = excluded.origin_attr, content_selector = excluded.content_selector""",
            (kind, s.get("name", url), url, s.get("feed"), s.get("origin_attr"), s.get("content_selector"), now_iso()),
        )
        ids[kind].append(conn.execute("SELECT id FROM sources WHERE kind = ? AND url = ?", (kind, url)).fetchone()[0])
    for n in ids["news"]:
        for f in ids["facebook"]:
            conn.execute("INSERT OR IGNORE INTO source_links (news_id, fb_id, created_at) VALUES (?, ?, ?)", (n, f, now_iso()))
    # Stav FB backfillu drive lezel v data/fb_state.json (jen jeden profil).
    old_state = ROOT / "data" / "fb_state.json"
    if old_state.exists() and len(ids["facebook"]) == 1:
        conn.execute("UPDATE sources SET state = ? WHERE id = ?", (old_state.read_text(encoding="utf-8"), ids["facebook"][0]))
        old_state.rename(old_state.with_suffix(".json.migrated"))
    # Zdroje, ktere uz v configu nebyly a nemaji zadna data, jsou zbytecne.
    conn.execute("""DELETE FROM sources WHERE enabled = 0 AND id NOT IN (SELECT DISTINCT source_id FROM items)""")
    conn.execute("INSERT INTO meta (key, value) VALUES ('sources_from_config', ?)", (now_iso(),))
    conn.commit()


def links(conn: sqlite3.Connection) -> set[tuple[int, int]]:
    """Aktivni vazby (oba zdroje zapnute) jako {(news_id, fb_id)}."""
    return {(r[0], r[1]) for r in conn.execute(
        """SELECT l.news_id, l.fb_id FROM source_links l
           JOIN sources n ON n.id = l.news_id AND n.enabled = 1
           JOIN sources f ON f.id = l.fb_id AND f.enabled = 1"""
    )}


def active_sources(conn: sqlite3.Connection, kind: str) -> list[sqlite3.Row]:
    """Zapnute zdroje, ktere jsou v nejake aktivni vazbe (jen ty se stahuji a zpracovavaji)."""
    ids = {l[0] if kind == "news" else l[1] for l in links(conn)}
    rows = conn.execute("SELECT * FROM sources WHERE kind = ? AND enabled = 1 ORDER BY id", (kind,)).fetchall()
    return [r for r in rows if r["id"] in ids]


def get_state(conn: sqlite3.Connection, source_id: int) -> dict:
    row = conn.execute("SELECT state FROM sources WHERE id = ?", (source_id,)).fetchone()
    return json.loads(row["state"] or "{}") if row else {}


def set_state(conn: sqlite3.Connection, source_id: int, **kw) -> None:
    state = get_state(conn, source_id)
    state.update(kw)
    conn.execute("UPDATE sources SET state = ? WHERE id = ?", (json.dumps(state, ensure_ascii=False), source_id))
    conn.commit()


def delete_source(conn: sqlite3.Connection, source_id: int) -> None:
    """Smaze zdroj vcetne polozek, paru, posouzeni, hodnoceni a obrazku."""
    ids = f"SELECT id FROM items WHERE source_id = {int(source_id)}"
    for table, cols in (("judgements", ("article_id", "post_id")), ("feedback", ("article_id", "post_id")),
                        ("matches", ("article_id", "post_id"))):
        for col in cols:
            conn.execute(f"DELETE FROM {table} WHERE {col} IN ({ids})")
    for rel in [p for (raw,) in conn.execute(f"SELECT images FROM items WHERE source_id = {int(source_id)}")
                for p in jl(raw)]:
        (ROOT / rel).unlink(missing_ok=True)
    conn.execute(f"DELETE FROM search_idx WHERE rowid IN ({ids})")
    conn.execute(f"DELETE FROM embeddings WHERE item_id IN ({ids})")
    conn.execute(f"DELETE FROM extractions WHERE item_id IN ({ids})")
    conn.execute("DELETE FROM items WHERE source_id = ?", (source_id,))
    conn.execute("DELETE FROM source_links WHERE news_id = ? OR fb_id = ?", (source_id, source_id))
    conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
    conn.commit()


def jl(s: str | None) -> list:
    return json.loads(s) if s else []
