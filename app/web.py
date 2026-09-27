import json
import sqlite3
import threading
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import ai, pipeline, report, search
from .config import ROOT, load_config, now_iso, parse_iso
from .candidates import score_new
from .db import connect, delete_source, get_usage, jl, set_state
from .sources import brightdata, news

HERE = Path(__file__).resolve().parent
cfg = load_config()
TZ = ZoneInfo(cfg["app"]["timezone"])

app = FastAPI(title="news-fb-matcher")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
(ROOT / "data" / "fb_images").mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=ROOT / "data" / "fb_images"), name="media")
templates = Jinja2Templates(directory=HERE / "templates")

VERDICT_LABELS = {"shoda": "Shoda", "souvisejici_tema": "Související téma", "nesouvisi": "Nesouvisí"}
TYPE_LABELS = {
    "sdileni_clanku": "sdílí článek", "citace": "citace", "stejne_tvrzeni": "stejné tvrzení",
    "rozpor": "rozpor", "reakce": "reakce", "stejne_tema": "stejné téma", "zadny": "—",
}
RULE_LABELS = {"link": "odkaz na článek", "citation": "doslovná shoda"}


def short_origin(u: str | None) -> str:
    """https://t.me/selskyrozum -> t.me/selskyrozum"""
    return (u or "").split("://", 1)[-1].rstrip("/")


def media_url(rel: str) -> str:
    """data/fb_images/x.jpg -> /media/x.jpg"""
    return "/media/" + rel.rsplit("/", 1)[-1]


def local_dt(s: str | None) -> str:
    if not s:
        return ""
    return parse_iso(s).astimezone(TZ).strftime("%d. %m. %Y %H:%M")


templates.env.filters["local"] = local_dt
templates.env.filters["local_date"] = lambda v: parse_iso(v).astimezone(TZ).strftime("%d. %m. %Y") if v else ""
templates.env.filters["jl"] = jl
templates.env.filters["short_origin"] = short_origin
templates.env.filters["media"] = media_url
templates.env.filters["hl"] = search.highlight
templates.env.filters["snip"] = search.snippet
templates.env.globals.update(VERDICT_LABELS=VERDICT_LABELS, TYPE_LABELS=TYPE_LABELS, RULE_LABELS=RULE_LABELS)


def db():
    return connect(cfg)


_c = db()
search.ensure(_c)
_c.close()


SEARCH_SUB = "SELECT rowid FROM search_idx WHERE search_idx MATCH ?"


def _ctx(conn) -> dict:
    last = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    fb = cfg.get("facebook", {})
    bd = None
    if brightdata.enabled(cfg):
        used = get_usage(conn, brightdata.PROVIDER, brightdata.month_key())
        limit = fb["monthly_free_records"]
        bd = {"ready": brightdata.ready(cfg), "used": used, "limit": limit,
              "warn": used >= fb["warn_at"] * limit, "over": used >= limit}
    return {"running": pipeline.is_running(conn), "last_run": last, "ai_ready": ai.ready(cfg), "bd": bd}


MATCH_SQL = """
SELECT m.*, a.title AS a_title, a.url AS a_url, a.published_at AS a_pub, a.origin AS a_origin,
       p.text AS p_text, p.url AS p_url, p.published_at AS p_pub,
       sa.name AS a_source, sp.name AS p_source,
       j.verdict, j.match_type, j.explanation, f.rating
FROM matches m
JOIN items a ON a.id = m.article_id
JOIN items p ON p.id = m.post_id
JOIN sources sa ON sa.id = a.source_id
JOIN sources sp ON sp.id = p.source_id
LEFT JOIN judgements j ON j.article_id = m.article_id AND j.post_id = m.post_id
LEFT JOIN feedback f ON f.article_id = m.article_id AND f.post_id = m.post_id
"""


VERDICT_FILTERS = ["shoda", "souvisejici_tema", "nesouvisi", "neposouzeno"]


def _selected_verdicts(verdict: list[str] | None, submitted: bool) -> list[str]:
    """Zaskrtnute verdikty. Nic zaskrtnuto ve formulari = vse; bez formulare = jen shoda."""
    legacy = {"relevantni": ["shoda", "souvisejici_tema"], "all": VERDICT_FILTERS}
    sel = [x for v in (verdict or []) for x in legacy.get(v, [v]) if x in VERDICT_FILTERS]
    if not sel:
        sel = VERDICT_FILTERS if submitted else ["shoda"]
    return [v for v in VERDICT_FILTERS if v in sel]


