#!/usr/bin/env python3
"""Ops diagnosis Agent — Day 10: history-case archive (plan Ch.6 module 8).

A finished incident whose root cause is known and whose service
recovered is archived as one JSON file per incident under
``data/cases/<incident_id>.json`` so a later incident can cite it
through search_runbook. The archive is the last link of module 8:
"把本次案例归档回知识库".

What is archived, and what is NOT (the rule lives in
should_archive(), and the reason is returned so the report can say
why a given incident has no case file):

  * archived: status is "done" (the incident finished through the
    report node), the human decision was not a rejection, the
    category is known (not "unknown"), the diagnosis Top 1
    confidence is >= 0.5, and verify() actually saw recovery.
  * NOT archived: unknown root cause (there is no cause to learn
    from — archiving it would teach the retriever to answer the
    next unknown symptom with a made-up precedent); rejected
    incidents (nothing was executed, so there is no outcome to
    learn from); incidents that ended unrecovered (verify never
    passed — the处置 did not work, so it must not be recommended
    to the next incident); incidents that did not end "done"
    (awaiting_approval, or expire-closed — still open or closed
    without a finished run).

Archiving is idempotent: archive_case() never overwrites an existing
case file — a report regenerated later (Day 10 `report` command) or
run twice returns the file that is already there.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CASES_DIR = ROOT / "data" / "cases"


def get_cases_dir() -> Path:
    """Where history cases live. OPS_AGENT_CASES_DIR (Days 11-12 eval
    harness) points it at a throwaway directory so one eval run never
    pollutes — or reads — the real case library; unset it means the
    default data/cases. Read from the environment on every call so a
    harness subprocess picks up whatever it was started with."""
    override = os.environ.get("OPS_AGENT_CASES_DIR", "").strip()
    return Path(override) if override else CASES_DIR

# Plan Ch.4 / Ch.6 module 4: below this confidence a diagnosis is
# "unknown" and must not be archived as a known-cause case.
ARCHIVE_CONFIDENCE_THRESHOLD = 0.5


def case_path(incident_id: str) -> Path:
    return get_cases_dir() / f"{incident_id}.json"


def _top_confidence(state) -> float:
    """Diagnosis Top 1 confidence: the structured Diagnosis when the
    incident has one (Day 5+), else the top hypothesis."""
    if getattr(state, "diagnosis", None) is not None:
        causes = state.diagnosis.causes or []
        if causes:
            return float(causes[0].confidence)
    if state.hypotheses:
        return float(state.hypotheses[0].confidence)
    return 0.0


def _top_cause(state) -> str:
    if getattr(state, "diagnosis", None) is not None:
        causes = state.diagnosis.causes or []
        if causes:
            return causes[0].cause
    if state.hypotheses:
        return state.hypotheses[0].cause
    return ""


def _is_recovered(state) -> bool:
    """Same check as nodes.is_recovered(): a verify step that passed.
    (Reimplemented here, not imported from nodes, so this module has
    no import cycle — nodes imports cases from report().)"""
    return any(r.get("step") == "verify" and r.get("ok")
               for r in state.exec_result)


def should_archive(state) -> tuple[bool, str]:
    """(eligible, reason). The reason is human-readable so the report
    can state why an incident was or was not archived."""
    if state.status != "done":
        return False, f"事件未以 done 收尾（status={state.status}）"
    approval = state.approval or {}
    if approval.get("approved") is False:
        return False, "事件已被驳回，未执行任何处置，无可归档的结果"
    if not state.category or state.category == "unknown":
        return False, "根因为 unknown，没有可归档的已知根因"
    conf = _top_confidence(state)
    if conf < ARCHIVE_CONFIDENCE_THRESHOLD:
        return False, (f"诊断 Top 1 置信度 {conf:.2f} < "
                       f"{ARCHIVE_CONFIDENCE_THRESHOLD:g}，未采纳根因")
    if not _is_recovered(state):
        return False, "verify 未通过（未恢复），处置未生效，不归档"
    return True, "符合归档条件"


def archive_case(state) -> Path | None:
    """Archive this incident as a history case, or return None when
    it must not be archived (see should_archive). Idempotent: an
    existing case file is returned untouched, never overwritten."""
    ok, _reason = should_archive(state)
    if not ok:
        return None
    path = case_path(state.incident_id)
    if path.exists():
        return path
    # 处置摘要：正向方案步骤（回滚步骤是失败路径的备用步骤，不
    # 是本次实际执行的处置，不进摘要）；执行结果：每条 exec 记录
    # 的成败 + verify 的恢复结论。
    forward = [p.step for p in state.plan if not p.is_rollback]
    exec_lines = []
    for r in state.exec_result:
        detail = str(r.get("detail", ""))[:100]
        exec_lines.append(
            f"{r.get('step', '')}：{'成功' if r.get('ok') else '失败'}"
            + (f"（{detail}）" if detail else ""))
    record = {
        "incident_id": state.incident_id,
        "category": state.category,
        "root_cause": _top_cause(state),
        "confidence": round(_top_confidence(state), 2),
        "evidence_refs": sorted({e.ref for e in state.evidence}),
        "action_summary": "；".join(forward),
        "exec_summary": "；".join(exec_lines),
        "duration_ms": round(sum(t.get("ms", 0) for t in state.trace), 2),
        "archived_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    get_cases_dir().mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def load_cases() -> list[dict]:
    """All archived cases, oldest first. A corrupt file is skipped,
    never fatal — one bad case must not break retrieval."""
    cases_dir = get_cases_dir()
    if not cases_dir.exists():
        return []
    out = []
    for p in sorted(cases_dir.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001 — skip, don't fail retrieval
            continue
    return out
