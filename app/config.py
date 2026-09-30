import os
import tomllib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: str | None = None) -> dict:
    # MATCHER_CONFIG umozni spustit s jinym configem (napr. testovaci DB).
    p = Path(path or os.environ.get("MATCHER_CONFIG") or ROOT / "config.toml")
    with open(p, "rb") as f:
        return tomllib.load(f)


def _dotenv() -> dict:
    """Soubor .env v koreni projektu (KLIC=hodnota na radek). Do gitu nepatri – je v .gitignore."""
    path = ROOT / ".env"
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def env(name: str) -> str | None:
    """Tajny klic: na Windows nejdriv aktualni hodnota z uzivatelskeho registru (setx – projevi se bez
    restartu; promenna v bezicim procesu muze byt zastarala), pak promenna prostredi, pak soubor .env."""
    val = None
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                val = winreg.QueryValueEx(key, name)[0]
        except OSError:
            val = None
    return val or os.environ.get(name) or _dotenv().get(name) or None


def db_path(cfg: dict) -> Path:
    return ROOT / cfg["app"]["db_path"]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def to_iso(dt: datetime) -> str:
    """Datum -> ISO v UTC (naivni datum bereme jako UTC)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
