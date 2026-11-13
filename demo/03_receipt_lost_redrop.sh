#!/usr/bin/env bash
# 演示 3：SFTP 认领回执丢失 + 仪器同名重投
#   1) 武装「回执丢失」：认领改名在服务器生效后，客户端按回执随断线丢失处理
#   2) 投放 report.csv（内容 A）并触发采集：改名成功但回执“丢失”，
#      断线重连后重试将真实地报“源路径不存在”
#   3) 在重试前的窗口期内，仪器以同名 report.csv 重投（内容 B）——
#      即旧版本把旧件误标 lost 的故障现场
#   4) 验证：旧件 A 按原来源正常归档（不误判丢失）；新件 B 留在投递目录，
#      未被旧任务认领、下载或删除；再采一轮，B 独立归档
set -euo pipefail
cd "$(dirname "$0")/.."
BASE="${BASE:-http://localhost:8080}"
DC="docker compose"

say() { printf '\n\033[1;34m== %s ==\033[0m\n' "$*"; }

# 触发一轮采集并等待该批次结束（按批次状态判定，无竞态）
collect_and_wait() {
  local bid st
  bid=$(curl -fsS -X POST "$BASE/api/collect" | jq -r '.batch_id // empty')
  if [ -z "$bid" ]; then
    echo "已有轮次在运行，等待其结束…"
    for _ in $(seq 1 120); do
      [ "$(curl -fsS "$BASE/api/progress" 2>/dev/null | jq -r .running)" = "false" ] && return 0
      sleep 1
    done
    echo "超时：采集未结束" >&2; exit 1
  fi
  echo "批次 #$bid 已启动"
  for _ in $(seq 1 120); do
    st=$(curl -fsS "$BASE/api/batches" | jq -r ".batches[] | select(.id==$bid) | .status")
    [ "$st" = "finished" ] && return 0
    sleep 1
  done
  echo "超时：批次 $bid 未结束" >&2; exit 1
}

say "0. 环境检查"
curl -fsS "$BASE/healthz" >/dev/null && echo "collector 正常"

say "1. 武装「回执丢失」演示，仪器投放 report.csv（内容标签 RECEIPT_A）"
curl -fsS -X POST "$BASE/api/demo/lose-receipt" | jq .
$DC exec -T seeder sh /scripts/drop.sh report.csv RECEIPT_A

say "2. 触发采集：改名在服务器生效，但回执按“丢失”注入"
curl -fsS -X POST "$BASE/api/collect" | jq '{started, batch_id}'

say "3. 等待改名生效信号，随后在重试窗口期内同名重投（内容标签 RECEIPT_B）"
MARKER_OK=""
for _ in $(seq 1 60); do
  if $DC exec -T collector test -f /data/flags/receipt_lost 2>/dev/null; then
    MARKER_OK=1; break
  fi
  sleep 0.5
done
[ -n "$MARKER_OK" ] || { echo "✘ 未观察到回执丢失信号（改名可能未被执行）" >&2; exit 1; }
echo "改名已在服务器生效、回执已丢失；仪器此刻同名重投 report.csv（RECEIPT_B）"
$DC exec -T seeder sh /scripts/drop.sh report.csv RECEIPT_B
echo "采集器断线重连后重试改名，将报“源路径不存在”——旧版本在此把旧件误标 lost"

say "4. 等待本轮结束，验证旧件 A 正常归档（未被误判丢失）"
for _ in $(seq 1 120); do
  [ "$(curl -fsS "$BASE/api/progress" 2>/dev/null | jq -r .running)" = "false" ] && break
  sleep 1
done
FID_A=$(curl -fsS "$BASE/api/progress" | jq -r '[.files[] | select(.filename=="report.csv")] | first | .id // empty')
ST_A=$(curl -fsS "$BASE/api/progress" | jq -r "[.files[] | select(.id==$FID_A)] | first | .status")
echo "report.csv 文件 id=$FID_A 状态=$ST_A"
[ "$ST_A" = "archived" ] || { echo "✘ 旧件未正常归档（状态=$ST_A）" >&2; exit 1; }
curl -fsS "$BASE/api/files/$FID_A/download" -o /tmp/demo3_A.csv
grep -q RECEIPT_A /tmp/demo3_A.csv && echo "旧件（id=$FID_A）可下载，内容为 RECEIPT_A ✔"
grep -q RECEIPT_B /tmp/demo3_A.csv && { echo "✘ 旧件内容被同名新件污染！" >&2; exit 1; } || true

say "5. 验证同名新件 B 未被旧任务认领、下载或删除"
if $DC exec -T sftp test -f /home/inst/drop/report.csv; then
  echo "投递目录中的新件 report.csv 仍在，未被旧任务删除 ✔"
else
  echo "✘ 新件被旧任务误删！" >&2; exit 1
fi
if $DC exec -T sftp sh -c 'grep -q RECEIPT_B /home/inst/drop/report.csv'; then
  echo "投递目录中的新件内容确为 RECEIPT_B，未被旧任务改动 ✔"
fi
LEFT_CLAIM=$($DC exec -T sftp sh -c 'find /home/inst/claim -type f 2>/dev/null | wc -l' | tr -d '[:space:]')
[ "$LEFT_CLAIM" = "0" ] && echo "远端认领目录已清空（旧任务只清理自己的认领件）✔"

say "6. 第二轮采集：同名新件 B 独立归档"
collect_and_wait
FID_B=$(curl -fsS "$BASE/api/progress" | jq -r '[.files[] | select(.filename=="report.csv")] | last | .id // empty')
[ -n "$FID_B" ] && [ "$FID_B" != "$FID_A" ] || { echo "✘ 未产生新文件记录" >&2; exit 1; }
curl -fsS "$BASE/api/files/$FID_B/download" -o /tmp/demo3_B.csv
grep -q RECEIPT_B /tmp/demo3_B.csv && echo "新件（id=$FID_B）内容为 RECEIPT_B ✔"
curl -fsS "$BASE/api/files/$FID_A/download" -o /tmp/demo3_A2.csv
cmp -s /tmp/demo3_A.csv /tmp/demo3_A2.csv && echo "旧件（id=$FID_A）内容仍为 RECEIPT_A，未被改变 ✔"
$DC exec -T sftp sh -c 'test -z "$(ls -A /home/inst/drop)" && echo "投递目录已腾空 ✔"'

say "演示 3 完成：回执丢失 + 同名重投下，旧件按原来源独立归档，新件留给后续采集"
