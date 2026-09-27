"""Cely beh: stahnout -> extrahovat -> embeddingy -> skorovat -> AI posoudit."""
import threading
from datetime import timedelta

from . import ai, report, search
from .candidates import score_new
from .config import load_config, now_iso, parse_iso
from .db import active_sources, connect
from .extract import embed_new, extract_new
from .judge import judge_new
from .sources import brightdata, news

_lock = threading.Lock()
STALE_RUN = timedelta(hours=3)


def is_running(conn) -> bool:
    row = conn.execute("SELECT started_at FROM runs WHERE status = 'running' ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return False
    return parse_iso(now_iso()) - parse_iso(row["started_at"]) < STALE_RUN


def run(trigger: str = "manual", cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    if not _lock.acquire(blocking=False):
        return {"status": "busy"}
    conn = connect(cfg)
    try:
        # Zamek i mezi procesy (nocni uloha vs. tlacitko ve webu).
        if is_running(conn):
            return {"status": "busy"}
        run_id = conn.execute(
            "INSERT INTO runs (trigger, started_at, status) VALUES (?, ?, 'running')", (trigger, now_iso())
        ).lastrowid
        conn.commit()

        lines: list[str] = []

        def log(msg: str) -> None:
            lines.append(msg)
            print(msg, flush=True)
            conn.execute("UPDATE runs SET log = ? WHERE id = ?", ("\n".join(lines), run_id))
            conn.commit()

        stats = {"clanky": 0, "prispevky": 0, "extrakce": 0, "embeddingy": 0, "pary": 0, "posouzeni": 0}
        status = "ok"
        ai.reset_usage()
        try:
            news_src, fb_src = active_sources(conn, "news"), active_sources(conn, "facebook")
            if not news_src or not fb_src:
                log("!! Zadna aktivni vazba web <-> profil – neni co porovnavat (nastav ve webu: Zdroje).")

            log("1) Stahuji clanky")
            for src in news_src:
                try:
                    stats["clanky"] += news.ingest(conn, src, cfg, log)
                except Exception as e:
                    log(f"  {src['name']}: chyba ({e})")
                    status = "error"
            log(f"  novych clanku: {stats['clanky']}")

            log("2) Prispevky z Facebooku")
            if fb_src and not brightdata.ready(cfg):
                log("  !! Chybi BRIGHTDATA_TOKEN – stahovani z FB se preskakuje.")
            elif fb_src:
                for src in fb_src:
                    log(f"  {src['name']}:")
                    try:
                        stats["prispevky"] += brightdata.fetch(conn, src, cfg, log)
                    except Exception as e:
                        log(f"  Bright Data: chyba ({e})")
                        status = "error"
            log(f"  novych prispevku: {stats['prispevky']}")

            if not ai.ready(cfg):
                log("!! Chybi OPENAI_API_KEY – extrakce, parovani a posouzeni se preskakuji.")
                status = "error"
            else:
                log("3) AI extrakce")
                stats["extrakce"] = extract_new(conn, cfg, log)
                log(f"  extrahovano: {stats['extrakce']}")

                log("4) Embeddingy")
                stats["embeddingy"] = embed_new(conn, cfg, log)
                log(f"  hotovo: {stats['embeddingy']}")

                log("5) Skorovani paru (bez AI)")
                stats["pary"] = score_new(conn, cfg, log)

                log("6) AI posouzeni kandidatu")
                stats["posouzeni"] = judge_new(conn, cfg, log)
                log(f"  posouzeno: {stats['posouzeni']}")

                log(f"7) Vyhledavaci index: {search.rebuild(conn)} polozek")

                log("8) Report")
                try:
                    if not report.maybe_auto(conn, cfg, log):
                        log("  jeste neni cas na dalsi report")
                except Exception as e:
                    log(f"  report selhal: {e!r}")

                u = ai.usage
                log(f"Tokeny: prompt {u['prompt_tokens']}, odpoved {u['completion_tokens']}, "
                    f"embeddingy {u['embedding_tokens']}")
                stats["tokeny"] = u["prompt_tokens"] + u["completion_tokens"]
        except Exception as e:
            log(f"!! Beh spadl: {e!r}")
            status = "error"

        summary = ", ".join(f"{k} {v}" for k, v in stats.items())
        conn.execute(
            "UPDATE runs SET finished_at = ?, status = ?, summary = ? WHERE id = ?",
            (now_iso(), status, summary, run_id),
        )
        conn.commit()
        return {"status": status, "run_id": run_id, **stats}
    finally:
        conn.close()
        _lock.release()


def run_in_background(trigger: str = "web") -> bool:
    if _lock.locked():
        return False
    threading.Thread(target=run, args=(trigger,), daemon=True).start()
    return True