@app.get("/", response_class=HTMLResponse)
def index(request: Request, min_score: float | None = None, verdict: list[str] | None = Query(None), vf: int = 0,
          days: int = 14, link: str = "all", q: str = ""):
    conn = db()
    try:
        # Jen pary existujicich vazeb (po zruseni vazby jeji pary zmizi).
        where = ["m.score >= ?", "MAX(a.published_at, p.published_at) >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)",
                 "EXISTS (SELECT 1 FROM source_links l WHERE l.news_id = a.source_id AND l.fb_id = p.source_id)"]
        if min_score is None:
            min_score = cfg["judge"]["min_score"]
        args: list = [min_score, f"-{days} days"]
        if link != "all" and "-" in link:
            n_id, f_id = (int(x) for x in link.split("-", 1))
            where += ["a.source_id = ?", "p.source_id = ?"]
            args += [n_id, f_id]
        fts = search.to_fts(q)
        if fts:
            where.append(f"(m.article_id IN ({SEARCH_SUB}) OR m.post_id IN ({SEARCH_SUB}))")
            args += [fts, fts]
        verdicts = _selected_verdicts(verdict, bool(vf))
        if len(verdicts) < len(VERDICT_FILTERS):
            named = [v for v in verdicts if v != "neposouzeno"]
            conds = ["j.verdict IS NULL"] if "neposouzeno" in verdicts else []
            if named:
                conds.append(f"j.verdict IN ({','.join('?' * len(named))})")
                args += named
            where.append("(" + " OR ".join(conds) + ")")
        search_err = ""
        try:
            rows = conn.execute(
                MATCH_SQL + " WHERE " + " AND ".join(where) + " ORDER BY m.score DESC LIMIT 200", args
            ).fetchall()
        except sqlite3.OperationalError:
            rows, search_err = [], "Hledaný výraz se nepodařilo zpracovat, zkus ho zjednodušit."
        counts = {r["kind"]: r["n"] for r in conn.execute("SELECT kind, COUNT(*) n FROM items GROUP BY kind")}
        pending = conn.execute(
            """SELECT COUNT(*) FROM items i JOIN sources s ON s.id = i.source_id AND s.enabled = 1
               WHERE i.status IN ('new', 'extracted')"""
        ).fetchone()[0]
        return templates.TemplateResponse(request, "index.html", {
            **_ctx(conn), "rows": rows, "counts": counts, "pending": pending, "links": _links(conn),
            "f": {"min_score": min_score, "verdicts": verdicts, "days": days, "link": link, "q": q},
            "search_err": search_err,
        })
    finally:
        conn.close()


@app.get("/match/{a_id}/{p_id}", response_class=HTMLResponse)
def match_detail(request: Request, a_id: int, p_id: int):
    conn = db()
    try:
        m = conn.execute(MATCH_SQL + " WHERE m.article_id = ? AND m.post_id = ?", (a_id, p_id)).fetchone()
        if not m:
            return RedirectResponse("/", status_code=303)
        items = {}
        for key, iid in (("a", a_id), ("p", p_id)):
            items[key] = conn.execute("SELECT * FROM items WHERE id = ?", (iid,)).fetchone()
            items[key + "_ex"] = conn.execute("SELECT * FROM extractions WHERE item_id = ?", (iid,)).fetchone()
        j = conn.execute("SELECT * FROM judgements WHERE article_id = ? AND post_id = ?", (a_id, p_id)).fetchone()
        w = cfg["matching"]["weights"]
        signals = [
            ("S1 Významová podobnost", m["s_sem"], w["semantic"], f"kosinová podobnost {m['cos_raw']:.3f}"),
            ("S2 Shoda entit", m["s_ent"], w["entities"], "osoby, organizace, místa (váha dle vzácnosti)"),
            ("S3 Klíčová slova", m["s_kw"], w["keywords"], "váha dle vzácnosti"),
            ("S4 Konkrétní čísla", m["s_num"], w["numbers"], "shodné částky, procenta, data"),
            ("S5 Časová blízkost", m["s_time"], w["time"], "exp(−dny / τ)"),
        ]
        first = "post" if m["p_pub"] < m["a_pub"] else "article"
        return templates.TemplateResponse(request, "match.html", {
            **_ctx(conn), "m": m, "j": j, "signals": signals, "first": first, **items,
        })
    finally:
        conn.close()


