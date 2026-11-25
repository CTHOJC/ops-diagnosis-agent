#!/usr/bin/env python3
"""Ops diagnosis Agent — Day 1 skeleton: the 8 graph nodes (plan Ch.4).

Node order and fallback behaviour follow the plan's node table:
  classify -> collect_evidence -> diagnose -> make_plan -> human_approval
  -> execute -> verify -> report
  diagnose low confidence -> collect_evidence again (max 2 rounds)
  verify not recovered -> diagnose again (max 1 round)
  high risk -> human_approval interrupts; low risk -> execute directly

Day 1 reality: classify/diagnose are transparent keyword rules over the
demo evidence, not an LLM. The rule tables are small and inspectable on
purpose — Day 5 landed the structured-output shape on top of the same
rules (see diagnosis.py) and wired the LLM engine in behind
diagnose_with_engine() with a loud recorded fallback, so the reasoning
can swap without changing any node interface (state in, state out).
"""
from __future__ import annotations

import hashlib
import os
import time
from concurrent.futures import ThreadPoolExecutor

from . import tools
from .diagnosis import Action, diagnose_with_engine
from .state import (DIAGNOSE_CONFIDENCE_THRESHOLD, EVIDENCE_TOOL_TIMEOUT_S,
                    MAX_EVIDENCE_ITEMS, MAX_EVIDENCE_ROUNDS,
                    MAX_VERIFY_RETRIES, Evidence, Hypothesis, PlanStep)

# ---------------------------------------------------------------------------
# classify — keyword rules (Day 5: LLM classifier, same output shape)
# ---------------------------------------------------------------------------
_RULES = [
    (("502", "bad gateway", "upstream"), "upstream_down", "P1"),
    (("oom", "out of memory", "被杀"), "oom_killed", "P1"),
    (("cpu", "打满", "load"), "cpu_saturated", "P2"),
    (("磁盘", "disk full", "写满"), "disk_full", "P2"),
    (("慢查询", "slow query"), "slow_query", "P2"),
    # Day 11-12: scenarios 6-13. Order matters (first match wins) and
    # every rule's keywords were checked against the other new alerts:
    # no new alert contains an earlier rule's keywords (e.g. the cache
    # alert says "过期" but the cert rule only matches "证书" /
    # "certificate", never bare "过期"; the mq alert says "变慢" but
    # the slow rule only matches "慢查询" / "slow query").
    (("发布后", "regression", "新版本"), "release_regression", "P1"),
    (("下游", "downstream", "雪崩"), "downstream_timeout", "P1"),
    (("连接池", "connection pool", "耗尽"), "pool_exhausted", "P1"),
    (("配置变更", "限流阈值", "config change"), "config_change", "P2"),
    (("证书", "certificate"), "cert_expired", "P1"),
    (("消息队列", "积压", "消费者"), "mq_backlog", "P2"),
    (("缓存击穿", "热点", "cache breakdown"), "cache_breakdown", "P2"),
    (("网络抖动", "丢包", "jitter"), "network_jitter", "P2"),
]


def classify(state):
    text = (state.alert.get("description", "") + " "
            + state.alert.get("title", "")).lower()
    for keywords, category, severity in _RULES:
        if any(k.lower() in text for k in keywords):
            state.category, state.severity = category, severity
            break
    else:
        state.category, state.severity = "unknown", "P3"
    state.status = "running"
    return state


# ---------------------------------------------------------------------------
# collect_evidence — Day 4: the 4 read-only tools run in parallel
# (plan Ch.3 data flow, Ch.4 node table), then evidence is trimmed to a
# structured list of at most MAX_EVIDENCE_ITEMS items (plan Ch.6 module 2).
#
# Rules, all from the plan:
# - parallel: logs / metrics / status / runbook in one ThreadPoolExecutor
#   round; assembly order stays fixed (logs, metrics, status, runbook) so
#   a run is deterministic regardless of which thread finishes first.
# - degrade: a tool that times out (> EVIDENCE_TOOL_TIMEOUT_S, plan Ch.5)
#   or raises is recorded in state.evidence_gaps and the diagnosis
#   continues with the evidence already gathered — one broken source
#   never blocks the run (plan Ch.4 node table).
# - trim: metrics (1) + status (1) + runbook (top 3) are reserved first,
#   logs fill the remaining slots; summaries are capped at 120 chars and
#   duplicates (same source + ref, including from an earlier round) are
#   dropped. Every item carries a timestamp — runbook chunks have none of
#   their own, so the collection time ("now") is recorded, the same
#   convention metrics/status already used.
# Threading note: the tools only read state and append to
# state.tool_audit (list.append is atomic under the GIL); no tool mutates
# shared state during collection.
# ---------------------------------------------------------------------------
def _one_line(text, limit=120):
    """Evidence summaries are single lines: chunk text can contain newlines
    and markdown, which would break the one-item-per-line report layout."""
    return " ".join(str(text).split())[:limit]


def _run_tool(name, fn):
    """Run one evidence tool; failure is a value, not an exception."""
    try:
        return name, True, fn()
    except Exception as e:  # noqa: BLE001 — degrade, don't fail the node
        return name, False, e


