#!/usr/bin/env python3
"""Ops diagnosis Agent — Days 11-12: 15-scenario eval harness.

Runs every scenario in eval/scenarios.json against the real CLI
(inject -> demo --alert --auto-approve) in a subprocess, scores the
stored structured Diagnosis, and writes eval/report.json (machine
readable) + eval/report.md (human readable).

Scoring (plan Ch.9 口径):
  * Top 1 命中：Diagnosis.causes[0].cause == 场景标准根因。
  * Top 3 命中：标准根因在 causes[:3] 里（单独统计）。
  * 证据完整率：诊断中置信度 > 0 的原因所引用的 refs，能在
    state.evidence 里定位到的比例（Diagnosis 校验本身已保证
    引用存在，这里是独立重算，不是信任校验）。
  * 误操作数：state.tool_audit 里出现的高危工具调用（restart /
    scale / rollback / update_config）中，执行时 approval.approved
    不为真的次数。--auto-approve 下 approval 是批准的，所以口径
    上记 0；场景 14 收尾的 approve 同理。目标 0。
  * 耗时：单次 demo 子进程的墙钟时间（秒）。
  * 场景 14（重复告警风暴）不参与 Top 1 / Top 3 统计：同一告警
    连发 3 次（不加 --auto-approve，第一起事件停在
    human_approval 未结束），新建事件数应为 1；随后 approve 收尾。
  * 场景 15（未知型故障）：标准根因就是"未知型故障，需转人工"，
    另核查 is_unknown、Top 1 置信度 < 0.5、只给低风险处置、未执
    行任何高危工具。

Isolation （评测隔离，可复现）：
  * 每个场景跑之前清空一个临时案例目录，并把
    OPS_AGENT_CASES_DIR 指过去（app/cases.py 与
    app/retrieval.py 都支持该环境变量，默认 data/cases）。
    真实的 data/cases/ 一个字节不动；场景之间也不会互相读到对方
    归档的历史案例——诊断时看到的案例证据只来自本场景内归档的
    案例，而归档发生在 report 节点、诊断之后，所以评测里每个场
    景的诊断实际上看不到任何历史案例。
  * harness 开始时删除 data/dedup.json（去重记录是全局的；上
    一次评测若留下未结束的待确认事件，会让本次的同指纹告警误
    合并）。data/incidents/ 不清理——它是审计轨迹，评测报告里
    每个场景都记了 incident_id，可回放。
  * OPS_DIAGNOSIS_ENGINE=rule：诊断走规则推理（没有 LLM key，
    与日常跑法一致），不调任何真实 LLM 端点。

Usage (from ops-agent/):
  python3 eval/run_eval.py            # 跑全部 15 个场景
  python3 eval/run_eval.py --only 1 4 5   # 只跑指定场景（调试用）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
EVAL_DIR = HERE

HIGH_RISK_TOOLS = {"restart_service", "scale_service",
                   "rollback_release", "update_config"}
INCIDENT_RE = re.compile(r"^incident (INC-[\S]+):", re.MULTILINE)
MERGED_RE = re.compile(r"重复告警，已合并到 (INC-[\S]+)")


def run_cli(args, env, timeout=180):
    """Run the agent CLI in a subprocess; returns (rc, stdout, stderr)."""
    p = subprocess.run(
        [sys.executable, "-m", "app.main", *args],
        cwd=ROOT, env=env, capture_output=True, text=True,
        timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def load_state(incident_id):
    p = ROOT / "data" / "incidents" / incident_id / "state.json"
    return json.loads(p.read_text(encoding="utf-8"))


def score_diagnosis(state, expected_cause):
    """Score one finished incident's structured diagnosis."""
    diag = state.get("diagnosis") or {}
    causes = diag.get("causes") or []
    ev_refs = {e["ref"] for e in state.get("evidence", [])}
    cited = []
    for c in causes:
        if float(c.get("confidence", 0)) > 0:
            cited.extend(c.get("evidence_refs") or [])
    cited_unique = sorted(set(cited))
    found = sum(1 for r in cited_unique if r in ev_refs)
    completeness = (found / len(cited_unique)) if cited_unique else 1.0
    # 误操作：高危工具审计记录里，执行时没有批准记录的次数。
    approval = state.get("approval") or {}
    approved = bool(approval.get("approved"))
    misops = sum(
        1 for a in state.get("tool_audit", [])
        if a.get("tool") in HIGH_RISK_TOOLS and not approved)
    # 交叉核对：plan 里 result 以 ok 开头的高危步骤（回滚步骤除外，
    # 它们只在失败路径执行，同样受同一份批准覆盖）。
    plan_misops = sum(
        1 for p in state.get("plan", [])
        if p.get("tool") in HIGH_RISK_TOOLS and not p.get("is_rollback")
        and str(p.get("result", "")).startswith("ok:") and not approved)
    assert misops == plan_misops, (
        f"misop count mismatch: audit={misops} plan={plan_misops}")
    recovered = any(r.get("step") == "verify" and r.get("ok")
                    for r in state.get("exec_result", []))
    top1 = causes[0] if causes else {}
    return {
        "incident_id": state.get("incident_id", ""),
        "status": state.get("status", ""),
        "category": state.get("category", ""),
        "top1_cause": top1.get("cause", ""),
        "top1_confidence": round(float(top1.get("confidence", 0)), 2),
        "top1_hit": top1.get("cause") == expected_cause,
        "top3_hit": expected_cause in [c.get("cause") for c in causes[:3]],
        "top3": [(c.get("cause"), round(float(c.get("confidence", 0)), 2))
                 for c in causes[:3]],
        "evidence_count": len(state.get("evidence", [])),
        "evidence_cited": len(cited_unique),
        "evidence_completeness": round(completeness, 2),
        "misoperations": misops,
        "recovered": recovered,
        "is_unknown": bool(diag.get("is_unknown")),
        "high_risk_executed": sorted(
            {a.get("tool") for a in state.get("tool_audit", [])
             if a.get("tool") in HIGH_RISK_TOOLS}),
    }


