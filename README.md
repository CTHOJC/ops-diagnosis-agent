# 智能运维诊断 Agent — 冲刺第 1–14 天（2026-11-16 至 2026-11-20）

按 `../ops-agent-plan/ops-agent-plan.pdf` 第 11 章交付。
用户 2026-11-16 确认“按照方案先执行”，方案本身不改，骨架提前一周搭起来；
第 2–12 天同日/次日跟进完成，第 13–14 天做联调、演示与收尾（见文末）。

## 快速开始（第一次拿到这个项目，5 分钟跑通主链路）

```bash
cd ~/workspace/goals/ai/files/ops-agent
python3 --version            # 需要 Python 3.12+（本机 3.12.3）
python3 -c "import numpy; print(numpy.__version__)"   # 需要 numpy 1.26.4（已装）
python3 -m app.main inject cpu                       # 注入一例 CPU 打满
python3 -m app.main demo --alert cpu --auto-approve  # 一口气跑完：诊断 → 确认 → 执行 → 验证 → 报告
python3 -m app.main inject clear                     # 演示服务回基线
bash demo.sh                                         # 完整演示：502 中断/恢复、CPU、unknown、回滚、trace、评测
python3 eval/run_eval.py                             # 15 场景评测（约 1 分钟，见 eval/report.md）
```

不需要 Docker、真 ES / Milvus、LLM key——上面每条命令都是本机标准库 + numpy
跑通的。跑不通时先看文末“现状对照表”：哪些是本机实测过的、哪些要等你在
自己机器上接。架构见 [docs/architecture.md](docs/architecture.md)，
演示实录见 [docs/demo-transcript.md](docs/demo-transcript.md)。

## 今天交付了什么

- `app/state.py` — AgentState，字段与方案第 4 章的状态表一致（incident/alert、category/severity、evidence[]、hypotheses[]、plan[]/risk_level、approval/exec_result、retry_count/status），可序列化进 JSON 检查点。
- `app/graph.py` — 8 节点图 + 条件边（diagnose 低置信回 collect 最多 2 轮；高风险进 human_approval；verify 未恢复回 diagnose 最多 1 轮）。`build_graph()` 有 langgraph 就用真 StateGraph，否则用同一套节点和边的顺序执行器（和 RAG 项目 generate.py 的做法一样）。
- `app/nodes.py` — 8 个节点：classify / collect_evidence / diagnose / make_plan / human_approval / execute / verify / report。
- `app/tools.py` — 8 个工具，按方案第 5 章分级：4 个只读自动执行（search_logs、query_metrics、search_runbook、get_service_status），4 个高危必须人工确认（restart_service、scale_service、rollback_release、update_config，改配置先留快照）。高危工具无确认记录直接拒绝。
- `app/store.py` — JSON 文件检查点：每个节点边界都落盘，`data/incidents/<id>/state.json`；进程重启后能从 human_approval 处恢复。
- `app/report.py` — Markdown 复盘报告（时间线、根因、证据、操作、Trace、工具审计、成本）。
- `app/main.py` — CLI：demo / approve / reject / status / report。
- `demo_alerts/nginx-502.json` — 预置告警（Nginx 502，上游实例宕机）。

## 跑法（全部 CLI 命令，第 13–14 天联调时逐条跑过）

```bash
cd ~/workspace/goals/ai/files/ops-agent
python3 -m app.main demo            # 跑到人工确认处停下，状态已落盘
python3 -m app.main approve <INC-...> --by eng --reason "确认重启"   # 恢复执行，出报告
python3 -m app.main demo --auto-approve   # 一口气跑完（演示用）
python3 -m app.main demo --alert cpu|oom|unknown|disk-full|slow-query|release|downstream|pool|config|cert|mq|cache|jitter [--auto-approve]
python3 -m app.main inject cpu|oom|disk|slowquery|release|downstream|pool|config|cert|mq|cache|jitter|clear
python3 -m app.main status <INC-...> [--list-steps]   # 看状态、方案步骤、节点 Trace、工具审计
python3 -m app.main report <INC-...>      # 打印复盘报告（无已保存报告时现场重生成）
python3 -m app.main trace <INC-...>       # 回放一次诊断全过程（任何状态可用）
python3 -m app.main diagnosis <INC-...>   # 打印结构化诊断 diagnosis/v1 的 JSON
python3 -m app.main reject <INC-...> --by eng --reason "先别动"   # 驳回，不执行
python3 -m app.main modify <INC-...> --by eng --reason <理由> --drop-step <n>[,<n>...]  # 删正向步骤后恢复执行
python3 -m app.main expire [--hours N]    # 关闭超过 N 小时（默认 24）未确认的事件
python3 -m app.diagnosis                  # 结构化诊断自测 20/20
python3 -m mcp_server.client smoke        # MCP 一键验证 7/7
python3 eval/run_eval.py                  # 15 场景评测（见 eval/report.md）
```

已验证（2026-11-16）：预置 502 告警流经 8 个节点，在 human_approval 处中断落盘；
`approve` 后恢复执行，重启演示上游、复查错误率归零、生成复盘报告。节点顺序与方案第 4 章一致。

## 第 2 天：演示服务 + 故障注入 + ES 日志写入（2026-11-16 同日）

按冲刺表第 2 天交付物：能手动注入 CPU / OOM 两类故障（方案第 9 章评测场景 1–2）。

- `demo_service/service.py` — 演示服务状态（`demo_service/state.json`）+ 故障注入：`cpu`（CPU 98%、P95 2100ms、错误率 0.06）和 `oom`（内存 99%、OutOfMemoryError、实例被杀），`clear` 回基线。
- `demo_service/es_writer.py` — 日志按 ES 文档形状写 `ops-logs` 索引（`OPS_ES_URL`，默认 `http://127.0.0.1:9200`，即 RAG 项目 compose 里的 ES，localhost only）；ES 不可达时原样落到 `demo_service/es-fallback.jsonl`，`search_logs` 从同一处读回，所以注入的故障就是 Agent 实际诊断的证据，不是另一份拷贝。
- Agent 侧接线：`app/tools.py` 的 `search_logs` / `query_metrics` / `get_service_status` 读演示服务状态；`restart_service` 执行后清掉已注入故障，`verify` 才能看到恢复；`app/nodes.py` 给“CPU 打满”和“内存 OOM 被杀”各加了一套处置方案（高危重启 + 回滚预案）。
- 新预置告警：`demo_alerts/cpu-high.json`、`demo_alerts/oom-killed.json`；CLI：`python3 -m app.main inject cpu|oom|clear`（第 11–12 天扩到 12 类故障：另见下文，`inject` 现支持 cpu / oom / disk / slowquery / release / downstream / pool / config / cert / mq / cache / jitter / clear）。

已验证（2026-11-16）：注入 cpu 后跑 `--alert cpu --auto-approve`，根因“CPU 打满”置信度 1.00，重启后错误率归零出报告；注入 oom 后跑 `--alert oom --auto-approve`，根因“内存 OOM 被杀”置信度 1.00，同样恢复出报告；不加 `--auto-approve` 时 CPU 事件仍在 human_approval 处停下；清掉故障后原 502 演示回归通过。日志落盘形状已确认：`es-fallback.jsonl` 里是 `@timestamp/service/level/message/fault` 的 ES 文档。

复核时发现并修了两个问题（主代理独立复核，2026-11-16）：（1）事件编号只精确到秒，同一秒内连跑两次会互相覆盖状态和报告，现已加毫秒 + 已存在时自动加后缀；（2）`search_logs` 原来读回 fallback 文件的全部历史日志，旧故障的日志会混进新诊断的证据，现只读当前注入故障的日志；证据去重也从 (时间+内容) 改为按内容去重，重复注入不再刷屏。修完后 CPU / OOM / 502 三条全量重跑通过，同一秒内三起事件编号互不相同。

## 第 3 天：MCP Server 与三个只读工具（2026-11-16 同日）

按冲刺表第 3 天交付物：三个只读取证工具可独立用脚本调用验证。

- `mcp_server/server.py` — MCP Server（方案第 5 章）：JSON-RPC 2.0 over stdio（换行分隔 JSON，MCP stdio 传输），方法 `initialize` / `tools/list` / `tools/call` / `ping`。本机没装 MCP SDK，协议层是手写的标准库实现。暴露的三个工具是方案第 3 章数据流里的并行取证三件套：`search_logs`、`query_metrics`、`search_runbook`，全是只读。`get_service_status` 这次没进 MCP——它是状态检查不是取证证据，后面再接。
- 方案第 5 章的四条工具规矩都在服务端落实，不靠调用方自觉：参数契约（inputSchema，缺参 / 错类型 / 未知参数直接 -32602 拒绝，不猜参数）、只读 10 秒超时、每工具每会话最多 10 次调用（防循环烧调用）、结果裁剪（工具本身只返 ≤10 条日志 / Top-3 手册块）、审计（每次调用记 `mcp_server/audit.jsonl`：参数、耗时、摘要、成败）。
- **高危工具不在这个 Server 上**：`restart_service` 等四个高危工具调 `tools/call` 返回的是 unknown tool（-32602），不是“拒绝执行”——这条 Server 上根本没有通向改状态操作的路。高危操作仍走图的 human_approval 中断。
- `mcp_server/client.py` — 独立验证脚本：`list` 列工具、`call --tool … --args '{…}'` 单调一个、`smoke` 一键全查（list + 每工具一调 + 缺参必须拒 + 高危必须未知）。

已验证（2026-11-16）：`python3 -m mcp_server.client smoke` 7/7 通过；注入 cpu 后经 MCP 调 `query_metrics` 看到 CPU 98% / 错误率 0.06、`search_logs` 看到刚注入的 CPU 日志；三条图回归（502 / cpu / oom `--auto-approve`）全部 DONE。复核时发现并修了一个小问题：`search_logs` 的去重原来保留同一故障最早一次注入的日志，证据时间戳是旧的（第一次注入的时间），现在只保留最新一次注入的同内容日志，时间戳与刚注入的故障一致。

跑法：

```bash
cd ~/workspace/goals/ai/files/ops-agent
python3 -m mcp_server.client smoke          # 一键验证 7 项
python3 -m mcp_server.client list            # 只应看到 3 个只读工具
python3 -m mcp_server.client call --tool search_logs --args '{"service": "web-api", "keywords": ["upstream", "502"]}'
python3 -m mcp_server.client call --tool query_metrics --args '{"service": "web-api"}'
python3 -m mcp_server.client call --tool search_runbook --args '{"query": "Nginx 返回 502 先检查哪三项"}'
```

## 第 4 天：取证并行化与证据裁剪（2026-11-16 同日）

按冲刺表第 4 天交付物：一次事件产出结构化证据列表（方案第 6 章模块 2：单事件不超过 10 条关键证据，每条带时间戳）。

