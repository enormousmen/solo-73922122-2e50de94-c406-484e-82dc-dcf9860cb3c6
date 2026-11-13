#!/usr/bin/env bash
# 演示 1：同名文件再次投放
#   1) 投放 report.csv（内容 A）并采集归档
#   2) 仪器以同名 report.csv 再次投放（内容 B）
#   3) 验证：旧任务的下载仍是内容 A；投递目录里的新件 B 未被旧任务下载或删除
#   4) 再采一轮，验证新件 B 独立归档、可下载
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

say "1. 仪器投放 report.csv（内容标签 A）"
$DC exec -T seeder sh /scripts/drop.sh report.csv BATCH_A

say "2. 触发第一轮采集"
collect_and_wait
FID_A=$(curl -fsS "$BASE/api/progress" | jq -r '.files[] | select(.filename=="report.csv") | .id' | head -1)
echo "report.csv 已归档，文件 id=$FID_A"
curl -fsS "$BASE/api/files/$FID_A/download" -o /tmp/demo1_A.csv
grep -q BATCH_A /tmp/demo1_A.csv && echo "下载内容校验：含 BATCH_A ✔"

say "3. 仪器以同名 report.csv 再次投放（内容标签 B）"
$DC exec -T seeder sh /scripts/drop.sh report.csv BATCH_B
$DC exec -T sftp sh -c 'ls -l /home/inst/drop/'

say "4. 验证旧任务不受同名新件影响"
curl -fsS "$BASE/api/files/$FID_A/download" -o /tmp/demo1_A2.csv
if cmp -s /tmp/demo1_A.csv /tmp/demo1_A2.csv; then
  echo "旧文件（id=$FID_A）下载内容不变，仍是 BATCH_A ✔"
else
  echo "✘ 旧文件内容被同名新件污染！" >&2; exit 1
fi
if $DC exec -T sftp test -f /home/inst/drop/report.csv; then
  echo "投递目录中的新件 report.csv 仍在，未被旧任务删除 ✔"
else
  echo "✘ 新件被旧任务误删！" >&2; exit 1
fi

say "5. 第二轮采集：同名新件独立归档"
collect_and_wait
FID_B=$(curl -fsS "$BASE/api/progress" | jq -r '.files[] | select(.filename=="report.csv") | .id' | tail -1)
[ "$FID_B" = "$FID_A" ] && { echo "✘ 未产生新文件记录" >&2; exit 1; }
curl -fsS "$BASE/api/files/$FID_B/download" -o /tmp/demo1_B.csv
grep -q BATCH_B /tmp/demo1_B.csv && echo "新文件（id=$FID_B）内容为 BATCH_B ✔"
curl -fsS "$BASE/api/files/$FID_A/download" -o /tmp/demo1_A3.csv
grep -q BATCH_A /tmp/demo1_A3.csv && echo "旧文件（id=$FID_A）内容仍为 BATCH_A ✔"
$DC exec -T sftp sh -c 'test -z "$(ls -A /home/inst/drop)" && echo "投递目录已腾空 ✔"'

say "演示 1 完成：同名再投放互不影响，新旧原件各自可下载"
