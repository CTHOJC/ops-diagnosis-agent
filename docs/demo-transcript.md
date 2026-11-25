# 演示实录 — demo.sh（冲刺第 13–14 天）

录制时间：2026-11-20 15:40（本机时间）。录制方式：`bash demo.sh > /tmp/demo-day13-run.log 2>&1`，
下面每步的命令与输出都是那次运行的原样照录，没有手写、没有改动数字；
唯一例外是步骤 1 的报告只贴了前 30 行（原文有标注），其余步骤输出完整。
演示脚本见 `demo.sh`；运行环境：Python 3.12.3、numpy 1.26.4、规则推理（`OPS_DIAGNOSIS_ENGINE=rule`），
不调真实 LLM、不碰真后端。

## 开场

```text
===== 智能运维诊断 Agent 演示（demo.sh）=====
环境：本机演示数据 + 规则推理，不调真实 LLM、不碰真后端。
每步之间先 inject clear，演示服务回基线。
```

## 步骤 1：502 中断 → approve 恢复出报告

解说：502 告警在人工确认处停下，批准后重启上游、错误率归零、报告生成——主链路第一次就跑通。

```text
----- 步骤 1：502 中断 → approve 恢复出报告 -----
502 告警没有注入故障，它的证据来自 nginx-502 预设的上游 down 状态；
方案含高危重启，所以图在 human_approval 处停下，等人确认。
$ python3 -m app.main inject clear
fault cleared; web-api back to baseline (logs via file)
state: demo_service/state.json (fault=None)
$ python3 -m app.main demo --alert nginx-502
incident INC-20261120-153959-816: Nginx 返回 502，错误率上升
nodes: classify -> collect_evidence -> diagnose -> make_plan -> human_approval
  classify: 0.01ms (status=running)
  collect_evidence: 140.95ms (status=running)
  diagnose: 0.33ms (status=running)
  make_plan: 0.06ms (status=running)
  human_approval: 0.0ms (status=awaiting_approval)

STOPPED at human_approval (risk=high). State persisted to data/incidents/INC-20261120-153959-816/
Plan: 4 steps, 1 high-risk, 1 rollback steps, 1 paired
Resume: python3 -m app.main approve INC-20261120-153959-816 --by <you> --reason <why>
Reject: python3 -m app.main reject INC-20261120-153959-816 --by <you> --reason <why>
Modify: python3 -m app.main status INC-20261120-153959-816 --list-steps, then modify INC-20261120-153959-816 --by <you> --reason <why> --drop-step <n>[,<n>...]
事件 INC-20261120-153959-816 已停在 human_approval，现在批准它恢复执行：
$ python3 -m app.main approve INC-20261120-153959-816 --by demo --reason 演示：确认重启上游实例
nodes: classify -> collect_evidence -> diagnose -> make_plan -> human_approval -> human_approval -> execute -> verify -> report
  classify: 0.01ms (status=running)
  collect_evidence: 140.95ms (status=running)
  diagnose: 0.33ms (status=running)
  make_plan: 0.06ms (status=running)
  human_approval: 0.0ms (status=awaiting_approval)
  human_approval: 0.0ms (status=approved)
  execute: 0.17ms (status=executing)
  verify: 0.13ms (status=verifying)
  report: 1.63ms (status=done)

status=done. Report: /home/hatch/workspace/goals/ai/files/ops-agent/data/incidents/INC-20261120-153959-816/report.md
$ python3 -m app.main report INC-20261120-153959-816
# 运维诊断复盘报告 — INC-20261120-153959-816

- 告警：Nginx 返回 502，错误率上升（web-api，P1）
- 分类：upstream_down / P1
- 状态：done
- 生成时间：2026-11-20 15:40:00

## 根因假设

- 上游实例宕机（置信度 1.00，证据：log:2026-11-16T06:54:40:dee67ddf, log:2026-11-16T06:55:01:56e01f88, log:2026-11-16T06:55:02:8eba6795, runbook:0765931b250c3cd2, runbook:d1e6b9238c8d7e79）
- 内存 OOM 被杀（置信度 0.33，证据：runbook:4841bff45bfc4941）
- CPU 打满（置信度 0.00，证据：无）
- 磁盘写满（置信度 0.00，证据：无）
- 数据库慢查询（置信度 0.00，证据：无）
- 发布后错误率上升（置信度 0.00，证据：无）
- 下游超时雪崩（置信度 0.00，证据：无）
- 连接池耗尽（置信度 0.00，证据：无）
- 配置变更引发异常（置信度 0.00，证据：无）
- 证书过期（置信度 0.00，证据：无）
- 消息队列积压（置信度 0.00，证据：无）
- 缓存击穿（置信度 0.00，证据：无）
- 单实例网络抖动（置信度 0.00，证据：无）

## 结构化诊断输出（Day 5，diagnosis/v1）

- 来源：rules（规则推理，未接 LLM）
- 需人工确认：是
- 已执行检查：
  - 查日志：3 条命中
（注：上面报告只贴了前 30 行，脚本里是 `report … | head -30`；完整报告在 data/incidents/INC-20261120-153959-816/report.md。）
```

