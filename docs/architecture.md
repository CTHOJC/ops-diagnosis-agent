# 智能运维诊断 Agent — 架构图（冲刺第 13–14 天，2026-11-20）

文字版架构图：没有画图文件，ASCII 图就是架构图本身，改代码时先改这里。
运行链路：CLI → 8 节点图 → 工具 → 数据/模型。图本身目前直接调 `app.tools`，
还没改走 MCP 客户端（第 3 天的 MCP Server 是独立验证用的，见下）。

## 五层架构

```
┌─────────────────────────────────────────────────────────────────┐
│ 接入层  CLI / 评测入口                                           │
│  app/main.py（demo / approve / reject / modify / expire /       │
│  status / report / trace / diagnosis / inject）                  │
│  eval/run_eval.py（15 场景评测 harness，子进程调 CLI）            │
│  mcp_server/client.py（MCP smoke / list / call，独立验证脚本）    │
└──────────────────────────────┬──────────────────────────────────┘
                               │ 传 AgentState（内存对象）
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│ 编排层  8 节点图（app/graph.py + app/nodes.py）                    │
│  classify → collect_evidence → diagnose → make_plan →           │
│  human_approval → execute → verify → report                     │
│  状态机：AgentState（app/state.py），每次节点边界落盘              │
└──────────────────────────────┬──────────────────────────────────┘
                               │ 节点调工具（函数调用，不是 HTTP）
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│ 工具层  MCP Server + 8 个工具                                     │
│  app/tools.py：4 只读自动（search_logs / query_metrics /         │
│  search_runbook / get_service_status）+ 4 高危须确认               │
│  （restart_service / scale_service / rollback_release /          │
│   update_config，改配置先留快照）                                 │
│  mcp_server/server.py：JSON-RPC 2.0 over stdio，只暴露 3 个只读   │
│  工具（search_logs / query_metrics / search_runbook），高危工具   │
│  在这个 Server 上根本不存在（调了返回 unknown tool）              │
└──────────────────────────────┬──────────────────────────────────┘
                               │ 读演示服务 / 索引 / 案例库
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│ 数据层                                                          │
│  demo_service/：演示服务状态（state.json）+ ES 形状日志落盘        │
│    （es-fallback.jsonl；ES 可达时写 ops-logs 索引）               │
│  data/incidents/<id>/state.json：JSON 文件检查点（审计轨迹）      │
│  app/runbook_chunks.jsonl：7 篇手册的 35 chunk 本地索引           │
│  data/cases/<id>.json：已归档案例（done + 已知根因才归档）         │
│  data/dedup.json：10 分钟去重（fingerprint + 时间戳，文件版）     │
└──────────────────────────────┬──────────────────────────────────┘
                               │ 诊断时读
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│ 模型层                                                          │
│  app/diagnosis.py：规则推理（关键词打分，默认）+                    │
│    OpenAICompatibleAdapter（OPS_LLM_API_KEY，未设 key 不调用）    │
│  app/retrieval.py：本地 BM25 + TF-IDF + RRF + 本地词法 Rerank     │
│    （cross-encoder 留接口，本机未装 sentence-transformers）       │
└─────────────────────────────────────────────────────────────────┘
```

### 接入层 — 为什么存在，换掉影响什么

CLI 是人唯一的操作面：demo 跑图、approve / reject / modify 回传确认结果、
expire 关超时事件、status / trace / report 查结果。评测 harness 不走内部函数，
而是子进程调 CLI，所以评测测的是用户真正会跑的那条路。换掉 CLI（例如改成
FastAPI）时，图的节点、工具、检查点都不用动，只要新入口同样传 AgentState、
同样在 human_approval 处停下；但评测 harness 要跟着改入口，否则评测数字失效。

### 编排层 — 为什么存在，换掉影响什么

8 节点图是唯一的状态机：每个节点只做一步（分类、取证、诊断、定方案、等确认、
执行、验证、出报告），条件边写死在 `app/graph.py`（低置信回取证最多 2 轮、
verify 未恢复回诊断最多 1 轮、高危必经人工确认）。`build_graph()` 有 langgraph
就用真 StateGraph，否则用同一套节点和边的顺序执行器——换编排框架只换
`build_graph()`，节点函数不用重写。状态落盘在每次节点边界，所以进程退出后
`approve` 能从 human_approval 处续跑；如果检查点格式变了，旧事件的
`state.json` 读不出来，跨进程恢复就断了（新增字段必须带默认值，见第 7–9 天）。

