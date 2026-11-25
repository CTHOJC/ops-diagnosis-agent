#!/usr/bin/env python3
"""Ops diagnosis Agent — Day 1 skeleton: the 8 tools (plan Ch.5).

Risk grading follows the plan exactly:
  read-only, auto:  search_logs, query_metrics, search_runbook,
                    get_service_status
  high-risk, human confirmation required: restart_service, scale_service,
                    rollback_release, update_config (snapshot first)

Day 1 reality, stated plainly: there is no live ES / Prometheus / Milvus
yet, so the read-only tools answer from a small built-in demo dataset that
mirrors the RAG starter corpus (same 6 sample ops manuals). Day 6:
search_runbook runs the real three-stage retrieval in app/retrieval.py
(local BM25 + TF-IDF + RRF + rerank over this project's own 35-chunk
index, built from the 7 sample manuals incl. cpu-high.md) and falls back
to the built-in mini runbook only when no index and no corpus exist.
High-risk tools act only on the in-memory demo service, never on anything
real, and refuse without an approval record (plan Ch.7).
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# Day 5: a runbook chunk must share at least this many distinct query
# tokens to count as evidence (see search_runbook). A single shared
# token is usually a bare number and fabricates evidence for unrelated
# symptoms. Verified on the demo alerts: real faults score >= 2,
# an unrelated symptom scores at most 1.
MIN_RUNBOOK_OVERLAP = 2

# ---------------------------------------------------------------------------
# Tool registry: name -> (risk, auto)
# ---------------------------------------------------------------------------
TOOLS = {
    "search_logs": ("read-only", True),
    "query_metrics": ("read-only", True),
    "search_runbook": ("read-only", True),
    "get_service_status": ("read-only", True),
    "restart_service": ("high", False),
    "scale_service": ("high", False),
    "rollback_release": ("high", False),
    "update_config": ("high", False),
}

# ---------------------------------------------------------------------------
# Demo dataset (Day 1 stand-in for ES / Prometheus / demo-service).
# Mirrors the RAG starter's nginx-502 manual so the two projects agree.
# ---------------------------------------------------------------------------
DEMO_LOGS = [
    {"ts": "2026-11-16T06:55:01", "service": "web-api",
     "level": "ERROR",
     "msg": "connect() failed (111: Connection refused) while connecting to upstream 10.0.1.21:8080"},
    {"ts": "2026-11-16T06:55:02", "service": "web-api",
     "level": "ERROR",
     "msg": "upstream timed out (110: Connection timed out) while reading response header from upstream"},
    {"ts": "2026-11-16T06:54:40", "service": "web-api",
     "level": "WARN",
     "msg": "upstream 10.0.1.21:8080 health check failed 3 times in a row"},
    {"ts": "2026-11-16T06:50:12", "service": "web-api",
     "level": "INFO",
     "msg": "deploy v1.4.2 finished, 2 instances rolling"},
]

DEMO_METRICS = {
    "web-api": {
        "error_rate_5m": 0.42, "error_rate_baseline": 0.001,
        "p95_latency_ms": 3200, "p95_baseline_ms": 180,
        "cpu_pct": 38, "mem_pct": 61,
    }
}

# ---------------------------------------------------------------------------
# Day 2: the demo service is the source of truth for injected faults.
# Its state file + ES-shaped logs (demo_service/) are read here so the
# Agent diagnoses the fault that was actually injected, not a copy.
# ---------------------------------------------------------------------------
DEMO_STATE_FILE = ROOT / "demo_service" / "state.json"
DEMO_LOG_FILE = ROOT / "demo_service" / "es-fallback.jsonl"


def _demo_state() -> dict:
    if DEMO_STATE_FILE.exists():
        try:
            return json.loads(DEMO_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _demo_logs() -> list[dict]:
    """ES-shaped docs from the demo service, as {ts, service, level, msg}.

    Only the CURRENT injected fault's logs are returned: the fallback file
    accumulates docs across injects (like an ES index), so without this
    filter a later diagnosis would read stale logs from an earlier fault.
    When no fault is injected, no demo logs are returned.
    """
    out = []
    fault = _demo_state().get("fault")
    if not fault or not DEMO_LOG_FILE.exists():
        return out
    for line in DEMO_LOG_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        if d.get("fault") != fault:
            continue
        out.append({"ts": d.get("@timestamp", ""), "service": d.get("service", ""),
                    "level": d.get("level", ""), "msg": d.get("message", "")})
    # The fallback file accumulates docs across injects of the same fault,
    # so the same message appears once per inject. Keep only the latest
    # occurrence of each message: otherwise evidence carries the timestamp
    # of the first-ever inject of this fault, not the one just injected.
    seen, latest = set(), []
    for log in reversed(out):
        if log["msg"] not in seen:
            seen.add(log["msg"])
            latest.append(log)
    return list(reversed(latest))


def _clear_demo_fault() -> None:
    """A restart of the demo service clears the injected fault (Day 2):
    the service comes back at baseline, so verify() sees recovery."""
    st = _demo_state()
    if st.get("fault"):
        st.update({"fault": None, "label": None, "cleared_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "metrics": {"error_rate_5m": 0.001, "error_rate_baseline": 0.001,
                               "p95_latency_ms": 180, "p95_baseline_ms": 180,
                               "cpu_pct": 38, "mem_pct": 61},
                   "status": {"upstream": "healthy", "instances": 2}})
        DEMO_STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2),
                                   encoding="utf-8")


# Fallback runbook when the RAG starter chunks are not on disk.
FALLBACK_RUNBOOK = [
    {"chunk_id": "demo-nginx-502-a", "source": "nginx-502 / 定位步骤",
     "text": "1. 确认上游进程是否存活：检查端口监听和进程状态，确认服务没有 OOM 退出。"
             "2. 检查上游连接数，确认是否打满连接池。"
             "3. 检查 Nginx 配置 proxy_connect_timeout、proxy_read_timeout 是否小于上游真实耗时。"},
    {"chunk_id": "demo-nginx-502-b", "source": "nginx-502 / 处理方法",
     "text": "上游实例宕机时重启上游实例，确认健康检查通过后再放量；"
             "上游依赖先挂时先恢复依赖，再重启上游。"},
]


def _audit(state, tool, args, ms, summary):
    state.tool_audit.append({
        "tool": tool, "args": args, "ms": round(ms, 2),
        "summary": summary, "ts": time.strftime("%H:%M:%S"),
    })


def _tokens(text):
    return [t for t in re.split(r"[^0-9a-zA-Z\u4e00-\u9fff]+", text.lower()) if t]


# ---------------------------------------------------------------------------
# Read-only tools (auto)
# ---------------------------------------------------------------------------
def search_logs(state, service, keywords, window_min=30):
    t0 = time.time()
    # Day 2: injected-fault logs (ES-shaped, via demo_service) first,
    # then the Day 1 static set; dedupe on message text, so repeated
    # injects of the same fault don't spam the evidence list.
    all_logs = _demo_logs() + DEMO_LOGS
    seen, logs = set(), []
    for l in all_logs:
        key = l["msg"]
        if key not in seen:
            seen.add(key)
            logs.append(l)
    hits = [l for l in logs
            if l["service"] == service
            and any(k.lower() in l["msg"].lower() for k in keywords)]
    _audit(state, "search_logs", {"service": service, "keywords": keywords},
           (time.time() - t0) * 1000, f"{len(hits)} log lines")
    return hits[:10]  # plan Ch.5: results are trimmed, never raw dumps


def query_metrics(state, service):
    t0 = time.time()
    m = DEMO_METRICS.get(service, {})
    # Day 2: an injected fault overlays the metrics (CPU 98% / mem 99%).
    demo = _demo_state()
    if demo.get("fault") and demo.get("metrics"):
        m = dict(m, **demo["metrics"])
    elif state.demo_service.get("upstream") == "healthy":
        # Day 6: a healthy-baseline preset (cpu/oom/unknown incidents,
        # see main._new_incident) means "no fault, service fine" — use
        # the demo service's baseline, not the 502-flavoured static
        # DEMO_METRICS (error_rate 0.42). An injected fault above
        # always wins over the preset.
        m = dict(m, error_rate_5m=0.001, p95_latency_ms=180,
                 cpu_pct=38, mem_pct=61)
    # Post-restart verification values. This used to be
    # `upstream == "healthy"`, which also matched a healthy preset at
    # incident creation and zeroed an injected fault's metrics during
    # collection; "restarted" is the actual condition (restart_service
    # is the only writer of restarted_at).
    if state.demo_service.get("restarted_at"):
        m = dict(m, error_rate_5m=0.0, p95_latency_ms=190,
                 cpu_pct=38, mem_pct=61)
    _audit(state, "query_metrics", {"service": service},
           (time.time() - t0) * 1000, "error_rate_5m=%.3f" % m.get("error_rate_5m", -1))
    return m


def search_runbook(state, query, top_k=3):
    """Module 3: Day 6 three-stage retrieval (app/retrieval.py) — BM25 +
    TF-IDF cosine fused with RRF k=60, then reranked (cross-encoder
    when installed, local lexical otherwise — retrieval.info always
    names which one actually ran), over this project's own 35-chunk
    index (7 manuals incl. cpu-high.md).

    A chunk is evidence only if it shares at least MIN_RUNBOOK_OVERLAP
    distinct whole-word tokens with the query. A single shared token
    is noise — on this corpus it is usually a bare number ("5", "15")
    — and returning such chunks fabricates evidence for unrelated
    symptoms: before this floor, the Day 5 unknown-symptom demo
    retrieved the nginx-502 manual on a one-token overlap and the
    keyword scorer then "diagnosed" an upstream outage at confidence
    1.00 for a font/CSS complaint. Verified overlaps on the demo
    alerts: real faults score >= 2 (502: 17, cpu: 4, oom: 2), the
    unknown symptom scores at most 1.
    """
    t0 = time.time()
    manual: list[dict] = []
    backend_note = ""
    try:
        from . import retrieval
        manual, info = retrieval.search(
            query, top_k=top_k, min_overlap=MIN_RUNBOOK_OVERLAP)
        backend_note = (f"reranker={info['reranker']}; "
                        f"es={info['es']['status']}; "
                        f"milvus={info['milvus']['status']}")
    except Exception as e:  # noqa: BLE001 — no index and no corpus:
        # fall back to the built-in mini runbook (same overlap gate),
        # never fail the evidence round over the handbook.
        docs = [{"chunk_id": d["chunk_id"], "doc": d["source"].split(" / ")[0],
                  "section": d["source"].split(" / ")[1], "text": d["text"]}
                 for d in FALLBACK_RUNBOOK]
        qt = set(_tokens(query))
        scored = []
        for d in docs:
            overlap = len(qt & set(_tokens(d.get("text", ""))))
            if overlap >= MIN_RUNBOOK_OVERLAP:
                scored.append((overlap, d))
        scored.sort(key=lambda x: -x[0])
        manual = [{"chunk_id": d["chunk_id"],
                   "source": f"{d.get('doc', '')} / {d.get('section', '')}",
                   "text": d.get("text", "")[:200]} for _, d in scored[:top_k]]
        backend_note = (f"fallback runbook: retrieval unavailable: {e}")
    # Day 10: history cases are a supplement, never a replacement —
    # at most one, appended AFTER the manual Top 3 (see
    # retrieval.search_cases: same MIN_RUNBOOK_OVERLAP gate, matched
    # on the case's "根因 + 处置摘要"). A case lookup failure never
    # breaks the manual retrieval above.
    cases: list[dict] = []
    try:
        from . import retrieval as _retrieval
        cases = _retrieval.search_cases(
            query, min_overlap=MIN_RUNBOOK_OVERLAP)
    except Exception:  # noqa: BLE001 — cases are best-effort
        cases = []
    _audit(state, "search_runbook", {"query": query},
           (time.time() - t0) * 1000,
           f"{len(manual)} runbook chunks"
           + (f" + {len(cases)} 历史案例" if cases else "")
           + f" ({backend_note})")
    return manual + cases


def get_service_status(state, service):
    t0 = time.time()
    upstream = state.demo_service.get("upstream", "down")
    instances = 2 if upstream == "healthy" else 1
    # Day 2: an injected fault is the live status (oom => down, cpu => degraded).
    demo = _demo_state()
    if demo.get("fault") and demo.get("status"):
        upstream = demo["status"].get("upstream", upstream)
        instances = demo["status"].get("instances", instances)
    out = {"service": service, "upstream": upstream,
           "instances": instances,
           "last_deploy": "v1.4.2 2026-11-16T06:50",
           "version": "v1.4.2"}
    _audit(state, "get_service_status", {"service": service},
           (time.time() - t0) * 1000, f"upstream={upstream}")
    return out


# ---------------------------------------------------------------------------
# High-risk tools (human confirmation required; demo service only)
# ---------------------------------------------------------------------------
def _require_approval(state, tool):
    if not state.approval.get("approved"):
        raise PermissionError(
            f"{tool} is high-risk and needs human approval first "
            "(plan Ch.5/Ch.7). Run the approve step, then resume.")


def restart_service(state, service):
    _require_approval(state, tool="restart_service")
    t0 = time.time()
    state.demo_service["upstream"] = "healthy"
    state.demo_service["restarted_at"] = time.strftime("%H:%M:%S")
    _clear_demo_fault()  # Day 2: restart clears the injected fault
    _audit(state, "restart_service", {"service": service},
           (time.time() - t0) * 1000, "upstream restarted (demo)")
    return {"ok": True, "service": service, "upstream": "healthy"}


def scale_service(state, service, replicas):
    _require_approval(state, tool="scale_service")
    t0 = time.time()
    state.demo_service["replicas"] = replicas
    _audit(state, "scale_service", {"service": service, "replicas": replicas},
           (time.time() - t0) * 1000, f"replicas={replicas} (demo)")
    return {"ok": True, "service": service, "replicas": replicas}


def rollback_release(state, service):
    _require_approval(state, tool="rollback_release")
    t0 = time.time()
    state.demo_service["version"] = "v1.4.1"
    # Day 11-12 (demo semantics): rolling back the release removes
    # the faulty version, so when the injected fault IS the bad
    # release (scenario 6) the fault is cleared — otherwise verify()
    # could never see recovery after a rollback. Any other injected
    # fault is left alone (a release rollback does not fix a disk
    # full or an OOM). restart_service already cleared the fault on
    # the Day 9 failure path before this runs, so this is a no-op
    # there.
    if _demo_state().get("fault") == "release":
        _clear_demo_fault()
    _audit(state, "rollback_release", {"service": service},
           (time.time() - t0) * 1000, "rolled back to v1.4.1 (demo)")
    return {"ok": True, "service": service, "version": "v1.4.1"}


def update_config(state, service, key, value):
    _require_approval(state, tool="update_config")
    t0 = time.time()
    # Plan Ch.5: config changes snapshot first.
    snap = {"service": service, "key": key,
            "old": state.demo_service.get("config", {}).get(key),
            "new": value, "ts": time.strftime("%H:%M:%S")}
    state.demo_service.setdefault("config", {})[key] = value
    state.demo_service.setdefault("config_snapshots", []).append(snap)
    # Day 11-12 (demo semantics, same rule as rollback_release
    # above): restoring the config is the fix for the injected
    # config-change fault (scenario 9) — when the injected fault IS
    # the bad config change, applying the corrected value clears it,
    # so verify() can see recovery. Any other fault is untouched.
    if _demo_state().get("fault") == "config":
        _clear_demo_fault()
    _audit(state, "update_config", {"service": service, "key": key},
           (time.time() - t0) * 1000, f"{key}={value} (snapshot kept)")
    return {"ok": True, "snapshot": snap}


HIGH_RISK_TOOLS = {"restart_service": restart_service,
                   "scale_service": scale_service,
                   "rollback_release": rollback_release,
                   "update_config": update_config}