@app.post("/feedback/{a_id}/{p_id}")
def feedback(a_id: int, p_id: int, rating: int = Form(...), back: str = Form("/")):
    conn = db()
    try:
        if rating == 0:
            conn.execute("DELETE FROM feedback WHERE article_id = ? AND post_id = ?", (a_id, p_id))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO feedback (article_id, post_id, rating, created_at) VALUES (?, ?, ?, ?)",
                (a_id, p_id, 1 if rating > 0 else -1, now_iso()),
            )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(back if back.startswith("/") else "/", status_code=303)


@app.post("/run")
def run_now():
    pipeline.run_in_background("web")
    return RedirectResponse("/runs", status_code=303)


@app.get("/items", response_class=HTMLResponse)
def items(request: Request, kind: str = "article", page: int = 1, source: int = 0, q: str = ""):
    kind = kind if kind in ("article", "post") else "article"
    per = 50
    conn = db()
    try:
        fts = search.to_fts(q)
        where, args = "i.kind = ? AND (? = 0 OR i.source_id = ?)", [kind, source, source]
        if fts:
            where += f" AND i.id IN ({SEARCH_SUB})"
            args.append(fts)
        search_err, total = "", None
        try:
            rows = conn.execute(
                f"""SELECT i.*, s.name AS source_name, e.summary, e.entities, e.keywords, e.inferred_persons,
                          e.image_text,
                          (SELECT COUNT(*) FROM matches m WHERE m.article_id = i.id OR m.post_id = i.id) AS n_matches
                   FROM items i JOIN sources s ON s.id = i.source_id
                   LEFT JOIN extractions e ON e.item_id = i.id
                   WHERE {where} ORDER BY i.published_at DESC LIMIT ? OFFSET ?""",
                (*args, per + 1, (page - 1) * per),
            ).fetchall()
            if fts:
                total = conn.execute(f"SELECT COUNT(*) FROM items i WHERE {where}", args).fetchone()[0]
        except sqlite3.OperationalError:
            rows, search_err = [], "Hledaný výraz se nepodařilo zpracovat, zkus ho zjednodušit."
        sources = conn.execute(
            "SELECT id, name FROM sources WHERE kind = ? ORDER BY name", ("news" if kind == "article" else "facebook",)
        ).fetchall()
        return templates.TemplateResponse(request, "items.html", {
            **_ctx(conn), "rows": rows[:per], "kind": kind, "page": page, "has_next": len(rows) > per,
            "sources": sources, "source": source, "q": q, "total": total, "search_err": search_err,
        })
    finally:
        conn.close()


@app.get("/runs", response_class=HTMLResponse)
def runs(request: Request):
    conn = db()
    try:
        return templates.TemplateResponse(request, "runs.html", {
            **_ctx(conn),
            "runs": conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 30").fetchall(),
        })
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Zdroje a vazby
# ---------------------------------------------------------------------------
def _redirect(**params) -> RedirectResponse:
    q = urlencode({k: v for k, v in params.items() if v})
    return RedirectResponse("/sources" + (f"?{q}" if q else ""), status_code=303)


def _links(conn) -> list:
    return conn.execute(
        """SELECT l.news_id, l.fb_id, n.name AS news_name, f.name AS fb_name,
                  n.enabled AND f.enabled AS active,
                  (SELECT COUNT(*) FROM matches m JOIN items a ON a.id = m.article_id JOIN items p ON p.id = m.post_id
                    WHERE a.source_id = l.news_id AND p.source_id = l.fb_id AND m.score >= ?) AS n_pairs,
                  (SELECT COUNT(*) FROM matches m JOIN items a ON a.id = m.article_id JOIN items p ON p.id = m.post_id
                    JOIN judgements j ON j.article_id = m.article_id AND j.post_id = m.post_id
                    WHERE a.source_id = l.news_id AND p.source_id = l.fb_id AND j.verdict = 'shoda') AS n_shoda
           FROM source_links l JOIN sources n ON n.id = l.news_id JOIN sources f ON f.id = l.fb_id
           ORDER BY n.name, f.name""",
        (cfg["judge"]["min_score"],),
    ).fetchall()