def collect_evidence(state):
    service = state.alert.get("service", "web-api")
    state.retry_count += 1
    keywords = {"upstream_down": ["upstream", "502", "connect"],
                "oom_killed": ["oom", "memory"],
                "cpu_saturated": ["cpu"],
                "disk_full": ["disk", "no space"],
                # Day 11-12: "lock" is needed — the lock-wait log line
                # contains neither "slow" nor "query", so without it
                # the line never reaches the evidence and slow_query
                # caps at 2/3 confidence.
                "slow_query": ["slow", "query", "lock"],
                "release_regression": ["release", "regression", "rollback"],
                "downstream_timeout": ["downstream", "timeout", "circuit"],
                "pool_exhausted": ["pool"],
                "config_change": ["config", "threshold", "rate_limit"],
                "cert_expired": ["certificate", "expired", "https"],
                "mq_backlog": ["queue", "backlog", "consumer"],
                "cache_breakdown": ["cache", "hot key"],
                "network_jitter": ["jitter", "packet"]}.get(state.category, ["error"])
    query = state.alert.get("description", "") or state.category

    jobs = {
        "logs": lambda: tools.search_logs(state, service, keywords),
        "metrics": lambda: tools.query_metrics(state, service),
        "status": lambda: tools.get_service_status(state, service),
        "runbook": lambda: tools.search_runbook(state, query),
    }
    results, gaps = {}, []
    # No `with` block: executor shutdown with wait=True would sit behind a
    # timed-out tool until its thread finished, defeating the timeout.
    # shutdown(wait=False) lets collect_evidence return as soon as every
    # future has answered or timed out; a late thread finishes on its own
    # and its result is discarded (it never touches state after this point
    # except the audit append, which is thread-safe).
    ex = ThreadPoolExecutor(max_workers=len(jobs))
    try:
        futs = {name: ex.submit(_run_tool, name, fn)
                for name, fn in jobs.items()}
        for name, fut in futs.items():
            try:
                _, ok, payload = fut.result(timeout=EVIDENCE_TOOL_TIMEOUT_S)
            except TimeoutError:
                gaps.append(f"{name}: timeout>{EVIDENCE_TOOL_TIMEOUT_S:g}s")
                continue
            if ok:
                results[name] = payload
            else:
                gaps.append(f"{name}: {payload}")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    state.evidence_gaps = gaps

    # Assemble in fixed source order, reserving slots so no one source
    # crowds the others out (metrics/status/runbook are singletons).
    # Day 10: the runbook results may carry one history case
    # (source "历史案例 / <id>") after the manual chunks. Manual
    # Top 3 keep their reserved slots; the case is a supplement that
    # never displaces a manual chunk — the evidence total still caps
    # at MAX_EVIDENCE_ITEMS.
    runbook_results = results.get("runbook") or []
    manual = [c for c in runbook_results
              if not c.get("source", "").startswith("历史案例")][:3]
    cases = [c for c in runbook_results
             if c.get("source", "").startswith("历史案例")][:1]
    runbook = manual + cases
    reserve = (1 if results.get("metrics") is not None else 0) \
        + (1 if results.get("status") is not None else 0) + len(runbook)
    logs_allowed = max(0, MAX_EVIDENCE_ITEMS - reserve)
    fresh = []
    for log in (results.get("logs") or [])[:logs_allowed]:
        fresh.append(Evidence(source="logs", timestamp=log["ts"],
                              summary=_one_line(log["msg"]),
                              # ref must distinguish log lines that share a
                              # timestamp: one inject writes 3 docs in the
                              # same second, and ref=log:ts alone collapsed
                              # them in dedup (OOM lost its "Killed process"
                              # line and confidence fell 1.00 -> 0.67).
                              ref=(f"log:{log['ts']}:"
                                   f"{hashlib.md5(log['msg'].encode()).hexdigest()[:8]}")))
    m = results.get("metrics")
    if m is not None:
        fresh.append(Evidence(
            source="metrics", timestamp="now",
            summary=(f"error_rate_5m {m.get('error_rate_5m')} "
                     f"(baseline {m.get('error_rate_baseline')}), "
                     f"p95 {m.get('p95_latency_ms')}ms"),
            ref="metrics:error_rate_5m"))
    st = results.get("status")
    if st is not None:
        fresh.append(Evidence(
            source="status", timestamp="now",
            # Day 6: the label is "上游", not "upstream" — the English
            # word here was a field label, not a symptom, but diagnose()
            # scores evidence summaries by keyword, so a healthy service
            # still scored "upstream" 1/4 and every unrelated event
            # carried a phantom "上游实例宕机" 0.25 (and the unknown
            # diagnosis cited status:upstream as its only evidence).
            # Real faults do not need the label: their 1.00 comes from
            # logs + runbook, which still contain the keywords.
            summary=(f"上游 {st['upstream']}, {st['instances']} instances, "
                     f"last deploy {st['last_deploy']}"),
            ref="status:upstream"))
    for c in runbook:
        if c.get("source", "").startswith("历史案例"):
            # Day 10: a cited history case keeps its source in the
            # summary, so the evidence list itself shows which past
            # incident it came from ("历史案例 / <incident_id>").
            fresh.append(Evidence(source="runbook", timestamp="now",
                                  summary=_one_line(
                                      f"{c['source']}：{c['text']}"),
                                  ref=f"runbook:{c['chunk_id']}"))
        else:
            fresh.append(Evidence(source="runbook", timestamp="now",
                                  summary=_one_line(c["text"]),
                                  ref=f"runbook:{c['chunk_id']}"))

    # Dedupe against evidence from earlier rounds, then cap the total.
    seen = {(e.source, e.ref) for e in state.evidence}
    for e in fresh:
        if (e.source, e.ref) not in seen:
            seen.add((e.source, e.ref))
            state.evidence.append(e)
    del state.evidence[MAX_EVIDENCE_ITEMS:]
    return state


