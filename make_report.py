"""Vytvori PDF report (shrnuti shod) za obdobi.

python make_report.py              -> poslednich [report] period_days dni (vychozi 7)
python make_report.py --days 30    -> poslednich 30 dni
python make_report.py --auto       -> zapocita se jako automaticky (odtud bezi 7denni cyklus)
"""
import argparse

from app import report
from app.config import load_config
from app.db import connect

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int)
    ap.add_argument("--auto", action="store_true")
    a = ap.parse_args()
    cfg = load_config()
    res = report.generate(connect(cfg), cfg, days=a.days, trigger="auto" if a.auto else "manual")
    print(f"Report: {res['path']}")
    print(f"Shod {res['shoda']}, souvisejicich {res['souvisejici']}, temata: "
          + ", ".join(f"{t['name']} ({t['pairs']})" for t in res["topics"]))