def _sources(conn, kind: str) -> list[dict]:
    out = []
    for r in conn.execute(
        """SELECT s.*, COUNT(i.id) AS n_items, MAX(i.published_at) AS last_item,
                  SUM(i.published_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-3 days')) AS n_3d,
                  (SELECT COUNT(*) FROM source_links l WHERE l.news_id = s.id OR l.fb_id = s.id) AS n_links
           FROM sources s LEFT JOIN items i ON i.source_id = s.id
           WHERE s.kind = ? GROUP BY s.id ORDER BY s.enabled DESC, s.name""",
        (kind,),
    ):
        d = dict(r)
        d["state"] = json.loads(r["state"] or "{}")
        d["per_day"] = round((r["n_3d"] or 0) / 3)
        out.append(d)
    return out


def _normalize_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def _score_in_background() -> None:
    """Po nove vazbe spocitat pary hned (bez AI, trva sekundy); AI posouzeni probehne pri behu."""
    def work():
        conn = connect(cfg)
        try:
            if not pipeline.is_running(conn):
                score_new(conn, cfg, lambda m: None)
        finally:
            conn.close()
    threading.Thread(target=work, daemon=True).start()


def _verify_in_background(source_id: int, url: str, keep_name: bool) -> None:
    def work():
        conn = connect(cfg)
        try:
            try:
                name = brightdata.verify(conn, url, cfg)
                if not keep_name:
                    conn.execute("UPDATE sources SET name = ? WHERE id = ?", (name, source_id))
                set_state(conn, source_id, verify="ok", verify_msg=f"Ověřeno: {name}")
            except Exception as e:
                set_state(conn, source_id, verify="error", verify_msg=str(e)[:300])
        finally:
            conn.close()
    threading.Thread(target=work, daemon=True).start()


@app.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request, msg: str = "", err: str = ""):
    conn = db()
    try:
        fb = _sources(conn, "facebook")
        return templates.TemplateResponse(request, "sources.html", {
            **_ctx(conn), "msg": msg, "err": err,
            "news": _sources(conn, "news"), "fb": fb, "links": _links(conn),
            "verifying": any(s["state"].get("verify") == "pending" for s in fb),
        })
    finally:
        conn.close()


@app.post("/sources/news")
def add_news(url: str = Form(...), name: str = Form(""), content_selector: str = Form(""),
             origin_attr: str = Form("")):
    url = _normalize_url(url)
    selector, attr = content_selector.strip() or None, origin_attr.strip() or None
    conn = db()
    try:
        if conn.execute("SELECT 1 FROM sources WHERE kind = 'news' AND url = ?", (url,)).fetchone():
            return _redirect(err="Tento web už je v seznamu.")
        try:
            check = news.check_site(url, selector, attr)
        except Exception as e:
            return _redirect(err=f"Web se nepodařilo přidat: {e}")
        state = {"sample": check["sample"], "feed_entries": check["entries"]}
        conn.execute(
            """INSERT INTO sources (kind, name, url, feed_url, enabled, origin_attr, content_selector, state, created_at)
               VALUES ('news', ?, ?, ?, 1, ?, ?, ?, ?)""",
            (name.strip() or news.site_name(url), url, check["feed"], attr, selector,
             json.dumps(state, ensure_ascii=False), now_iso()),
        )
        conn.commit()
        return _redirect(msg=f"Web přidán. Feed: {check['feed']} ({check['entries']} položek). "
                             "Zkontroluj ukázku vytaženého textu a propoj web s profilem.")
    finally:
        conn.close()


