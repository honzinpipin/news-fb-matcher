"""PDF report: shrnuti shod za obdobi (zdroje, temata, grafy, seznam shod, Telegram zdroje).

Temata urci jedno volani AI nad vsemi pary obdobi (shoda + souvisejici tema).
Grafy: kolacovy (podil temat, max 6 dilu + Ostatni) a sloupcovy (pary po dnech po tematech).
"""
import io
import json
import math
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from reportlab.lib import colors  # noqa: E402
from reportlab.lib.enums import TA_LEFT  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.styles import ParagraphStyle  # noqa: E402
from reportlab.lib.units import mm  # noqa: E402
from reportlab.pdfbase import pdfmetrics  # noqa: E402
from reportlab.pdfbase.ttfonts import TTFont  # noqa: E402
from reportlab.platypus import (CondPageBreak, Image, KeepTogether, Paragraph, SimpleDocTemplate,  # noqa: E402
                                Spacer, Table, TableStyle)

from . import ai, telegram  # noqa: E402
from .config import ROOT, now_iso, parse_iso, to_iso  # noqa: E402

REPORT_DIR = ROOT / "data" / "reports"
MAX_TOPICS = 8        # temat od AI (v tabulce vsechna)
CHART_TOPICS = 5      # v grafech 5 nejvetsich + "Ostatní" (kolac max 6 dilu)
# Kategoricka paleta (validovane poradi, svetly rezim – PDF je na bilem) + neutralni seda pro "Ostatni".
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]
OTHER = "#9a9893"
INK, INK2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#ffffff"
VERDICT_CZ = {"shoda": "Shoda", "souvisejici_tema": "Související téma", "nesouvisi": "Nesouvisí"}
TYPE_CZ = {"sdileni_clanku": "sdílí článek", "citace": "citace", "stejne_tvrzeni": "stejné tvrzení",
           "rozpor": "rozpor", "reakce": "reakce", "stejne_tema": "stejné téma", "zadny": "—"}

# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
PAIRS_SQL = """
SELECT m.article_id, m.post_id, m.score, j.verdict, j.match_type, j.explanation, j.shared_points,
       a.title AS a_title, a.url AS a_url, a.published_at AS a_pub, a.origin AS a_origin, sa.name AS a_source,
       p.text AS p_text, p.url AS p_url, p.published_at AS p_pub, sp.name AS p_source,
       pe.summary AS p_summary, pe.image_text AS p_image_text
FROM matches m
JOIN judgements j ON j.article_id = m.article_id AND j.post_id = m.post_id
JOIN items a ON a.id = m.article_id
JOIN items p ON p.id = m.post_id
JOIN sources sa ON sa.id = a.source_id
JOIN sources sp ON sp.id = p.source_id
JOIN source_links l ON l.news_id = a.source_id AND l.fb_id = p.source_id
LEFT JOIN extractions pe ON pe.item_id = p.id
WHERE m.score >= ? AND MAX(a.published_at, p.published_at) BETWEEN ? AND ?
ORDER BY (j.verdict = 'shoda') DESC, m.score DESC
"""


