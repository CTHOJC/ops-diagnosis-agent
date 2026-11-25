#!/usr/bin/env python3
"""Ops diagnosis Agent — Day 10: Trace replay (plan Ch.8 observability).

`render_trace(state)` replays one incident as a human-readable story
in chronological order: node transitions, tool calls, and the key
decisions (classification, diagnosis, risk, human confirmation,
rollback, verification). It works for every persisted state —
awaiting_approval / done / closed / rejected — because it only reads
the persisted state, never re-runs the graph.

How it differs from `status`: status is a state snapshot (current
status, plan, trace lines, tool audit); trace is the story in time
order, with each node annotated by what it decided and a separate
"关键决策点" section that collects the decisions in one place, so
the whole incident can be understood without anyone filling in
gaps by hand. Node annotations are derived from the persisted
final state (no per-node snapshots were stored on Days 1–9), so a
node's line describes what that step concluded for this incident,
not a frozen copy of the state at that instant.
"""
from __future__ import annotations

import json
import time


def _counts_by_source(state, source):
    return sum(1 for e in state.evidence if e.source == source)


def _node_detail(state, node: str) -> str:
    """What this node concluded for this incident (from the persisted
    state — see the module docstring). Empty when there is nothing
    specific to say."""
    if node == "classify":
        return f"分类结果：{state.category}（{state.severity}）"
    if node == "collect_evidence":
        detail = (f"证据 {len(state.evidence)} 条：日志 "
                  f"{_counts_by_source(state, 'logs')} / 指标 "
                  f"{_counts_by_source(state, 'metrics')} / 状态 "
                  f"{_counts_by_source(state, 'status')} / 手册 "
                  f"{_counts_by_source(state, 'runbook')}")
        if state.evidence_gaps:
            detail += f"；取证缺口：{'; '.join(state.evidence_gaps)}"
        return detail
    if node == "diagnose":
        if not state.hypotheses:
            return "未产出根因假设"
        top = state.hypotheses[0]
        src = ""
        if state.diagnosis is not None:
            src = f"，来源：{state.diagnosis.source}"
            if state.diagnosis.is_unknown:
                return (f"诊断结论：未知型故障，需转人工"
                        f"（Top 1 置信度 {top.confidence:.2f} < 0.5）{src}")
        return (f"诊断 Top 1：{top.cause}（置信度 {top.confidence:.2f}）{src}")
    if node == "make_plan":
        from .nodes import plan_pairing
        pairing = plan_pairing(state)
        return (f"方案 {len(state.plan)} 步：{pairing['high']} 高危 / "
                f"{pairing['rollback']} 回滚 / {pairing['paired']} 已配对，"
                f"风险等级 {state.risk_level}")
    if node == "human_approval":
        ap = state.approval or {}
        if state.status == "awaiting_approval" and not ap:
            return "等待人工确认（尚未确认，事件在此停下）"
        if ap.get("approved"):
            detail = (f"人工确认通过：{ap.get('by', '?')}，"
                      f"理由：{ap.get('reason', '')}")
            if ap.get("modified"):
                detail += (f"；方案已修改：删了步骤 "
                           f"{ap.get('dropped_steps', [])}")
            return detail
        if ap:
            return (f"人工确认驳回：{ap.get('by', '?')}，"
                    f"理由：{ap.get('reason', '')}")
        return "未经过人工确认"
    if node == "execute":
        steps = [r for r in state.exec_result
                 if r.get("step") != "verify"]
        ok = sum(1 for r in steps if r.get("ok"))
        detail = f"执行 {len(steps)} 条记录：成功 {ok} / 失败 {len(steps) - ok}"
        if state.rollback_triggered:
            detail += (f"；失败触发回滚，已回滚：{state.rolled_back_steps}")
        return detail
    if node == "verify":
        verifies = [r for r in state.exec_result
                    if r.get("step") == "verify"]
        if not verifies:
            return "尚未验证"
        last = verifies[-1]
        return (f"验证{'已恢复' if last.get('ok') else '未恢复'}"
                f"（{last.get('detail', '')}）")
    if node == "report":
        return (f"报告：{state.report_path}"
                if state.report_path else "报告尚未生成")
    return ""