@app.post("/sources/fb")
def add_fb(url: str = Form(...), name: str = Form("")):
    url = _normalize_url(url)
    host = urlparse(url).netloc.lower()
    if not (host == "facebook.com" or host.endswith(".facebook.com")):
        return _redirect(err="Adresa musí být z facebook.com.")
    conn = db()
    try:
        if conn.execute("SELECT 1 FROM sources WHERE kind = 'facebook' AND url = ?", (url,)).fetchone():
            return _redirect(err="Tento profil už je v seznamu.")
        ready = brightdata.ready(cfg)
        state = ({"verify": "pending"} if ready
                 else {"verify": "error", "verify_msg": "Chybí BRIGHTDATA_TOKEN, nelze ověřit."})
        sid = conn.execute(
            "INSERT INTO sources (kind, name, url, enabled, state, created_at) VALUES ('facebook', ?, ?, 1, ?, ?)",
            (name.strip() or url, url, json.dumps(state), now_iso()),
        ).lastrowid
        conn.commit()
    finally:
        conn.close()
    if ready:
        _verify_in_background(sid, url, keep_name=bool(name.strip()))
    return _redirect(msg="Profil přidán, ověřuji ho (stažení 1 příspěvku, asi minuta).")


@app.post("/sources/{source_id}/verify")
def reverify(source_id: int):
    conn = db()
    try:
        s = conn.execute("SELECT * FROM sources WHERE id = ? AND kind = 'facebook'", (source_id,)).fetchone()
        if not s or not brightdata.ready(cfg):
            return _redirect(err="Nelze ověřit (chybí profil nebo BRIGHTDATA_TOKEN).")
        set_state(conn, source_id, verify="pending", verify_msg="")
    finally:
        conn.close()
    _verify_in_background(source_id, s["url"], keep_name=s["name"] != s["url"])
    return _redirect(msg="Ověřuji profil…")