### 工具层 — 为什么存在，换掉影响什么

工具是唯一能碰外部世界（演示服务、日志、指标、手册）的地方，节点本身不直接
读文件：这样取证并行、超时降级、审计、高危确认都能集中在一处管。风险分级是
安全边界——4 个只读工具自动执行，4 个高危工具没有批准记录直接拒绝；MCP
Server 只暴露 3 个只读工具，高危操作在 MCP 通路上根本不存在。换掉工具实现
（例如 search_logs 改读真 ES）时，只要返回形状不变（`{chunk_id, source, text}`
这类），节点、诊断、报告都不用动；但高危工具一旦绕过 `_require_approval`，
误操作口径（评测里目标 0 的那项）就没了。

### 数据层 — 为什么存在，换掉影响什么

四类数据各管一摊：演示服务是“被诊断的系统”的替身（故障注入、指标、日志都从
这里来）；JSON 检查点是审计轨迹（每个事件从分类到报告全程可回放，删了就没法
复盘）；手册索引是诊断引用的出处（每条 runbook 证据都有 chunk_id 可查）；
案例库是“上次怎么修好的”记忆。换掉其中任何一个都要改对应的读取点：真 ES
接进来要改 `es_writer` 和 `search_logs` 的读路径；PostgreSQL / Redis 接进来
要替换检查点和 `dedup.json`（现在是文件版，没有 Redis 的过期与并发保证，
多进程同时 demo 同一告警会有竞态）。

### 模型层 — 为什么存在，换掉影响什么

诊断与检索是唯二“会猜”的地方，所以被单独隔成一层：规则推理先把闭环跑通，
LLM 适配器只负责把同样的输入（证据 + 告警）换成模型调用，输出走同一套
`validate_diagnosis` 校验——引用了没收集过的证据、置信度越界、分类不一致
就拒绝并回退规则。检索同理：BM25 / TF-IDF / RRF / Rerank 四段可以逐段换成
真向量（Milvus）或 cross-encoder，只要 `search()` 返回的形状不变，`search_runbook`
的调用方不用动。换掉模型层影响的是诊断质量（Top 1 / 引用准确率），不影响
图的流转、确认与执行——这也是评测能把“诊断命中”与“误操作”分开算的原因。

## 8 节点流转图

```
                       ┌──────────────────────┐
                       │ classify（分类）      │
                       │ 关键词规则读告警本身  │
                       └──────────┬───────────┘
                                  │ category / severity
                                  ▼
                       ┌──────────────────────┐
                       │ collect_evidence     │
                       │ 4 只读工具并行取证， │
                       │ 证据 ≤10 条，超时记缺口│
                       └──────────┬───────────┘
                                  │ evidence[]
                    ┌─────────────┘  Top 1 置信度 < 0.5
                    │  回边①：低置信回取证（最多 2 轮）
                    ▼
                       ┌──────────────────────┐
                       │ diagnose（诊断）      │
                       │ diagnosis/v1 结构化  │
                       │ 输出，校验不过即抛错  │
                       └──────────┬───────────┘
                                  │ Diagnosis（causes / actions）
                                  ▼
                       ┌──────────────────────┐
                       │ make_plan（定方案）   │
                       │ 正向步骤 + 1:1 配对  │
                       │ 回滚步骤，每步标来源  │
                       └──────────┬───────────┘
                                  │ plan[] / risk_level
                                  ▼
              ┌────────────────────────────────────────┐
              │ human_approval（人工确认）◀── 中断点   │
              │ 高危方案停在这里落盘，等 approve /     │
              │ reject / modify；低风险方案直接通过     │
              └───────────────┬────────────────────────┘
                              │ 批准后
                              ▼
                       ┌──────────────────────┐
                       │ execute（执行）       │
                       │ 按 plan 顺序执行，    │
                       │ 回滚步骤跳过；任一步  │
                       │ 失败立即停并自动回滚  │
                       └──────────┬───────────┘
                                  │ exec_result[]
                                  ▼
                       ┌──────────────────────┐
                       │ verify（验证）        │
                       │ error_rate < 0.01 即 │
                       │ 恢复；未恢复回 diagnose│
                       │ 重诊（最多 1 轮）     │
                       └──────────┬───────────┘
                    ┌─────────────┘  回边②：未恢复回 diagnose
                    │  （重建 plan 时跳过已删 / 已回滚的步骤）
                    ▼
                       ┌──────────────────────┐
                       │ report（报告）        │
                       │ Markdown 复盘报告 +  │
                       │ 案例归档（符合条件时）│
                       └──────────────────────┘
                                  │
                                  ▼
                              status=done
```