- `app/nodes.py` 的 `collect_evidence` 改成并行：search_logs / query_metrics / get_service_status / search_runbook 四个只读工具同一轮 `ThreadPoolExecutor` 发出，组装顺序固定为日志 → 指标 → 状态 → 手册，与哪个线程先返回无关。工具只读状态、不改共享状态，审计追加是原子操作。
- **超时降级**（方案第 4 章节点表、第 5 章）：单个工具超过 10 秒（`EVIDENCE_TOOL_TIMEOUT_S`，`app/state.py`）或抛错，不阻塞整轮——记进新字段 `state.evidence_gaps`（如 `metrics: timeout>10s`），诊断用已拿到的证据继续；复盘报告里单独列出“取证缺口”。`shutdown(wait=False)` 保证超时后立即返回，不会被挂住的工具线程拖住。
- **证据裁剪**：指标 1 条 + 状态 1 条 + 手册 Top 3 先占位，日志补剩余空位，总数不超过 `MAX_EVIDENCE_ITEMS = 10`；摘要压成单行、每条 ≤120 字符；按 (source, ref) 去重，第二轮取证不再重复加同一条；手册 chunk 本身没有时间戳，记采集时间 `now`，与指标/状态现有的做法一致。

已验证（2026-11-16，验证脚本为临时脚本，未随项目提交）：4 个工具各睡 0.3 秒时取证 0.30 秒（顺序跑要 1.2 秒，并行生效）；search_runbook 抛错时其余三路照常取证、缺口有记录；query_metrics 睡 1 秒、超时 0.2 秒时 0.20 秒返回、缺口记为超时；20 条日志 + 3 条手册的输入压到正好 10 条（日志 5、指标 1、状态 1、手册 3），单例一个不少；第二轮取证证据数 8 → 8 不增长。三条图回归（502 / cpu / oom `--auto-approve`）全部 DONE，根因置信度仍是 1.00；不加 `--auto-approve` 时仍在人工确认处停下，approve 后恢复出报告；MCP `smoke` 7/7 通过。502 报告里的证据现在是 8 条单行（3 日志 + 1 指标 + 1 状态 + 3 手册），每条带时间戳。

复核时发现并修了一个问题：日志去重的 ref 只用时间戳，一次注入的三条日志同秒，OOM 的“Killed process”那条被去重掉，根因置信度从 1.00 掉到 0.67；现已在 ref 里加上消息摘要，修完重跑三条置信度都回到了 1.00（OOM 证据 7 条、含 2 条日志）。

先说清楚：并行提速的 0.30 秒 vs 1.2 秒是拿人为加延迟的工具测出来的，演示工具本身是瞬间返回的，真实收益要等接真 ES / Prometheus / Milvus 之后才有实测数；超时降级目前也只在模拟故障上验过，没见过真超时。

## 第 5 天：诊断节点与结构化输出（2026-11-19）

按冲刺表第 5 天交付物：diagnose 节点输出机器可校验的结构化诊断（方案第 6 章模块 4、第 9 章评测口径），而不是自由文本。

- `app/diagnosis.py` — `Diagnosis`（diagnosis/v1）+ `DIAGNOSIS_SCHEMA`：category、按置信度降序的 causes（每项 cause、confidence、evidence_refs）、一句话推理摘要、已执行检查 `checks_performed`（从实际取证结果数出来的，不是编的）、建议处置 `recommended_actions`（每项 action、risk，高危必填 rollback）、needs_human、显式未知标志 `is_unknown`、source（`rules` | `llm`）、model。`validate_diagnosis` 每次构造都校验：置信度在 [0, 1] 且降序、引用的 ref 必须真实存在于已收集证据中、置信度 > 0 的根因必须有证据支撑、低置信度 / 未知型必须置 `needs_human`、根因名必须在合同词汇表内、未知型只能提低风险处置且必须转人工、高危处置必须有回滚且必须置 `needs_human`。校验不过直接抛 `DiagnosisError`，不让坏排名流到下游。`python3 -m app.diagnosis` 自测 20/20 通过（含无 key 时强制 LLM 引擎回退规则并记录的用例）。
- 推理引擎可插拔，`diagnose_with_engine()` 是 diagnose() 的入口：`OPS_DIAGNOSIS_ENGINE` 选 `auto`（默认，有 key 才试 LLM）/ `rule` / `llm`；`OpenAICompatibleAdapter`（标准库 urllib，`/chat/completions` + `response_format: json_object`）key 从 `OPS_LLM_API_KEY` 取（`OPS_AGENT_LLM_API_KEY` 作别名），base URL / 模型 / 超时可配；LLM 输出的 JSON 走同一套校验，引用了没收集过的证据、置信度越界、分类与 classify 结果不一致就拒绝。**LLM 任何失败（无 key、超时、HTTP 错、坏 JSON、校验不过）都记进 `state.diagnosis_engine_failures` 并大声回退到规则推理**——输出的 source 字段永远说明是谁产生的，绝不拿规则输出冒充 LLM 回答。没有 key 时 `auto` 直接走规则，不尝试调用。
- 未知型故障（计划第 9 章场景 15）：Top 1 置信度 < 0.5（`DIAGNOSE_CONFIDENCE_THRESHOLD`）时诊断输出 category=unknown、is_unknown=true、低置信度，只给“整理已有证据并转人工处理”这一个低风险处置，绝不编造根因。`demo --alert unknown`（`demo_alerts/unknown-symptom.json`：页面字体突然变大，无匹配日志/手册/指标）就是这条路的回归用例。
- `app/main.py` 新增 `diagnosis <incident_id>`：直接打印该事件的结构化诊断 JSON；复盘报告新增“结构化诊断输出”一节（检查清单、建议处置及风险/回滚、LLM 失败记录）和按调用次数动态的成本行（规则推理仍是模型调用 0 次）。

已验证（2026-11-19）：三条图回归（502 / cpu / oom `--auto-approve`）全部 DONE，根因置信度仍是 1.00，Diagnosis Top 1 与 hypotheses 一致、source=rules；不加 `--auto-approve` 仍在人工确认处停下，approve 后恢复出报告；复核时发现并修了一个校验过严的问题：0 置信度的候选根因本来就没有证据可引，按“必须引证据”会误杀，现在只要求置信度 > 0 的根因引证据。未知型路径（`demo --alert unknown`，页面字体突然变大）已验证：输出 category=unknown、is_unknown=true、Top 1 为“未知型故障，需转人工”（0.25），只给一个低风险转人工处置，未执行任何高危工具；四份诊断都通过 `Diagnosis.from_dict` + `validate_diagnosis` 重校验。LLM 回退链路用本地 stdlib HTTP stub 验过 7/7（有效回复走 llm 引擎；坏 JSON / 置信度 2.0 / 不可达地址 / 无 key 强制 llm 都回退规则并记录；auto 无 key 时直接走规则、不记失败）；`python3 -m app.diagnosis` 自测 20/20；MCP `smoke` 7/7；证据仍 ≤10 条（502：8、cpu：5、oom：6、unknown：2）。

验证时发现并修了一个真问题：`search_runbook` 原来只要 1 个词重叠就返回手册块，unknown 症状靠裸数字“5”命中了 nginx-502 手册，关键词打分于是把字体问题“诊断”成上游宕机、置信度 1.00。现要求至少 2 个词重叠（`MIN_RUNBOOK_OVERLAP`，`app/tools.py`），实测真故障重叠 ≥2（502：17、cpu：3、oom：2），unknown 最高 1；修完重跑三条回归仍是 1.00（cpu 证据 6→5、oom 7→6，各少一个单 token 命中的手册块，Top 1 与处置不变）。

先说清楚：结构化输出的形状先落了地，底下还是规则推理——简历上仍写“规则诊断、未接 LLM”，等 key 确认、LLM 路径跑通并通过 15 场景评测之后再改口径。另有一处演示数据的毛刺：`_new_incident` 把上游 down 的状态/指标硬编码进了每个事件（502 预设的默认值），与故障无关的事件也会在次要假设里看到“上游实例宕机”0.25；不影响 Top 1 和 unknown 判定，但值得在后续修掉。

## 第 6 天：接 RAG 知识库检索（2026-11-20）

按冲刺表第 6 天交付物：诊断能引用手册与历史案例出处——search_runbook 接真 BM25 + 向量 + Rerank（方案第 11 章）。

- `app/retrieval.py`（新增，标准库 + numpy 1.26.4）——三段式检索：
  - **检索**：BM25（照搬 `rag-week1-starter/bm25_baseline.py` 的算法，k1=1.5、b=0.75，ES BM25 的本地实现）+ 向量路（TF-IDF 余弦，tokenizer 照搬 `hybrid_retrieval.py`，与 Milvus ANN 同形状），RRF k=60 融合。
  - **Rerank**：先尝试 `sentence-transformers` 的 CrossEncoder（`BAAI/bge-reranker-base`），能用就用；本机未装，走本地词法重排（BM25 重打分 + 整词重叠），返回结果里注明本次实际用的 reranker（`local-lexical`），绝不假装用了 cross-encoder。
  - **ES / Milvus 探测**：ES 走 HTTP 试连 `OPS_ES_URL`（默认 `http://127.0.0.1:9200`，查 `rag_chunks` 索引——`index_chunks.py` 定义的那个）；Milvus 试 `import pymilvus`（`rag_chunks` 集合，`http://localhost:19530`）。本机没有 Docker，两者都不可达/未装，优雅降级到本地实现，降级原因记进每次检索的 info 和工具审计（做法与 `demo_service/es_writer.py` 的 fallback 一致）。
  - **语料**：现场读 `rag-week1-starter/corpus/*.md` 的 7 篇手册，按 `chunk.py` 的切块逻辑（按标题切，chunk_id 稳定哈希）生成 ops-agent 自己的索引 `app/runbook_chunks.jsonl`——35 个 chunk（7 篇 × 5 节）。`rag-week1-starter/chunks.jsonl` 一个字节没动（仍是 30 行，RAG Week 1 的评测数字不受影响）。
