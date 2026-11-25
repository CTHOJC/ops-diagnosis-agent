#!/usr/bin/env python3
"""Ops diagnosis Agent — Day 2: ES log writer (plan Ch.3/Ch.10, sprint Day 2).

Writes log documents in Elasticsearch shape to index ``ops-logs`` at
``OPS_ES_URL`` (default http://127.0.0.1:9200 — the same ES instance the
RAG project runs in docker-compose.yml, port 9200, localhost only).

Honest fallback, same as the rest of this project: this machine has no
Docker / no live ES, so when ES is unreachable the exact same document
is appended to ``demo_service/es-fallback.jsonl`` and read back from
there. The document shape is identical either way, so swapping the
fallback for the live index changes no caller — ``search_logs`` in
``app/tools.py`` reads through ``read_logs()`` here.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
FALLBACK_FILE = HERE / "es-fallback.jsonl"
INDEX = "ops-logs"


def es_url() -> str:
    return os.environ.get("OPS_ES_URL", "http://127.0.0.1:9200").rstrip("/")


def _es_post(path: str, payload: dict, timeout: float = 2.0) -> dict:
    req = urllib.request.Request(
        f"{es_url()}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def write_logs(logs: list[dict]) -> tuple[str, int]:
    """Write log docs; returns ("es"|"file", count written).

    A doc is {"@timestamp", "service", "level", "message", "fault"} —
    the fields ES would index, plus nothing ES-specific.
    """
    written_es = 0
    for doc in logs:
        try:
            _es_post(f"/{INDEX}/_doc", doc)
            written_es += 1
        except Exception:
            break
    if written_es == len(logs):
        return "es", written_es
    # Fallback: same docs, one JSON line each, so search_logs can read them.
    with FALLBACK_FILE.open("a", encoding="utf-8") as f:
        for doc in logs[written_es:]:
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")
    return "file", len(logs)


def read_logs(service: str | None = None, limit: int = 100) -> list[dict]:
    """Read log docs, live ES first, fallback file when ES is down."""
    try:
        body = {"size": limit, "sort": [{"@timestamp": "desc"}],
                "query": {"match": {"service": service}} if service else {"match_all": {}}}
        res = _es_post(f"/{INDEX}/_search", body)
        return [hit["_source"] for hit in res["hits"]["hits"]]
    except Exception:
        pass
    if not FALLBACK_FILE.exists():
        return []
    docs = [json.loads(line) for line in
            FALLBACK_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]
    if service:
        docs = [d for d in docs if d.get("service") == service]
    return docs[-limit:]
