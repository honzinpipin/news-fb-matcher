"""Tenka vrstva nad OpenAI API: strukturovany vystup (JSON schema) a embeddingy."""
import json
import threading
import time

import requests

from .config import env


class AIError(Exception):
    pass


# Soucet tokenu za aktualni beh (pipeline ho vypise do logu).
_usage_lock = threading.Lock()
usage = {"prompt_tokens": 0, "completion_tokens": 0, "embedding_tokens": 0}


def reset_usage() -> None:
    with _usage_lock:
        for k in usage:
            usage[k] = 0


def _add_usage(**kw) -> None:
    with _usage_lock:
        for k, v in kw.items():
            usage[k] += v or 0


def api_key(cfg: dict) -> str | None:
    return env("OPENAI_API_KEY") or (cfg["openai"].get("api_key") or "").strip() or None


def ready(cfg: dict) -> bool:
    return bool(api_key(cfg))


def _post(cfg: dict, path: str, payload: dict) -> dict:
    key = api_key(cfg)
    if not key:
        raise AIError("Chybi OPENAI_API_KEY.")
    url = cfg["openai"]["base_url"].rstrip("/") + path
    last = None
    for attempt in range(4):
        try:
            r = requests.post(
                url,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
                timeout=cfg["openai"].get("timeout_s", 120),
            )
        except requests.RequestException as e:
            last = str(e)
        else:
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}: {r.text[:300]}"
            if r.status_code not in (408, 409, 429) and r.status_code < 500:
                break  # chyba pozadavku, opakovani nepomuze
        time.sleep(2 ** attempt * 2)
    raise AIError(last or "neznama chyba")


def chat_json(cfg: dict, system: str, user: str, schema: dict, name: str) -> dict:
    # Novejsi modely (gpt-5.x) nepodporuji temperature != 1, proto ji neposilame.
    data = _post(cfg, "/chat/completions", {
        "model": cfg["openai"]["model"],
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": name, "strict": True, "schema": schema},
        },
    })
    u = data.get("usage") or {}
    _add_usage(prompt_tokens=u.get("prompt_tokens"), completion_tokens=u.get("completion_tokens"))
    msg = data["choices"][0]["message"]
    if msg.get("refusal"):
        raise AIError(f"Model odmitl odpovedet: {msg['refusal']}")
    try:
        return json.loads(msg["content"])
    except (TypeError, json.JSONDecodeError) as e:
        raise AIError(f"Neplatny JSON od modelu: {e}")


def embed(cfg: dict, texts: list[str]) -> list[list[float]]:
    data = _post(cfg, "/embeddings", {"model": cfg["openai"]["embedding_model"], "input": texts})
    _add_usage(embedding_tokens=(data.get("usage") or {}).get("total_tokens"))
    return [d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"])]