# ---------------------------------------------------------------------------
# diagnose — rank hypotheses by how much evidence supports them.
# Low confidence -> ask for one more evidence round, never guess.
# Day 5: the ranked hypotheses are also emitted as a validated structured
# Diagnosis (diagnosis/v1) through diagnose_with_engine(): the reasoning
# engine is pluggable — rule scoring by default, the LLM engine when
# OPS_DIAGNOSIS_ENGINE selects it and a key is configured — and any LLM
# failure is recorded on the state and falls back to the rule engine.
# The node interface does not change: hypotheses still drive make_plan,
# and the Diagnosis is the same ranking in a machine-checkable shape
# for the Days 11–12 eval. A Diagnosis that fails validation raises
# here, loudly, instead of letting a bad ranking flow downstream.
# ---------------------------------------------------------------------------
_HYPOTHESES = {
    "upstream_down": ("上游实例宕机", ["upstream", "502", "connect", "timed out"]),
    "oom_killed": ("内存 OOM 被杀", ["oom", "memory", "killed"]),
    "cpu_saturated": ("CPU 打满", ["cpu", "load", "hot loop"]),
    "disk_full": ("磁盘写满", ["disk", "space"]),
    "slow_query": ("数据库慢查询", ["slow", "query", "lock"]),
    # Day 11-12: each new cause's full keyword set appears in its own
    # injected logs (demo_service/service.py FAULTS) and nowhere else
    # in the evidence blob — checked against the 7-manual corpus and
    # the static demo logs before adding (e.g. "regression" /
    # "rollback" / "release" / "downstream" / "circuit" / "connection
    # pool" / "config" / "certificate" / "backlog" / "consumer" /
    # "cache" / "jitter" / "packet loss" occur in no manual at all).
    # Deliberately NOT used as keywords: "deploy" / "v1.4.2" /
    # "error" — all three sit in the status/metrics summary of EVERY
    # event, so any hypothesis using them would score >= 0.67 on the
    # unknown symptom and break scenario 15.
    "release_regression": ("发布后错误率上升", ["regression", "release", "rollback"]),
    "downstream_timeout": ("下游超时雪崩", ["downstream", "timeout", "circuit"]),
    "pool_exhausted": ("连接池耗尽", ["connection pool", "exhausted", "leak"]),
    "config_change": ("配置变更引发异常", ["config", "threshold", "rate_limit"]),
    "cert_expired": ("证书过期", ["certificate", "expired", "https"]),
    "mq_backlog": ("消息队列积压", ["backlog", "consumer", "queue"]),
    "cache_breakdown": ("缓存击穿", ["cache", "hot key", "expired"]),
    "network_jitter": ("单实例网络抖动", ["jitter", "packet loss", "retransmit"]),
}

# Reverse map for the diagnose() tie-break (cause name -> category key).
_CAUSE_KEY = {cause: key for key, (cause, _kws) in _HYPOTHESES.items()}


def _checks_from_evidence(state):
    """What was actually checked, derived from the collected evidence
    (never invented: counts come from state.evidence)."""
    counts: dict[str, int] = {}
    for e in state.evidence:
        counts[e.source] = counts.get(e.source, 0) + 1
    checks = []
    if counts.get("logs"):
        checks.append(f"查日志：{counts['logs']} 条命中")
    if counts.get("metrics"):
        checks.append("查指标：error_rate_5m 与 P95 延迟")
    if counts.get("status"):
        checks.append("查服务状态：上游与实例数")
    if counts.get("runbook"):
        checks.append(f"查手册：{counts['runbook']} 个相关块")
    if not checks:
        checks.append("取证未返回任何证据（日志/指标/状态/手册均为空）")
    if state.evidence_gaps:
        checks.append(f"取证缺口（已降级继续）：{'; '.join(state.evidence_gaps)}")
    return checks


def _actions_for_cause(cause):
    """Recommended actions for a root cause, from the same _PLANS table
    make_plan executes — the diagnosis and the executed plan can never
    disagree about what was recommended. The tool name is dropped: the
    diagnosis records what to do and its risk, not how to call it.
    Day 7: _PLANS holds forward steps only. The explicit rollback steps
    make_plan() generates after each high-risk step are NOT recommended
    actions — they never run in the normal flow (execute() skips them)
    and only run on the Day 9 failure path — so they are excluded here
    by construction. The rollback text of each high-risk step is still
    carried in Action.rollback, as the Diagnosis contract requires."""
    steps = _PLANS.get(cause, _PLANS["未知型故障，需转人工"])
    return [Action(action=s, risk=r, rollback=rb)
            for s, _tool, r, rb, *_rest in steps]


def diagnose(state):
    blob = " ".join(e.summary.lower() for e in state.evidence)
    scored = []
    for key, (cause, kws) in _HYPOTHESES.items():
        hits = [k for k in kws if k in blob]
        refs = [e.ref for e in state.evidence
                if any(k in e.summary.lower() for k in hits)]
        scored.append(Hypothesis(cause=cause, confidence=len(hits) / len(kws),
                                 evidence_refs=sorted(set(refs))))
    # Day 11-12: ties are broken toward the classified category.
    # classify() read the alert itself; when the collected evidence
    # supports two causes equally (e.g. a lexically close manual
    # cites "cpu"+"load" for a slow-query incident), the alert's own
    # classification decides instead of dict order. This only orders
    # equal confidences — a uniquely supported cause still wins on
    # its own score, and it never applies to the unknown path below
    # (no hypothesis key equals "unknown").
    scored.sort(key=lambda h: (-h.confidence,
                               0 if _CAUSE_KEY.get(h.cause) == state.category else 1))
    state.hypotheses = scored
    top = scored[0]
    if top.confidence < DIAGNOSE_CONFIDENCE_THRESHOLD:
        # Unknown-type fault: do not fabricate (plan Ch.9 scenario 15).
        state.hypotheses = [Hypothesis(cause="未知型故障，需转人工",
                                       confidence=top.confidence,
                                       evidence_refs=top.evidence_refs)]
    # Day 5: structured output for the same ranking, validated before
    # anything downstream reads it.
    state.diagnosis, failures = diagnose_with_engine(
        state, hypotheses=state.hypotheses,
        checks_performed=_checks_from_evidence(state),
        recommended_actions=_actions_for_cause(state.hypotheses[0].cause))
    state.diagnosis_engine_failures.extend(failures)
    return state


