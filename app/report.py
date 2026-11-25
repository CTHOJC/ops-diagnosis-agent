#!/usr/bin/env python3
"""Ops diagnosis Agent — Day 1 skeleton: Markdown postmortem (plan Ch.6
module 8). The report must be readable without human editing: timeline,
root cause, evidence with refs, actions taken, timings and cost.
"""
from __future__ import annotations

import time

from .state import AgentState


def render_report(state: AgentState) -> str:
    lines = [f"# 运维诊断复盘报告 — {state.incident_id}",
             "",
             f"- 告警：{state.alert.get('title', '')}（{state.alert.get('service', '')}，"
             f"{state.alert.get('severity', '')}）",
             f"- 分类：{state.category} / {state.severity}",
             f"- 状态：{state.status}",
             f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
             ""]
    # Day 8: an incident closed by expire (timeout) records why and when.
    if state.closed_reason:
        closed_at = (time.strftime('%Y-%m-%d %H:%M:%S',
                                   time.localtime(state.closed_at))
                     if state.closed_at else "未记录")
        lines.append(f"- 关闭原因：{state.closed_reason}（关闭时间：{closed_at}）")
        lines.append("")
    lines += ["## 根因假设", ""]
    for h in state.hypotheses:
        refs = ", ".join(h.evidence_refs) if h.evidence_refs else "无"
        lines.append(f"- {h.cause}（置信度 {h.confidence:.2f}，证据：{refs}）")
    lines += ["", "## 结构化诊断输出（Day 5，diagnosis/v1）", ""]
    if state.diagnosis is not None:
        d = state.diagnosis
        lines.append(f"- 来源：{d.source}"
                     + (f"（模型 {d.model}）" if d.model else "（规则推理，未接 LLM）"))
        lines.append(f"- 需人工确认：{'是' if d.needs_human else '否'}")
        if d.is_unknown:
            lines.append("- 结论：未知型故障，未采纳任何根因，转人工处理")
        lines.append("- 已执行检查：")
        for c in d.checks_performed:
            lines.append(f"  - {c}")
        lines.append("- 建议处置：")
        for a in d.recommended_actions:
            lines.append(f"  - [{a.risk}] {a.action}"
                         + (f"（回滚：{a.rollback}）" if a.rollback else ""))
        if state.diagnosis_engine_failures:
            lines.append("- LLM 引擎失败记录（已回退到规则推理）：")
            for f in state.diagnosis_engine_failures:
                lines.append(f"  - {f}")
        lines.append("")
        lines.append("```json")
        lines.append(d.to_json())
        lines.append("```")
    else:
        lines.append("- （旧事件，诊断时还没有结构化输出）")
    lines += ["", "## 证据", ""]
    for e in state.evidence:
        lines.append(f"- [{e.source}] {e.timestamp} {e.summary}（{e.ref}）")
    if state.evidence_gaps:
        lines.append(f"- 取证缺口（已降级继续，未阻塞诊断）："
                     f"{'; '.join(state.evidence_gaps)}")
    lines += ["", "## 处置方案与执行", ""]
    # Day 7: forward steps and rollback steps are rendered separately.
    # A rollback step is defined, paired and shown — but dormant: it
    # runs only on the Day 9 failure path, never in normal execution.
    # Every step shows its source (a runbook chunk ref) or the explicit
    # "建议人工判断" marker; "" means the incident predates Day 7.
    # (Lazy import: nodes imports this module lazily from report().)
    from .nodes import plan_pairing
    pairing = plan_pairing(state)
    lines.append(f"- 高危步骤 {pairing['high']} 个，回滚步骤 {pairing['rollback']} 个，"
                 f"已配对 {pairing['paired']} 对")
    for p in state.plan:
        if p.is_rollback:
            continue
        src = p.source or "来源未标注（Day 7 前的事件）"
        lines.append(f"- [{p.risk}] {p.step}（{p.tool}）— {p.result or '未执行'}"
                     f" | 来源：{src}")
        if p.rollback:
            lines.append(f"  - 回滚预案：{p.rollback}（显式回滚步骤见下节）")
    rollbacks = [p for p in state.plan if p.is_rollback]
    lines += ["", "### 回滚步骤（仅失败时执行）", ""]
    if not rollbacks:
        lines.append("- 无（本方案没有高危步骤，或为 Day 7 前的事件）")
    for p in rollbacks:
        src = p.source or "来源未标注（Day 7 前的事件）"
        lines.append(f"- {p.step}（{p.tool}）— {p.result or '未执行'}"
                     f" | 回滚对象：{p.rollback_of or '未指明'}"
                     f" | 来源：{src}"
                     + (f" | 参数：{p.params}" if p.params else ""))
        if p.rollback:
            lines.append(f"  - 说明：{p.rollback}")
    # Day 9: the failure path, rendered on its own so a rollback and
    # its post-rollback verification can never be mistaken for a
    # normal run. Only present when a rollback actually fired.
    if state.rollback_triggered:
        lines += ["", "### 回滚与恢复验证（第 9 天，失败路径）", ""]
        lines.append("- 说明：人工确认时批的是整份 plan（含配对的回滚步骤），"
                     "失败触发的回滚是已授权计划的一部分，自动执行，不再二次确认；"
                     f"原计划批准人：{state.approval.get('by', '?')}，"
                     f"理由：{state.approval.get('reason', '')}；"
                     "回滚工具在注册表里仍是高危工具。")
        for r in state.exec_result:
            if not r.get("ok") and r.get("step") != "verify":
                lines.append(f"- 失败步骤：{r['step']} — {r.get('detail', '')}")
        rb_names = {p.step for p in rollbacks}
        for r in state.exec_result:
            if r.get("step") in rb_names:
                lines.append(f"- 已执行的回滚步骤：{r['step']} — "
                             f"{'成功' if r.get('ok') else '失败'}："
                             f"{r.get('detail', '')}")
        for r in state.exec_result:
            if r.get("step") == "verify":
                lines.append(f"- 回滚后验证：{'已恢复' if r.get('ok') else '未恢复'}"
                             f"（{r.get('detail', '')}）")
        if state.rolled_back_steps:
            lines.append(f"- 已回滚的正向步骤（重诊后不再重新执行）："
                         f"{state.rolled_back_steps}")
    if state.approval:
        lines.append("")
        lines.append(f"人工确认：{state.approval.get('by', '?')}，"
                     f"{'通过' if state.approval.get('approved') else '驳回'}，"
                     f"理由：{state.approval.get('reason', '')}")
        # Day 8: a modified approval records exactly what was dropped,
        # so the report shows the removed steps even though they left
        # the plan (the audit entry in exec_result says the same).
        if state.approval.get("modified"):
            lines.append(f"方案已修改：删了步骤 "
                         f"{state.approval.get('dropped_steps', [])}"
                         f"（原序号 {state.approval.get('dropped_indices', [])}）"
                         f"，配对的回滚步骤一并移除："
                         f"{state.approval.get('dropped_rollback_steps', [])}"
                         f"；修改后风险等级：{state.risk_level}")
        if state.approval.get("expired"):
            lines.append("（超时自动关闭，见上方关闭原因）")
    lines += ["", "## 节点 Trace", ""]
    for t in state.trace:
        lines.append(f"- {t['node']}：{t['ms']}ms — {t.get('note', '')}")
    lines += ["", "## 工具调用审计", ""]
    for a in state.tool_audit:
        lines.append(f"- {a['tool']}（{a['ms']}ms）：{a['summary']}")
    # Day 10: case archive (plan Ch.6 module 8, last link). The report
    # node archives before rendering, so a filed case shows here; an
    # incident that was not archived says why, instead of silently
    # having no case file.
    lines += ["", "## 案例归档（第 10 天）", ""]
    from .cases import case_path, should_archive
    if case_path(state.incident_id).exists():
        lines.append(f"- 已归档：data/cases/{state.incident_id}.json"
                     "（历史案例库，下次同类告警检索时可引用）")
    else:
        _ok, _reason = should_archive(state)
        lines.append(f"- 未归档：{_reason}")
    lines += ["",
              "## 成本",
              ""]
    if state.diagnosis is not None and state.diagnosis.source == "llm":
        llm_attempts = 1 + len(state.diagnosis_engine_failures)
    else:
        llm_attempts = len(state.diagnosis_engine_failures)
    if llm_attempts:
        detail = (f"（{state.diagnosis.model}）"
                  if state.diagnosis is not None
                  and state.diagnosis.source == "llm"
                  else "（均失败，已回退到规则推理）")
        lines.append(f"- 模型调用：{llm_attempts} 次{detail}，"
                     "Token 成本以调用方返回为准")
    else:
        lines.append("- 模型调用：0 次（规则推理，未接 LLM；Token 成本 0）")
    lines.append("- 说明：本报告由骨架自动生成；实测数字以评测集跑通后为准。")
    lines.append("")
    return "\n".join(lines)