## 步骤 2：CPU 一键跑通

解说：注入 CPU 打满后一口气跑完，根因 CPU 打满置信度 1.00，重启后恢复出报告。

```text
----- 步骤 2：CPU 一键跑通 -----
先注入 CPU 打满故障，再一口气跑完（--auto-approve）：诊断、确认、
重启、验证、报告全在一次命令里。
$ python3 -m app.main inject clear
fault cleared; web-api back to baseline (logs via file)
state: demo_service/state.json (fault=None)
$ python3 -m app.main inject cpu
injected cpu (CPU 打满): 3 log docs via file, metrics cpu=98% mem=61%
state: demo_service/state.json (fault=cpu)
$ python3 -m app.main demo --alert cpu --auto-approve
incident INC-20261120-154000-742: CPU 打满，接口延迟上升
nodes: classify -> collect_evidence -> diagnose -> make_plan -> human_approval -> execute -> verify -> report
  classify: 0.02ms (status=running)
  collect_evidence: 135.75ms (status=running)
  diagnose: 0.34ms (status=running)
  make_plan: 0.05ms (status=running)
  human_approval: 0.0ms (status=running)
  execute: 0.41ms (status=executing)
  verify: 0.18ms (status=verifying)
  report: 1.34ms (status=done)

DONE. Report: /home/hatch/workspace/goals/ai/files/ops-agent/data/incidents/INC-20261120-154000-742/report.md
```

## 步骤 3：unknown 转人工

解说：无匹配信号的症状判 unknown、Top 1 置信度 0.00，只给低风险转人工处置，没动任何高危工具。

```text
----- 步骤 3：unknown 转人工 -----
页面字体突然变大：没有匹配的日志、手册与指标，诊断应判 unknown、
只给低风险转人工处置，不执行任何高危工具。
$ python3 -m app.main inject clear
fault cleared; web-api back to baseline (logs via file)
state: demo_service/state.json (fault=None)
$ python3 -m app.main demo --alert unknown --auto-approve
incident INC-20261120-154001-249: 页面样式异常，字体突然变大
nodes: classify -> collect_evidence -> diagnose -> collect_evidence -> diagnose -> make_plan -> execute -> verify -> report
  classify: 0.04ms (status=running)
  collect_evidence: 130.29ms (status=running)
  diagnose: 0.24ms (status=running)
  collect_evidence: 17.93ms (status=running)
  diagnose: 0.15ms (status=running)
  make_plan: 0.05ms (status=running)
  execute: 0.05ms (status=executing)
  verify: 0.28ms (status=verifying)
  report: 1.61ms (status=done)

DONE. Report: /home/hatch/workspace/goals/ai/files/ops-agent/data/incidents/INC-20261120-154001-249/report.md
```

## 步骤 4：故意失败触发回滚

解说：第 4 步被钩子故意打败，执行立即停止，配对回滚步骤自动把版本滚回 v1.4.1，verify 复查已恢复。