def collect(conn, cfg: dict, start: datetime, end: datetime) -> dict:
    s, e = to_iso(start), to_iso(end)
    rows = [dict(r) for r in conn.execute(PAIRS_SQL, (cfg["judge"]["min_score"], s, e))]
    pairs = [r for r in rows if r["verdict"] in ("shoda", "souvisejici_tema")]
    for i, r in enumerate(pairs, 1):
        r["no"] = i
        r["when"] = max(r["a_pub"], r["p_pub"])
        r["first"] = "příspěvek" if r["p_pub"] < r["a_pub"] else "článek"
    sources = [dict(r) for r in conn.execute(
        """SELECT s.id, s.kind, s.name, s.url,
                  (SELECT COUNT(*) FROM items i WHERE i.source_id = s.id AND i.published_at BETWEEN ? AND ?) AS n_period,
                  (SELECT COUNT(*) FROM source_links l WHERE l.news_id = s.id OR l.fb_id = s.id) AS n_links
           FROM sources s WHERE s.enabled = 1
             AND s.id IN (SELECT news_id FROM source_links UNION SELECT fb_id FROM source_links)
           ORDER BY s.kind DESC, s.name""", (s, e))]
    links = [dict(r) for r in conn.execute(
        """SELECT n.name AS news_name, f.name AS fb_name FROM source_links l
           JOIN sources n ON n.id = l.news_id JOIN sources f ON f.id = l.fb_id
           WHERE n.enabled = 1 AND f.enabled = 1 ORDER BY n.name, f.name""")]
    verdicts = Counter(r["verdict"] for r in rows)
    return {"start": start, "end": end, "pairs": pairs, "sources": sources, "links": links,
            "counts": {
                "articles": sum(x["n_period"] for x in sources if x["kind"] == "news"),
                "posts": sum(x["n_period"] for x in sources if x["kind"] == "facebook"),
                "judged": len(rows), "shoda": verdicts["shoda"],
                "souvisejici": verdicts["souvisejici_tema"], "nesouvisi": verdicts["nesouvisi"],
            }}


# ---------------------------------------------------------------------------
# Temata (AI)
# ---------------------------------------------------------------------------
TOPIC_SYSTEM = f"""Analyzujes seznam paru (zpravodajsky clanek + prispevek na Facebooku), ktere spolu obsahove
souvisi. Urci hlavni temata a shrn, o cem shody jsou.
- topics: 3 az {MAX_TOPICS} temat (pri mene nez 6 parech klidne 1–2). Seskupuj: radeji mene vetsich temat
  nez hodne malych. name = cesky, max 4 slova, KONKRETNI kauza, udalost nebo osoba (napr. "Odvolani vlady
  Babise", "Kauza Dozimetr a STAN", "Kritika TOP 09"). Nepouzivej zastresujici kategorie jako "Ceska
  politika" – takovou oblast rozdel na konkretni temata. description = 1 veta.
- assignments: pro KAZDY par ze vstupu (vsechna cisla #, zadne nevynechej) jeden zaznam
  {{pair: cislo paru, topic: poradove cislo tematu 1..N}}. topic = 0 ("Ostatni") jen pro par
  [Související téma], ktery k zadnemu tematu opravdu nepatri. Pary [Shoda] vzdy do nektereho tematu.
- overview: 2–3 odstavce cesky (oddelene prazdnym radkem): co je hlavnim obsahem shod, ktere osoby a
  udalosti se opakuji, kdy prispevek predbehl clanek. Vecne, bez vlastniho hodnoceni."""

TOPIC_SCHEMA = {
    "type": "object",
    "properties": {
        "overview": {"type": "string"},
        "topics": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "description": {"type": "string"}},
            "required": ["name", "description"], "additionalProperties": False}},
        "assignments": {"type": "array", "items": {
            "type": "object",
            "properties": {"pair": {"type": "integer"}, "topic": {"type": "integer"}},
            "required": ["pair", "topic"], "additionalProperties": False}},
    },
    "required": ["overview", "topics", "assignments"], "additionalProperties": False,
}