- `app/tools.py` 的 `search_runbook` 改走 `retrieval.search`，返回形状不变（`{chunk_id, source, text}`，source 为“手册名 / 章节”），top_k=3，`MIN_RUNBOOK_OVERLAP=2` 的底线保留（Day 5 的教训）；索引与语料都不存在时仍回落到内置 `FALLBACK_RUNBOOK`。审计摘要现在写明本次的 reranker 与后端状态（例：`3 runbook chunks (reranker=local-lexical; es=unreachable; milvus=unavailable)`）。
- **毛刺 (2) 修掉**：`app/main.py` 的 `_new_incident` 按告警类型设初值——nginx-502 保持 down 预设（502 演示靠它出证据），cpu / oom / unknown 用健康基线（注入的故障会再覆盖）。配套修了两处，否则只改初值修不干净：`query_metrics` 原来把“upstream == healthy”当成“已恢复”直接把指标清零，健康预设会在取证时就抹掉注入故障的指标（CPU 0.06 → 0.0），现改成只认 `restarted_at`（只有 restart_service 写它）；`collect_evidence` 的状态摘要原来写 `upstream down`，摘要里的 `upstream` 一词会被 diagnose 的关键词计分——健康服务也凭空多一个“上游实例宕机”0.25，现写“上游 …”（中文标签，含义不变，502 / CPU / OOM 的 1.00 本来就来自日志 + 手册，不靠这个标签）。
- **毛刺 (1) 验收**：CPU 事件的 runbook 首条引用现为 `cpu-high / 现象`（之前是 mysql-slow-query）。如实说明：Top 3 里第 2 条仍是 `mysql-slow-query / 现象`——它与 CPU 告警共享 cpu / 接口 / P95 三个整词，是真实的词法近邻，不是 bug；检索器没有为把它挤出去而调参，诊断 Top 1 不受影响。

已验证（2026-11-20，评测脚本为临时脚本 `/tmp/eval_retrieval_day6.py`，未随项目提交）：
- `python3 -m app.diagnosis` 自测 20/20；`python3 -m mcp_server.client smoke` 7/7；不加 `--auto-approve` 的 502 演示仍在 human_approval 处停下。
- 三条图回归全部 DONE，根因置信度 1.00：502（INC-20261120-145636-127，证据 8 条）、cpu（INC-20261120-145637-931，证据 7 条）、oom（INC-20261120-145638-389，证据 6 条）；`diagnosis` 命令打印的结构化诊断 JSON 均通过 `Diagnosis.from_dict` + `validate_diagnosis` 重校验（即 `diagnosis` 子命令本身的输出）。
- 未知故障（INC-20261120-145638-834）：仍判 unknown、Top 1 置信度 0.00（修前是 0.25，全部来自预设污染）、只给一个低风险转人工处置、未执行重启；证据 2 条（指标基线 error_rate 0.001 / P95 180ms + 状态“上游 healthy”），runbook 0 条；次要假设“上游实例宕机”已消失（其余根因均为 0.00）。
- 证据仍 ≤10 条（502：8、cpu：7、oom：6、unknown：2）。
- 检索评测（新检索器，35 chunk，不加 MIN_RUNBOOK_OVERLAP 门限，测纯检索质量）：seed 12 题 Recall@1 9/12 = 0.75、Recall@5 12/12 = 1.00；expansion 11 题 Recall@1 6/11 = 0.55、Recall@5 8/11 = 0.73。eval-13 仍 miss（RRF 第 7 位，期望 chunk 在 top-5 之外一位），与 Week 1 的结论一致——TF-IDF 是词法替身，没有语义匹配能力。Expansion 的 Recall@5 从 Week 1 的 0.91（30 chunk）降到 0.73：不是 rerank 的问题（纯 RRF 在 35 chunk 上也是 0.73），而是新增的 cpu-high.md 占了 top-5 的位置——eval-22、eval-23 的期望 chunk 都在 RRF 第 6 位，只差一位。没有为凑数调参。

先说清楚（诚实说明）：本地 BM25 是真实现；向量路是 TF-IDF 词法替身、不是神经 embedding；Rerank 是本地词法版，cross-encoder 已留接口但本机未装 sentence-transformers；ES / Milvus 均降级到本地（ES 连接被拒、pymilvus 未装），等用户在自己机器上起 Docker 后再跑真后端；演示数据仍是示例语料（7 篇手册 / 35 chunk）。

## 第 7 天：方案生成与风险分级——回滚步骤显式化与来源标注（2026-11-20）

按冲刺表第 7 天交付物：每个高危步骤都有对应的回滚步骤、每步写明操作/预期效果/风险等级/回滚方式、方案引用手册或案例来源（方案第 11 章，对照第 6 章模块 5 的完成标准）。

- `app/state.py` — `PlanStep` 增加三个带默认值的字段：`is_rollback`（是否为回滚步骤，默认 False）、`rollback_of`（它回滚的高危步骤文本，默认 ""）、`source`（步骤来源，默认 ""）。默认值保证 Day 7 之前落盘的 `state.json` 仍能经 `from_dict` 加载（已实测旧事件 INC-20261116-065915 正常加载，新字段全为默认值，round-trip 正常）。
- `app/nodes.py` — 回滚从“文本描述”升级为“显式回滚步骤”：`_PLANS` 仍只存正向步骤（诊断的推荐处置与之同源）；`make_plan()` 在每个 high 步骤后立即追加一个对应的低风险回滚步骤（`is_rollback=True`，`rollback_of` 指向高危步骤文本，`rollback` 字段写明它在回滚什么，1:1 配对）。回滚工具全部真实存在于 `app/tools.py` 的工具表：`restart_service` → `rollback_release`（原回滚文本写的就是“回滚到上一版本”）；`scale_service` → 再次调 `scale_service` 调回原副本数（演示基线 2）；`update_config` → 再次调 `update_config` 写回快照旧值；`rollback_release` 本身已是回滚操作，它的配对步骤是只读的 `get_service_status`“确认回滚后版本并转人工复核”；未列入映射的高危工具 fallback 同样是只读确认 + 转人工，不编造自动回滚。`execute()` 跳过所有回滚步骤（不调用、不记 `exec_result`，只在 plan 里标 `skipped`），正常执行绝不跑回滚。`_actions_for_cause()` 同步：诊断的 `recommended_actions` 只含正向步骤（回滚步骤不进入），高危处置的回滚信息仍保留在 `Action.rollback` 里。
- 方案来源：`make_plan()` 生成步骤时，从 `state.evidence` 里已收集的 runbook 证据取 Top 1 chunk 引用（`runbook:<chunk_id>`，与证据列表同一编号，可追溯）填进每步的 `source`；没有任何手册/案例支撑的步骤，`source` 明确标为“建议人工判断”（未知型方案只有这一步：它收集不到任何 runbook 证据）。回滚步骤继承它所回滚的高危步骤的来源。
- `app/report.py` — 处置方案区拆成两节：正向步骤每步显示来源或“建议人工判断”标记；回滚步骤单独列为“回滚步骤（仅失败时执行）”，每步显示回滚对象、说明与来源；节首显示“高危步骤 N 个，回滚步骤 M 个，已配对 N 对”。`app/main.py` 的 `status` 与不加 `--auto-approve` 的中断提示也显示同样的配对数（`plan_pairing()`，`app/nodes.py`）。

已验证（2026-11-20，断言脚本为临时脚本 `/tmp/verify_day7.py`，未随项目提交，77 条断言全部通过）：
- `python3 -m app.diagnosis` 自测 20/20；`python3 -m mcp_server.client smoke` 7/7；不加 `--auto-approve` 的 502 演示仍在 human_approval 处停下（INC-20261120-150143-458，中断提示显示 plan 4 步、1 高危、1 回滚、1 配对）。
- 三条图回归全部 DONE，根因置信度 1.00：502（INC-20261120-150128-925，证据 8 条）、cpu（INC-20261120-150134-888，证据 7 条）、oom（INC-20261120-150135-333，证据 6 条）；每份 plan 都是 4 步（3 正向 + 1 回滚），高危 1、回滚 1、配对 1，回滚工具均为 `rollback_release`；502 的来源是 `runbook:0765931b250c3cd2`，cpu 是 `runbook:e10e9cda74776683`（cpu-high / 现象），oom 是 `runbook:1e466760d6113def`；诊断的推荐处置里没有回滚步骤，高危处置仍带回滚文本。
- 未知故障（INC-20261120-150135-775）：仍判 unknown、Top 1 置信度 0.00（< 0.5）、只给一个低风险转人工处置、未执行重启；plan 只有 1 步，来源标为“建议人工判断”；证据 2 条。
- 证据仍 ≤10 条（502：8、cpu：7、oom：6、unknown：2）。正常执行的 `exec_result` 里没有回滚步骤（回滚步骤在 plan 里标为 skipped）。

先说清楚（诚实说明）：回滚步骤目前是“已定义、可配对、可展示”——正常流程里它被跳过，真正触发自动回滚是第 9 天的活（失败路径：verify 未恢复时按配对执行回滚）。另外回滚步骤在 plan 里标 `risk="low"` 是计划层面的记账（它是休眠步骤，不增加正向方案的风险等级），不是说底层操作本身无风险：`rollback_release` / `scale_service` / `update_config` 在工具注册表里仍是高危工具，第 9 天的触发路径触发它们之前必须重新过人工确认。还有一处待第 9 天补的口子：`execute()` 今天调用高危工具只传 `(state, service)`，而 `scale_service` 需要 `replicas` 参数、`update_config` 需要 `key/value` 参数——回滚步骤的文本里写明了目标值（原副本数 2、快照旧值），但第 9 天触发时必须把这些参数传进去，今天的跳过逻辑让这个问题不影响任何已验证的行为。演示数据仍是示例语料（7 篇手册 / 35 chunk）。

## 第 8 天：人工确认中断与恢复——修改、跨进程续跑、超时关闭（2026-11-20）

按冲刺表第 8 天交付物：确认、驳回、修改三种结果都可回传并恢复执行；服务重启后待确认事件可续跑；超时未确认自动关闭（方案第 11 章，对照第 6 章模块 6 与第 4 章节点表）。

- `app/main.py` — 新子命令 `modify` 与 `expire`；`status` 现在列出带序号的全部步骤（`status --list-steps` 只列步骤），`demo` 的中断提示也给出 modify 的跑法。
  - `modify <id> --by <人> --reason <理由> --drop-step <n>[,<n>...]`：n 是 plan 里的步骤序号（从 1 数，见 `status --list-steps`），一次可删多个（逗号分隔，可重复传参）。只允许删正向步骤；删回滚步骤明确拒绝并报错退出（exit 2），因为那会破坏第 7 天的 1:1 配对；序号越界、事件不在 awaiting_approval 同样拒绝（exit 2），被拒时事件原样不动。
  - `expire [--hours N]`（默认 24 小时）：扫描全部已落盘事件，只处理 `awaiting_approval` 的；按 `state.created_at` 判断，超时的置为 closed、记录关闭原因与关闭时间（`state.closed_reason` / `closed_at`，`app/state.py` 新增字段，带默认值，Day 8 之前的 `state.json` 仍能加载）、走 report 节点生成复盘报告（报告里有“关闭原因”与关闭时间）；未超时的不动。命令打印扫描总数、待确认数、关闭数、跳过数。