def needs_more_evidence(state):
    return (state.hypotheses
            and state.hypotheses[0].confidence < DIAGNOSE_CONFIDENCE_THRESHOLD
            and state.retry_count < MAX_EVIDENCE_ROUNDS)


# _PLANS holds forward steps only (Day 7): each tuple is
# (step, tool, risk, rollback-text). The explicit rollback PlanSteps
# are generated by make_plan() from _ROLLBACK_SPECS below, never stored
# here, so _actions_for_cause() (diagnosis recommended actions) can
# never pick up a rollback step.
_PLANS = {
    "上游实例宕机": [
        ("确认上游进程与端口状态", "get_service_status", "low", ""),
        ("重启上游实例（演示环境）", "restart_service", "high",
         "确认健康检查失败则回滚到上一版本"),
        ("复查错误率与健康检查", "query_metrics", "low", ""),
    ],
    # Day 2: the two injectable faults (plan Ch.9 scenarios 1–2).
    "CPU 打满": [
        ("确认 CPU 与负载曲线", "query_metrics", "low", ""),
        ("重启异常实例释放 CPU（演示环境）", "restart_service", "high",
         "重启后 CPU 未回落则回滚到上一版本"),
        ("复查 CPU 与错误率", "query_metrics", "low", ""),
    ],
    "内存 OOM 被杀": [
        ("确认实例状态与 OOM 日志", "get_service_status", "low", ""),
        ("重启被杀实例（演示环境）", "restart_service", "high",
         "重启后再次 OOM 则回滚到上一版本并转人工"),
        ("复查内存与错误率", "query_metrics", "low", ""),
    ],
    # Day 11-12: scenarios 4-13. _PLANS tuples are (step, tool, risk,
    # rollback-text) with an optional 5th element, the params dict the
    # tool needs beyond (state, service) — only the config fix uses
    # one today (update_config needs key/value); make_plan() carries
    # it into PlanStep.params and execute() passes **params.
    # High-risk step per cause (task spec): release rolls back the
    # release (rollback_release); config restores the threshold
    # (update_config, params key/value); every other new cause restarts
    # the faulty instance (restart_service, paired with
    # rollback_release by the Day 7 _ROLLBACK_SPECS). Demo honesty:
    # restart_service / rollback_release / update_config clear the
    # injected demo fault (see tools.py) — in a real system a restart
    # does not fix a slow query or an expired certificate; the demo
    # only proves the loop (diagnose -> approve -> execute -> verify),
    # not the real fix.
    "磁盘写满": [
        ("确认磁盘使用率与占用目录", "query_metrics", "low", ""),
        ("重启持有已删除文件的进程释放磁盘（演示环境）", "restart_service", "high",
         "重启后磁盘未释放则回滚到上一版本并转人工"),
        ("复查磁盘使用率与写入", "query_metrics", "low", ""),
    ],
    "数据库慢查询": [
        ("确认慢查询日志与锁等待", "search_logs", "low", ""),
        ("重启异常实例释放连接（演示环境）", "restart_service", "high",
         "重启后慢查询未缓解则回滚到上一版本并转人工"),
        ("复查 P95 与慢查询数", "query_metrics", "low", ""),
    ],
    "发布后错误率上升": [
        ("确认发布版本与错误率曲线", "get_service_status", "low", ""),
        ("回滚到上一版本 v1.4.1（演示环境）", "rollback_release", "high",
         "回滚后错误率未回落则转人工复核"),
        ("复查错误率与健康检查", "query_metrics", "low", ""),
    ],
    "下游超时雪崩": [
        ("确认下游延迟与超时指标", "query_metrics", "low", ""),
        ("重启异常实例释放堆积请求（演示环境）", "restart_service", "high",
         "重启后下游仍超时则回滚到上一版本并转人工"),
        ("复查错误率与下游延迟", "query_metrics", "low", ""),
    ],
    "连接池耗尽": [
        ("确认连接池使用率与等待线程", "query_metrics", "low", ""),
        ("重启异常实例释放泄漏连接（演示环境）", "restart_service", "high",
         "重启后连接池仍耗尽则回滚到上一版本并转人工"),
        ("复查连接池与错误率", "query_metrics", "low", ""),
    ],
    "配置变更引发异常": [
        ("确认配置快照与当前限流阈值", "get_service_status", "low", ""),
        ("恢复限流阈值为 1000（演示环境）", "update_config", "high",
         "恢复后异常未缓解则用快照写回变更前的值并转人工",
         {"key": "rate_limit", "value": "1000"}),
        ("复查错误率与限流拒绝数", "query_metrics", "low", ""),
    ],
    "证书过期": [
        ("确认证书有效期与 TLS 握手失败", "search_runbook", "low", ""),
        ("重启异常实例（演示环境）", "restart_service", "high",
         "重启后证书仍过期则回滚到上一版本并转人工"),
        ("复查 TLS 握手与错误率", "query_metrics", "low", ""),
    ],
    "消息队列积压": [
        ("确认队列深度与消费者状态", "query_metrics", "low", ""),
        ("重启消费者实例（演示环境）", "restart_service", "high",
         "重启后积压未缓解则回滚到上一版本并转人工"),
        ("复查队列深度与消费速率", "query_metrics", "low", ""),
    ],
    "缓存击穿": [
        ("确认缓存命中率与热点 Key 状态", "query_metrics", "low", ""),
        ("重启异常实例重建缓存（演示环境）", "restart_service", "high",
         "重启后缓存仍击穿则回滚到上一版本并转人工"),
        ("复查缓存命中率与错误率", "query_metrics", "low", ""),
    ],
    "单实例网络抖动": [
        ("确认单实例网络丢包与延迟", "query_metrics", "low", ""),
        ("重启抖动实例（演示环境）", "restart_service", "high",
         "重启后网络仍抖动则回滚到上一版本并转人工"),
        ("复查丢包率与错误率", "query_metrics", "low", ""),
    ],
    "未知型故障，需转人工": [
        ("整理已有证据并转人工处理", "search_runbook", "low", ""),
    ],
}


