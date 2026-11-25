#!/usr/bin/env python3
"""Ops diagnosis Agent — Day 2: demo service + fault injection (plan Ch.9/Ch.11).

Sprint Day 2 deliverable: "演示服务 + 故障注入 + ES 日志写入 —
能手动注入 CPU / OOM 两类故障" (plan p.12). The two faults are the
first two rows of the plan's 15-scenario eval set (Ch.9):
  1. CPU 打满 — 死循环 / 异常流量 (metrics + logs)
  2. 内存 OOM 被杀 — 内存泄漏 / 大对象加载 (logs + status)

This is a *demo* service, stdlib only, no Docker on this machine:
state lives in ``demo_service/state.json`` and every injected fault
writes ES-shaped log documents through ``es_writer`` (live ES when the
RAG compose stack is up, ``es-fallback.jsonl`` otherwise). The Agent's
read-only tools (`search_logs`, `query_metrics`, `get_service_status`
in ``app/tools.py``) read this state, so an injected fault is what the
Agent actually diagnoses — not a second, separate copy of the data.

Usage:
  python3 -m demo_service.service inject cpu   # or: oom
  python3 -m demo_service.service inject clear
  python3 -m demo_service.service status
  # …or through the Agent CLI: python3 -m app.main inject cpu
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from demo_service import es_writer  # noqa: E402

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "state.json"
SERVICE = "web-api"

BASELINE_METRICS = {"error_rate_5m": 0.001, "error_rate_baseline": 0.001,
                    "p95_latency_ms": 180, "p95_baseline_ms": 180,
                    "cpu_pct": 38, "mem_pct": 61}

FAULTS = {
    "cpu": {
        "label": "CPU 打满",
        "metrics": dict(BASELINE_METRICS, error_rate_5m=0.06,
                        p95_latency_ms=2100, cpu_pct=98),
        "status": {"upstream": "degraded", "instances": 2},
        "logs": [
            ("ERROR", "CPU usage 98% sustained for 5m on web-api-1, load average 12.4 (4 cores)"),
            ("WARN", "request queue backing up: p95 latency 2100ms, timeouts rising"),
            ("ERROR", "hot loop suspected in handler /api/report: single thread at 100% CPU"),
        ],
    },
    "oom": {
        "label": "内存 OOM 被杀",
        "metrics": dict(BASELINE_METRICS, error_rate_5m=0.35,
                        p95_latency_ms=3200, cpu_pct=45, mem_pct=99),
        "status": {"upstream": "down", "instances": 1},
        "logs": [
            ("ERROR", "java.lang.OutOfMemoryError: Java heap space in web-api-1"),
            ("ERROR", "Out of memory: Killed process 1234 (java) total-vm 8.2GB, heap grew 200MB/min before kill"),
            ("ERROR", "connect() failed (111: Connection refused) while connecting to upstream 10.0.1.21:8080"),
        ],
    },
    # Day 11-12: scenarios 4-5 (core) + 6-13. Every fault carries 3 log
    # lines whose wording is chosen so the keyword scorer in
    # app/nodes.py can tell the causes apart (see the _HYPOTHESES
    # keywords: each cause's full keyword set appears in its own logs
    # and in no other fault's logs, metrics or status summary — in
    # particular nothing here contains "upstream"/"502"/"deploy"/
    # "error" as a hypothesis keyword, so the unknown symptom still
    # scores 0). Metrics keep error_rate_5m well above the 0.01
    # verify threshold so verify() only passes after the fault is
    # cleared by the plan's high-risk step.
    "disk": {
        "label": "磁盘写满",
        "metrics": dict(BASELINE_METRICS, error_rate_5m=0.28,
                        p95_latency_ms=1800, cpu_pct=45, mem_pct=70,
                        disk_pct=100),
        "status": {"upstream": "degraded", "instances": 2},
        "logs": [
            ("ERROR", "No space left on device: disk usage 100% on /var, writes failing"),
            ("WARN", "disk full: /var/log 98% full, logrotate not running, logs not rotated"),
            ("ERROR", "deleted file still held by process: lsof shows deleted log held by web-api-1, disk space not freed"),
        ],
    },
    "slowquery": {
        "label": "数据库慢查询",
        "metrics": dict(BASELINE_METRICS, error_rate_5m=0.12,
                        p95_latency_ms=2400, cpu_pct=85, mem_pct=70),
        "status": {"upstream": "degraded", "instances": 2},
        "logs": [
            ("ERROR", "slow query detected: SELECT * FROM orders took 12.3s, rows_examined 1.2M, full table scan, missing index"),
            ("ERROR", "lock wait timeout: transaction waiting for row lock on orders, SHOW ENGINE INNODB STATUS shows lock wait 8s"),
            ("WARN", "slow query count rising: p95 latency 2400ms, database CPU 85%, slow queries 120/min"),
        ],
    },
    "release": {
        "label": "发布后错误率上升",
        "metrics": dict(BASELINE_METRICS, error_rate_5m=0.38,
                        p95_latency_ms=2600, cpu_pct=60, mem_pct=65),
        "status": {"upstream": "degraded", "instances": 2},
        "logs": [
            ("ERROR", "release regression suspected: v1.4.2 error rate 0.02 -> 0.38 within 10m of deploy"),
            ("WARN", "new version v1.4.2 exceptions rising: NullPointerException in /api/report after release"),
            ("ERROR", "deploy v1.4.2 finished, 2 instances rolling; error rate rising, rollback to v1.4.1 advised"),
        ],
    },
    "downstream": {
        "label": "下游超时雪崩",
        "metrics": dict(BASELINE_METRICS, error_rate_5m=0.31,
                        p95_latency_ms=8000, cpu_pct=55, mem_pct=68),
        "status": {"upstream": "degraded", "instances": 2},
        "logs": [
            ("ERROR", "downstream timeout: payment-service call timeout 3000ms, latency p99 8000ms"),
            ("WARN", "downstream dependency slow: payment-service p99 8000ms, thread pool waiting, circuit breaker not configured"),
            ("ERROR", "downstream timeout cascade: retries amplifying traffic, error rate rising"),
        ],
    },
    "pool": {
        "label": "连接池耗尽",
        "metrics": dict(BASELINE_METRICS, error_rate_5m=0.22,
                        p95_latency_ms=4500, cpu_pct=50, mem_pct=72),
        "status": {"upstream": "degraded", "instances": 2},
        "logs": [
            ("ERROR", "connection pool exhausted: active 50/50, waiting threads 120, no idle connections"),
            ("WARN", "connection leak suspected: connections not returned after query, pool wait queue growing"),
            ("ERROR", "connection pool exhausted again: timeout waiting for connection from pool"),
        ],
    },
    "config": {
        "label": "配置变更引发异常",
        "metrics": dict(BASELINE_METRICS, error_rate_5m=0.41,
                        p95_latency_ms=900, cpu_pct=40, mem_pct=62),
        "status": {"upstream": "degraded", "instances": 2},
        "logs": [
            ("ERROR", "config change detected: rate_limit threshold changed 1000 -> 10, requests rejected 429"),
            ("WARN", "rate_limit threshold 10 too low: 90% requests rejected after config change"),
            ("ERROR", "config change by operator: limit threshold misconfigured, rollback config advised"),
        ],
    },
    "cert": {
        "label": "证书过期",
        "metrics": dict(BASELINE_METRICS, error_rate_5m=0.45,
                        p95_latency_ms=1200, cpu_pct=42, mem_pct=63),
        "status": {"upstream": "degraded", "instances": 2},
        "logs": [
            ("ERROR", "certificate expired: HTTPS certificate for web-api expired 2026-11-20, TLS handshake failed"),
            ("ERROR", "TLS handshake failed: certificate expired, clients report CERT_HAS_EXPIRED"),
            ("WARN", "certificate expires: HTTPS certificate expired, renew certificate immediately"),
        ],
    },
    "mq": {
        "label": "消息队列积压",
        "metrics": dict(BASELINE_METRICS, error_rate_5m=0.18,
                        p95_latency_ms=1500, cpu_pct=48, mem_pct=66),
        "status": {"upstream": "degraded", "instances": 2},
        "logs": [
            ("ERROR", "message queue backlog growing: 120000 messages pending, consumer lag 45m"),
            ("WARN", "consumer stalled: mq consumer stopped processing, backlog rising, no new acks"),
            ("ERROR", "mq backlog alert: queue depth 120000, consumer throughput dropped to 0"),
        ],
    },
    "cache": {
        "label": "缓存击穿",
        "metrics": dict(BASELINE_METRICS, error_rate_5m=0.33,
                        p95_latency_ms=3800, cpu_pct=88, mem_pct=75),
        "status": {"upstream": "degraded", "instances": 2},
        "logs": [
            ("ERROR", "cache breakdown: hot key expired, 10000 requests hit database directly"),
            ("WARN", "hot key expired: cache miss rate 99%, database connections surging after hot key expiry"),
            ("ERROR", "cache stampede: hot key expired at same second, database overwhelmed"),
        ],
    },
    "jitter": {
        "label": "单实例网络抖动",
        "metrics": dict(BASELINE_METRICS, error_rate_5m=0.09,
                        p95_latency_ms=2600, cpu_pct=44, mem_pct=64),
        "status": {"upstream": "degraded", "instances": 2},
        "logs": [
            ("ERROR", "network jitter on web-api-2 only: packet loss 35%, latency spikes, other instances healthy"),
            ("WARN", "single instance network jitter: web-api-2 packet loss 35%, retransmits rising"),
            ("ERROR", "network jitter: web-api-2 unreachable intermittently, packet loss 35%"),
        ],
    },
}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"service": SERVICE, "fault": None, "metrics": BASELINE_METRICS,
            "status": {"upstream": "healthy", "instances": 2}}


def _save(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                          encoding="utf-8")


def inject(fault: str) -> dict:
    """Inject `cpu` or `oom`; returns the new state (with where logs went)."""
    if fault not in FAULTS:
        raise ValueError(f"unknown fault {fault!r}; use cpu, oom or clear")
    spec = FAULTS[fault]
    docs = [{"@timestamp": _now(), "service": SERVICE, "level": lvl,
             "message": msg, "fault": fault} for lvl, msg in spec["logs"]]
    dest, n = es_writer.write_logs(docs)
    state = {"service": SERVICE, "fault": fault, "label": spec["label"],
             "injected_at": _now(), "metrics": spec["metrics"],
             "status": spec["status"], "logs_written": n, "logs_via": dest}
    _save(state)
    return state


def clear() -> dict:
    """Clear any injected fault; the service returns to baseline."""
    doc = {"@timestamp": _now(), "service": SERVICE, "level": "INFO",
           "message": "fault cleared, web-api back to baseline", "fault": None}
    dest, n = es_writer.write_logs([doc])
    state = {"service": SERVICE, "fault": None, "label": None,
             "cleared_at": _now(), "metrics": BASELINE_METRICS,
             "status": {"upstream": "healthy", "instances": 2},
             "logs_written": n, "logs_via": dest}
    _save(state)
    return state


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="demo-service")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("inject")
    p.add_argument("fault", choices=["cpu", "oom", "disk", "slowquery",
                                     "release", "downstream", "pool",
                                     "config", "cert", "mq", "cache",
                                     "jitter", "clear"])
    sub.add_parser("status")
    args = ap.parse_args(argv)
    if args.cmd == "inject":
        state = clear() if args.fault == "clear" else inject(args.fault)
    else:
        state = load_state()
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