- `app/nodes.py` — `apply_modification()`：删正向步骤时其配对的回滚步骤一并移除（配对保持一致），`risk_level` 按剩余正向步骤重算（无 high 步骤则变 low）；改动记进 `state.approval`（approved=True + modified 标记、删了哪几步、原序号、配对移除的回滚步骤、理由）和 `state.exec_result`（一条 `modify (human_approval)` 审计记录），被删步骤从执行队列移除。`make_plan()` 会跳过 `approval["dropped_steps"]` 里的步骤——这是必须的：删掉重启后 verify 失败会触发一次重诊断，重建 plan 时若不跳过，删掉的重启步骤会悄悄回来并执行。`report()` 节点：带 `closed_reason` 的事件（即 expire 关闭的）走完报告仍保持 closed；正常与驳回流程仍以 done 结尾，和以前一样。
- `app/report.py` — 报告新增“关闭原因（+关闭时间）”行（仅 expire 关闭的事件有）和修改记录行（删了哪几步、配对回滚步骤、修改后风险等级），被删步骤即使已不在 plan 里，报告里仍有据可查。
- 跨进程恢复：没有新代码——检查点仍是 `data/incidents/<id>/state.json`，进程 A 跑 demo 停在 human_approval 后退出，进程 B（全新 python 进程）`status` 确认仍在 awaiting_approval，进程 C（全新 python 进程）`approve` 恢复到 done（见下验证）。

跑法：

```bash
cd ~/workspace/goals/ai/files/ops-agent
python3 -m app.main demo --alert nginx-502        # 进程 A：停在 human_approval 后退出
python3 -m app.main status <id> --list-steps      # 进程 B：看步骤序号，确认仍在 awaiting_approval
python3 -m app.main modify <id> --by eng --reason "复查步骤与 verify 重复，删掉" --drop-step 4
python3 -m app.main approve <id> --by eng --reason "确认重启"   # 进程 C：恢复执行到底
python3 -m app.main expire                        # 关闭超过 24 小时未确认的事件
python3 -m app.main expire --hours 48             # 上限改成 48 小时
```

已验证（2026-11-20）：
- 回归：`python3 -m app.diagnosis` 自测 20/20；`python3 -m mcp_server.client smoke` 7/7；三条图回归全部 DONE、根因置信度 1.00：502（INC-20261120-150502-685，证据 8 条）、cpu（INC-20261120-150506-859，证据 7 条）、oom（INC-20261120-150507-294，证据 6 条）；unknown（INC-20261120-150507-726）仍判 unknown、Top 1 置信度 0.00（< 0.5）、只给一个低风险转人工处置、未执行重启、证据 2 条。证据仍 ≤10 条。
- modify 删低风险步：502 事件 INC-20261120-150515-419，`--drop-step 4`（“复查错误率与健康检查”）后恢复执行，最终 DONE；报告里该步未执行、其余步骤正常、高危重启仍执行；approval 与报告都记了删步记录。
- modify 删高危步：502 事件 INC-20261120-150521-907，`--drop-step 2`（“重启上游实例”）后恢复执行，最终 DONE、未执行重启、配对的回滚步骤一并消失（plan 只剩 2 步）、risk_level 变 low、报告里有据可查。注意：因为没重启，verify 两次都失败（error_rate 未归零），事件以“未恢复”收尾出报告——删高危处置的后果如实留在报告里，没有被掩盖；删掉的重启步在重诊断后也没有回来（`make_plan` 的跳过逻辑生效，exec 里两次循环都没有重启）。
- modify 多删：502 事件 INC-20261120-150602-882，`--drop-step 1,4` 一次删两个低风险步，只剩重启 + 回滚，恢复后 DONE、重启执行。
- modify 拒绝：502 事件 INC-20261120-150526-774 上删回滚步骤（--drop-step 3）被拒绝（exit 2，明确说明会破坏 1:1 配对）；--drop-step 9 越界被拒绝；--drop-step 2,3（含回滚步）被拒绝；对已 done 的事件 modify 被拒绝；四次被拒后事件都原样不动（仍 awaiting_approval 或 done，plan 未变）。
- 跨进程恢复：同一事件 INC-20261120-150526-774，demo 进程退出后，独立的 `status` 进程确认仍在 awaiting_approval，再独立的 `approve` 进程恢复到 DONE（重启执行、报告已生成）。
- expire：测试事件 INC-20261120-150547-163（停在 human_approval，created_at 改为 25 小时前——只改了这个新建测试事件自己的 state.json）被默认 `expire`（24 小时）关闭：status=closed、关闭原因与关闭时间已记录、报告已生成、未执行任何处置；同批新建的未超时事件 INC-20261120-150547-441 与另外 3 个当日待确认事件不动。命令输出：`scanned 85 incidents, 5 awaiting_approval, closed 1, skipped 4`。`expire --hours 0` 没有实跑——它会关掉当时全部 4 个待确认的测试事件，与验证无关，故意没跑；--hours 0 走的是同一条阈值判断（阈值 0），逻辑上与已验证的默认 run 一致。
- reject 路径仍可用：502 事件 INC-20261120-150533-758 reject 后 status=done、exec 为空、报告已生成。

先说清楚（诚实说明）：确认页目前是 CLI，没有 Web 页面；超时关闭目前是手动触发的 `expire` 命令，不是后台定时任务——超过 24 小时未确认的事件只有在有人跑 `expire` 时才会被关闭；删高危步骤后事件会以“未恢复”收尾（verify 失败两次后出报告），这是删处置的真实后果，不是 bug。演示数据仍是示例语料。

## 第 9 天：执行、回滚与恢复验证——故意失败能触发自动回滚（2026-11-20）

按冲刺表第 9 天交付物：执行中任一步失败立即停止并触发自动回滚，回滚后仍走 verify 复查（方案第 11 章，对照第 6 章模块 7 与第 4 章节点表 execute / verify 行）。

- `app/state.py` — `PlanStep` 增加 `params: dict`（默认 `{}`，第 7 天留的缺口：`execute()` 原来只传 `(state, service)`，`scale_service` 要 `replicas`、`update_config` 要 `key/value`，现在高危工具调用时传 `**params`，无 params 时仍是原来的 `(state, service)` 调用形状）；`AgentState` 增加 `rollback_triggered: bool`（是否走过回滚）与 `rolled_back_steps: list[str]`（已回滚的正向步骤文本）。两个新字段都有默认值，Day 9 之前的 `state.json` 仍能加载（已实测旧事件 INC-20261116-065915 正常加载）。
- `app/nodes.py` — 失败注入钩子（测试用，默认关闭）：环境变量 `OPS_AGENT_FAIL_STEP=N`（plan 步骤序号，从 1 数，与 `status --list-steps` 一致），`execute()` 执行到该序号的正向步骤时主动抛错模拟工具失败。未设置时行为与之前完全一致；钩子只改 `execute()` 的流程，不改任何工具本身；**测试钩子，生产不用**（代码注释与本节都写明了）。钩子指向回滚步骤序号时无效果——回滚步骤在正常流程里是休眠的，根本不会执行到。
- 失败 → 停止 → 自动回滚：`execute()` 里任一步骤失败（钩子触发的错，或工具本身抛的非 `PermissionError`——`PermissionError` 仍按原有逻辑 blocked 关闭）时立即停止后续正向步骤；对已成功执行的 high 步骤，按第 7 天的配对关系逆序执行其配对回滚步骤（只回滚已成功执行的步骤；未执行的高危步骤不回滚；回滚步骤本身失败会记录下来、继续下一个回滚，不会整个卡住）。回滚执行结果进 `exec_result`（步骤名 + ok + detail）和 plan 里对应回滚步骤的 `result`。**设计决策**：人工确认时批的是整份 plan（含配对的回滚步骤），所以失败路径的回滚是已授权计划的一部分，自动触发，不再二次弹窗；回滚工具在注册表里仍是高危工具（这一点不变），`_require_approval` 在每次回滚调用里仍会跑，报告里能看到是谁批准的原计划。
- 回滚步骤带参数：`make_plan()` 给 `scale_service` 的回滚步骤填 `params={"replicas": 2}`（原副本数）；`update_config` 的回滚在执行时从快照取旧值——回滚触发时 `_resolve_rollback_params()` 读 `demo_service.config_snapshots` 里该 key 的最新快照，把 `{"key": ..., "value": 旧值}` 填进回滚步骤的 `params` 再调用；快照不存在时回滚步骤记失败并写明原因（不猜值）。`status` 现在会显示步骤的 `params`（有参数时）。
- 回滚后恢复验证：回滚执行完后仍走 `verify` 节点复查指标；`verify` 的 `exec_result` detail 在回滚后写明，例如 `"rolled back, error_rate_5m=0.0"`，与正常恢复的 `"error_rate_5m=..."` 可区分；报告新增“回滚与恢复验证（第 9 天，失败路径）”一节，单独列出失败步骤、已执行的回滚步骤及结果、回滚后验证结果、原计划批准人、已回滚的正向步骤。verify 未恢复仍按现有逻辑回 diagnose 最多一轮——已回滚的步骤不会在重诊后回来：`make_plan()` 跳过 `rolled_back_steps`（与第 8 天 `dropped_steps` 同一套常驻指令机制），其配对的回滚步骤也随之消失（只为实际在重建 plan 里的 high 步骤生成回滚步骤）。

跑法（主场景：502 演示，第 4 步“复查错误率与健康检查”是重启成功之后的一步）：

```bash
cd ~/workspace/goals/ai/files/ops-agent
python3 -m app.main inject clear
python3 -m app.main demo --alert nginx-502        # 停在 human_approval
OPS_AGENT_FAIL_STEP=4 python3 -m app.main approve <id> --by eng --reason "确认重启"
# 第 1 步 ok、第 2 步重启 ok、第 4 步失败 → 停止 → 配对回滚步骤自动执行 ok
# → verify 复查 → 出报告；报告里有“回滚与恢复验证”一节
python3 -m app.main report <id>                   # 看失败步骤、回滚步骤、回滚后验证
```

边界场景（同一跑法，只换环境变量）：`OPS_AGENT_FAIL_STEP=2`（重启本身失败，无已成功的高危步骤，不执行回滚，事件正常收尾出报告）；`OPS_AGENT_FAIL_STEP=1`（只读检查失败，同样无回滚）。

