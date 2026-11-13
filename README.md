# 仪器 CSV 采集归档系统

仪器数据员用于归档 SFTP 投递目录中 CSV 的采集系统：归档后腾空接收空间，仪器可再次使用同名文件投递。
采集网页基于 **FastAPI + AsyncSSH + SQLite**，源地址由部署配置固定，采集器对源端**只通过 SFTP 协议**操作。

## 组成

```
┌────────────┐   临时文件改名投放   ┌──────────────┐
│ 仪器/seeder │ ─────────────────▶ │ SFTP 源       │
└────────────┘                    │  /drop  投递目录│
                                  │  /claim 认领目录│
                                  └──────▲───────┘
                                         │ 仅 SFTP
                                  ┌──────┴───────┐        ┌─────────┐
                                  │ collector    │───────▶│ 浏览器   │
                                  │ FastAPI+SQLite│  HTTP  │ 采集/查询│
                                  │ /data 归档原件 │        │ 下载原件 │
                                  └──────────────┘        └─────────┘
```

- **sftp**：本地演示用 SFTP 源（alpine + openssh），模拟仪器侧服务器。`drop` 为投递目录，`claim` 为采集器专用认领目录。
- **seeder**：模拟仪器，先写临时文件再原子改名投放（与源端投放方式一致）。
- **collector**：采集器。不挂载投递卷，只经 SFTP 访问源端；本地 `/data` 卷保存 SQLite 库、归档原件与临时区。`restart: unless-stopped` 保证退出后自动拉起并续办。

## 快速启动

```bash
docker compose up -d --build
# 网页: http://localhost:8080
```

投放一批文件并在网页上点击「开始采集」，或用命令行：

```bash
docker compose exec seeder sh /scripts/drop.sh report.csv LOT_001
curl -X POST http://localhost:8080/api/collect
```

## 演示

四个演示脚本（依赖 `docker compose`、`curl`、`jq`），建议在全新环境（`docker compose down -v && docker compose up -d --build`）上顺序执行：

### 演示 1：同名再次投放 —— `demo/01_same_name_redrop.sh`

1. 投放 `report.csv`（内容 A）并采集归档；
2. 仪器以**同名** `report.csv` 再次投放（内容 B）；
3. 验证旧任务的下载仍是内容 A，投递目录中的新件 B **未被旧任务下载或删除**；
4. 再采一轮，新件独立归档，新旧原件各自可下载。

原理：旧任务只引用自己的认领路径 `/claim/batch_<id>/`，与投递目录中的同名新件完全隔离。

### 演示 2：认领后退出，重启自动续办 —— `demo/02_crash_after_claim.sh`

1. 投放两个文件，通过 `POST /api/demo/crash-after-claim` 武装「认领后退出」；
2. 触发采集：采集器把文件改名移入认领目录后**立刻退出进程**（`os._exit(4)`）；
3. docker 重启策略拉起采集器，启动时自动续办：已登记未完成的继续办，
   **远端已认领但本地未登记的孤儿件**也会被扫描登记、归档；
4. 验证：归档不重复（数量恰好 +2）、原件可下载、远端认领件全部删除。

网页上也有「武装『认领后退出』演示」按钮，效果相同。

### 演示 3：认领回执丢失 + 同名重投 —— `demo/03_receipt_lost_redrop.sh`

复现并验证修复仪器数据员遇到的漏归档故障：**改名已在服务器生效，但回执随断线丢失**，
重试报「源路径不存在」，恰逢仪器同名重投。

1. 通过 `POST /api/demo/lose-receipt` 武装「回执丢失」（下一次认领改名在服务器生效后，
   客户端按回执丢失处理，断线重连后的重试将真实地报「源路径不存在」）；
2. 投放 `report.csv`（内容 A）并触发采集；在重试前的窗口期内仪器**同名重投**（内容 B）；
3. 验证：旧件 A **不被误判 lost**，按原来源正常归档、可下载；投递目录中的新件 B
   未被旧任务认领、下载或删除；
4. 再采一轮，B 独立归档，新旧原件各自可下载。

原理：认领目标路径为本任务专属，**目标已存在即证明改名已成功**，与源路径上是否
出现同名新件无关；回执不明时不直接判丢失。

### 演示 4：历史误标 lost 记录自动恢复 —— `demo/04_lost_recovery.sh`

1. 模拟旧版本故障遗留：库中存在 `lost` 记录，但其认领件真实存在于认领目录；
2. 触发续办：该记录**自动恢复**为已认领并归档（页面显示「已恢复」徽章），
   原件可下载，远端认领件在发布后清理；
3. 重启采集器：不重复归档、不改变已发布原件；
4. 对照：认领件也真正缺失的记录**保持 lost**，明确报告、不可下载（409）。

## 可靠性设计（需求映射）

