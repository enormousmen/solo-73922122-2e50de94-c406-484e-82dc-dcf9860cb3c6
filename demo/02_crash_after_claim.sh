#!/usr/bin/env bash
# 演示 2：认领后进程退出，重启自动续办
#   1) 投放两个文件，武装「认领后退出」
#   2) 触发采集：采集器把文件改名移入认领目录后立刻退出进程
#   3) 容器重启策略拉起采集器，启动时自动续办（含远端已认领、本地未登记的孤儿）
#   4) 验证：不重复归档、原件可下载、远端认领件全部删除
set -euo pipefail
cd "$(dirname "$0")/.."
BASE="${BASE:-http://localhost:8080}"
DC="docker compose"

say() { printf '\n\033[1;34m== %s ==\033[0m\n' "$*"; }
restart_count() { docker inspect -f '{{.RestartCount}}' csv-collector; }
archived_count() { $DC exec -T collector sh -c 'find /data/archive -type f 2>/dev/null | wc -l' | tr -d '[:space:]'; }

say "0. 基线状态"
RC0=$(restart_count); AC0=$(archived_count)
echo "collector 重启次数=$RC0，已归档文件数=$AC0"

say "1. 仪器投放 crash_a.csv / crash_b.csv"
$DC exec -T seeder sh /scripts/drop.sh crash_a.csv CRASH_DEMO_A
$DC exec -T seeder sh /scripts/drop.sh crash_b.csv CRASH_DEMO_B

say "2. 武装「认领后退出」并触发采集"
curl -fsS -X POST "$BASE/api/demo/crash-after-claim" | jq .
curl -fsS -X POST "$BASE/api/collect" | jq '{started, batch_id}'
echo "等待采集器认领后退出（约 3 秒）…"
sleep 3

say "3. 确认进程已退出并被 docker 重启"
for i in $(seq 1 30); do
  RC1=$(restart_count)
  [ "$RC1" -gt "$RC0" ] && break
  sleep 1
done
echo "重启次数：$RC0 -> $RC1"
[ "$RC1" -gt "$RC0" ] || { echo "✘ 采集器未发生重启" >&2; exit 1; }
for i in $(seq 1 60); do
  curl -fsS "$BASE/healthz" >/dev/null 2>&1 && break
  sleep 1
done
echo "采集器已恢复"

say "4. 等待自动续办完成"
for _ in $(seq 1 120); do
  P=$(curl -fsS "$BASE/api/progress" 2>/dev/null || echo '{"running":true}')
  RUN=$(echo "$P" | jq -r '.running')
  LEFT=$(echo "$P" | jq '[.files[]? | select(.status=="claiming" or .status=="claimed" or .status=="downloading")] | length')
  [ "$RUN" = "false" ] && [ "$LEFT" = "0" ] && break
  sleep 1
done
curl -fsS "$BASE/api/progress" | jq '{running, counts, files: [.files[] | {id, filename, status}]}'

say "5. 验证：无重复归档、原件可下载、远端认领件已清空"
AC1=$(archived_count)
echo "归档文件数：$AC0 -> $AC1（应恰好 +2）"
[ "$((AC1 - AC0))" = "2" ] || { echo "✘ 归档数量异常（可能重复归档）" >&2; exit 1; }

for name in crash_a.csv crash_b.csv; do
  FID=$(curl -fsS "$BASE/api/progress" | jq -r ".files[] | select(.filename==\"$name\") | .id" | head -1)
  curl -fsS "$BASE/api/files/$FID/download" -o "/tmp/demo2_$name"
  grep -q CRASH_DEMO "/tmp/demo2_$name" && echo "$name (id=$FID) 可下载且内容正确 ✔"
done

LEFT_REMOTE=$($DC exec -T sftp sh -c 'find /home/inst/claim -type f 2>/dev/null | wc -l' | tr -d '[:space:]')
echo "远端认领目录剩余文件数：$LEFT_REMOTE"
[ "$LEFT_REMOTE" = "0" ] || { echo "✘ 远端认领件未删净" >&2; exit 1; }

say "演示 2 完成：认领后退出 → 重启自动续办 → 不重复归档、远端清理干净"