def topics(cfg: dict, pairs: list[dict]) -> dict:
    """Vraci {"overview", "topics": [{name, description, color, pairs:[no]}], "chart"}; kazdy par ma r["topic"]."""
    if not pairs:
        return {"overview": "", "topics": [], "chart": []}
    lines = [f"Pocet paru: {len(pairs)} (cisla 1–{len(pairs)})."]
    for r in pairs:
        post = (r["p_summary"] or r["p_text"] or "")[:300].replace("\n", " ")
        lines.append(f"#{r['no']} [{VERDICT_CZ[r['verdict']]}] clanek ({r['a_pub'][:16]}): {r['a_title']} | "
                     f"prispevek ({r['p_pub'][:16]}): {post} | drive vysel: {r['first']}")
    res = {"overview": "", "topics": [], "assignments": []}
    if ai.ready(cfg):
        try:
            res = ai.chat_json(cfg, TOPIC_SYSTEM, "\n".join(lines), TOPIC_SCHEMA, "temata")
        except ai.AIError:
            pass
    names = [t["name"] for t in res["topics"][:MAX_TOPICS]]
    groups: dict[int, list[int]] = {}
    for a in res["assignments"]:
        if 1 <= a["topic"] <= len(names) and 1 <= a["pair"] <= len(pairs):
            groups.setdefault(a["topic"], [])
            if a["pair"] not in {n for g in groups.values() for n in g}:
                groups[a["topic"]].append(a["pair"])
    res["topics"] = [{**t, "pair_ids": groups.get(i, [])} for i, t in enumerate(res["topics"][:MAX_TOPICS], 1)]
    by_no = {r["no"]: r for r in pairs}
    out = []
    for t in res["topics"][:MAX_TOPICS]:
        nos = [n for n in t["pair_ids"] if n in by_no and "topic" not in by_no[n]]
        for n in nos:
            by_no[n]["topic"] = t["name"]
        if nos:
            out.append({"name": t["name"], "description": t["description"], "pairs": nos})
    rest = [r["no"] for r in pairs if "topic" not in r]
    for n in rest:
        by_no[n]["topic"] = "Ostatní"
    out.sort(key=lambda t: -len(t["pairs"]))
    # Barvy: 5 nejvetsich temat ma vlastni barvu, ostatni se v grafech slucuji do sede "Ostatní".
    for i, t in enumerate(out):
        t["color"] = PALETTE[i] if i < CHART_TOPICS else OTHER
    if rest:
        out.append({"name": "Ostatní", "description": "Páry bez jednoznačného tématu.", "pairs": rest, "color": OTHER})
    chart = [t for t in out[:CHART_TOPICS] if t["name"] != "Ostatní"]
    merged = [n for t in out if t not in chart for n in t["pairs"]]
    if merged:
        chart.append({"name": "Ostatní", "pairs": merged, "color": OTHER})
    for t in chart:
        for n in t["pairs"]:
            by_no[n]["chart_topic"] = t["name"]
    return {"overview": res["overview"], "topics": out, "chart": chart}


# ---------------------------------------------------------------------------
# Grafy
# ---------------------------------------------------------------------------
def _style_axes(ax):
    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=INK2, labelsize=8, length=0)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def pie_chart(topic_list: list[dict]) -> io.BytesIO:
    sizes = [len(t["pairs"]) for t in topic_list]
    total = sum(sizes)
    fig, ax = plt.subplots(figsize=(4.2, 3.4), dpi=200)
    wedges, _ = ax.pie(sizes, colors=[t["color"] for t in topic_list], startangle=90, counterclock=False,
                       wedgeprops={"edgecolor": SURFACE, "linewidth": 2, "width": 0.42})
    for w, n in zip(wedges, sizes):
        if n / total >= 0.08:  # popisek jen kdyz se vejde; ostatni hodnoty jsou v tabulce
            ang = (w.theta2 + w.theta1) / 2
            x, y = 0.79 * math.cos(math.radians(ang)), 0.79 * math.sin(math.radians(ang))
            ax.text(x, y, f"{round(100 * n / total)} %", ha="center", va="center", fontsize=8,
                    color=INK, fontweight="bold",
                    bbox={"boxstyle": "round,pad=0.2", "fc": SURFACE, "ec": "none", "alpha": 0.85})
    ax.text(0, 0.06, str(total), ha="center", va="center", fontsize=16, color=INK, fontweight="bold")
    ax.text(0, -0.16, "párů", ha="center", va="center", fontsize=8, color=INK2)
    ax.set(aspect="equal")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=SURFACE)
    plt.close(fig)
    buf.seek(0)
    return buf