已验证（2026-11-20，验证脚本为临时脚本 `/tmp/verify_day9_config.py`、`/tmp/verify_day9_rebuild.py`、`/tmp/verify_day9_params.py`，未随项目提交）：
- 回归（钩子未设置）：`python3 -m app.diagnosis` 自测 20/20；`python3 -m mcp_server.client smoke` 7/7；三条图回归全部 DONE、根因置信度 1.00：502（INC-20261120-150953-688，证据 8 条）、cpu（INC-20261120-150954-138，证据 7 条）、oom（INC-20261120-150954-576，证据 6 条）；unknown（INC-20261120-150955-022）仍判 unknown、Top 1 置信度 0.00（< 0.5）、只给一个低风险转人工处置、未执行重启、证据 2 条。证据仍 ≤10 条；正常执行的 `exec_result` 里没有回滚步骤，`rollback_triggered` 均为 False。
- 回滚主场景（`OPS_AGENT_FAIL_STEP=4`）：502 事件 INC-20261120-151000-713，不加 `--auto-approve` 先停在 human_approval，`approve` 时设钩子：第 1 步 ok、第 2 步重启 ok（`restarted_at` 已记录）、第 4 步失败（钩子触发）→ 停止 → 配对回滚步骤 `rollback_release` 自动执行 ok → verify 复查 `rolled back, error_rate_5m=0.0` 已恢复 → DONE 出报告。`state.json` 的 `demo_service.version` 回到 `v1.4.1`（重启确实执行过、回滚确实执行过），`rollback_triggered=true`，`rolled_back_steps=["重启上游实例（演示环境）"]`；报告“回滚与恢复验证”一节列出失败步骤、回滚步骤成功、回滚后验证已恢复、批准人 eng。
- 回滚边界：`OPS_AGENT_FAIL_STEP=2`（INC-20261120-151016-440）：重启失败，无已成功的高危步骤，`exec_result` 里无回滚步骤，`rollback_triggered=false`，version 仍为 `v1.4.2`、`restarted_at` 为空，事件正常收尾出报告（verify 两次未恢复，error_rate 0.42——重试循环里钩子仍在环境变量里，第 2 步两次都失败，这是如实记录，不是掩盖）；`OPS_AGENT_FAIL_STEP=1`（INC-20261120-151022-570）：只读检查失败，同样无回滚、未重启、正常收尾出报告。
- 回滚后重诊不带回已回滚步骤（单测）：`rolled_back_steps` 含重启时 `make_plan()` 重建的 plan 只有两步（确认状态、复查），重启及其配对回滚步骤都不回来；第 8 天 `dropped_steps` 的跳过逻辑同时复核仍有效。
- modify 删步 + 回滚不冲突：502 事件 INC-20261120-151030-525，`--drop-step 4` 后 `approve`（无钩子），最终 DONE，重启执行、verify 通过，plan 剩 3 步。
- 配置快照回滚单测（`/tmp/verify_day9_config.py`，15/15 通过）：`update_config` 改 `timeout` 30s → 60s，快照记下 old=30s/new=60s；用快照旧值调回，值恢复 30s，快照记录补到 2 条且第二条 old=60s/new=30s；回滚解析器有快照时返回 `{"key": "timeout", "value": "30s"}` 并填进步骤 params，无 key 时取最新快照，无快照时返回明确失败原因；`make_plan` 的 `scale_service` 回滚步骤带 `params={"replicas": 2}`（合成 plan 验证，当前 `_PLANS` 里还没有 scale/update_config 的正向步骤）。

先说清楚（诚实说明）：回滚是在演示环境上验证的；回滚后“恢复”的判定仍是演示指标 `error_rate < 0.01`（`restart_service` 写 `restarted_at` 后指标归零，`rollback_release` 本身不改错误率——主场景里 verify 通过是因为重启已清掉故障，不是因为回滚修好了什么）；`update_config` 的回滚依赖快照，快照缺失时回滚会记失败；当前 `_PLANS` 里的正向步骤只有 `restart_service` 一个高危工具，`scale_service` / `update_config` 的回滚路径只在合成 plan 与单测里走通过，还没有一条真实 plan 触发过；钩子指向回滚步骤序号时无效果（回滚步骤正常流程里不执行）；边界场景里重试循环会重复触发同一个钩子失败（钩子在进程环境变量里，重诊重建 plan 后同一序号的步骤会再次失败），测试时如需只失败一次，应在重试前 unset 环境变量——演示环境的局限，不是线上行为。

## 第 10 天：复盘报告与 Trace 查询——一键报告、可回放、案例归档回库（2026-11-20）

按冲刺表第 10 天交付物：完整报告一键生成、按 incident_id 查询 Trace 并回放诊断全过程、案例归档回知识库（方案第 11 章，对照第 6 章模块 8 与第 8 章可观测性）。

- `app/trace.py`（新增）— `render_trace(state)`：按时间线讲故事，不是状态快照。分三节：时间线（每个节点名、耗时、流转后状态 + 该节点对本事件的结论：分类结果、证据条数、诊断 Top 1 与置信度、方案步数与配对数、人工确认结果、执行成功/失败数、验证是否恢复、报告路径）、工具调用（按调用顺序：工具名、参数摘要、耗时、返回摘要）、关键决策点（分类、诊断、方案与风险、人工确认、回滚、验证、收尾，一处看全）。`python3 -m app.main trace <id>` 对所有状态都可用（awaiting_approval / done / closed / reject 关闭的都实测过）。与 `status` 的区别：status 是当前状态快照（plan、trace 行、工具审计），trace 是按时间顺序的叙事（含决策）；节点旁的结论来自落盘的最终状态（第 1–9 天没有存每节点的快照），报告里如实写了这一点。
- `app/main.py` 的 `report` 改成一键生成：已有已保存报告时仍打印已保存的；没有时现场用 state 重新渲染并保存（render_report + save_report），再打印。任何已落盘事件都能一键出报告：已 done 的、reject 的、expire 关闭的、甚至还在 awaiting_approval 的都实测过（awaiting 事件现场出报告后状态不变，仍是 awaiting_approval）。
- `app/cases.py`（新增）— 案例归档（模块 8 最后一环）：report 节点在事件以 done 收尾且根因为已知类型（category 不是 unknown、诊断 Top 1 置信度 ≥ 0.5）时，把案例归档到 `data/cases/<incident_id>.json`（字段：incident_id、category、根因、置信度、证据 refs、处置摘要、执行结果、耗时（节点耗时总和）、归档时间）。不归档的三种情况，理由写进代码注释和报告里：unknown（没有可学的已知根因，归档会给下一个未知症状编造先例）、被驳回（未执行，无结果可学）、未恢复收尾（verify 没通过，处置未生效，不能推荐给下一个事件）；未以 done 收尾的（awaiting、expire 关闭）也不归档。归档幂等：文件已存在时直接返回，不覆盖（重复跑 report 后文件 md5 不变）。
- 历史案例进检索：`search_runbook` 在手册 Top 3 之后追加最多 1 条历史案例（source="历史案例 / <incident_id>"，文本写明根因、置信度与处置结果），手册 Top 3 优先、案例不挤占手册位置，证据总数仍 ≤10（取证时手册 3 + 案例 1 先占位，日志补剩余空位）。案例检索是 `app/retrieval.py` 的新函数 `search_cases()`，`search()` 的手册检索逻辑一个字节没动。匹配用同一门限 MIN_RUNBOOK_OVERLAP=2，但中文没有空格、整词重叠对案例文本太严（"持续打满"≠"打满"，502 案例与告警整词重叠为 0），所以案例匹配复用 retrieval 的 tokenizer（Latin 词 + CJK bigram），去掉单字/单数字噪音，并要求根因本身至少贡献一个非通用词（"实例/服务"这种通用词不算——"服务实例掉线"和"上游实例宕机"共享"实例"，不能仅凭它把 502 案例推给 OOM 事件）。离线验算：CPU→CPU 3、502→502 2、OOM→OOM 4；unknown→CPU/→OOM 各 2 但根因贡献为 0、unknown→502 为 1，三个 unknown 配对都被拦住；跨类型配对根因贡献均为 0。
- 报告节点容错：`nodes.report()` 里 render_report 抛错时不丢事件——捕获异常、保存一份只含 Trace 与原始状态摘要的最小报告（`render_minimal_report()`，注明"完整报告生成失败，见 Trace"），状态仍按原流程置 done/closed，错误记进 trace。归档失败同样只记 trace，不阻塞报告。

跑法：

```bash
cd ~/workspace/goals/ai/files/ops-agent
python3 -m app.main trace <INC-...>              # 回放一次诊断全过程（任何状态）
python3 -m app.main report <INC-...>             # 有报告打印已保存的，没有现场重新生成并打印
python3 -m app.main demo --alert cpu --auto-approve   # 跑完即归档 data/cases/<id>.json
ls data/cases/                                   # 看已归档案例
```

已验证（2026-11-20，容错单测为临时脚本 `/tmp/verify_day10_fault.py`，未随项目提交）：
- 回归：`python3 -m app.diagnosis` 自测 20/20；`python3 -m mcp_server.client smoke` 7/7；三条图回归全部 DONE、根因置信度 1.00：502（INC-20261120-151729-685，证据 8 条）、cpu（INC-20261120-151735-063，证据 7 条）、oom（INC-20261120-151735-498，证据 6 条）；unknown（INC-20261120-151735-942）仍判 unknown、Top 1 置信度 0.00（< 0.5）、只给一个低风险转人工处置、未执行重启、证据 2 条。证据仍 ≤10 条。
- trace 实测：done 事件 INC-20261120-151741-489（8 节点、6 次工具调用、分类/诊断/确认决策齐全）；awaiting_approval 事件 INC-20261120-151809-893（5 节点，停在 human_approval，"等待确认中"）；reject 事件 INC-20261120-151805-015（驳回人 eng、理由、未执行任何处置）；expire 关闭事件 INC-20261120-151804-740（status closed、关闭原因与等待 25.0 小时）。
- report 再生成实测：删掉 INC-20261120-151741-489 的 report.md 再跑 `report <id>`，现场重新生成并打印，文件恢复；reject 事件 INC-20261120-151805-015 删报告后同样一键出报告；awaiting 事件 INC-20261120-151809-893 也能现场出报告，之后状态仍是 awaiting_approval。
- 案例归档与引用实测：CPU 演示 INC-20261120-151735-063 跑完到 done，`data/cases/` 多了 `INC-20261120-151735-063.json`（根因 CPU 打满、置信度 1.00、处置摘要 3 步、执行结果含 verify 已恢复）；新的 CPU 演示 INC-20261120-151741-489 的 runbook 证据里出现"历史案例 / INC-20261120-151735-063"（ref `runbook:case:INC-20261120-151735-063`），手册 chunk 仍是 3 条，证据总数 8 条；新的 unknown 事件 INC-20261120-151753-452 的 runbook 证据里没有历史案例（证据 2 条），且本身没有产生归档文件，报告"案例归档"一节写明"未归档：根因为 unknown，没有可归档的已知根因"。归档幂等：对 INC-20261120-151741-489 重复跑 report，案例文件 md5 不变（`2a45933710345ac0c2f559ebe736234c`），`data/cases/` 仍是 5 个文件。
- 第 9 天回滚路径仍可用：502 事件 INC-20261120-151830-403，`OPS_AGENT_FAIL_STEP=4` 下 approve：第 4 步失败 → 配对回滚步骤 `rollback_release` 自动执行成功 → verify 复查 `rolled back, error_rate_5m=0.0` 已恢复 → DONE；报告仍有"回滚与恢复验证"一节，`rollback_triggered=true`，已回滚 `["重启上游实例（演示环境）"]`。
- 报告容错单测（`/tmp/verify_day10_fault.py`）：把 `render_report` 换成必抛错的桩再跑 `nodes.report()`，状态仍是 done，保存的是最小报告（含"完整报告生成失败，见 Trace"、节点 Trace、原始状态摘要），trace 里记了失败原因；随后用真实 render 恢复了该事件的报告。