```text
----- 步骤 4：故意失败触发回滚 -----
同样的 502 事件，approve 时设 OPS_AGENT_FAIL_STEP=4：第 4 步（复查）
失败 → 立即停止 → 配对的回滚步骤自动执行 → verify 复查 → 出报告。
这是测试钩子（默认关闭），生产不用。
$ python3 -m app.main inject clear
fault cleared; web-api back to baseline (logs via file)
state: demo_service/state.json (fault=None)
$ python3 -m app.main demo --alert nginx-502
incident INC-20261120-154001-749: Nginx 返回 502，错误率上升
nodes: classify -> collect_evidence -> diagnose -> make_plan -> human_approval
  classify: 0.02ms (status=running)
  collect_evidence: 138.54ms (status=running)
  diagnose: 0.41ms (status=running)
  make_plan: 0.06ms (status=running)
  human_approval: 0.0ms (status=awaiting_approval)

STOPPED at human_approval (risk=high). State persisted to data/incidents/INC-20261120-154001-749/
Plan: 4 steps, 1 high-risk, 1 rollback steps, 1 paired
Resume: python3 -m app.main approve INC-20261120-154001-749 --by <you> --reason <why>
Reject: python3 -m app.main reject INC-20261120-154001-749 --by <you> --reason <why>
Modify: python3 -m app.main status INC-20261120-154001-749 --list-steps, then modify INC-20261120-154001-749 --by <you> --reason <why> --drop-step <n>[,<n>...]
事件 INC-20261120-154001-749 已停在 human_approval，现在带失败钩子批准它：
$ OPS_AGENT_FAIL_STEP=4 python3 -m app.main approve INC-20261120-154001-749 --by demo --reason "演示：确认重启（带失败注入）"
nodes: classify -> collect_evidence -> diagnose -> make_plan -> human_approval -> human_approval -> execute -> verify -> report
  classify: 0.02ms (status=running)
  collect_evidence: 138.54ms (status=running)
  diagnose: 0.41ms (status=running)
  make_plan: 0.06ms (status=running)
  human_approval: 0.0ms (status=awaiting_approval)
  human_approval: 0.0ms (status=approved)
  execute: 0.26ms (status=executing)
  verify: 0.23ms (status=verifying)
  report: 2.01ms (status=done)

status=done. Report: /home/hatch/workspace/goals/ai/files/ops-agent/data/incidents/INC-20261120-154001-749/report.md
回滚证据（报告里的“回滚与恢复验证”一节）：
### 回滚与恢复验证（第 9 天，失败路径）

- 说明：人工确认时批的是整份 plan（含配对的回滚步骤），失败触发的回滚是已授权计划的一部分，自动执行，不再二次确认；原计划批准人：demo，理由：演示：确认重启（带失败注入）；回滚工具在注册表里仍是高危工具。
- 失败步骤：复查错误率与健康检查 — injected failure at plan step 4 (复查错误率与健康检查) via OPS_AGENT_FAIL_STEP=4 (test hook, not a real tool error)
- 已执行的回滚步骤：回滚：回滚到上一版本 — 成功：{'ok': True, 'service': 'web-api', 'version': 'v1.4.1'}
- 回滚后验证：已恢复（rolled back, error_rate_5m=0.0）
- 已回滚的正向步骤（重诊后不再重新执行）：['重启上游实例（演示环境）']

人工确认：demo，通过，理由：演示：确认重启（带失败注入）
```

## 步骤 5：trace 回放

解说：回放第 4 步那个走过回滚的事件：9 个节点、7 次工具调用、回滚决策全在 trace 里。

```text
----- 步骤 5：trace 回放 -----
回放第 4 步那个走过回滚的事件：时间线、工具调用、关键决策点一处看全。
$ python3 -m app.main trace INC-20261120-154001-749
# 诊断 Trace 回放 — INC-20261120-154001-749

- 告警：Nginx 返回 502，错误率上升（web-api，P1）
- 分类：upstream_down / P1
- 状态：done
- 创建时间：2026-11-20 15:40:01
- 节点：9 个，工具调用：7 次

## 时间线（按节点顺序）

1. classify（0.02ms）— status=running
   分类结果：upstream_down（P1）
2. collect_evidence（138.54ms）— status=running
   证据 9 条：日志 3 / 指标 1 / 状态 1 / 手册 4
3. diagnose（0.41ms）— status=running
   诊断 Top 1：上游实例宕机（置信度 1.00），来源：rules
4. make_plan（0.06ms）— status=running
   方案 4 步：1 高危 / 1 回滚 / 1 已配对，风险等级 high
5. human_approval（0.0ms）— status=awaiting_approval
   人工确认通过：demo，理由：演示：确认重启（带失败注入）
6. human_approval（0.0ms）— status=approved
   人工确认通过：demo，理由：演示：确认重启（带失败注入）
7. execute（0.26ms）— status=executing
   执行 4 条记录：成功 3 / 失败 1；失败触发回滚，已回滚：['重启上游实例（演示环境）']
8. verify（0.23ms）— status=verifying
   验证已恢复（rolled back, error_rate_5m=0.0）
9. report（2.01ms）— status=done
   报告：/home/hatch/workspace/goals/ai/files/ops-agent/data/incidents/INC-20261120-154001-749/report.md

## 工具调用（按调用顺序）

1. search_logs（0.2ms，15:40:01）— 3 log lines；参数：{"service": "web-api", "keywords": ["upstream", "502", "connect"]}
2. query_metrics（0.06ms，15:40:01）— error_rate_5m=0.420；参数：{"service": "web-api"}
3. get_service_status（0.12ms，15:40:01）— upstream=down；参数：{"service": "web-api"}
4. search_runbook（137.09ms，15:40:01）— 3 runbook chunks + 1 历史案例 (reranker=local-lexical; es=unreachable; milvus=unavailable)；参数：{"query": "用户访问返回 502 Bad Gateway，Nginx 错误日志出现 connect() failed (111: Connection refused) 或 upstream timed out，监控显示上游…
5. restart_service（0.14ms，15:40:02）— upstream restarted (demo)；参数：{"service": "web-api"}
6. rollback_release（0.04ms，15:40:02）— rolled back to v1.4.1 (demo)；参数：{"service": "web-api"}
7. query_metrics（0.19ms，15:40:02）— error_rate_5m=0.000；参数：{"service": "web-api"}

## 关键决策点

- 分类：upstream_down（P1），依据告警“Nginx 返回 502，错误率上升”
- 诊断：Top 1 上游实例宕机（置信度 1.00）（来源：rules）；证据：log:2026-11-16T06:54:40:dee67ddf, log:2026-11-16T06:55:01:56e01f88, log:2026-11-16T06:55:02:8eba6795, runbook:0765931b250c3cd2, runbook:d1e6b9238c8d7e79
- 方案与风险：4 步，风险等级 high；高危 1 个，回滚 1 个，已配对 1 对
- 人工确认：demo 通过（理由：演示：确认重启（带失败注入））
- 回滚：已触发（失败后自动回滚），已回滚步骤：['重启上游实例（演示环境）']
- 验证：已恢复（rolled back, error_rate_5m=0.0）
- 收尾：状态 done；报告：/home/hatch/workspace/goals/ai/files/ops-agent/data/incidents/INC-20261120-154001-749/report.md
```

