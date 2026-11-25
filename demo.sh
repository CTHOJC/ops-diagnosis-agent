#!/usr/bin/env bash
# 智能运维诊断 Agent — 演示脚本（冲刺第 13–14 天，2026-11-20）
#
# 录屏式脚本：按顺序演示完整故事，全程无需人工输入。
# 每步之间先 inject clear 清状态；不调真实 LLM（OPS_DIAGNOSIS_ENGINE=rule，
# 诊断走规则推理），不碰真后端（演示服务 + 本地文件）。
#
# 用法（从项目根目录）：
#   bash demo.sh
#
# 六步故事：
#   ① 502 中断 → approve 恢复出报告
#   ② CPU 一键跑通
#   ③ unknown 转人工
#   ④ 故意失败触发回滚（OPS_AGENT_FAIL_STEP=4）
#   ⑤ trace 回放（回放第 ④ 步的事件）
#   ⑥ 评测 harness（15 场景）
#
# 注意：第 ①、④ 步需要“停下来展示”——脚本用不带 --auto-approve 的 demo
# 跑到 human_approval 中断、打印提示，再用 approve 恢复；approve 的
# incident id 是脚本内从 demo 输出里取的最新事件 id，没有写死。
set -euo pipefail
cd "$(dirname "$0")"
export OPS_DIAGNOSIS_ENGINE=rule

# run: 先回显命令，再执行（实录里能看到每步跑的是哪条命令）。
run() { echo "\$ $*"; "$@"; }

# demo_stop <alert> <outfile>: 跑 demo 到 human_approval 中断。demo 的输出
# 经 tee 同时打印（走 stderr）并存文件；函数本身的标准输出只有新事件 id，
# 这样调用方用 $(demo_stop ...) 取值时不会把演示输出混进来。
demo_stop() {
    local alert="$1" outfile="$2"
    echo "\$ python3 -m app.main demo --alert $alert" >&2
    python3 -m app.main demo --alert "$alert" 2>&1 | tee "$outfile" >&2
    grep -o 'INC-[0-9-]*' "$outfile" | head -1
}

echo "===== 智能运维诊断 Agent 演示（demo.sh）====="
echo "环境：本机演示数据 + 规则推理，不调真实 LLM、不碰真后端。"
echo "每步之间先 inject clear，演示服务回基线。"

echo
echo "----- 步骤 1：502 中断 → approve 恢复出报告 -----"
echo "502 告警没有注入故障，它的证据来自 nginx-502 预设的上游 down 状态；"
echo "方案含高危重启，所以图在 human_approval 处停下，等人确认。"
run python3 -m app.main inject clear
ID_502=$(demo_stop nginx-502 /tmp/ops-agent-demo-step1.out)
echo "事件 $ID_502 已停在 human_approval，现在批准它恢复执行："
run python3 -m app.main approve "$ID_502" --by demo --reason "演示：确认重启上游实例"
run python3 -m app.main report "$ID_502" | head -30

echo
echo "----- 步骤 2：CPU 一键跑通 -----"
echo "先注入 CPU 打满故障，再一口气跑完（--auto-approve）：诊断、确认、"
echo "重启、验证、报告全在一次命令里。"
run python3 -m app.main inject clear
run python3 -m app.main inject cpu
run python3 -m app.main demo --alert cpu --auto-approve

echo
echo "----- 步骤 3：unknown 转人工 -----"
echo "页面字体突然变大：没有匹配的日志、手册与指标，诊断应判 unknown、"
echo "只给低风险转人工处置，不执行任何高危工具。"
run python3 -m app.main inject clear
run python3 -m app.main demo --alert unknown --auto-approve

echo
echo "----- 步骤 4：故意失败触发回滚 -----"
echo "同样的 502 事件，approve 时设 OPS_AGENT_FAIL_STEP=4：第 4 步（复查）"
echo "失败 → 立即停止 → 配对的回滚步骤自动执行 → verify 复查 → 出报告。"
echo "这是测试钩子（默认关闭），生产不用。"
run python3 -m app.main inject clear
ID_RB=$(demo_stop nginx-502 /tmp/ops-agent-demo-step4.out)
echo "事件 $ID_RB 已停在 human_approval，现在带失败钩子批准它："
echo "\$ OPS_AGENT_FAIL_STEP=4 python3 -m app.main approve $ID_RB --by demo --reason \"演示：确认重启（带失败注入）\""
OPS_AGENT_FAIL_STEP=4 python3 -m app.main approve "$ID_RB" --by demo --reason "演示：确认重启（带失败注入）"
echo "回滚证据（报告里的“回滚与恢复验证”一节）："
run python3 -m app.main report "$ID_RB" | grep -A 8 "回滚与恢复验证" || true

echo
echo "----- 步骤 5：trace 回放 -----"
echo "回放第 4 步那个走过回滚的事件：时间线、工具调用、关键决策点一处看全。"
run python3 -m app.main trace "$ID_RB"

echo
echo "----- 步骤 6：评测 harness（15 场景）-----"
echo "子进程调 CLI 跑全部 15 个场景：Top 1 / Top 3、证据完整率、误操作、"
echo "耗时、去重；报告写 eval/report.md 与 eval/report.json。"
run python3 eval/run_eval.py

echo
echo "===== 演示结束 ====="
run python3 -m app.main inject clear
echo "演示服务已回基线。报告在 data/incidents/<id>/report.md，"
echo "评测报告在 eval/report.md，完整实录见 docs/demo-transcript.md。"