先说清楚（诚实说明）：案例库是演示事件归档的（2026-11-20 联调时 `data/cases/` 有 38 个文件），不是生产案例；案例检索是词法匹配（Latin 词 + CJK bigram），与手册检索同一门限，没有语义匹配；报告仍是 Markdown 文件，没有 Web 页面；trace 的节点旁结论来自落盘的最终状态，不是每节点的瞬时快照（第 1–9 天没存快照）；回滚后收尾的事件也会归档（它符合 done + 已知根因 + 已恢复的条件），归档文件里执行结果如实记了失败与回滚——要不要把"走过回滚"的事件排除在归档之外，是第 11–12 天可以定的一条规则。

## 现在还是什么（诚实说明）

- **演示数据，不是真实系统**：只读工具读的是内置的演示日志/指标；search_runbook 读的是 ops-agent 自己的 35-chunk 手册索引（第 6 天起：本地 BM25 + TF-IDF + RRF + 本地词法 Rerank；真 ES / Milvus / cross-encoder 在用户机器起 Docker 后再接）。
- **规则推理，不是 LLM**：classify / diagnose 默认仍是关键词规则；第 5 天已把 LLM 引擎接进 diagnose()（有 key 且 `OPS_DIAGNOSIS_ENGINE` 选了它才调 LLM），但 key 尚未确认，所以实际跑的仍是规则推理，接口（state 进、state 出）不用改图。
- **没有 FastAPI**：那是后面的活；图本身目前仍直接调 `app.tools`，还没改走 MCP 客户端——第 3 天的交付是 Server + 独立脚本验证，不是改图接线。没有真 ES / Prometheus / Milvus / PostgreSQL / Redis，检查点目前是 JSON 文件，ES 日志目前走本地 fallback 文件（ES 在你机器上 compose 起了之后 `es_writer` 会直接写 `ops-logs` 索引）。
- **评测数字有了，但要打折扣读**：15 个场景评测在第 11–12 天已跑完（见 `eval/report.md`：Top 1 14/14、Top 3 14/14、误操作 0、平均 0.30 秒、去重通过）。1.00 不是模型泛化能力——日志、指标、告警文案都是按识别关键词写的，诊断也是同一套关键词打分；简历上只能写“自建 15 场景评测集，Top 1 14/14（演示信号）”，不能写成通用准确率。`search_runbook` 的引用仍是词法匹配（第 6 天接的是本地 BM25 + TF-IDF 替身 + 本地词法 Rerank，不是真向量/真 cross-encoder）。

## 下一步（按冲刺表）

- 第 2 天：演示服务 + 故障注入 + ES 日志写入 ✅ 已完成（见上）
- 第 3 天：MCP Server 与三个只读工具 ✅ 已完成（见上）
- 第 4 天：取证并行化与证据裁剪 ✅ 已完成（见上）
- 第 5 天：诊断节点与结构化输出（接 LLM）✅ 已完成（见上）：结构化输出 + 可插拔引擎 + 未知型转人工；LLM 引擎已接线，key 确认后即可跑通（本地 stub 已验证回退链路）
- 第 6 天：接 RAG 知识库检索 ✅ 已完成（见上）：本地 BM25 + TF-IDF + RRF + 本地词法 Rerank（cross-encoder 留接口，本机未装）；ES / Milvus 均降级到本地；CPU 引用纠正为 cpu-high，unknown 的“上游实例宕机”次要假设已消失
- 第 7 天：方案生成与风险分级 ✅ 已完成（见上）：回滚步骤显式化（1:1 配对、execute 跳过）、每步来源标注（runbook chunk 引用或“建议人工判断”）、报告与 status 显示配对数；真正触发自动回滚是第 9 天的活
- 第 8 天：人工确认中断与恢复 ✅ 已完成（见上）：modify（删正向步骤、删回滚步骤拒绝、删高危步配对回滚一并移除并重算 risk_level）、跨进程恢复（JSON 检查点，独立进程 status/approve）、expire 超时关闭（默认 24 小时、手动触发、走 report 节点）；确认页仍是 CLI，超时关闭不是后台定时任务
- 第 9 天：执行、回滚与恢复验证 ✅ 已完成（见上）：失败注入钩子 `OPS_AGENT_FAIL_STEP`（测试用，默认关闭）、失败立即停止并按配对自动回滚（只回滚已成功的高危步骤，回滚失败不卡住）、回滚步骤带参数（scale 回滚 `{"replicas": 2}`、update_config 回滚执行时读快照旧值、无快照记失败）、回滚后 verify 复查与报告单独的“回滚后验证”一节、已回滚步骤在重诊重建 plan 时不再回来；回滚在演示环境验证，恢复判定仍是演示指标
- 第 10 天：复盘报告与 Trace 查询 ✅ 已完成（见上）：`trace` 回放命令（时间线 + 工具调用 + 关键决策点，任何状态可用）、`report` 一键重生成（无已保存报告时现场渲染并保存）、案例归档回知识库（`data/cases/`，done + 已知根因 + 置信度 ≥ 0.5 + 已恢复才归档，幂等）与历史案例检索（手册 Top 3 后最多 1 条，同一门限 2）、报告节点容错（render 失败时保存最小报告，状态不变）；案例库是演示事件归档，检索是词法匹配，报告仍是 Markdown 文件，没有 Web 页面
- 第 11–12 天：15 场景评测 harness 与 5 类核心场景补齐 ✅ 已完成（见下）：12 类故障注入、15 场景评测集与 harness（Top 1 14/14、Top 3 14/14、误操作 0、平均 0.30 秒、去重通过）、10 分钟去重（`data/dedup.json` 文件版）
- 第 13–14 天：联调、演示与收尾 ✅ 已完成（见文末）：README 按新人视角从头复现并修掉过时内容、快速开始、架构图（`docs/architecture.md`）、演示脚本与实录（`demo.sh`、`docs/demo-transcript.md`）、收尾清理与全量验证

## 第 11–12 天：15 场景评测 harness 与 5 类核心场景补齐（2026-11-20）

按冲刺表第 11–12 天交付物：评测集前 5 个场景跑通（方案第 9 章），实际 15 个全跑了一遍，逐场景报告，不挑着报。

### 改了什么

- **故障注入补齐（`demo_service/service.py`）**：FAULTS 从 cpu / oom 扩到 12 类，新增 disk（磁盘 100%、"No space left on device"、已删除文件仍被进程占用、状态 degraded）、slowquery（慢日志、锁等待、缺索引提示、P95 2400ms）与 release / downstream / pool / config / cert / mq / cache / jitter（每类 3 条日志，措辞按识别关键词写）。`python3 -m app.main inject` 与 `demo_service.service inject` 的选项同步新增；`demo_alerts/` 新增 disk-full / slow-query / release / downstream / pool / config / cert / mq / cache / jitter 共 10 个预置告警（`app/main.py` 的 ALERT_FILES 同步）。
- **识别规则（`app/nodes.py`）**：classify 加 8 条新规则（release / downstream / pool / config / cert / mq / cache / jitter，顺序与关键词逐一核对过：新告警不含更早规则的关键词，例如缓存告警说“过期”但证书规则只匹配“证书”/certificate，消息队列告警说“变慢”但慢查询规则只匹配“慢查询”/slow query）；diagnose 的 `_HYPOTHESES` 加 8 个新假设（关键词全部只在自家注入日志里出现，事先 grep 过 7 篇手册与静态日志；刻意没用 "deploy" / "v1.4.2" / "error" 作关键词——它们在每个事件的状态/指标摘要里都有，用了会让未知型症状也得 0.67）；`_PLANS` 加 10 套处置方案（disk / slowquery + 8 个新场景），每个高危步骤配回滚步骤（沿用第 7 天配对机制：release 的高危步骤就是 rollback_release，config 的高危步骤是 update_config、参数 `{"key": "rate_limit", "value": "1000"}`——为此 `_PLANS` 元组支持可选的第 5 个元素 params，`make_plan()` 会写进 PlanStep.params，`execute()` 原样 `**params` 传给工具；其余高危步骤是 restart_service，配对 rollback_release）。`app/diagnosis.py` 的 KNOWN_CAUSES 同步加 8 个新根因名，否则结构化诊断校验会拒收。
- **演示语义（`app/tools.py`）**：rollback_release 在注入故障就是 release 时清掉故障（回滚掉坏版本），update_config 在注入故障就是 config 时清掉故障（改回正确阈值就是修复）；其他故障不动。没有这两句，场景 6 / 9 的 verify 永远过不了（rollback / 改配置本身不写 `restarted_at`，指标不会归零）。restart_service 本来就清故障。
- **去重（`app/main.py`，方案第 6 章模块 1）**：`demo` 入口先查 `data/dedup.json`，fingerprint = 告警自带的 fingerprint（无则 service + title）；10 分钟内同一 fingerprint 且前一事件仍未结束（awaiting_approval / running / executing / verifying）时不建事件、不跑图，打印“重复告警，已合并到 \<已有事件 id\>”并返回；超过 10 分钟或前一事件已结束则正常建新事件。两个分支都在 harness 外手工验过（见下）。
- **评测隔离（`app/cases.py` / `app/retrieval.py`）**：`cases.get_cases_dir()` 读 `OPS_AGENT_CASES_DIR` 环境变量（默认 `data/cases`），archive_case / load_cases / search_cases 都走它；harness 给每个场景一个空的临时案例目录（`scene-<id>/`），真实 `data/cases/` 未动，场景之间不互相读到对方归档的案例，归档结果保留在各自目录备查；harness 开始时删 `data/dedup.json`（去重是全局的，上次评测留下的未结束事件会让本次同指纹告警误合并）。
- **评测 harness（`eval/` 新建）**：`eval/scenarios.json`（15 个场景：id、名称、标准根因、inject 的 fault、alert 文件、主要证据来源）与 `eval/run_eval.py`（子进程调 CLI 跑每个场景：inject → `demo --alert … --auto-approve` → 读 `state.json` 判定；场景 14 单独跑：同一告警连发 3 次不加 `--auto-approve`，数新建事件数；场景 15 用已有的 unknown）。每场景记录 Top 1 / Top 3 命中、证据完整率（诊断引用的 refs 是否都在 state.evidence 里，独立重算）、高危误操作数（tool_audit 里的高危工具调用在执行时 approval.approved 必须为真，否则计 1，并与 plan 里的高危步骤执行记录交叉核对，两边必须一致；auto-approve 下 approval 是批准的，记 0）、单次耗时（demo 子进程墙钟时间）。输出 `eval/report.md` 与 `eval/report.json`。`OPS_DIAGNOSIS_ENGINE=rule`，不调任何真实 LLM 端点。
- **识别规则的三处修补（都是修信号或规则，不是改评测）**：
  1. 检索门限 `app/retrieval.py`：`content_overlap()` 取代裸 `word_overlap()` 作证据门限——个位数裸数字（"2"/"4"，来自 "v1.4.2" 的分词）与 "日志出现"/"错误日志出现" 这类手册 boilerplate 不再计入重叠。修前：release 告警与 5 篇手册只共享 {"2","4"} 就各返回一条手册，disk 告警凭 {"100","日志出现"} 命中 CPU 手册、凭 {"space","日志出现"} 命中 OOM 手册；修后 disk 只剩 disk-full 手册，release / jitter 的垃圾手册全部掉到 0，502 / CPU / OOM 的手册 Top 3 不变，unknown 仍是 0 条。注意：多位数数字保留——"502" 是真实错误码，MCP smoke 的 search_runbook 查询只共享 {"nginx","502"}，全排除数字会把它打到 1、smoke 挂掉（修第一版时真挂过一次，已改回只排除个位数）。
  2. CPU 假设加第 3 个关键词 "hot loop"：disk / slowquery 的证据里都会混进 CPU 手册（"load average 12.4"），CPU 原来只凭 cpu+load 两个词就能到 1.00，和磁盘/慢查询打平、按字典序赢；加了只在 CPU 注入日志里出现的 "hot loop" 后，disk / slowquery 里的 CPU 假设只到 0.67，CPU 场景本身仍是 1.00（"hot loop" 在第三条注入日志里）。
  3. diagnose 平局按分类破平：置信度相同时优先与 `state.category` 一致的假设（分类读的是告警本身；只排相等置信度，唯一高分不受影响；unknown 不走这条路）。
  4. pool 的取证关键词从 `["pool", "connection"]` 缩到 `["pool"]`：静态 502 日志里 "(111: Connection refused)" 含 "connection"，会把两条 502 日志漏进连接池证据，上游假设被抬到 0.75；缩窄后三条注入日志都在，上游掉出 Top 3。
  5. 历史案例匹配的通用词表 `GENERIC_ROOT_TERMS` 加 "异常"/"错误"/"日志"/"指标"：未知型症状（"…无错误日志、无指标异常…"）凭 bigram "异常" 与根因"配置变更引发异常"配对成功，引用了一条配置变更案例——诊断仍是 unknown 0.00（案例文本里没有 config 关键词，不影响打分），但引用本身是错的；修后 unknown 证据回到 2 条（指标 + 状态），CPU→CPU 等正常同因引用仍在（已实测）。

