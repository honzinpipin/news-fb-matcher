"""Spusti webove rozhrani: python run_server.py -> http://127.0.0.1:8010"""
import uvicorn

from app.config import load_config

if __name__ == "__main__":
    cfg = load_config()
    uvicorn.run("app.web:app", host=cfg["app"]["host"], port=cfg["app"]["port"])
