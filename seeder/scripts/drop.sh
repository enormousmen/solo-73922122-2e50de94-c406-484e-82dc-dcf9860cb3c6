#!/bin/sh
# 模拟仪器投放：先写临时文件，再原子改名为正式文件（与源端投放方式一致）。
# 用法: drop.sh <文件名> <内容标签> [行数]
set -eu

name="${1:?用法: drop.sh <文件名> <内容标签> [行数]}"
label="${2:?缺少内容标签}"
rows="${3:-100}"

tmp="/drop/.tmp.${name}.$$"
trap 'rm -f "$tmp"' EXIT

{
  echo "id,label,value,ts"
  i=1
  while [ "$i" -le "$rows" ]; do
    echo "$i,$label,$((i * 7 % 1000)),$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    i=$((i + 1))
  done
} > "$tmp"
chmod 644 "$tmp"

# 原子投放：同名文件已存在时直接覆盖（仪器会复用同名文件）
mv -f "$tmp" "/drop/$name"
trap - EXIT
echo "已投放 /drop/$name (标签=$label, 行数=$rows)"
