#!/usr/bin/env bash
# 演示 4：历史误标 lost 记录的自动恢复
#   1) 模拟旧版本故障遗留：认领件真实存在于认领目录，但记录被误标 lost
#      （当年改名回执丢失 + 同名重投 触发旧版误判，重启后认领件无人认领）
#   2) 触发续办：认领件仍在的记录自动恢复为已认领并归档（标记“已恢复”），
#      浏览器可查看恢复状态并下载原件
#   3) 重启采集器：不重复归档、不改变已发布原件
#   4) 对照：认领件也真正缺失的记录保持 lost，明确报告、不可下载
set -euo pipefail
cd "$(dirname "$0")/.."
BASE="${BASE:-http://localhost:8080}"
DC="docker compose"

say() { printf '\n\033[1;34m== %s ==\033[0m\n' "$*"; }
archived_count() { $DC exec -T collector sh -c 'find /data/archive -type f 2>/dev/null | wc -l' | tr -d '[:space:]'; }

say "0. 环境检查"
curl -fsS "$BASE/healthz" >/dev/null && echo "collector 正常"
AC0=$(archived_count); echo "当前已归档文件数=$AC0"

say "1. 构造旧版本遗留：库中两条 lost 记录，其一认领件真实存在"
IDS=$($DC exec -T collector python - <<'PY'
import sqlite3
from datetime import datetime, timezone
now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
con = sqlite3.connect("/data/app.db", timeout=30)
cur = con.execute(
    "INSERT INTO batches(note, status, started_at, finished_at) VALUES(?,?,?,?)",
    ("legacy-mislabeled", "finished", now, now),
)
bid = cur.lastrowid
def add_file(name, err):
    c = con.execute(
        "INSERT INTO files(batch_id, filename, origin_path, remote_claim_path,"
        " size, status, created_at, updated_at, error)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (bid, name, f"/drop/{name}", f"/claim/batch_{bid}/{name}",
         None, "lost", now, now, err),
    )
    return c.lastrowid
kept = add_file("legacy_kept.csv", "旧版本误标：认领时源文件已不存在")
gone = add_file("truly_gone.csv", "旧版本误标：认领时源文件已不存在")
con.commit()
print(f"BATCH_ID={bid}")
print(f"KEPT_ID={kept}")
print(f"GONE_ID={gone}")
con.close()
PY
)
echo "$IDS"
eval "$(echo "$IDS" | grep -E '^(BATCH_ID|KEPT_ID|GONE_ID)=')"

say "2. 把“当年已认领成功”的原件放回认领目录（模拟改名其实已成功）"
$DC exec -T sftp sh -c "
  mkdir -p /home/inst/claim/batch_$BATCH_ID &&
  printf 'id,label,value\n1,LEGACY_KEPT,42\n2,LEGACY_KEPT,43\n' > /home/inst/claim/batch_$BATCH_ID/legacy_kept.csv &&
  chown -R instrument:instrument /home/inst/claim/batch_$BATCH_ID"
$DC exec -T sftp ls -l /home/inst/claim/batch_$BATCH_ID

say "3. 触发续办（一轮采集会先续办历史遗留）"
curl -fsS -X POST "$BASE/api/collect" | jq '{started, batch_id}'
for _ in $(seq 1 120); do
  ST=$(curl -fsS "$BASE/api/batches/$BATCH_ID/files" | jq -r "[.files[] | select(.id==$KEPT_ID)] | first | .status")
  [ "$ST" = "archived" ] && break
  sleep 1
done
say "4. 验证：误标记录已自动恢复并归档"
curl -fsS "$BASE/api/batches/$BATCH_ID/files" | jq '.files[] | {id, filename, status, recovered, error}'
ST=$(curl -fsS "$BASE/api/batches/$BATCH_ID/files" | jq -r "[.files[] | select(.id==$KEPT_ID)] | first | .status")
REC=$(curl -fsS "$BASE/api/batches/$BATCH_ID/files" | jq -r "[.files[] | select(.id==$KEPT_ID)] | first | .recovered")
[ "$ST" = "archived" ] || { echo "✘ 误标记录未恢复（状态=$ST）" >&2; exit 1; }
[ "$REC" = "1" ] || { echo "✘ 缺少“已恢复”标记" >&2; exit 1; }
echo "legacy_kept.csv (id=$KEPT_ID)：lost → archived，已恢复标记 recovered=1 ✔"
curl -fsS "$BASE/api/files/$KEPT_ID/download" -o /tmp/demo4_kept.csv
grep -q LEGACY_KEPT /tmp/demo4_kept.csv && echo "原件可下载，内容为 LEGACY_KEPT ✔"
if $DC exec -T sftp test -f /home/inst/claim/batch_$BATCH_ID/legacy_kept.csv 2>/dev/null; then
  echo "✘ 远端认领件未清理" >&2; exit 1
else
  echo "远端认领件已在发布后清理 ✔"
fi

say "5. 对照：认领件也缺失的记录保持 lost，明确报告、不可下载"
ST2=$(curl -fsS "$BASE/api/batches/$BATCH_ID/files" | jq -r "[.files[] | select(.id==$GONE_ID)] | first | .status")
[ "$ST2" = "lost" ] || { echo "✘ 真正缺失的记录未保持 lost（状态=$ST2）" >&2; exit 1; }
CODE=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/files/$GONE_ID/download")
[ "$CODE" = "409" ] || { echo "✘ 缺失记录竟可下载（HTTP $CODE）" >&2; exit 1; }
echo "truly_gone.csv (id=$GONE_ID)：保持 lost，下载返回 409 ✔"

say "6. 重启采集器：不重复归档、不改变已发布原件"
AC1=$(archived_count)
$DC restart collector >/dev/null
for _ in $(seq 1 60); do
  curl -fsS "$BASE/healthz" >/dev/null 2>&1 && break
  sleep 1
done
for _ in $(seq 1 60); do
  [ "$(curl -fsS "$BASE/api/progress" 2>/dev/null | jq -r .running)" = "false" ] && break
  sleep 1
done
AC2=$(archived_count)
echo "归档文件数：重启前 $AC1 → 重启后 $AC2"
[ "$AC1" = "$AC2" ] || { echo "✘ 重启后归档数量变化（重复归档？）" >&2; exit 1; }
ST3=$(curl -fsS "$BASE/api/batches/$BATCH_ID/files" | jq -r "[.files[] | select(.id==$KEPT_ID)] | first | .status")
[ "$ST3" = "archived" ] || { echo "✘ 重启后已恢复记录状态异常（$ST3）" >&2; exit 1; }
curl -fsS "$BASE/api/files/$KEPT_ID/download" -o /tmp/demo4_kept2.csv
cmp -s /tmp/demo4_kept.csv /tmp/demo4_kept2.csv && echo "重启后原件内容不变，仍可下载 ✔"

say "演示 4 完成：误标 lost 且认领件仍在的记录自动恢复归档；真正缺失的明确报告"