# ---------------------------------------------------------------------------
# make_plan — steps with a risk tag; every high-risk step gets a rollback.
# Day 7 (plan Ch.6 module 5): the rollback is an explicit PlanStep, not
# a text note on the high-risk step. make_plan() appends exactly one
# rollback step immediately after each high-risk step (1:1 pairing,
# rollback_of names the high step's text). Rollback steps are marked
# risk="low" in the plan: they are dormant — execute() skips them and
# they only run on the Day 9 failure path — so they must not inflate
# the plan's forward risk_level. That "low" is plan accounting, not a
# claim that the underlying operation is risk-free: the rollback tool
# itself stays in tools.HIGH_RISK_TOOLS (rollback_release /
# scale_service / update_config), so the Day 9 trigger path must still
# enforce human approval before invoking it. Each rollback step's
# rollback field states in words what it undoes.
#
# Tool choice per high-risk tool (all tools exist in app/tools.py,
# nothing invented):
# - restart_service -> rollback_release: the original rollback text
#   already said "回滚到上一版本"; rollback_release is that operation.
# - scale_service -> scale_service again, back to the pre-change
#   replica count (demo baseline 2, stated in the step text; the Day 9
#   trigger must pass the recorded count as the replicas argument —
#   execute() today calls high-risk tools with (state, service) only).
# - update_config -> update_config again, writing back the snapshot's
#   old value (snapshot-first semantics, plan Ch.5; the Day 9 trigger
#   must pass the snapshotted key/old value).
# - rollback_release -> get_service_status: it already IS a rollback,
#   so there is nothing further to auto-undo; the paired step confirms
#   the post-rollback version/health and hands off to a human.
# - any other high-risk tool -> get_service_status + human review
#   (no invented auto-undo).
# ---------------------------------------------------------------------------
_ROLLBACK_SPECS = {
    "restart_service": {
        "step": "回滚：回滚到上一版本",
        "tool": "rollback_release",
        "rollback": "回滚“{high}”：若重启后健康检查失败或故障复现，执行本步骤回滚到上一版本",
    },
    "scale_service": {
        "step": "回滚：调回原副本数 2",
        "tool": "scale_service",
        "rollback": "回滚“{high}”：把副本数调回操作前的原副本数（演示基线 2）",
    },
    "update_config": {
        "step": "回滚：用快照恢复配置旧值",
        "tool": "update_config",
        "rollback": "回滚“{high}”：用 update_config 快照把配置写回旧值",
    },
    "rollback_release": {
        "step": "回滚：确认回滚后版本并转人工复核",
        "tool": "get_service_status",
        "rollback": "本步骤本身已是回滚操作（“{high}”）：无进一步自动回滚，确认回滚后版本与健康状态并转人工复核",
    },
}
_ROLLBACK_FALLBACK = {
    "step": "回滚：确认状态并转人工复核",
    "tool": "get_service_status",
    "rollback": "回滚“{high}”：无预定义的自动回滚，确认服务状态后转人工判断",
}


def _runbook_source(state):
    """The top runbook chunk ref collected in evidence (the retrieval
    query is the alert text, so the top chunk is the most cause-relevant
    one), or "" when no runbook evidence was collected. The ref is the
    same "runbook:<chunk_id>" string the evidence list carries, so a
    plan source can be traced back to the evidence item."""
    for e in state.evidence:
        if e.source == "runbook":
            return e.ref
    return ""


def plan_pairing(state):
    """High-risk / rollback pairing counts for the report and status
    output: how many high-risk forward steps, how many rollback steps,
    and how many rollback steps point at a real high-risk step."""
    high = [p for p in state.plan if p.risk == "high" and not p.is_rollback]
    rollbacks = [p for p in state.plan if p.is_rollback]
    high_texts = {p.step for p in high}
    paired = sum(1 for r in rollbacks if r.rollback_of in high_texts)
    return {"high": len(high), "rollback": len(rollbacks), "paired": paired}


