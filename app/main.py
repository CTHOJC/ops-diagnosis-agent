#!/usr/bin/env python3
"""Ops diagnosis Agent — Day 1 skeleton: CLI entry point.

Usage (from the ops-agent/ directory):
  python3 -m app.main demo
      Run the preset nginx-502 alert. Stops at human_approval (the plan is
      high-risk), persists the state, and prints how to resume.
  python3 -m app.main demo --auto-approve
      Same run, but auto-approves at the checkpoint (one-shot demo/CI).
  python3 -m app.main inject cpu|oom|disk|slowquery|release|downstream|pool|config|cert|mq|cache|jitter|clear
      Inject a demo fault into the demo service (or clear it).
  python3 -m app.main demo --alert cpu|oom|disk-full|slow-query|release|downstream|pool|config|cert|mq|cache|jitter|unknown
      Run a preset alert (default nginx-502). Stops at human_approval
      unless --auto-approve is given.
  python3 -m app.main modify <incident_id> --by eng --reason <why> --drop-step <n>[,<n>...]
      Drop plan step(s) of an incident stopped at human_approval,
      then resume it (Day 8; rollback steps cannot be dropped).
  python3 -m app.main expire [--hours N]
      Close awaiting_approval incidents older than N hours (default 24).
  python3 -m app.main diagnosis <incident_id>
      Print the stored structured Diagnosis (diagnosis/v1) as JSON.
  python3 -m app.main reject <incident_id> --by eng --reason "先别动"
      Record rejection; the graph closes with a report instead of executing.
  python3 -m app.main status <incident_id>
      Show status, node trace and tool audit for a persisted incident.
  python3 -m app.main trace <incident_id>
      Replay the incident as a human-readable story in time order
      (Day 10: nodes, tool calls, key decisions; any status).
  python3 -m app.main report <incident_id>
      Print the saved report, or render one on the spot from the
      persisted state when none was saved (Day 10).

Day 5 scope: diagnose now emits a validated structured Diagnosis
(source="rules" by default — the LLM engine in app/diagnosis.py is
wired in behind diagnose_with_engine() and runs when
OPS_DIAGNOSIS_ENGINE selects it and OPS_LLM_API_KEY is set; any LLM
failure is recorded on the state and falls back to rules loudly. No
real LLM endpoint is called by any test: the stub tests in the Day 5
verification use a local stdlib HTTP server). A `diagnosis` subcommand
prints the stored Diagnosis as JSON.

Day 1 scope: demo data only, no live ES / Prometheus / Milvus, no LLM,
no FastAPI yet (those land on sprint Days 2-3). Everything here runs with
the standard library only.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import graph, nodes, store  # noqa: E402
from app.state import AgentState  # noqa: E402
from demo_service import service as demo_service  # noqa: E402

DEMO_ALERTS = Path(__file__).resolve().parent.parent / "demo_alerts"
ALERT_FILES = {"nginx-502": "nginx-502.json", "cpu": "cpu-high.json",
               "oom": "oom-killed.json", "unknown": "unknown-symptom.json",
               # Day 11-12: scenarios 4-13.
               "disk-full": "disk-full.json", "slow-query": "slow-query.json",
               "release": "release.json", "downstream": "downstream.json",
               "pool": "pool.json", "config": "config.json",
               "cert": "cert.json", "mq": "mq.json",
               "cache": "cache.json", "jitter": "jitter.json"}


def _new_incident(alert: dict, alert_key: str = "nginx-502") -> AgentState:
    # Millisecond precision + existence bump: two demos in the same second
    # must never share an incident_id (a same-second collision silently
    # overwrote an earlier incident's state and report).
    base = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
    incident_id = f"INC-{base}"
    n = 2
    while store.exists(incident_id):
        incident_id = f"INC-{base}-{n}"
        n += 1
    # Day 6: the initial demo-service state depends on the alert type.
    # nginx-502 keeps the old down preset — the 502 demo has no fault
    # injection, its evidence comes from this preset (static logs /
    # metrics) — while cpu / oom / unknown start from a healthy
    # baseline. The old code hardcoded upstream "down" into every
    # incident, so an unrelated event scored a phantom "上游实例宕机"
    # 0.25 (the status summary fed the diagnose keyword scorer) and an
    # unknown symptom showed error_rate 0.42 from the 502-flavoured
    # static metrics. An injected fault still overrides the baseline
    # (tools.query_metrics / get_service_status read the demo-service
    # fault state first), and query_metrics only zeroes metrics after
    # an actual restart (restarted_at), not from the preset.
    upstream = "down" if alert_key == "nginx-502" else "healthy"
    state = AgentState(
        incident_id=incident_id, alert=alert,
        demo_service={"upstream": upstream, "replicas": 2,
                      "version": "v1.4.2", "config": {}})
    store.save(state)
    return state


def _print_trace(state):
    print("nodes: " + " -> ".join(t["node"] for t in state.trace))
    for t in state.trace:
        print(f"  {t['node']}: {t['ms']}ms ({t.get('note', '')})")


def _dedup_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "dedup.json"


def _dedup_fingerprint(alert: dict) -> str:
    fp = alert.get("fingerprint") or ""
    if fp:
        return str(fp)
    return f"{alert.get('service', 'web-api')}:{alert.get('title', '')}"


def _dedup_load() -> dict:
    p = _dedup_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _dedup_check(alert: dict) -> str | None:
    """Return the existing incident id when this alert dedups into an
    open event, else None. A record older than 10 minutes, or one
    whose event already ended, does not dedup."""
    rec = _dedup_load().get(_dedup_fingerprint(alert)) or {}
    incident_id = rec.get("incident_id")
    if not incident_id:
        return None
    if time.time() - float(rec.get("created_at", 0)) > 600:
        return None
    try:
        prev = store.load(incident_id)
    except Exception:
        return None
    if prev.status in ("awaiting_approval", "running", "executing",
                       "verifying"):
        return incident_id
    return None


def _dedup_record(alert: dict, incident_id: str) -> None:
    p = _dedup_path()
    data = _dedup_load()
    data[_dedup_fingerprint(alert)] = {
        "incident_id": incident_id, "created_at": time.time()}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                 encoding="utf-8")


def cmd_demo(args):
    alert = json.loads((DEMO_ALERTS / ALERT_FILES[args.alert]).read_text(encoding="utf-8"))
    # Day 11-12 (plan Ch.6 module 1, scenario 14): 10-minute dedup.
    # fingerprint = service + alert fingerprint (the alert files carry
    # one; falls back to service + title). Within 10 minutes of the
    # same fingerprint, if the previous event is still open
    # (awaiting_approval / running / executing / verifying) the new
    # alert merges into it instead of creating an event or running
    # the graph. Past 10 minutes, or a previous event that already
    # ended, a new event is created normally. Records live in
    # data/dedup.json: {fingerprint: {incident_id, created_at}}.
    merged = _dedup_check(alert)
    if merged is not None:
        print(f"重复告警，已合并到 {merged}")
        return 0
    state = _new_incident(alert, alert_key=args.alert)
    _dedup_record(alert, state.incident_id)
    print(f"incident {state.incident_id}: {alert['title']}")
    if args.auto_approve:
        state.approval = {"approved": True, "by": "demo-auto",
                          "reason": "Day 1 one-shot demo"}
    state = graph.run_from(state)
    _print_trace(state)
    if state.status == "awaiting_approval":
        pairing = nodes.plan_pairing(state)
        print(f"\nSTOPPED at human_approval (risk={state.risk_level}). "
              f"State persisted to data/incidents/{state.incident_id}/")
        print(f"Plan: {len(state.plan)} steps, {pairing['high']} high-risk, "
              f"{pairing['rollback']} rollback steps, "
              f"{pairing['paired']} paired")
        print(f"Resume: python3 -m app.main approve {state.incident_id} "
              f"--by <you> --reason <why>")
        print(f"Reject: python3 -m app.main reject {state.incident_id} "
              f"--by <you> --reason <why>")
        print(f"Modify: python3 -m app.main status {state.incident_id} "
              f"--list-steps, then modify {state.incident_id} "
              f"--by <you> --reason <why> --drop-step <n>[,<n>...]")
    elif state.status == "done":
        print(f"\nDONE. Report: {state.report_path}")
    return 0


def cmd_approve(args):
    state = store.load(args.incident_id)
    state.approval = {"approved": True, "by": args.by, "reason": args.reason}
    state = graph.resume(state)
    _print_trace(state)
    print(f"\nstatus={state.status}"
          + (f". Report: {state.report_path}" if state.report_path else ""))
    return 0


def cmd_modify(args):
    """Day 8: modify the plan of an incident stopped at human_approval,
    then resume it. --drop-step takes 1-based plan positions as shown
    by `status` / `status --list-steps`, comma-separated, repeatable.
    Rollback steps cannot be dropped (Day 7 pairing); dropping a
    high-risk forward step removes its paired rollback step too and
    recomputes risk_level (see nodes.apply_modification)."""
    state = store.load(args.incident_id)
    drop = []
    for chunk in args.drop_step:
        for tok in chunk.split(","):
            tok = tok.strip()
            if not tok:
                continue
            try:
                drop.append(int(tok))
            except ValueError:
                print(f"modify rejected: --drop-step {tok!r} is not a step number")
                return 2
    try:
        nodes.apply_modification(state, drop, by=args.by, reason=args.reason)
    except ValueError as e:
        print(f"modify rejected: {e}")
        return 2
    state = graph.resume(state)
    _print_trace(state)
    print(f"\nstatus={state.status} (modified plan: "
          f"{len(state.plan)} steps left, risk={state.risk_level})"
          + (f". Report: {state.report_path}" if state.report_path else ""))
    return 0


def cmd_expire(args):
    """Day 8: close incidents that waited too long at human_approval
    (plan Ch.4 node table: the human_approval failure fallback).
    Scans every persisted incident; only awaiting_approval ones older
    than --hours (default 24, judged on state.created_at) are closed,
    with the reason + close time recorded and a report generated via
    the report node so every incident stays reportable. Younger
    awaiting incidents are left untouched. Manual trigger, not a
    background timer (see README Day 8)."""
    now = time.time()
    limit_s = args.hours * 3600
    scanned = awaiting = closed = skipped = 0
    for p in sorted(store.DATA_DIR.glob("*/state.json")):
        scanned += 1
        try:
            state = store.load(p.parent.name)
        except Exception as e:  # noqa: BLE001 — one bad file must not
            # block the scan; it is reported and counted as skipped.
            print(f"  skip {p.parent.name}: unreadable ({e})")
            skipped += 1
            continue
        if state.status != "awaiting_approval":
            continue
        awaiting += 1
        age_h = (now - state.created_at) / 3600
        if (now - state.created_at) <= limit_s:
            skipped += 1
            continue
        state.closed_reason = (
            f"超时未确认自动关闭：在 human_approval 处等待 {age_h:.1f} 小时，"
            f"超过 {args.hours:g} 小时上限")
        state.closed_at = now
        state.approval = {"approved": False, "by": "system:expire",
                          "reason": state.closed_reason, "expired": True,
                          "expired_at": time.strftime(
                              "%Y-%m-%d %H:%M:%S", time.localtime(now))}
        state.status = "closed"
        state = graph.resume(state)  # report node; stays "closed"
        closed += 1
        print(f"  closed {state.incident_id} (waited {age_h:.1f}h)"
              + (f", report: {state.report_path}" if state.report_path else ""))
    print(f"expire: scanned {scanned} incidents, {awaiting} awaiting_approval, "
          f"closed {closed}, skipped {skipped}")
    return 0


def cmd_reject(args):
    state = store.load(args.incident_id)
    state.approval = {"approved": False, "by": args.by, "reason": args.reason}
    state.status = "closed"
    state = graph.resume(state)
    _print_trace(state)
    print(f"\nstatus={state.status} (rejected, nothing executed)"
          + (f". Report: {state.report_path}" if state.report_path else ""))
    return 0


def cmd_status(args):
    state = store.load(args.incident_id)
    print(f"{state.incident_id}: status={state.status} "
          f"category={state.category} risk={state.risk_level}")
    pairing = nodes.plan_pairing(state)
    print(f"plan: {len(state.plan)} steps, {pairing['high']} high-risk, "
          f"{pairing['rollback']} rollback steps, {pairing['paired']} paired")
    # Day 8: every step is numbered (1-based) so `modify --drop-step n`
    # has an unambiguous target; rollback steps are marked and cannot
    # be dropped. --list-steps prints only this list.
    for i, p in enumerate(state.plan, 1):
        tag = "rollback" if p.is_rollback else p.risk
        line = f"  {i}. [{tag}] {p.step} ({p.tool})"
        if p.params:
            line += f" params={p.params}"
        if p.is_rollback:
            line += f" <- {p.rollback_of}"
        if p.result:
            line += f" — {p.result}"
        print(line)
    if args.list_steps:
        return 0
    if state.closed_reason:
        print(f"closed: {state.closed_reason}")
    if state.approval.get("modified"):
        print(f"modified: dropped {state.approval.get('dropped_steps', [])}, "
              f"paired rollback steps removed: "
              f"{state.approval.get('dropped_rollback_steps', [])}, "
              f"reason: {state.approval.get('reason', '')}")
    _print_trace(state)
    print("tools:")
    for a in state.tool_audit:
        print(f"  {a['tool']} ({a['ms']}ms): {a['summary']}")
    return 0


def cmd_inject(args):
    """Day 2: inject cpu / oom, or clear the fault."""
    if args.fault == "clear":
        state = demo_service.clear()
        print(f"fault cleared; web-api back to baseline (logs via {state['logs_via']})")
    else:
        state = demo_service.inject(args.fault)
        print(f"injected {args.fault} ({state['label']}): "
              f"{state['logs_written']} log docs via {state['logs_via']}, "
              f"metrics cpu={state['metrics']['cpu_pct']}% "
              f"mem={state['metrics']['mem_pct']}%")
    print(f"state: demo_service/state.json (fault={state.get('fault')})")
    return 0


def cmd_diagnosis(args):
    state = store.load(args.incident_id)
    if state.diagnosis is None:
        print(f"no structured diagnosis for {args.incident_id} "
              f"(incident predates Day 5, status={state.status})")
    else:
        print(state.diagnosis.to_json())
    return 0


def cmd_report(args):
    """Day 10: one-command report. A saved report is still printed
    as-is; when there is no saved report (deleted, or the incident
    closed before one was written — e.g. rejected / expired), the
    report is rendered on the spot from the persisted state and
    saved, then printed — so any persisted incident can produce a
    report with one command. Archiving is attempted first (same rule
    as the report node) so the regenerated report can say the case
    was archived. A render failure falls back to the minimal report
    (same rule as nodes.report)."""
    from app.cases import archive_case
    from app.report import render_minimal_report, render_report
    state = store.load(args.incident_id)
    if state.report_path and Path(state.report_path).exists():
        print(Path(state.report_path).read_text(encoding="utf-8"))
        return 0
    try:
        archive_case(state)
    except Exception:  # noqa: BLE001 — archiving must not block the report
        pass
    try:
        markdown = render_report(state)
    except Exception as e:  # noqa: BLE001 — minimal report, not "no report"
        print(f"(完整报告生成失败，已保存最小报告：{e})")
        markdown = render_minimal_report(state, e)
    state.report_path = str(store.save_report(state, markdown))
    store.save(state)
    print(f"(报告现场重新生成，已保存到 {state.report_path})")
    print(markdown)
    return 0


def cmd_trace(args):
    """Day 10: replay one incident as a human-readable story in time
    order (nodes, tool calls, key decisions). Works for any
    persisted state: awaiting_approval / done / closed / rejected."""
    from app.trace import render_trace
    state = store.load(args.incident_id)
    print(render_trace(state))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ops-agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("demo")
    p.add_argument("--auto-approve", action="store_true")
    p.add_argument("--alert", choices=list(ALERT_FILES), default="nginx-502",
                   help="preset alert: nginx-502 (default), cpu, oom, "
                        "unknown, disk-full, slow-query, release, "
                        "downstream, pool, config, cert, mq, cache, jitter")
    p.set_defaults(fn=cmd_demo)
    p = sub.add_parser("inject")
    p.add_argument("fault", choices=["cpu", "oom", "disk", "slowquery",
                                     "release", "downstream", "pool",
                                     "config", "cert", "mq", "cache",
                                     "jitter", "clear"],
                   help="Day 2 fault injection: cpu / oom / clear; "
                        "Day 11-12 adds disk / slowquery / release / "
                        "downstream / pool / config / cert / mq / "
                        "cache / jitter")
    p.set_defaults(fn=cmd_inject)
    for name, fn in (("approve", cmd_approve), ("reject", cmd_reject)):
        p = sub.add_parser(name)
        p.add_argument("incident_id")
        p.add_argument("--by", required=True)
        p.add_argument("--reason", default="")
        p.set_defaults(fn=fn)
    p = sub.add_parser("modify")
    p.add_argument("incident_id")
    p.add_argument("--by", required=True)
    p.add_argument("--reason", default="")
    p.add_argument("--drop-step", required=True, action="append",
                   help="1-based plan step number(s) to drop, as shown by "
                        "`status --list-steps`; comma-separated, repeatable; "
                        "forward steps only, rollback steps are refused")
    p.set_defaults(fn=cmd_modify)
    p = sub.add_parser("expire")
    p.add_argument("--hours", type=float, default=24.0,
                   help="close awaiting_approval incidents older than this "
                        "many hours (default 24)")
    p.set_defaults(fn=cmd_expire)
    p = sub.add_parser("status"); p.add_argument("incident_id")
    p.add_argument("--list-steps", action="store_true",
                   help="only list the numbered plan steps")
    p.set_defaults(fn=cmd_status)
    p = sub.add_parser("report"); p.add_argument("incident_id")
    p.set_defaults(fn=cmd_report)
    p = sub.add_parser("trace")
    p.add_argument("incident_id")
    p.set_defaults(fn=cmd_trace)
    p = sub.add_parser("diagnosis"); p.add_argument("incident_id")
    p.set_defaults(fn=cmd_diagnosis)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