def scene_env(env, cases_root, scene_id):
    """One fresh (empty) cases directory per scenario, so no scenario
    can read a case archived by an earlier one — and the archives
    stay on disk afterwards for inspection (unlike clearing one
    shared temp dir before every scenario, which would wipe the
    previous scenario's archive)."""
    d = Path(cases_root) / f"scene-{scene_id:02d}"
    d.mkdir(parents=True, exist_ok=True)
    e = dict(env)
    e["OPS_AGENT_CASES_DIR"] = str(d)
    return e, str(d)


def run_diagnosis_scenario(sc, env, cases_dir):
    env, cases_dir = scene_env(env, cases_dir, sc["id"])
    fault = sc.get("fault")
    rc, out, err = run_cli(
        ["inject", fault if fault else "clear"], env)
    if rc != 0:
        return {"id": sc["id"], "name": sc["name"], "error":
                f"inject failed rc={rc}: {(out + err).strip()[-300:]}"}
    t0 = time.perf_counter()
    rc, out, err = run_cli(
        ["demo", "--alert", sc["alert"], "--auto-approve"], env)
    duration = round(time.perf_counter() - t0, 2)
    m = INCIDENT_RE.search(out)
    if rc != 0 or not m:
        return {"id": sc["id"], "name": sc["name"],
                "error": f"demo failed rc={rc}: {(out + err).strip()[-500:]}",
                "duration_s": duration}
    rec = score_diagnosis(load_state(m.group(1)), sc["expected_cause"])
    rec.update({"id": sc["id"], "name": sc["name"],
                "expected_cause": sc["expected_cause"],
                "fault": fault, "alert": sc["alert"],
                "evidence_source": sc.get("evidence_source", ""),
                "duration_s": duration})
    # 场景 15 的额外核查：必须转人工、不编造。
    if sc["id"] == 15:
        rec["unknown_checks"] = {
            "is_unknown": rec["is_unknown"],
            "top1_below_threshold": rec["top1_confidence"] < 0.5,
            "only_low_risk_actions": not rec["high_risk_executed"],
            "needs_human": bool(
                (load_state(m.group(1)).get("diagnosis") or {})
                .get("needs_human")),
        }
        rec["unknown_ok"] = all(rec["unknown_checks"].values())
    return rec