def make_plan(state):
    cause = state.hypotheses[0].cause if state.hypotheses else "未知型故障，需转人工"
    steps = _PLANS.get(cause, _PLANS["未知型故障，需转人工"])
    # Day 8: a modification recorded at human_approval is a standing
    # instruction for this incident — a dropped step must not come back
    # when make_plan() runs again on the verify-retry loop (verify ->
    # diagnose -> make_plan). Without this, dropping the high-risk
    # restart would be undone: verify fails (error rate still high),
    # the retry rebuilds the full plan, and the restart runs anyway.
    # With no modification recorded the dropped set is empty and this
    # changes nothing.
    dropped = set((state.approval or {}).get("dropped_steps") or [])
    # Day 9: same standing-instruction rule for steps that were
    # already rolled back on the failure path — a rebuild after a
    # failed verify must not bring back a change the rollback just
    # undid (e.g. the restart whose follow-up check failed). The
    # paired rollback step disappears with its forward step, since
    # make_plan() only generates a rollback for a high step that is
    # actually in the rebuilt plan.
    dropped |= set(state.rolled_back_steps or [])
    steps = [s for s in steps if s[0] not in dropped]
    # Day 7: every step cites its source. With runbook evidence, the
    # top chunk ref; without any manual/case support, the step is
    # explicitly marked "建议人工判断" instead of silently looking
    # sourced (the unknown-cause plan always lands here: it collects
    # no runbook evidence).
    source = _runbook_source(state) or "建议人工判断"
    expanded: list[PlanStep] = []
    for tup in steps:
        s, t, r, rb = tup[:4]
        params = dict(tup[4]) if len(tup) > 4 else {}
        expanded.append(PlanStep(step=s, tool=t, risk=r, rollback=rb,
                                 source=source, params=params))
        if r == "high":
            spec = _ROLLBACK_SPECS.get(t, _ROLLBACK_FALLBACK)
            # Day 9: a rollback step that needs arguments carries them
            # in params so the Day 9 trigger can pass them to the tool.
            # scale_service rolls back to the pre-change replica count
            # (demo baseline 2). update_config has no value to fill at
            # plan time — the old value lives in the snapshot the
            # forward step writes, so its params are resolved at
            # rollback time from that snapshot (see
            # _resolve_rollback_params); a missing snapshot fails the
            # rollback step with a reason instead of guessing a value.
            rb_params: dict = {}
            if t == "scale_service":
                rb_params = {"replicas": 2}
            expanded.append(PlanStep(
                step=spec["step"], tool=spec["tool"], risk="low",
                rollback=spec["rollback"].format(high=s),
                is_rollback=True, rollback_of=s, source=source,
                params=rb_params))
    state.plan = expanded
    # risk_level reflects the forward plan only: dormant rollback steps
    # (risk "low" by construction) must not change it.
    state.risk_level = "high" if any(
        p.risk == "high" and not p.is_rollback for p in state.plan) else "low"
    return state


# ---------------------------------------------------------------------------
# human_approval — high risk interrupts here; state is persisted by the
# runner before returning, so a restart resumes from this node (plan Ch.4).
# ---------------------------------------------------------------------------
def human_approval(state):
    if state.risk_level == "high" and not state.approval:
        state.status = "awaiting_approval"
    return state


def apply_modification(state, drop_numbers, by="", reason=""):
    """Day 8: modify the plan of an incident stopped at human_approval
    (plan Ch.4 node table: approve / reject / modify all resume).

    drop_numbers are 1-based positions in state.plan as shown by
    `status` / `status --list-steps`. Only forward steps may be dropped:
    dropping a rollback step would break the Day 7 1:1 pairing, so it is
    refused with a ValueError before anything is mutated. Dropping a
    high-risk forward step removes its paired rollback step with it, so
    the pairing stays consistent; risk_level is recomputed from the
    remaining forward steps. The dropped steps leave the execution
    queue but stay auditable: the removal is recorded in
    state.exec_result and in state.approval (modified=True, which
    steps, which paired rollback steps, who, why). make_plan() honours
    approval["dropped_steps"] if it ever rebuilds the plan on the
    verify-retry loop, so a dropped step cannot silently come back.
    """
    if state.status != "awaiting_approval":
        raise ValueError(
            f"incident {state.incident_id} is not awaiting approval "
            f"(status={state.status}); modify only applies to an incident "
            f"stopped at human_approval")
    numbers = sorted(set(drop_numbers))
    if not numbers:
        raise ValueError("no --drop-step given: pass at least one step number")
    dropped = []
    for n in numbers:
        if n < 1 or n > len(state.plan):
            raise ValueError(
                f"step {n} out of range: plan has {len(state.plan)} steps "
                f"(numbered from 1, see `status --list-steps`)")
        step = state.plan[n - 1]
        if step.is_rollback:
            raise ValueError(
                f"step {n} (\"{step.step}\") is a rollback step and cannot "
                f"be dropped: removing it would break the Day 7 1:1 "
                f"rollback pairing (drop its high-risk forward step "
                f"instead — the paired rollback step is removed with it)")
        dropped.append(step)
    dropped_texts = {s.step for s in dropped}
    # Paired rollback steps go with their forward step (index-based, so
    # duplicate step texts elsewhere in the plan are never touched).
    remove_idx = {n - 1 for n in numbers}
    remove_idx |= {i for i, p in enumerate(state.plan)
                   if p.is_rollback and p.rollback_of in dropped_texts}
    removed_rollbacks = [state.plan[i].step for i in sorted(remove_idx)
                         if state.plan[i].is_rollback]
    # Audit record first: the removal must be traceable even though the
    # steps leave the plan.
    state.exec_result.append({
        "step": "modify (human_approval)", "ok": True,
        "detail": (f"modified by {by}: dropped forward steps "
                   f"{[s.step for s in dropped]} (plan positions "
                   f"{numbers}); paired rollback steps removed: "
                   f"{removed_rollbacks}; reason: {reason}")})
    state.plan = [p for i, p in enumerate(state.plan)
                  if i not in remove_idx]
    # risk_level reflects the forward plan only (Day 7 convention):
    # dropping the last high-risk step drops the plan to low.
    state.risk_level = "high" if any(
        p.risk == "high" and not p.is_rollback for p in state.plan) else "low"
    state.approval = {"approved": True, "by": by, "reason": reason,
                      "modified": True,
                      "dropped_steps": [s.step for s in dropped],
                      "dropped_indices": numbers,
                      "dropped_rollback_steps": removed_rollbacks}
    return state