## 步骤 6：评测 harness

解说：15 场景一次跑完：Top 1 14/14、Top 3 14/14、误操作 0、去重通过。

```text
----- 步骤 6：评测 harness（15 场景）-----
子进程调 CLI 跑全部 15 个场景：Top 1 / Top 3、证据完整率、误操作、
耗时、去重；报告写 eval/report.md 与 eval/report.json。
$ python3 eval/run_eval.py
[1/15] CPU 打满 ...
  top1=CPU 打满 1.00 hit=True ev_completeness=1.00 misops=0 0.31s
[2/15] 内存 OOM 被杀 ...
  top1=内存 OOM 被杀 1.00 hit=True ev_completeness=1.00 misops=0 0.36s
[3/15] 接口大面积 502 ...
  top1=上游实例宕机 1.00 hit=True ev_completeness=1.00 misops=0 0.30s
[4/15] 数据库慢查询 ...
  top1=数据库慢查询 1.00 hit=True ev_completeness=1.00 misops=0 0.31s
[5/15] 磁盘写满 ...
  top1=磁盘写满 1.00 hit=True ev_completeness=1.00 misops=0 0.31s
[6/15] 发布后错误率上升 ...
  top1=发布后错误率上升 1.00 hit=True ev_completeness=1.00 misops=0 0.30s
[7/15] 下游超时雪崩 ...
  top1=下游超时雪崩 1.00 hit=True ev_completeness=1.00 misops=0 0.31s
[8/15] 连接池耗尽 ...
  top1=连接池耗尽 1.00 hit=True ev_completeness=1.00 misops=0 0.31s
[9/15] 配置变更引发异常 ...
  top1=配置变更引发异常 1.00 hit=True ev_completeness=1.00 misops=0 0.30s
[10/15] 证书过期 ...
  top1=证书过期 1.00 hit=True ev_completeness=1.00 misops=0 0.31s
[11/15] 消息队列积压 ...
  top1=消息队列积压 1.00 hit=True ev_completeness=1.00 misops=0 0.31s
[12/15] 缓存击穿 ...
  top1=缓存击穿 1.00 hit=True ev_completeness=1.00 misops=0 0.30s
[13/15] 单实例网络抖动 ...
  top1=单实例网络抖动 1.00 hit=True ev_completeness=1.00 misops=0 0.33s
[14/15] 重复告警风暴 ...
  dedup_ok=True events_created=1
[15/15] 未知型故障 ...
  top1=未知型故障，需转人工 0.00 hit=True ev_completeness=1.00 misops=0 0.31s

summary: Top1 14/14, Top3 14/14, misops=0, dedup_ok=True
report: eval/report.md, eval/report.json (temp cases dir: /tmp/ops-agent-eval-cases-840gzxim)

===== 演示结束 =====
$ python3 -m app.main inject clear
fault cleared; web-api back to baseline (logs via file)
state: demo_service/state.json (fault=None)
演示服务已回基线。报告在 data/incidents/<id>/report.md，
评测报告在 eval/report.md，完整实录见 docs/demo-transcript.md。
```