def render_trace(state) -> str:
    lines = [f"# 诊断 Trace 回放 — {state.incident_id}",
             "",
             f"- 告警：{state.alert.get('title', '')}（{state.alert.get('service', '')}，"
             f"{state.alert.get('severity', '')}）",
             f"- 分类：{state.category} / {state.severity}",
             f"- 状态：{state.status}",
             f"- 创建时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(state.created_at))}",
             f"- 节点：{len(state.trace)} 个，工具调用：{len(state.tool_audit)} 次",
             ""]
    if state.closed_reason:
        lines.append(f"- 关闭原因：{state.closed_reason}")
        lines.append("")
    lines += ["## 时间线（按节点顺序）", ""]
    if not state.trace:
        lines.append("- （本次事件还没有节点记录）")
    for i, t in enumerate(state.trace, 1):
        lines.append(f"{i}. {t['node']}（{t['ms']}ms）— {t.get('note', '')}")
        detail = _node_detail(state, t["node"])
        if detail:
            lines.append(f"   {detail}")
    lines += ["", "## 工具调用（按调用顺序）", ""]
    if not state.tool_audit:
        lines.append("- （本次事件还没有工具调用记录）")
    for j, a in enumerate(state.tool_audit, 1):
        args = json.dumps(a.get("args", {}), ensure_ascii=False)
        if len(args) > 120:
            args = args[:117] + "…"
        lines.append(f"{j}. {a['tool']}（{a['ms']}ms，{a.get('ts', '')}）"
                     f"— {a.get('summary', '')}；参数：{args}")
    lines += ["", "## 关键决策点", ""]
    # 1. 分类
    lines.append(f"- 分类：{state.category}（{state.severity}），"
                 f"依据告警“{state.alert.get('title', '')}”")
    # 2. 诊断
    if state.hypotheses:
        top = state.hypotheses[0]
        src = (f"（来源：{state.diagnosis.source}）"
               if state.diagnosis is not None else "")
        if state.diagnosis is not None and state.diagnosis.is_unknown:
            lines.append(f"- 诊断：未知型故障，需转人工"
                         f"（Top 1 置信度 {top.confidence:.2f} < 0.5）{src}；"
                         "未采纳任何根因，未执行高危处置")
        else:
            refs = ", ".join(top.evidence_refs) if top.evidence_refs else "无"
            lines.append(f"- 诊断：Top 1 {top.cause}"
                         f"（置信度 {top.confidence:.2f}）{src}；"
                         f"证据：{refs}")
    else:
        lines.append("- 诊断：（尚未诊断）")
    # 3. 方案与风险
    if state.plan:
        from .nodes import plan_pairing
        pairing = plan_pairing(state)
        lines.append(f"- 方案与风险：{len(state.plan)} 步，风险等级 "
                     f"{state.risk_level}；高危 {pairing['high']} 个，"
                     f"回滚 {pairing['rollback']} 个，"
                     f"已配对 {pairing['paired']} 对")
    else:
        lines.append("- 方案与风险：（尚未生成方案）")
    # 4. 人工确认
    ap = state.approval or {}
    if state.status == "awaiting_approval":
        lines.append("- 人工确认：等待确认中（尚未有人确认/驳回/修改）")
    elif ap.get("approved"):
        detail = (f"- 人工确认：{ap.get('by', '?')} 通过"
                  f"（理由：{ap.get('reason', '')}）")
        if ap.get("modified"):
            detail += (f"；修改：删了步骤 {ap.get('dropped_steps', [])}"
                       f"（原序号 {ap.get('dropped_indices', [])}）")
        if ap.get("expired"):
            detail += "；后因超时自动关闭"
        lines.append(detail)
    elif ap:
        lines.append(f"- 人工确认：{ap.get('by', '?')} 驳回"
                     f"（理由：{ap.get('reason', '')}）；未执行任何处置")
    else:
        lines.append("- 人工确认：低风险方案，未经过人工确认环节，直接执行")
    # 5. 回滚
    if state.rollback_triggered:
        lines.append(f"- 回滚：已触发（失败后自动回滚），已回滚步骤："
                     f"{state.rolled_back_steps}")
    elif any(not r.get("ok") and r.get("step") != "verify"
             for r in state.exec_result):
        lines.append("- 回滚：有步骤失败，但失败前没有已成功的高危步骤，"
                     "故未执行回滚")
    else:
        lines.append("- 回滚：未触发（执行无失败，或尚未执行）")
    # 6. 验证与收尾
    verifies = [r for r in state.exec_result if r.get("step") == "verify"]
    if verifies:
        last = verifies[-1]
        lines.append(f"- 验证：{'已恢复' if last.get('ok') else '未恢复'}"
                     f"（{last.get('detail', '')}）")
    else:
        lines.append("- 验证：尚未验证")
    tail = f"- 收尾：状态 {state.status}"
    if state.closed_reason:
        tail += f"（{state.closed_reason}）"
    if state.report_path:
        tail += f"；报告：{state.report_path}"
    lines.append(tail)
    lines.append("")
    return "\n".join(lines)