# ---------------------------------------------------------------------------
# execute — steps in order; any failure stops the rest and rolls back.
#
# Day 9 (plan Ch.11, Ch.6 module 7, Ch.4 node table: "execute 任一步失
# 败立即停止并触发回滚"):
# - Failure hook (TEST ONLY): OPS_AGENT_FAIL_STEP=N (1-based plan
#   position, as shown by `status --list-steps`) makes execute() raise
#   at that forward step, simulating a tool failure. Unset (the
#   default) the behaviour is exactly the Day 1–8 one; the hook never
#   touches the tools themselves and must never be set in production.
#   A hook aimed at a rollback step number has no effect — rollback
#   steps are dormant in the normal flow and are never executed here.
# - Any step failure (hook, or a tool raising anything other than
#   PermissionError — which still closes the incident as "blocked",
#   exactly as before) stops the remaining forward steps immediately.
# - Automatic rollback: for every high-risk step that already
#   succeeded, its paired rollback step (Day 7 1:1 pairing) runs in
#   reverse order. Steps that never ran are NOT rolled back. A
#   rollback step that itself fails is recorded and the next rollback
#   still runs — one bad undo must not wedge the whole failure path.
# - Design decision (also in the README): the human approval at
#   human_approval covers the WHOLE plan, paired rollback steps
#   included, so a failure-path rollback is part of the already
#   approved plan and fires automatically without a second prompt.
#   The rollback tools stay high-risk in the registry
#   (tools.HIGH_RISK_TOOLS) — that does not change — and the report
#   names who approved the original plan, so the authorisation is
#   traceable. _require_approval still runs inside each rollback
#   tool call, so a rollback can never fire on an unapproved run.
# - Params (Day 7 gap, closed here): high-risk tools are called with
#   **step.params — empty params means "(state, service)" only, the
#   Day 1–8 call shape, so the normal path is byte-for-byte unchanged.
# ---------------------------------------------------------------------------
def _fail_step_from_env():
    """1-based plan position from OPS_AGENT_FAIL_STEP, or None.

    Unset, blank, non-numeric or < 1 means "no hook" — the hook is
    opt-in and off by default, so a stray value can never fail a
    normal run by accident."""
    raw = os.environ.get("OPS_AGENT_FAIL_STEP", "").strip()
    if not raw:
        return None
    try:
        n = int(raw)
    except ValueError:
        return None
    return n if n >= 1 else None


def _resolve_rollback_params(state, rb_step):
    """Fill in the arguments a rollback step needs; returns
    (params, error). error is None on success.

    - scale_service: params were filled by make_plan()
      ({"replicas": 2}); kept as-is.
    - update_config: the old value comes from the snapshot the forward
      step wrote (snapshot-first, plan Ch.5). Prefer the snapshot for
      the key named in the step's params; with no key recorded, take
      the most recent snapshot. No snapshot at all -> the rollback
      step fails with a reason instead of guessing a value.
    - anything else: the step's params as recorded (usually {})."""
    tool = rb_step.tool
    if tool == "scale_service":
        params = dict(rb_step.params) or {"replicas": 2}
        rb_step.params = params
        return params, None
    if tool == "update_config":
        params = dict(rb_step.params)
        snaps = state.demo_service.get("config_snapshots") or []
        snap = None
        key = params.get("key")
        if key is not None:
            for s in reversed(snaps):
                if s.get("key") == key:
                    snap = s
                    break
        elif snaps:
            snap = snaps[-1]
        if snap is None:
            return None, ("no config snapshot found for update_config "
                          "rollback, cannot restore old value")
        params = {"key": snap["key"], "value": snap.get("old")}
        rb_step.params = params
        return params, None
    return dict(rb_step.params), None


def _run_rollbacks(state, succeeded_high):
    """Undo succeeded high-risk steps, most recent first, using each
    step's paired rollback step. A rollback that fails is recorded
    (plan step result + exec_result) and the next one still runs.
    Every high step whose rollback was attempted is recorded in
    state.rolled_back_steps so make_plan() never rebuilds it on the
    verify-retry loop; a high step with no paired rollback step in the
    plan is reported but not marked (nothing was undone)."""
    service = state.alert.get("service", "web-api")
    for high in reversed(succeeded_high):
        rb = next((p for p in state.plan
                   if p.is_rollback and p.rollback_of == high.step), None)
        if rb is None:
            state.exec_result.append({
                "step": f"rollback: {high.step}", "ok": False,
                "detail": ("no paired rollback step found in plan; "
                           "nothing rolled back")})
            continue
        params, err = _resolve_rollback_params(state, rb)
        if err is not None:
            rb.result = f"failed: {err}"
            state.exec_result.append({"step": rb.step, "ok": False,
                                      "detail": err})
            state.rolled_back_steps.append(high.step)
            continue
        try:
            if rb.tool in tools.HIGH_RISK_TOOLS:
                out = tools.HIGH_RISK_TOOLS[rb.tool](state, service, **params)
            else:
                # Paired rollback that is itself read-only
                # (rollback_release -> get_service_status confirm).
                out = getattr(tools, rb.tool)(state, service)
            rb.result = f"ok: {out}"
            state.exec_result.append({"step": rb.step, "ok": True,
                                      "detail": str(out)})
        except PermissionError as e:
            # Approval was recorded for the plan, so this should not
            # happen; if it does, record it and keep rolling back.
            rb.result = f"blocked: {e}"
            state.exec_result.append({"step": rb.step, "ok": False,
                                      "detail": str(e)})
        except Exception as e:  # noqa: BLE001 — one bad undo must not
            # wedge the remaining rollbacks.
            rb.result = f"failed: {e}"
            state.exec_result.append({"step": rb.step, "ok": False,
                                      "detail": str(e)})
        state.rolled_back_steps.append(high.step)