def render_minimal_report(state: AgentState, error: Exception) -> str:
    """Day 10 fault tolerance: when render_report() raises, the report
    node saves this instead of losing the incident. It contains only
    what can be rendered without any of the full report's machinery —
    the node Trace and a raw summary of the persisted state — and says
    so up front. Built from plain fields only, so it (almost) cannot
    fail the same way the full render did."""
    lines = [f"# 运维诊断复盘报告（最小报告）— {state.incident_id}",
             "",
             f"- 注意：完整报告生成失败，见 Trace（错误：{error}）。"
             "本报告只含 Trace 与原始状态摘要，事件状态不受影响。",
             f"- 告警：{state.alert.get('title', '')}（{state.alert.get('service', '')}，"
             f"{state.alert.get('severity', '')}）",
             f"- 分类：{state.category} / {state.severity}",
             f"- 状态：{state.status}",
             f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
             ""]
    if state.closed_reason:
        lines.append(f"- 关闭原因：{state.closed_reason}")
        lines.append("")
    lines += ["## 节点 Trace", ""]
    for t in state.trace:
        lines.append(f"- {t['node']}：{t['ms']}ms — {t.get('note', '')}")
    lines += ["", "## 工具调用审计", ""]
    for a in state.tool_audit:
        lines.append(f"- {a['tool']}（{a['ms']}ms）：{a['summary']}")
    lines += ["", "## 原始状态摘要", ""]
    lines.append("- 根因假设："
                 + "; ".join(f"{h.cause}（{h.confidence:.2f}）"
                             for h in state.hypotheses) or "- 根因假设：（无）")
    lines.append(f"- 证据：{len(state.evidence)} 条，"
                 f"取证缺口：{'; '.join(state.evidence_gaps) or '无'}")
    lines.append("- 处置方案：")
    for p in state.plan:
        lines.append(f"  - [{'rollback' if p.is_rollback else p.risk}] "
                     f"{p.step}（{p.tool}）— {p.result or '未执行'}")
    if state.approval:
        lines.append(f"- 人工确认：{state.approval.get('by', '?')}，"
                     f"{'通过' if state.approval.get('approved') else '驳回'}，"
                     f"理由：{state.approval.get('reason', '')}")
    lines.append("- 执行结果：")
    for r in state.exec_result:
        lines.append(f"  - {r.get('step', '')}："
                     f"{'成功' if r.get('ok') else '失败'} — "
                     f"{str(r.get('detail', ''))[:200]}")
    if state.rollback_triggered:
        lines.append(f"- 已回滚步骤：{state.rolled_back_steps}")
    lines.append("")
    return "\n".join(lines)