def run_storm_scenario(sc, env, cases_dir):
    """Scenario 14: same alert 3x, no --auto-approve. The first demo
    stops at human_approval (still open); the 2nd and 3rd must merge
    into it instead of creating new events. Then approve to close."""
    env, cases_dir = scene_env(env, cases_dir, 14)
    rc, out, err = run_cli(["inject", sc["fault"]], env)
    if rc != 0:
        return {"id": 14, "name": sc["name"],
                "error": f"inject failed rc={rc}: {(out + err).strip()[-300:]}"}
    created, merged = [], []
    for _ in range(3):
        rc, out, err = run_cli(["demo", "--alert", sc["alert"]], env)
        if rc != 0:
            return {"id": 14, "name": sc["name"],
                    "error": f"demo failed rc={rc}: {(out + err).strip()[-500:]}"}
        m = INCIDENT_RE.search(out)
        g = MERGED_RE.search(out)
        if m:
            created.append(m.group(1))
        if g:
            merged.append(g.group(1))
    rec = {"id": 14, "name": sc["name"], "expected_cause": None,
           "fault": sc["fault"], "alert": sc["alert"],
           "evidence_source": sc.get("evidence_source", ""),
           "demos_sent": 3, "events_created": len(created),
           "created_incidents": created,
           "merged_into": merged,
           "dedup_ok": len(created) == 1 and len(merged) == 2
           and len(set(merged)) == 1 and merged[0] == created[0]}
    if created:
        # 诊断信息（事件仍在 awaiting_approval，诊断已落盘）与收尾。
        st = load_state(created[0])
        d = score_diagnosis(st, "CPU 打满")
        rec.update({"first_event_top1": d["top1_cause"],
                    "first_event_top1_confidence": d["top1_confidence"],
                    "first_event_top1_hit": d["top1_hit"],
                    "evidence_count": d["evidence_count"],
                    "misoperations": d["misoperations"]})
        rc, out, err = run_cli(
            ["approve", created[0], "--by", "eval",
             "--reason", "场景 14 收尾：确认执行"], env)
        st = load_state(created[0])
        rec["closed_status"] = st.get("status", "")
        rec["recovered"] = any(
            r.get("step") == "verify" and r.get("ok")
            for r in st.get("exec_result", []))
    return rec