def bar_chart(pairs: list[dict], topic_list: list[dict], start: datetime, end: datetime, tz) -> io.BytesIO:
    days = []
    d = start.astimezone(tz).date()
    while d <= end.astimezone(tz).date():
        days.append(d)
        d += timedelta(days=1)
    counts = defaultdict(Counter)
    for r in pairs:
        counts[r["chart_topic"]][parse_iso(r["when"]).astimezone(tz).date()] += 1
    fig, ax = plt.subplots(figsize=(7.2, 3.0), dpi=200)
    _style_axes(ax)
    bottom = [0] * len(days)
    x = range(len(days))
    for t in topic_list:
        vals = [counts[t["name"]][day] for day in days]
        ax.bar(x, vals, bottom=bottom, width=0.62, color=t["color"], edgecolor=SURFACE, linewidth=2,
               label=t["name"])
        bottom = [b + v for b, v in zip(bottom, vals)]
    for i, total in enumerate(bottom):
        if total:
            ax.text(i, total + 0.1, str(total), ha="center", va="bottom", fontsize=8, color=INK2)
    ax.set_xticks(list(x), [f"{day.day}. {day.month}." for day in days])
    ax.set_ylabel("párů", fontsize=8, color=INK2)
    ax.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(integer=True))
    ax.set_ylim(0, max(bottom + [1]) * 1.2)
    ax.legend(loc="upper left", bbox_to_anchor=(1.0, 1.0), frameon=False, fontsize=7, labelcolor=INK)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=SURFACE)
    plt.close(fig)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def _fonts() -> tuple[str, str]:
    ttf = os.path.join(os.path.dirname(matplotlib.__file__), "mpl-data", "fonts", "ttf")
    if "DejaVu" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("DejaVu", os.path.join(ttf, "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont("DejaVu-Bold", os.path.join(ttf, "DejaVuSans-Bold.ttf")))
        pdfmetrics.registerFontFamily("DejaVu", normal="DejaVu", bold="DejaVu-Bold")
        _GLYPHS.update(pdfmetrics.getFont("DejaVu").face.charToGlyph.keys())
    plt.rcParams["font.family"] = "DejaVu Sans"
    return "DejaVu", "DejaVu-Bold"


_GLYPHS: set = set()


def _esc(s) -> str:
    """HTML escape + vynechani znaku, ktere pismo neumi (emoji z Telegramu by byly ctverecky)."""
    s = "".join(ch for ch in str(s or "") if ch.isspace() or ord(ch) in _GLYPHS) if _GLYPHS else str(s or "")
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _cut(s: str | None, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def build_pdf(conn, data: dict, tp: dict, path, cfg: dict, tz) -> None:
    reg, bold = _fonts()
    st = {
        "title": ParagraphStyle("t", fontName=bold, fontSize=18, leading=22, textColor=INK, spaceAfter=2),
        "sub": ParagraphStyle("s", fontName=reg, fontSize=9.5, leading=13, textColor=INK2, spaceAfter=10),
        "h2": ParagraphStyle("h2", fontName=bold, fontSize=12.5, leading=16, textColor=INK, spaceBefore=12, spaceAfter=6,
                            keepWithNext=1),
        # Nadpis pred dlouhou tabulkou: bez keepWithNext (tabulka se muze rozdelit), misto toho CondPageBreak.
        "h2t": ParagraphStyle("h2t", fontName=bold, fontSize=12.5, leading=16, textColor=INK, spaceBefore=12,
                              spaceAfter=6),
        "body": ParagraphStyle("b", fontName=reg, fontSize=9.5, leading=13.5, textColor=INK, alignment=TA_LEFT, spaceAfter=6),
        "cell": ParagraphStyle("c", fontName=reg, fontSize=8, leading=10.5, textColor=INK),
        "cellb": ParagraphStyle("cb", fontName=bold, fontSize=8, leading=10.5, textColor=INK),
        "small": ParagraphStyle("sm", fontName=reg, fontSize=7.5, leading=10, textColor=INK2),
        "stat": ParagraphStyle("st", fontName=bold, fontSize=15, leading=18, textColor=INK),
    }
    P = lambda text, s="cell": Paragraph(text, st[s])  # noqa: E731
    fmt = lambda iso: parse_iso(iso).astimezone(tz).strftime("%d. %m. %Y %H:%M")  # noqa: E731
    start, end = data["start"].astimezone(tz), data["end"].astimezone(tz)
    c = data["counts"]
    grid = TableStyle([("FONTNAME", (0, 0), (-1, -1), reg), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                       ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor(GRID)),
                       ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)])
    head = [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f3f2ef"))]

    story = [
        P("Shrnutí shod článků a příspěvků", "title"),
        P(f"Období <b>{start:%d. %m. %Y} – {end:%d. %m. %Y}</b> ({(end.date() - start.date()).days} dní) · "
          f"vytvořeno {datetime.now(tz):%d. %m. %Y %H:%M}", "sub"),
    ]

    # Prehled cisel
    stats = [("Článků", c["articles"]), ("Příspěvků", c["posts"]), ("Posouzených párů", c["judged"]),
             ("Shod", c["shoda"]), ("Souvisejících", c["souvisejici"]), ("Nesouvisí", c["nesouvisi"])]
    t = Table([[P(str(v), "stat") for _, v in stats], [P(k, "small") for k, _ in stats]],
              colWidths=[30 * mm] * 6)
    t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor(GRID)),
                           ("INNERGRID", (0, 0), (-1, -1), 0, colors.white),
                           ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                           ("LEFTPADDING", (0, 0), (-1, -1), 7)]))
    story += [t]

    # Zdroje
    story.append(P("Sledované zdroje", "h2"))
    rows = [[P("<b>Typ</b>"), P("<b>Název</b>"), P("<b>Adresa</b>"), P("<b>V období</b>")]]
    for s in data["sources"]:
        rows.append([P("Facebook" if s["kind"] == "facebook" else "Web"), P(_esc(s["name"])),
                     P(f'<link href="{_esc(s["url"])}" color="#2a78d6">{_esc(_cut(s["url"], 60))}</link>'),
                     P(f'{s["n_period"]} {"příspěvků" if s["kind"] == "facebook" else "článků"}')])
    t = Table(rows, colWidths=[20 * mm, 50 * mm, 80 * mm, 30 * mm], repeatRows=1)
    t.setStyle(grid)
    t.setStyle(TableStyle(head))
    story.append(t)
    if data["links"]:
        story.append(Spacer(1, 4))
        story.append(P("Porovnávané dvojice: " + "; ".join(f'{_esc(l["news_name"])} ↔ {_esc(l["fb_name"])}'
                                                           for l in data["links"]), "small"))

    # Obsah shod
    story.append(P("Co je obsahem shod", "h2"))
    if tp["overview"]:
        for para in [x.strip() for x in tp["overview"].split("\n\n") if x.strip()]:
            story.append(P(_esc(para), "body"))
        story.append(P("Shrnutí a témata vytvořila AI z párů „shoda“ a „související téma“.", "small"))
    elif not data["pairs"]:
        story.append(P("V tomto období nebyla nalezena žádná shoda ani související téma.", "body"))

    if tp["topics"]:
        story.append(P("Témata", "h2"))
        legend_rows = [["", P("<b>Téma</b>"), P("<b>Párů</b>"), P("<b>Shody</b>")]]
        for tpc in tp["topics"]:
            n_shoda = sum(1 for r in data["pairs"] if r["topic"] == tpc["name"] and r["verdict"] == "shoda")
            legend_rows.append([
                "", P(f'<b>{_esc(tpc["name"])}</b><br/><font color="{INK2}">{_esc(tpc["description"])}</font>'),
                P(str(len(tpc["pairs"]))), P(str(n_shoda))])
        lt = Table(legend_rows, colWidths=[4 * mm, 58 * mm, 13 * mm, 15 * mm])
        lt.setStyle(grid)
        lt.setStyle(TableStyle([("LEFTPADDING", (0, 0), (0, -1), 0), ("RIGHTPADDING", (0, 0), (0, -1), 0)]))
        for i, tpc in enumerate(tp["topics"], 1):
            lt.setStyle(TableStyle([("BACKGROUND", (0, i), (0, i), colors.HexColor(tpc["color"]))]))
        pie = Image(pie_chart(tp["chart"]), width=82 * mm, height=66 * mm)
        row = Table([[pie, lt]], colWidths=[86 * mm, 94 * mm])
        row.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
        story += [row, KeepTogether([
            P("Vývoj témat v čase", "h2"),
            Image(bar_chart(data["pairs"], tp["chart"], data["start"], data["end"], tz), width=180 * mm, height=75 * mm),
            P("Počet párů podle dne, kdy vyšla novější z obou položek. Grafy ukazují 5 největších témat, "
              "menší témata jsou sloučena do „Ostatní“ (šedě i v tabulce).", "small")])]

    # Shody podrobne
    shody = [r for r in data["pairs"] if r["verdict"] == "shoda"]
    related = [r for r in data["pairs"] if r["verdict"] != "shoda"][: cfg["report"]["max_pairs_listed"]]
    shody_head = P(f"Shody ({len(shody)})", "h2")
    if not shody:
        story += [shody_head, P("Žádné shody v tomto období.", "body")]
    for i, r in enumerate(shody):
        origin = telegram.username(r["a_origin"])
        src = f' · Telegram @{origin}' if origin else ""
        block = Table([
            [P(f'<b>#{r["no"]} · skóre {r["score"]:.2f} · {TYPE_CZ.get(r["match_type"], "")} · '
               f'téma: {_esc(r["topic"])} · dřív vyšel {r["first"]}</b>', "cell"), ""],
            [P(f'<b>Článek</b> ({_esc(r["a_source"])}, {fmt(r["a_pub"])}{src})<br/>'
               f'<link href="{_esc(r["a_url"])}" color="#2a78d6">{_esc(r["a_title"])}</link>'),
             P(f'<b>Příspěvek</b> ({_esc(r["p_source"])}, {fmt(r["p_pub"])})<br/>'
               f'<link href="{_esc(r["p_url"])}" color="#2a78d6">{_esc(_cut(r["p_text"], 260))}</link>'
               + (f'<br/><font color="{INK2}">Text na obrázku: {_esc(_cut(r["p_image_text"], 160))}</font>'
                  if r["p_image_text"] else ""))],
            [P(f'<font color="{INK2}">{_esc(r["explanation"])}</font>'), ""],
        ], colWidths=[90 * mm, 90 * mm])
        block.setStyle(TableStyle([("SPAN", (0, 0), (1, 0)), ("SPAN", (0, 2), (1, 2)),
                                   ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor(GRID)),
                                   ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f3f2ef")),
                                   ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                   ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
        story += [KeepTogether([shody_head, block] if i == 0 else [block]), Spacer(1, 6)]

    # Souvisejici temata strucne
    if related:
        story.append(CondPageBreak(45 * mm))
        story.append(P(f"Související témata ({len([r for r in data['pairs'] if r['verdict'] != 'shoda'])})", "h2t"))
        rows = [[P("<b>#</b>"), P("<b>Datum</b>"), P("<b>Téma</b>"), P("<b>Článek</b>"), P("<b>Příspěvek</b>")]]
        for r in related:
            rows.append([P(str(r["no"])), P(parse_iso(r["when"]).astimezone(tz).strftime("%d. %m.")), P(_esc(r["topic"])),
                         P(f'<link href="{_esc(r["a_url"])}" color="#2a78d6">{_esc(_cut(r["a_title"], 110))}</link>'),
                         P(f'<link href="{_esc(r["p_url"])}" color="#2a78d6">{_esc(_cut(r["p_text"], 130))}</link>')])
        t = Table(rows, colWidths=[10 * mm, 15 * mm, 31 * mm, 62 * mm, 62 * mm], repeatRows=1)
        t.setStyle(grid)
        t.setStyle(TableStyle(head))
        story.append(t)

    # Telegram zdroje
    tg = defaultdict(Counter)
    private = Counter()
    for r in data["pairs"]:
        name = telegram.username(r["a_origin"])
        if name:
            tg[name][r["verdict"]] += 1
        elif r["a_origin"]:
            private[r["a_origin"]] += 1
    story.append(CondPageBreak(45 * mm))
    story.append(P("Telegram zdroje článků ve shodách", "h2t"))
    if not tg and not private:
        story.append(P("Články ve shodách neuváděly Telegram zdroj.", "body"))
    else:
        rows = [[P("<b>Kanál</b>"), P("<b>Název</b>"), P("<b>Odběratelů</b>"), P("<b>Telegram ID</b>"),
                 P("<b>Shody</b>"), P("<b>Souvis.</b>")]]
        for name, cnt in sorted(tg.items(), key=lambda kv: (-kv[1]["shoda"], -sum(kv[1].values()))):
            info = telegram.channel_info(conn, name)
            subs = f'{info["subscribers"]:,}'.replace(",", " ") if info.get("subscribers") else "—"
            rows.append([P(f'<link href="https://t.me/{name}" color="#2a78d6">@{_esc(name)}</link>'),
                         P(_esc(info.get("title") or "—")), P(subs), P(str(info.get("chat_id") or "—")),
                         P(str(cnt["shoda"])), P(str(cnt["souvisejici_tema"]))])
        for link, n in private.items():
            rows.append([P(f'<link href="{_esc(link)}" color="#2a78d6">soukromá pozvánka</link>'),
                         P("—"), P("—"), P("—"), P("—"), P(str(n))])
        t = Table(rows, colWidths=[40 * mm, 48 * mm, 24 * mm, 34 * mm, 16 * mm, 18 * mm], repeatRows=1)
        t.setStyle(grid)
        t.setStyle(TableStyle(head))
        story.append(t)
        if not telegram.bot_token():
            story.append(Spacer(1, 3))
            story.append(P("Telegram ID doplní bot po nastavení proměnné prostředí TELEGRAM_BOT_TOKEN.", "small"))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(reg, 7.5)
        canvas.setFillColor(colors.HexColor(INK2))
        canvas.drawString(15 * mm, 10 * mm, f"news-fb-matcher · {start:%d. %m.} – {end:%d. %m. %Y}")
        canvas.drawRightString(A4[0] - 15 * mm, 10 * mm, f"strana {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=15 * mm, bottomMargin=16 * mm,
                            title=f"Shrnutí {start:%d. %m.} – {end:%d. %m. %Y}", author="news-fb-matcher")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)



# ---------------------------------------------------------------------------
# Verejne API
# ---------------------------------------------------------------------------
def generate(conn, cfg: dict, days: int | None = None, trigger: str = "manual",
             start: datetime | None = None, end: datetime | None = None) -> dict:
    tz = ZoneInfo(cfg["app"]["timezone"])
    end = end or datetime.now(timezone.utc)
    start = start or end - timedelta(days=days or cfg["report"]["period_days"])
    data = collect(conn, cfg, start, end)
    tp = topics(cfg, data["pairs"])
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ls, le = start.astimezone(tz), end.astimezone(tz)
    path = REPORT_DIR / f"report_{ls:%Y-%m-%d}_{le:%Y-%m-%d}.pdf"
    if path.exists():
        path = REPORT_DIR / f"report_{ls:%Y-%m-%d}_{le:%Y-%m-%d}_{datetime.now(tz):%H%M%S}.pdf"
    build_pdf(conn, data, tp, path, cfg, tz)
    stats = {**data["counts"], "topics": [{"name": t["name"], "pairs": len(t["pairs"])} for t in tp["topics"]]}
    rel = path.relative_to(ROOT).as_posix()
    rid = conn.execute(
        "INSERT INTO reports (period_start, period_end, created_at, trigger, path, stats) VALUES (?, ?, ?, ?, ?, ?)",
        (to_iso(start), to_iso(end), now_iso(), trigger, rel, json.dumps(stats, ensure_ascii=False)),
    ).lastrowid
    conn.commit()
    return {"id": rid, "path": str(path), **stats}


def maybe_auto(conn, cfg: dict, log) -> dict | None:
    """Automaticky report, kdyz od konce posledniho automatickeho uplynulo period_days."""
    rcfg = cfg.get("report", {})
    if not rcfg.get("auto", True):
        return None
    last = conn.execute("SELECT MAX(period_end) FROM reports WHERE trigger = 'auto'").fetchone()[0]
    if last and datetime.now(timezone.utc) - parse_iso(last) < timedelta(days=rcfg["period_days"]):
        return None
    res = generate(conn, cfg, trigger="auto")
    log(f"  report vytvoren: {res['path']} (shod {res['shoda']}, souvisejicich {res['souvisejici']})")
    return res
