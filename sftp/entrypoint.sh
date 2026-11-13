#!/bin/sh
set -e

# 主机密钥放数据卷外，首次启动生成
ssh-keygen -A

# 卷挂载后再次确保属主正确（chroot 目录本身必须属 root）
chown root:root /home/inst
chmod 755 /home/inst
mkdir -p /home/inst/drop /home/inst/claim
chown instrument:instrument /home/inst/drop /home/inst/claim

exec /usr/sbin/sshd -D -e