| 要求 | 实现 |
| --- | --- |
| 源地址固定、只走 SFTP | 源参数只来自环境变量；采集器不挂载投递卷，全部源端操作经 AsyncSSH SFTP |
| 先移入独立认领目录 | 扫描到文件后先登记（状态 `claiming`）再 `rename` 到 `/claim/batch_<id>/`，确认后置 `claimed` |
| 改名回执丢失不误判丢失、不偷新件 | 改名重试前先查认领目标：已存在即视为认领成功且**不再重复改名**（防止 OpenSSH rename 静默覆盖目标、把同名新件偷进认领目录）；目标路径为本任务专属，源路径上的同名新件不影响判定 |
| 同名新件不被旧任务认领/下载/删除 | 旧任务只操作自己的认领路径；下载只服务本地归档；删除只删认领路径；新件留给下一轮 |
| 误标 lost 但认领件仍在的记录自动恢复 | 每次续办先检查 `lost` 记录：认领件仍在则恢复为已认领并归档（标记 `recovered`，页面显示「已恢复」）；认领件也缺失的保持 `lost` 明确报告 |
| 落盘+索引发布后才删远端 | 临时文件下载 → fsync → 原子改名入 `/data/archive/batch_<id>/` → 同事务提交状态与来源索引（文件名/来源路径/大小/SHA-256/时间）→ 才删除远端认领件 |
| 断线/写盘失败/进程退出后重启续办 | 启动与每轮采集前都会 `resume_incomplete`：续办 `claiming/claimed/downloading` 行、恢复误标 `lost` 行、重试待删除件；续办重试认领前比对源文件大小，已被同名新件替换则不认领、按真正缺失报告；SFTP 断线自动重连重试；写盘失败保持 `claimed` 待续办 |
| 远端已认领但本地未登记 | 每次续办扫描认领目录，孤儿件登记入库并归档（崩溃在改名后/登记前、库丢失等场景） |
| 不重复归档、不改变已发布原件 | 认领路径唯一约束；落盘后崩溃的 retry 按 SHA-256 复用已归档原件；删除幂等（远端已缺失视为成功）；已发布行不再重新下载 |
| 删除失败显示待清理、原件仍可下载 | 删除失败置 `cleanup_pending`（页面显示「待清理」），定时任务自动重试；`published/cleanup_pending/archived` 均可下载 |
| 单进程同时只采一轮 | 进程内 asyncio 锁；采集中重复触发返回当前进度（`started:false` + 进度快照） |

### 文件状态机

```
claiming ──▶ claimed ──▶ downloading ──▶ published ──▶ archived
   │            ▲  (断线/写盘失败回退)      │ 删除失败
   │            └──────────────────────┘  ▼
   │                                cleanup_pending(待清理) ──重试──▶ archived
   ▼ (源缺失，且认领目标也不存在)
  lost ──(续办发现认领件仍在)──▶ claimed（标记 recovered，页面显示「已恢复」）
                                   (大小校验失败)
                                     failed
```

- `published`：原件已落盘并发布（可下载），远端认领件待删除。
- `cleanup_pending`：远端删除失败，**待清理**，原件照常可下载，系统自动重试。
- `lost`：源件与认领件均不存在（真正缺失，明确报告）；若续办发现认领件仍在，
  自动恢复为 `claimed` 并归档，`recovered` 置 1。
- 进程重启时 `claiming/claimed/downloading` 全部自动续办。

## 网页与 API

网页 `/`：触发采集、实时逐件进度（状态徽章、字节进度条、SHA-256）、批次历史、原件下载。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/collect` | 触发一轮采集；采集中重复调用返回 `started:false` 与当前进度 |
| GET | `/api/progress` | 当前/最近批次与逐件状态 |
| GET | `/api/batches`、`/api/batches/{id}/files` | 批次历史与批次文件 |
| GET | `/api/files/{id}/download` | 下载原件（发布后任意时刻） |
| GET | `/api/config` | 只读展示非敏感源配置 |
| POST | `/api/demo/crash-after-claim` | 武装「认领后退出」演示 |
| POST | `/api/demo/lose-receipt` | 武装「认领回执丢失」演示（下一次改名在服务器生效后按回执丢失处理） |
| DELETE | `/api/demo/lose-receipt` | 解除「认领回执丢失」武装 |
| GET | `/healthz` | 健康检查 |

## 配置（collector 环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SFTP_HOST/PORT/USER/PASSWORD` | `sftp/22/instrument/instrument` | 源端连接参数（演示值，生产请改密并固定 known_hosts） |
| `SFTP_DROP_DIR` / `SFTP_CLAIM_DIR` | `/drop` / `/claim` | 投递目录 / 认领目录 |
| `DATA_DIR` | `/data` | 本地数据（SQLite、归档、临时区） |
| `CLEANUP_RETRY_INTERVAL` | `15` | 待清理/续办重试间隔（秒） |
| `FILE_SUFFIX` | `.csv` | 采集文件后缀过滤 |
| `DEMO_RECEIPT_LOST_WINDOW` | `6` | 演示：回执丢失注入后、重试前的窗口（秒），供同名重投 |

## 目录结构

```
docker-compose.yml
collector/            # 采集器
  Dockerfile
  app/
    config.py         # 环境配置
    db.py             # SQLite 模式与状态机常量
    sftp_ops.py       # SFTP 访问层（自动重连）
    core.py           # 采集/续办核心
    main.py           # FastAPI 入口
    static/index.html # 网页
sftp/                 # 本地演示 SFTP 源（openssh + chroot）
seeder/scripts/drop.sh# 仪器投放模拟（临时文件改名）
demo/                 # 四个演示脚本
```

## 验证

本仓库代码已通过端到端验证（真实 AsyncSSH SFTP 服务端 + 采集器进程，服务端 rename
按 OpenSSH 语义静默覆盖已有目标）：基本采集与下载、同名再次投放隔离、
**认领回执丢失 + 窗口期同名重投**（旧件正常归档、新件留在投递目录、下轮独立归档）、
**历史误标 lost 记录自动恢复**（含重启不重复归档、真正缺失保持 lost 且不可下载）、
认领后进程退出→重启自动续办（归档不重复）、采集中重复触发（单轮互斥）、
旧版库结构迁移（recovered 列），共 50 项断言全部通过。

另以旧版认领逻辑做了对照实验：同样的回执丢失 + 同名重投注入下，旧逻辑把同名新件
改名偷进认领目录、旧件记录归档新件内容（掉包漏归档），证实故障与修复一一对应。

注意：采集器以**单 worker** 运行（进程内锁保证“同时只采一轮”），请勿用多 worker 启动。