### 评测表（`python3 eval/run_eval.py`，2026-11-20 15:41 最后一次跑，报告见 `eval/report.md` / `eval/report.json`）

| 场景 | 标准根因 | Top 1 | 置信度 | Top 1 | Top 3 | 证据完整 | 误操作 | 耗时 | 事件 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 CPU 打满 | CPU 打满 | CPU 打满 | 1.00 | 是 | 是 | 1.00 | 0 | 0.30s | INC-20261120-154102-466 |
| 2 内存 OOM 被杀 | 内存 OOM 被杀 | 内存 OOM 被杀 | 1.00 | 是 | 是 | 1.00 | 0 | 0.30s | INC-20261120-154102-923 |
| 3 接口大面积 502 | 上游实例宕机 | 上游实例宕机 | 1.00 | 是 | 是 | 1.00 | 0 | 0.30s | INC-20261120-154103-380 |
| 4 数据库慢查询 | 数据库慢查询 | 数据库慢查询 | 1.00 | 是 | 是 | 1.00 | 0 | 0.29s | INC-20261120-154103-832 |
| 5 磁盘写满 | 磁盘写满 | 磁盘写满 | 1.00 | 是 | 是 | 1.00 | 0 | 0.29s | INC-20261120-154104-278 |
| 6 发布后错误率上升 | 发布后错误率上升 | 发布后错误率上升 | 1.00 | 是 | 是 | 1.00 | 0 | 0.29s | INC-20261120-154104-728 |
| 7 下游超时雪崩 | 下游超时雪崩 | 下游超时雪崩 | 1.00 | 是 | 是 | 1.00 | 0 | 0.29s | INC-20261120-154105-171 |
| 8 连接池耗尽 | 连接池耗尽 | 连接池耗尽 | 1.00 | 是 | 是 | 1.00 | 0 | 0.30s | INC-20261120-154105-614 |
| 9 配置变更引发异常 | 配置变更引发异常 | 配置变更引发异常 | 1.00 | 是 | 是 | 1.00 | 0 | 0.31s | INC-20261120-154106-080 |
| 10 证书过期 | 证书过期 | 证书过期 | 1.00 | 是 | 是 | 1.00 | 0 | 0.30s | INC-20261120-154106-528 |
| 11 消息队列积压 | 消息队列积压 | 消息队列积压 | 1.00 | 是 | 是 | 1.00 | 0 | 0.30s | INC-20261120-154106-986 |
| 12 缓存击穿 | 缓存击穿 | 缓存击穿 | 1.00 | 是 | 是 | 1.00 | 0 | 0.30s | INC-20261120-154107-438 |
| 13 单实例网络抖动 | 单实例网络抖动 | 单实例网络抖动 | 1.00 | 是 | 是 | 1.00 | 0 | 0.30s | INC-20261120-154107-886 |
| 14 重复告警风暴 | 去重（新建 1 个事件） | CPU 打满（仅供参考） | 1.00 | — | — | — | 0 | — | INC-20261120-154108-336（连发 3 次，新建 1 个，另 2 次合并到它；随后 approve 收尾 done 且已恢复） |
| 15 未知型故障 | 未知型故障，需转人工 | 未知型故障，需转人工 | 0.00 | 是 | 是 | 1.00 | 0 | 0.30s | INC-20261120-154109-163 |

汇总：Top 1 命中率 14/14 = 1.00；Top 3 命中率 14/14 = 1.00；平均证据完整率 1.00；误操作总数 0（目标 0）；平均单次诊断耗时 0.30 秒；场景 14 去重通过。**没有没过的场景**——但 1.00 这个数要打折扣读，见下诚实说明。

### 验证（2026-11-20，全部自己跑过）

- 回归：`python3 -m app.diagnosis` 自测 20/20；`python3 -m mcp_server.client smoke` 7/7（中途修检索门限时挂过一次 search_runbook，原因是全排除数字后 smoke 查询只剩 1 个重叠词，改回只排除个位数后恢复）。
- 三条图回归全部 DONE、根因置信度 1.00：502（INC-20261120-153151-362，证据 9 条）、cpu（INC-20261120-153151-814，证据 8 条）、oom（INC-20261120-153152-282，证据 6 条）；unknown（INC-20261120-153152-772）仍判 unknown、Top 1 置信度 0.00（< 0.5）、只给一个低风险转人工处置、未执行高危工具、证据 2 条（修案例通用词表后重跑 INC-20261120-153226-830 同样是 2 条；中间有一次是 3 条，多的一条就是上面修掉的错引配置案例）。
- 新故障回归：inject disk → `demo --alert disk-full --auto-approve`（INC-20261120-153157-736，磁盘写满 1.00，证据 7 条）与 inject slowquery → `demo --alert slow-query --auto-approve`（INC-20261120-153158-218，数据库慢查询 1.00，证据 8 条）各跑一次，均 DONE 且已恢复。
- 完整 harness：`python3 eval/run_eval.py` 跑完 15 个场景（第 13–14 天联调时又完整跑过两次：15:40 单独跑一次、15:41 在 `demo.sh` 里跑一次，临时案例目录 `/tmp/ops-agent-eval-cases-hmb6k62m`，每场景 `scene-<id>/` 各归档 1 个案例，场景 15 为 0 个——unknown 不归档），`eval/report.md` 与 `eval/report.json` 已生成；逐场景结果与上表一致，两次的汇总数字与 15:33 那次完全相同（Top 1 14/14、Top 3 14/14、误操作 0、平均 0.30 秒、去重通过），报告以最后一次为准。harness 之前还完整跑过两次（一次在修检索门限前、一次在修案例通用词表前），结果同为 14/14。
- 第 9 天回滚路径仍可用：502 事件 INC-20261120-153319-947，`OPS_AGENT_FAIL_STEP=4` 下 approve：第 4 步失败 → 配对回滚步骤 `rollback_release` 自动执行成功 → verify 复查已恢复 → DONE，`rollback_triggered=true`，版本回到 v1.4.1，报告有“回滚与恢复验证”一节。
- 第 10 天 trace/report 仍可用：`trace INC-20261120-153319-947` 回放 9 节点、7 次工具调用、回滚决策齐全；`report` 打印已保存的复盘报告。
- 去重另两个分支手工验过：已 done 的 CPU 事件后新 demo 建了新事件（INC-20261120-153325-836）；把 `data/dedup.json` 里 CPU 指纹的时间改到 11 分钟前后再 demo，同样建了新事件（INC-20261120-153330-957）；两个待确认事件随后都 approve 收尾到 done，演示服务已 `inject clear` 回基线。

### 诚实说明