回边只有两条，没有别的环：回边①（diagnose → collect_evidence）是“证据不够再
取一轮”，回边②（verify → diagnose）是“没恢复就重诊一次”。两条回边都有次数
上限，超了就按原流程收尾出报告，不会死循环。唯一的中断点是 human_approval：
进程在这里退出后，`data/incidents/<id>/state.json` 还在，另一个进程
`approve` / `reject` / `modify` 能接着跑；其余节点之间不停，`demo` 一口气跑完
靠的就是 `--auto-approve` 事先把批准写进 state。

## 降级点（出问题时走哪条路）

| 降级点 | 正常路径 | 降级路径 | 在哪里看出来 |
| --- | --- | --- | --- |
| ES 不可达 | 日志写 `ops-logs` 索引 | 写本地 `demo_service/es-fallback.jsonl`，`search_logs` 从同一处读回 | `inject` 输出 `logs via file`；`demo_service/state.json` 的 `logs_via` |
| Milvus 未装 / 不可达 | 向量检索走 Milvus ANN | 本地 TF-IDF 余弦（词法替身，不是神经 embedding） | 检索 info / 工具审计里的 `milvus=unavailable` |
| cross-encoder 未装 | Rerank 走 `BAAI/bge-reranker-base` | 本地词法重排（BM25 重打分 + 整词重叠），结果里注明 `reranker=local-lexical` | 工具审计摘要，如 `3 runbook chunks (reranker=local-lexical; es=unreachable; milvus=unavailable)` |
| LLM 无 key | 诊断走 LLM | 规则推理，`source=rules`，`diagnosis_engine_failures` 不记失败（auto 下无 key 是预期行为，不尝试调用） | `diagnosis` 命令输出的 `source` 字段；`state.diagnosis_engine_failures` |
| LLM 调用失败 | 诊断走 LLM | 大声回退规则推理，失败记进 `state.diagnosis_engine_failures`，报告里单列 | 报告“结构化诊断输出”一节的 LLM 失败记录 |
| 工具超时（>10 秒） | 取证拿到全部 4 路证据 | 超时那路记进 `state.evidence_gaps`，诊断用已拿到的证据继续，报告单列“取证缺口” | 报告“取证缺口”一节；`evidence_gaps` |
| 手册/案例索引不存在 | `search_runbook` 走 35-chunk 索引 + 案例库 | 回落内置 `FALLBACK_RUNBOOK` | 检索 info |
| 报告渲染失败 | 正常 Markdown 报告 | 最小报告（Trace + 原始状态摘要，注明“完整报告生成失败，见 Trace”），状态仍按原流程置 done/closed | 保存的报告文件；trace 里记失败原因 |
| 执行中某步失败 | 按 plan 顺序执行到底 | 立即停，只回滚已成功的高危步骤（配对回滚，逆序），回滚失败记下来继续下一个，随后仍走 verify | 报告“回滚与恢复验证”一节；`rollback_triggered=true` |
| 人工长时不确认 | 等 approve / reject / modify | `expire`（默认 24 小时，手动触发）关闭事件、走 report 节点出报告，不执行任何处置 | `expire` 输出 `closed N`；报告“关闭原因”行 |

## 本地运行与线上部署的差别（先说清楚）

- 这里没有 FastAPI、没有真 ES / Prometheus / Milvus / PostgreSQL / Redis：
  检查点是 JSON 文件，去重是 `data/dedup.json` 文件版，日志是本地 fallback
  文件，指标是演示服务内存里的数。换成线上组件时，改的是工具层与数据层，
  编排层与模型层的接口不用动（见上）。
- 图目前直接调 `app.tools`，没走 MCP 客户端：MCP Server 是独立验证用的，
  不是运行时的通路。改走 MCP 时，高危工具仍不能进 Server——确认与执行必须
  经过图的 human_approval。
- 诊断目前是规则推理：`OPS_DIAGNOSIS_ENGINE=llm` 且 `OPS_LLM_API_KEY` 已设时
  才调真 LLM；没有 key 时 `auto` 直接走规则，不尝试调用，不产生费用。