def render_markdown(results, summary, cases_dir, started_at):
    lines = []
    lines.append("# 运维诊断 Agent — 15 场景评测报告（第 11–12 天）")
    lines.append("")
    lines.append(f"生成时间：{started_at}（本地时间）")
    lines.append(f"评测集：eval/scenarios.json（方案第 9 章 15 个场景）")
    lines.append("")
    lines.append("## 汇总")
    lines.append("")
    lines.append(f"- Top 1 命中率：{summary['top1_hits']}/{summary['diagnosis_scenarios']} = {summary['top1_rate']:.2f}")
    lines.append(f"- Top 3 命中率：{summary['top3_hits']}/{summary['diagnosis_scenarios']} = {summary['top3_rate']:.2f}")
    lines.append(f"- 平均证据完整率：{summary['mean_evidence_completeness']:.2f}")
    lines.append(f"- 误操作总数：{summary['total_misoperations']}（目标 0）")
    lines.append(f"- 平均单次诊断耗时：{summary['mean_duration_s']:.2f} 秒")
    lines.append(f"- 场景 14 去重：{'通过' if summary['dedup_ok'] else '未通过'}（连发 3 条相同告警新建事件数 = {summary['dedup_events_created']}，应为 1）")
    lines.append("")
    lines.append("## 逐场景结果")
    lines.append("")
    lines.append("| 场景 | 标准根因 | Top 1 | Top 1 置信度 | Top 1 命中 | Top 3 命中 | 证据完整率 | 误操作数 | 耗时（秒） | 事件 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in results:
        if r.get("error"):
            lines.append(f"| {r['id']} {r['name']} | — | — | — | — | — | — | — | — | 报错：{r['error'][:80]} |")
            continue
        if r["id"] == 14:
            lines.append(
                f"| 14 重复告警风暴 | 去重（新建 1 个事件） | {r.get('first_event_top1', '—')} | "
                f"{r.get('first_event_top1_confidence', '—')} | —（去重场景） | — | — | "
                f"{r.get('misoperations', '—')} | — | {r['created_incidents'][0] if r.get('created_incidents') else '—'} |")
            continue
        lines.append(
            f"| {r['id']} {r['name']} | {r['expected_cause']} | {r['top1_cause']} | "
            f"{r['top1_confidence']:.2f} | {'是' if r['top1_hit'] else '否'} | "
            f"{'是' if r['top3_hit'] else '否'} | {r['evidence_completeness']:.2f} | "
            f"{r['misoperations']} | {r['duration_s']:.2f} | {r['incident_id']} |")
    lines.append("")
    lines.append("## 评测隔离（可复现）")
    lines.append("")
    lines.append(f"- 案例隔离：每个场景用自己的空案例目录 `{cases_dir}/scene-<id>/`，并以 "
                "`OPS_AGENT_CASES_DIR` 指过去；真实的 `data/cases/` 未动，"
                "场景之间不互相读到对方归档的历史案例。诊断发生在归档"
                "之前，所以评测里每个场景的诊断实际上看不到任何历史案例；"
                "各场景归档的案例保留在各自的 `scene-<id>/` 目录里备查。")
    lines.append("- 去重隔离：harness 开始时删除 `data/dedup.json`，避免上一次"
                "评测留下的未结束事件让本次同指纹告警误合并。")
    lines.append("- 诊断引擎：`OPS_DIAGNOSIS_ENGINE=rule`（规则推理，无 LLM key，"
                "不调任何真实 LLM 端点）。")
    lines.append("- `data/incidents/` 不清理：它是审计轨迹，上表每个场景的"
                " incident_id 都可回放（`status` / `trace` / `report`）。")
    lines.append("")
    lines.append("## 诚实说明")
    lines.append("")
    lines.append("- 演示数据：故障全部由 `demo_service` 注入（日志、指标、状态均"
                "为演示值）；手册是 7 篇示例运维手册（35 chunk），检索是本地 "
                "BM25 + TF-IDF + RRF + 本地词法 Rerank（ES / Milvus 未接，"
                "cross-encoder 未装）；诊断是关键词规则推理，不是 LLM。")
    lines.append("- 场景 6–13 的处置是演示级简化：除发布回滚（rollback_release）"
                "与配置恢复（update_config）外，其余高危步骤都是重启实例——"
                "演示环境里重启会清掉注入故障，所以 verify 能看到恢复；真实"
                "系统里重启修不好慢查询、证书过期或缓存击穿，评测只证明"
                "了“诊断 → 确认 → 执行 → 验证”的闭环能跑通，不证明真实修复手段。")
    lines.append("- 检索仍是词法匹配：慢查询场景的手册 Top 3 里仍有一条 CPU 手册"
                "（接口 / P95 / 延迟等通用词重叠），但 CPU 假设因缺 \"hot loop\""
                "只到 0.67，未影响 Top 1；历史案例匹配同样是词法匹配，跨场景"
                "运行时可能引用错根因的案例（本次评测因案例隔离未出现）。")
    lines.append("- 场景 14 去重的另外两个分支（前一事件已结束时建新事件、"
                "超过 10 分钟时建新事件）在 harness 外手工验过：已 done 的 "
                "CPU 事件后新 demo 建了新事件；把 dedup 记录时间改到 11 分钟"
                "前后再 demo 也建了新事件。harness 内只跑了主分支（待确认"
                "事件未结束时合并）。")
    lines.append("")
    failed = [r for r in results
              if (r.get("error") or (r.get("top1_hit") is False)
                  or (r["id"] == 14 and not r.get("dedup_ok"))
                  or (r["id"] == 15 and not r.get("unknown_ok", True)))]
    if failed:
        lines.append("## 未通过的场景及原因")
        lines.append("")
        for r in failed:
            if r.get("error"):
                lines.append(f"- 场景 {r['id']} {r['name']}：运行报错：{r['error']}")
            elif r["id"] == 14:
                lines.append(f"- 场景 14 重复告警风暴：新建事件数 {r.get('events_created')}，应为 1；"
                             f"合并记录：{r.get('merged_into')}")
            elif r["id"] == 15:
                lines.append(f"- 场景 15 未知型故障：未知核查未通过：{r.get('unknown_checks')}")
            else:
                lines.append(f"- 场景 {r['id']} {r['name']}：Top 1 为 {r['top1_cause']} "
                             f"（{r['top1_confidence']:.2f}），标准根因为 {r['expected_cause']}；"
                             f"Top 3：{r['top3']}；证据 {r['evidence_count']} 条")
    else:
        lines.append("## 未通过的场景及原因")
        lines.append("")
        lines.append("无：14 个诊断场景 Top 1 全部命中，去重场景通过。")
    lines.append("")
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="run_eval")
    ap.add_argument("--only", type=int, nargs="*", default=None,
                    help="只跑指定场景 id（调试用），默认全部 15 个")
    args = ap.parse_args(argv)
    started_at = time.strftime("%Y-%m-%d %H:%M:%S")
    scenarios = json.loads(
        (EVAL_DIR / "scenarios.json").read_text(encoding="utf-8"))["scenarios"]
    if args.only:
        scenarios = [s for s in scenarios if s["id"] in set(args.only)]
    cases_dir = tempfile.mkdtemp(prefix="ops-agent-eval-cases-")
    env = dict(os.environ)
    env["OPS_AGENT_CASES_DIR"] = cases_dir
    env["OPS_DIAGNOSIS_ENGINE"] = "rule"
    # 去重记录是全局的：上一次评测若留下未结束的待确认事件，会让
    # 本次同指纹告警误合并；每次评测从干净的去重状态开始。
    dedup_file = ROOT / "data" / "dedup.json"
    if dedup_file.exists():
        dedup_file.unlink()
    results = []
    for sc in scenarios:
        print(f"[{sc['id']}/15] {sc['name']} ...", flush=True)
        if sc["kind"] == "dedup":
            rec = run_storm_scenario(sc, env, cases_dir)
        else:
            rec = run_diagnosis_scenario(sc, env, cases_dir)
        results.append(rec)
        if rec.get("error"):
            print(f"  ERROR: {rec['error']}", flush=True)
        elif sc["id"] == 14:
            print(f"  dedup_ok={rec.get('dedup_ok')} "
                  f"events_created={rec.get('events_created')}", flush=True)
        else:
            print(f"  top1={rec['top1_cause']} {rec['top1_confidence']:.2f} "
                  f"hit={rec['top1_hit']} ev_completeness="
                  f"{rec['evidence_completeness']:.2f} "
                  f"misops={rec['misoperations']} "
                  f"{rec['duration_s']:.2f}s", flush=True)
    # 评测结束清掉注入故障，演示服务回基线。
    run_cli(["inject", "clear"], env)
    diag = [r for r in results if r.get("top1_hit") is not None
            and not r.get("error")]
    top1_hits = sum(1 for r in diag if r["top1_hit"])
    top3_hits = sum(1 for r in diag if r["top3_hit"])
    n = len(diag)
    storm = next((r for r in results if r["id"] == 14), {})
    summary = {
        "diagnosis_scenarios": n,
        "top1_hits": top1_hits,
        "top1_rate": round(top1_hits / n, 4) if n else 0,
        "top3_hits": top3_hits,
        "top3_rate": round(top3_hits / n, 4) if n else 0,
        "mean_evidence_completeness": round(
            sum(r["evidence_completeness"] for r in diag) / n, 4) if n else 0,
        "total_misoperations": sum(r["misoperations"] for r in diag)
        + (storm.get("misoperations") or 0),
        "mean_duration_s": round(
            sum(r["duration_s"] for r in diag if "duration_s" in r) / n, 2)
        if n else 0,
        "dedup_ok": bool(storm.get("dedup_ok")),
        "dedup_events_created": storm.get("events_created"),
    }
    payload = {"generated_at": started_at,
               "isolation": {
                   "cases_dir": cases_dir,
                   "cases_env_var": "OPS_AGENT_CASES_DIR",
                   "real_cases_dir_untouched": "data/cases",
                   "dedup_reset": True,
                   "diagnosis_engine": "rule"},
               "summary": summary,
               "scenarios": results}
    (EVAL_DIR / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (EVAL_DIR / "report.md").write_text(
        render_markdown(results, summary, cases_dir, started_at),
        encoding="utf-8")
    print(f"\nsummary: Top1 {top1_hits}/{n}, Top3 {top3_hits}/{n}, "
          f"misops={summary['total_misoperations']}, "
          f"dedup_ok={summary['dedup_ok']}")
    print(f"report: eval/report.md, eval/report.json "
          f"(temp cases dir: {cases_dir})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