- **1.00 不是“模型很准”，是“信号是按规则造的”**：15 个场景的日志、指标、告警文案都是我按识别关键词写的，诊断也是同一套关键词打分——评测测的是“注入信号 → 取证 → 关键词打分 → 结构化输出 → 方案 → 确认 → 执行 → 验证”这条闭环能跑通、且 Top 1/Top 3/证据引用/误操作这些口径能被程序算出来，不是模型泛化能力。换一批没按关键词写的真实日志，数字会掉。简历上只能写“自建 15 场景评测集，Top 1 14/14（演示信号）”，不能写成通用准确率。
- **演示级处置**：除发布回滚与配置恢复外，场景 4–13 的高危步骤都是重启实例——演示环境里重启清故障所以 verify 能过；真实系统里重启修不好慢查询、证书过期、缓存击穿。评测证明的是闭环，不证明修复手段。
- **检索仍是词法匹配**：慢查询场景的手册 Top 3 里仍有一条 CPU 手册（接口 / P95 / 延迟等通用词重叠），只是 CPU 假设因缺 "hot loop" 只到 0.67；历史案例匹配同样是词法匹配，跨场景运行时可能引用错根因的案例（本次评测因案例隔离未出现；隔离外手工跑 unknown 时真出现过一次错引，已用通用词表修掉——见上第 5 条）。
- **耗时 0.30 秒是演示值**：工具全是本地文件/内存读写，没有真 ES / Prometheus / Milvus 调用，没有 LLM 调用；不要拿它和人工排查基线对比。
- **去重是文件版，不是 Redis**：方案第 6 章模块 1 说用 Redis 做 10 分钟去重，这里是 `data/dedup.json` 文件实现，语义相同（fingerprint + 10 分钟 + 未结束才合并），但没有 Redis 的过期与并发保证；多进程同时 demo 同一告警会有竞态，没测也没修。

## 第 13–14 天：联调、演示与收尾（2026-11-20）

按冲刺表第 13–14 天交付物：以“第一次拿到这个项目的人”的视角从头按
README 跑一遍、修掉过时内容，架构图、演示脚本与实录、收尾清理、全量验证。

### 改了什么

**README 修了 12 处过时内容**（联调时逐条跑命令发现的，不是凭印象改的）：
1. 标题只写到第 4 天 → 改为第 1–14 天总标题。
2. 新增“快速开始”小节：7 条命令跑通主链路（环境检查、注入 CPU、一键跑完、回基线、演示脚本、评测）。
3. 顶部“跑法”只列 5 条命令 → 补全为 13 条（inject / modify / expire / trace / diagnosis / diagnosis 自测 / smoke / 评测全在内）。
4. 6 处 `cd workspace/goals/ai/files/ops-agent` 相对路径 → `cd ~/workspace/goals/ai/files/ops-agent`（相对路径只在 ~ 下才成立）。
5. 第 2 天小节 `inject cpu|oom|clear` → 注明第 11–12 天已扩到 12 类故障并列出。
6. 第 10 天“案例库目前只有 5 条”→ 联调实数：`data/cases/` 有 38 个文件。
7. “现在还是什么”里“没有评测数字”→ 评测已在第 11–12 天跑完，见 `eval/report.md`，并保留“1.00 要打折扣读”的说明。
8. “下一步”列表只列到第 10 天 → 补第 11–12 天与第 13–14 天两条。
9. “下一步”第 10 天行里残留的“只有 5 条”→ 改掉。
10. 第 11–12 天评测表仍是 15:33 那次运行 → 同步到最后一次运行（15:41，`eval/report.json` 生成），逐场景事件 id 与耗时按 `report.json` 机械替换，汇总数字两次完全相同。
11. `app/main.py` 的 `--alert` 帮助只列 4 个选项 → 列全 14 个（代码小修，不改行为）。
12. `app/main.py` 模块 docstring 的 Usage 缺 inject / modify / expire / diagnosis → 补上；`requirements.txt` 里已过期的“RAG Week 2, Wed 2026-11-18”注释 → 改为 langgraph 未装、按需再装（代码小修，不改行为）。

除上面第 11、12 条外没有动代码：诊断、检索、回滚逻辑一个字节没改；
联调没有发现影响复现性的代码 bug，所以没有别的代码改动。

**架构图（新建 `docs/architecture.md`）**：五层 ASCII 图（接入层 CLI / 编排层
8 节点图 / 工具层 MCP + 8 工具 / 数据层演示服务 + JSON 检查点 + RAG 手册索引
+ 案例库 / 模型层规则推理 + LLM 适配器）与 8 节点流转图（两条回边与中断点），
每层 2–3 句说明“为什么存在、换掉影响什么”，并列出 10 个降级点
（ES / Milvus 不可达走本地、LLM 无 key 走规则、工具超时走降级等）。

**演示脚本与实录（新建 `demo.sh`、`docs/demo-transcript.md`）**：`bash demo.sh`
按顺序演示完整故事，每步之间先 `inject clear`，全程无需人工输入：
① 502 中断 → approve 恢复出报告；② CPU 一键跑通；③ unknown 转人工；
④ `OPS_AGENT_FAIL_STEP=4` 故意失败触发回滚；⑤ trace 回放第 ④ 步的事件；
⑥ 评测 harness。approve 的 incident id 是脚本内从 demo 输出里取的，没有写死。
实录是 2026-11-20 15:40 那次运行的完整输出原样照录（`bash demo.sh > log 2>&1`），
每步一句解说；唯一例外是步骤 1 的报告只贴了前 30 行（实录里有标注）。

### 怎么跑

```bash
cd ~/workspace/goals/ai/files/ops-agent
bash demo.sh              # 完整演示（约 1 分钟，含 15 场景评测）
python3 eval/run_eval.py  # 只跑评测
```

### 验证（2026-11-20，第 13–14 天最终全量验证，全部自己跑过）

- `python3 -m app.diagnosis` 自测 20/20；`python3 -m mcp_server.client smoke` 7/7。
- 三条图回归全部 DONE、根因置信度 1.00：502（INC-20261120-154042-333，证据 9 条）、cpu（INC-20261120-154042-819，证据 8 条）、oom（INC-20261120-154043-308，证据 6 条）。
- unknown（INC-20261120-154043-779）：仍判 unknown、Top 1 置信度 0.00（< 0.5）、is_unknown=true、只给低风险转人工处置、未执行任何高危工具、证据 2 条。
- 证据仍 ≤10 条（502：9、cpu：8、oom：6、unknown：2）。
- 评测 harness 重跑两次：15:40 单独跑一次、15:41 在 `demo.sh` 里跑一次，两次均为 Top 1 14/14、Top 3 14/14、误操作 0、平均 0.30 秒、去重通过；`eval/report.md` 以最后一次为准。
- `bash demo.sh` 从头跑通两次：15:40 第一次（实录来源，事件 153959-816 / 154000-742 / 154001-249 / 154001-749，评测临时目录 `/tmp/ops-agent-eval-cases-840gzxim`）与 15:41 重跑（事件 154059-777 / 154100-661 / 154101-123 / 154101-594，评测临时目录 `/tmp/ops-agent-eval-cases-hmb6k62m`），两次 exit 0，评测汇总相同。
- 联调中间还逐条跑过 README 里的命令：inject cpu / clear、502 中断 + approve、unknown、status --list-steps、modify 删第 4 步后恢复 DONE、trace、report、diagnosis、MCP list / call、expire（默认跑一次：0 关闭，因为当时 6 个待确认事件都不到 24 小时）——全部按 README 写的那样跑通。

### 收尾清理（2026-11-20）

- `demo.sh` 跑之前：`expire --hours 0` 关闭了 5 个冲刺测试留下的待确认事件（INC-20261120-145731-315、150143-458、150234-870、150547-441、151809-893，4 个 502 + 1 个 CPU，都是第 7–10 天测试留下的），`scanned 250 incidents, 5 awaiting_approval, closed 5, skipped 0`；复核后 awaiting_approval 为 0。没有删 `data/incidents/`——那是审计轨迹，只关事件、不删记录。
- `demo.sh` 跑完与最终验证跑完后复核：awaiting_approval 仍为 0（demo.sh 的中断事件都在脚本内 approve 了，评测场景 14 的事件也在 harness 内 approve 收尾了），演示服务 `inject clear` 回基线（`demo_service/state.json` 的 fault 为 None）。
- RAG 项目文件未动：`../rag-week1-starter/chunks.jsonl` 仍是 30 行，md5 `64c9ac7a8addf818775ee54af712730d`，与第 6 天记录一致。

### 诚实说明

- 演示脚本是录屏式脚本，没有视频文件：`demo.sh` 是按顺序跑命令的 bash 脚本，`docs/demo-transcript.md` 是它的文字实录，不是录屏。
- 架构图是文字版：`docs/architecture.md` 里的是 ASCII 图，没有画图文件。
- 他人复现仍需 Python 3.12 + numpy 1.26.4（本机已装，见快速开始）；Docker / 真 Milvus / 真 ES / LLM key 仍是待用户在自己机器上接的（见下表），接之前跑的是演示数据 + 规则推理。
- 评测 1.00 的折扣说明不变：信号是按规则造的，见第 11–12 天诚实说明。

## 现状对照表（第 13–14 天，2026-11-20）

| 事项 | 状态 | 说明 |
| --- | --- | --- |
| Python 3.12 + numpy 1.26.4 | ✅ 本机实测 | `python3 --version` 3.12.3，`numpy` 1.26.4；标准库 + numpy 跑完全部命令 |
| 快速开始 7 条命令 | ✅ 本机实测 | 从头跑通，见上 |
| 全部 CLI 命令（demo / approve / reject / modify / expire / status / report / trace / diagnosis / inject） | ✅ 本机实测 | 联调时逐条跑过 |
| 结构化诊断自测 20/20、MCP smoke 7/7 | ✅ 本机实测 | 最终验证时重跑通过 |
| 三条回归 502 / CPU / OOM（置信度 1.00）与 unknown 转人工 | ✅ 本机实测 | 最终验证时重跑通过，证据 ≤10 条 |
| 15 场景评测（Top 1 14/14、误操作 0） | ✅ 本机实测 | 15:40 与 15:41 各跑一次，数字相同 |
| `bash demo.sh` 从头跑通 | ✅ 本机实测 | 15:40 与 15:41 各跑一次，exit 0 |
| 演示服务回基线、0 个待确认事件、chunks.jsonl 未动 | ✅ 本机实测 | 收尾清理时复核 |
| 起 Docker 跑真 Milvus / ES | ⏳ 等用户在自己机器上做 | 本机无 Docker，两者均降级到本地（`es=unreachable; milvus=unavailable`） |
| 装 sentence-transformers 跑 cross-encoder rerank | ⏳ 等用户在自己机器上做 | 本机未装，走本地词法 Rerank（`reranker=local-lexical`） |
| 给 `OPS_LLM_API_KEY` 接真 LLM | ⏳ 等用户在自己机器上做 | 无 key 时诊断走规则推理（`source=rules`），不尝试调用 |
| 换 Redis 做去重 | ⏳ 等用户在自己机器上做 | 现在是 `data/dedup.json` 文件版，无 Redis 的过期与并发保证 |
| 接真实 Prometheus 指标 | ⏳ 等用户在自己机器上做 | 现在读演示服务内存里的指标；`query_metrics` 的超时降级只在模拟故障上验过 |
| FastAPI / Web 确认页 / 后台定时 expire | ⏳ 未做 | 确认页仍是 CLI，expire 仍是手动触发的命令 |
