"""Prepocita skore vsech paru (napr. po zmene vah nebo vzorce v config.toml).

AI posouzeni zustavaji: pokud par znovu projde prahem, pouzije se existujici verdikt
a AI se neplati znovu. Pak spustit beh (nebo rovnou: python rescore.py --judge).
"""
import sys

from app.candidates import score_new
from app.config import load_config
from app.db import connect
from app.judge import judge_new

if __name__ == "__main__":
    cfg = load_config()
    conn = connect(cfg)
    conn.execute("DELETE FROM matches")
    conn.execute("UPDATE items SET scored_at = NULL WHERE status = 'ready'")
    conn.commit()
    print("ulozeno paru:", score_new(conn, cfg, print))
    if "--judge" in sys.argv:
        print("posouzeno:", judge_new(conn, cfg, print))