def execute(state):
    state.status = "executing"
    service = state.alert.get("service", "web-api")
    fail_step = _fail_step_from_env()
    succeeded_high: list[PlanStep] = []
    failed = False
    for idx, step in enumerate(state.plan, 1):
        # Day 7: rollback steps are dormant in the normal flow — they
        # are defined, paired and shown, but never executed here. They
        # only run on the Day 9 failure path. Skipping means no
        # exec_result entry either, so a report can never mistake a
        # rollback for something that ran.
        if step.is_rollback:
            step.result = "skipped: rollback step, runs only on failure path (Day 9)"
            continue
        try:
            # Day 9 test hook (see the module comment above): fail
            # this forward step on purpose. It fires before the tool
            # runs, so a hook on a high-risk step means that step
            # never succeeded and is correctly NOT rolled back.
            if fail_step is not None and idx == fail_step:
                raise RuntimeError(
                    f"injected failure at plan step {idx} "
                    f"({step.step}) via OPS_AGENT_FAIL_STEP={fail_step} "
                    f"(test hook, not a real tool error)")
            if step.tool in tools.HIGH_RISK_TOOLS:
                fn = tools.HIGH_RISK_TOOLS[step.tool]
                # Day 9: pass **params ({} in the normal plans, so
                # this is still just (state, service) there).
                out = fn(state, service, **(step.params or {}))
                step.result = f"ok: {out}"
                state.exec_result.append({"step": step.step, "ok": True,
                                          "detail": str(out)})
                if step.risk == "high":
                    succeeded_high.append(step)
            else:
                step.result = "ok: read-only check, no state change"
                state.exec_result.append({"step": step.step, "ok": True,
                                          "detail": "read-only"})
        except PermissionError as e:
            step.result = f"blocked: {e}"
            state.exec_result.append({"step": step.step, "ok": False,
                                      "detail": str(e)})
            state.status = "closed"
            return state
        except Exception as e:  # noqa: BLE001 — any other failure
            # stops the run and triggers the rollback below.
            step.result = f"failed: {e}"
            state.exec_result.append({"step": step.step, "ok": False,
                                      "detail": str(e)})
            failed = True
            break
    if failed and succeeded_high:
        # Only steps that actually succeeded are undone; a step that
        # never ran (including the one that just failed) has nothing
        # to roll back, and its rollback step stays dormant.
        state.rollback_triggered = True
        _run_rollbacks(state, succeeded_high)
    return state


# ---------------------------------------------------------------------------
# verify — re-check metrics/logs; not recovered -> diagnose once more.
# ---------------------------------------------------------------------------
def verify(state):
    state.status = "verifying"
    m = tools.query_metrics(state, state.alert.get("service", "web-api"))
    recovered = m.get("error_rate_5m", 1) < 0.01
    # Day 9: a verify that runs after a rollback says so in its
    # detail, so the report (and is_recovered callers) can tell
    # "recovered after rollback" apart from a normal recovery. The
    # threshold is the same demo-metric check as before.
    if state.rollback_triggered:
        detail = f"rolled back, error_rate_5m={m.get('error_rate_5m')}"
    else:
        detail = f"error_rate_5m={m.get('error_rate_5m')}"
    state.exec_result.append({"step": "verify", "ok": recovered,
                              "detail": detail})
    return state


def is_recovered(state):
    return any(r.get("step") == "verify" and r.get("ok")
               for r in state.exec_result)


# ---------------------------------------------------------------------------
# report — Markdown postmortem (plan Ch.6 module 8), written by report.py.
# ---------------------------------------------------------------------------
def report(state):
    # Day 10 (plan Ch.6 module 8): the report node is also the
    # archiving point — a finished, known-cause, recovered incident
    # is filed back into the knowledge base as a history case
    # (data/cases/<id>.json) BEFORE rendering, so the report can say
    # it was archived. Archiving is best-effort: a failure is noted
    # in the trace and never blocks the report.
    #
    # Day 10 fault tolerance: if render_report() itself raises, the
    # incident must not be lost — save a minimal report (Trace +
    # raw state summary, marked as such) instead, keep the status
    # the original flow would have set (done/closed), and record
    # the error in the trace so the failure is visible, not silent.
    from .cases import archive_case
    from .report import render_minimal_report, render_report
    from .store import save_report
    # Day 8: an incident closed by expire (timeout) stays closed even
    # though it goes through the report node — the closed_reason marker
    # is what distinguishes it. Normal and rejected runs end "done"
    # exactly as before (rejected incidents carry no closed_reason).
    state.status = "closed" if state.closed_reason else "done"
    try:
        archive_case(state)
    except Exception as e:  # noqa: BLE001 — archiving must not lose
        # the incident; the failure is visible in the trace.
        state.trace.append({"node": "report", "ms": 0,
                            "note": f"案例归档失败（不影响报告）：{e}"})
    try:
        markdown = render_report(state)
    except Exception as e:  # noqa: BLE001 — render failure must not
        # lose the incident: fall back to a minimal report.
        state.trace.append({"node": "report", "ms": 0,
                            "note": f"完整报告生成失败，已保存最小报告：{e}"})
        markdown = render_minimal_report(state, e)
    state.report_path = str(save_report(state, markdown))
    return state


NODES = ["classify", "collect_evidence", "diagnose", "make_plan",
         "human_approval", "execute", "verify", "report"]

NODE_FUNCS = {"classify": classify, "collect_evidence": collect_evidence,
              "diagnose": diagnose, "make_plan": make_plan,
              "human_approval": human_approval, "execute": execute,
              "verify": verify, "report": report}