@app.post("/sources/{source_id}/toggle")
def toggle_source(source_id: int):
    conn = db()
    try:
        conn.execute("UPDATE sources SET enabled = 1 - enabled WHERE id = ?", (source_id,))
        conn.commit()
        row = conn.execute("SELECT name, enabled FROM sources WHERE id = ?", (source_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return _redirect()
    if row["enabled"]:
        _score_in_background()
    return _redirect(msg=f"„{row['name']}“ {'zapnut' if row['enabled'] else 'vypnut – data zůstávají'}.")


@app.post("/sources/{source_id}/delete")
def remove_source(source_id: int):
    conn = db()
    try:
        row = conn.execute("SELECT name FROM sources WHERE id = ?", (source_id,)).fetchone()
        if not row:
            return _redirect()
        delete_source(conn, source_id)
    finally:
        conn.close()
    return _redirect(msg=f"„{row['name']}“ smazán včetně dat.")


@app.post("/links")
def add_link(news_id: int = Form(...), fb_id: int = Form(...)):
    conn = db()
    try:
        ok = conn.execute(
            "SELECT COUNT(*) FROM sources WHERE (id = ? AND kind = 'news') OR (id = ? AND kind = 'facebook')",
            (news_id, fb_id),
        ).fetchone()[0] == 2
        if not ok:
            return _redirect(err="Vyber web i profil.")
        cur = conn.execute("INSERT OR IGNORE INTO source_links (news_id, fb_id, created_at) VALUES (?, ?, ?)",
                           (news_id, fb_id, now_iso()))
        if not cur.rowcount:
            return _redirect(err="Tato vazba už existuje.")
        # Uz zpracovane polozky obou zdroju se musi sparovat i spolu navzajem.
        conn.execute("UPDATE items SET scored_at = NULL WHERE status = 'ready' AND source_id IN (?, ?)",
                     (news_id, fb_id))
        conn.commit()
    finally:
        conn.close()
    _score_in_background()
    return _redirect(msg="Vazba vytvořena. Nové zdroje se stáhnou a AI posoudí páry při dalším běhu (Spustit teď).")


@app.post("/links/delete")
def remove_link(news_id: int = Form(...), fb_id: int = Form(...)):
    conn = db()
    try:
        conn.execute("DELETE FROM source_links WHERE news_id = ? AND fb_id = ?", (news_id, fb_id))
        # Pary teto vazby uz nejsou potreba (AI verdikty zustavaji jako data pro uceni vah).
        conn.execute(
            """DELETE FROM matches WHERE article_id IN (SELECT id FROM items WHERE source_id = ?)
               AND post_id IN (SELECT id FROM items WHERE source_id = ?)""",
            (news_id, fb_id),
        )
        conn.commit()
    finally:
        conn.close()
    return _redirect(msg="Vazba zrušena.")


# ---------------------------------------------------------------------------
# Shrnuti (PDF reporty)
# ---------------------------------------------------------------------------
_report_job: dict = {"running": False, "error": "", "label": ""}


def _report_in_background(start: datetime, end: datetime, label: str) -> bool:
    if _report_job["running"]:
        return False
    _report_job.update(running=True, error="", label=label)

    def work():
        conn = connect(cfg)
        try:
            report.generate(conn, cfg, trigger="manual", start=start, end=end)
        except Exception as e:
            _report_job["error"] = f"Report se nepodařilo vytvořit: {e}"
        finally:
            conn.close()
            _report_job["running"] = False
    threading.Thread(target=work, daemon=True).start()
    return True


def _report_path(conn, report_id: int) -> Path | None:
    row = conn.execute("SELECT path FROM reports WHERE id = ?", (report_id,)).fetchone()
    if not row:
        return None
    path = (ROOT / row["path"]).resolve()
    # Jen soubory ve slozce reportu (ochrana pred cestou mimo).
    return path if path.is_relative_to(report.REPORT_DIR.resolve()) and path.exists() else None


@app.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request, msg: str = "", err: str = ""):
    conn = db()
    try:
        rows = []
        for r in conn.execute("SELECT * FROM reports ORDER BY created_at DESC"):
            d = dict(r)
            d["stats"] = json.loads(r["stats"] or "{}")
            d["exists"] = _report_path(conn, r["id"]) is not None
            rows.append(d)
        last_auto = conn.execute("SELECT MAX(period_end) FROM reports WHERE trigger = 'auto'").fetchone()[0]
        next_auto = (parse_iso(last_auto) + timedelta(days=cfg["report"]["period_days"])).isoformat() if last_auto else None
        today = datetime.now(TZ).date()
        return templates.TemplateResponse(request, "reports.html", {
            **_ctx(conn), "reports": rows, "msg": msg, "err": err or _report_job["error"],
            "job": _report_job, "next_auto": next_auto, "period_days": cfg["report"]["period_days"],
            "today": today.isoformat(), "week_ago": (today - timedelta(days=7)).isoformat(),
        })
    finally:
        conn.close()


@app.post("/reports")
def create_report(days: str = Form("7"), date_from: str = Form(""), date_to: str = Form("")):
    now = datetime.now(timezone.utc)
    try:
        if days == "custom":
            d1, d2 = date.fromisoformat(date_from), date.fromisoformat(date_to)
            if d1 > d2:
                d1, d2 = d2, d1
            start = datetime.combine(d1, time.min, TZ)
            end = min(datetime.combine(d2, time.max, TZ), now.astimezone(TZ))
            label = f"{d1:%d. %m.} – {d2:%d. %m. %Y}"
        else:
            n = max(1, min(int(days), 365))
            start, end = now - timedelta(days=n), now
            label = f"posledních {n} dní"
    except ValueError:
        return RedirectResponse("/reports?err=" + "Neplatné období.", status_code=303)
    if not _report_in_background(start, end, label):
        return RedirectResponse("/reports?err=" + "Už se vytváří jiný report, počkej, až doběhne.", status_code=303)
    return RedirectResponse("/reports?msg=" + f"Vytvářím report za {label} (asi minuta).", status_code=303)


@app.get("/reports/{report_id}/pdf")
def report_pdf(report_id: int, download: int = 0):
    conn = db()
    try:
        path = _report_path(conn, report_id)
    finally:
        conn.close()
    if not path:
        return RedirectResponse("/reports?err=" + "Soubor reportu nebyl nalezen.", status_code=303)
    return FileResponse(path, media_type="application/pdf", filename=path.name,
                        content_disposition_type="attachment" if download else "inline")


@app.post("/reports/{report_id}/delete")
def delete_report(report_id: int):
    conn = db()
    try:
        path = _report_path(conn, report_id)
        if path:
            path.unlink(missing_ok=True)
        conn.execute("DELETE FROM reports WHERE id = ?", (report_id,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/reports?msg=" + "Report smazán.", status_code=303)
