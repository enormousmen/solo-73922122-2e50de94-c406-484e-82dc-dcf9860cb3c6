"""采集核心：单进程单轮采集、认领-落盘-发布-删除 状态机、断点续办。

顺序保证：
  1. 先把投递目录的文件改名移入独立认领目录 /claim/batch_<id>/（先登记后改名，
     改名确认后才标记 claimed）。此后投递的同名新件位于投递目录，旧任务只引用
     自己的认领路径，永远不会下载或删除新件。
  2. 改名回执可能随断线丢失（服务器已执行、响应未收到）：重试报“源路径不存在”
     时，只要认领目标已存在即视为认领成功——目标路径为本任务专属；源路径上的
     同名新件与本任务无关，不得据此把旧件误判为 lost。
  3. 原件完整落盘（临时文件 + fsync + 原子改名）并与来源索引（文件名/来源路径/
     大小/SHA-256/时间）在同一事务发布后，才删除远端认领件。
  4. 删除失败仅标记 cleanup_pending（待清理），已发布原件照常可下载。
  5. 任何中断（断线/写盘失败/进程退出）后，resume_incomplete() 负责续办：
     包括远端已认领但本地尚未登记的孤儿文件，以及曾被误标 lost 但认领件仍在
     的记录（自动恢复为已认领并归档，标记 recovered）；认领路径有唯一约束，
     落盘有同哈希复用，不会重复归档。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Optional

import asyncssh

from . import db as dbm
from .config import Settings

logger = logging.getLogger("collector")

CHUNK_REPORT = 512 * 1024  # 进度上报粒度


def sanitize_filename(name: str) -> str:
    name = name.replace("/", "_").replace("\\", "_")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return name if name not in ("", ".", "..") else "file"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def unique_path(path: Path) -> Path:
    n = 1
    while True:
        cand = path.with_name(f"{path.stem}.{n}{path.suffix}")
        if not cand.exists():
            return cand
        n += 1


class Ctx:
    """运行期上下文（在 main.py 中组装）。"""

    def __init__(self, cfg: Settings, db: dbm.Db, sftp) -> None:
        self.cfg = cfg
        self.db = db
        self.sftp = sftp
        self.lock = asyncio.Lock()
        self.current_task: Optional[asyncio.Task] = None


# ---------------------------------------------------------------- 单件处理


async def delete_remote(ctx: Ctx, row: dict) -> None:
    """发布完成后删除远端认领件；失败仅标记待清理。"""
    db = ctx.db
    try:
        await ctx.sftp.remove_checked(row["remote_claim_path"])
        await db.update_file(row["id"], status=dbm.ARCHIVED, error=None)
    except Exception as exc:  # noqa: BLE001 - 删除失败不影响已发布原件
        logger.warning("远端删除失败 id=%s: %s", row["id"], exc)
        await db.update_file(
            row["id"], status=dbm.CLEANUP_PENDING, error=f"远端删除失败，待清理: {exc}"
        )


async def process_file(ctx: Ctx, row: dict) -> None:
    """下载 -> 校验 -> 原子落盘 -> 与来源索引一起发布 -> 删除远端认领件。"""
    cfg, db = ctx.cfg, ctx.db
    fid = row["id"]
    await db.update_file(fid, status=dbm.DOWNLOADING, bytes_done=0, error=None)
    tmp = cfg.tmp_dir / f"{fid}.part"

    last = {"n": 0}

    async def report(done: int) -> None:
        if done - last["n"] >= CHUNK_REPORT:
            last["n"] = done
            await db.update_file(fid, bytes_done=done)

    try:
        sha, size = await ctx.sftp.download(row["remote_claim_path"], str(tmp), report)
    except Exception as exc:  # noqa: BLE001 - 断线/写盘失败：保持可续办状态
        logger.warning("下载失败 id=%s: %s", fid, exc)
        tmp.unlink(missing_ok=True)
        await db.update_file(fid, status=dbm.CLAIMED, error=f"下载失败(待续办): {exc}")
        return

    if row["size"] is not None and size != row["size"]:
        tmp.unlink(missing_ok=True)
        await db.update_file(
            fid,
            status=dbm.FAILED,
            error=f"大小不一致: 认领时 {row['size']} 字节, 实收 {size} 字节",
        )
        return

    batch_dir = cfg.archive_dir / f"batch_{row['batch_id']}"
    batch_dir.mkdir(parents=True, exist_ok=True)
    final = batch_dir / sanitize_filename(row["filename"])
    if final.exists():
        if sha256_file(final) == sha:
            # 上次崩溃在“落盘后、登记前”：同内容已归档，直接复用，不重复归档
            tmp.unlink(missing_ok=True)
        else:
            final = unique_path(final)
            os.replace(tmp, final)
    else:
        os.replace(tmp, final)
    fsync_dir(batch_dir)

    # 原件落盘 + 来源索引，一次提交同时发布
    await db.update_file(
        fid,
        status=dbm.PUBLISHED,
        sha256=sha,
        size=size,
        bytes_done=size,
        local_path=str(final),
        published_at=dbm.utcnow(),
        error=None,
    )
    await delete_remote(ctx, {**row, "id": fid, "remote_claim_path": row["remote_claim_path"]})


# ---------------------------------------------------------------- 续办


async def resume_incomplete(ctx: Ctx, current_batch_id: Optional[int]) -> None:
    """续办所有未完成事项（启动时、每轮采集前、定时清理都会调用）。"""
    db, sftp = ctx.db, ctx.sftp

    # 0) 曾被误标 lost 但认领件仍在的记录：认领件存在即证明当年改名已成功
    #    （典型成因：改名回执丢失，重试报源路径不存在，恰逢仪器同名重投，
    #    旧版存在性检查把旧件误判为 lost）。自动恢复为已认领并续办归档；
    #    认领件也不存在的保持 lost——真正缺失，明确报告。
    for row in await db.files_in_states((dbm.LOST,)):
        if await sftp.exists(row["remote_claim_path"]):
            logger.warning(
                "认领件仍在，恢复误标丢失的记录 id=%s: %s",
                row["id"], row["remote_claim_path"],
            )
            await db.update_file(
                row["id"],
                status=dbm.CLAIMED,
                recovered=1,
                error="认领件仍在，已从误标丢失自动恢复",
            )
            await process_file(ctx, await db.get_file(row["id"]))

    # 1) 已登记但未完成的行
    for row in await db.files_in_states(dbm.ACTIVE_STATES):
        if row["status"] == dbm.CLAIMING:
            # 崩溃发生在“登记后、改名确认前”：先确认认领件是否已在远端
            if await sftp.exists(row["remote_claim_path"]):
                await db.update_file(row["id"], status=dbm.CLAIMED)
            elif await sftp.exists(row["origin_path"]):
                # 认领件不存在 => 改名从未成功；但源路径上的文件可能已被仪器
                # 同名重投替换（登记到续办可能间隔很久）。大小与登记时不符则不
                # 认领：旧件内容已不可得，按真正缺失明确报告；新件留给后续采集。
                if row["size"] is not None:
                    cur_size = await sftp.size_of(row["origin_path"])
                    if cur_size is not None and cur_size != row["size"]:
                        await db.update_file(
                            row["id"], status=dbm.LOST,
                            error=f"源文件在认领前已被同名新件替换"
                                  f"（登记 {row['size']} 字节，现 {cur_size} 字节）",
                        )
                        continue
                try:
                    # 认领目录可能因极端情况缺失，改名前兜底创建
                    await sftp.makedirs(row["remote_claim_path"].rsplit("/", 1)[0])
                    await sftp.rename_checked(row["origin_path"], row["remote_claim_path"])
                    await db.update_file(row["id"], status=dbm.CLAIMED)
                except Exception as exc:  # noqa: BLE001
                    await db.update_file(row["id"], error=f"认领重试失败: {exc}")
                    continue
            else:
                await db.update_file(
                    row["id"], status=dbm.LOST, error="源文件与认领件均不存在"
                )
                continue
        fresh = await db.get_file(row["id"])
        if fresh["status"] in (dbm.CLAIMED, dbm.DOWNLOADING):
            await process_file(ctx, fresh)

    # 2) 远端已认领但本地未登记的孤儿（如崩溃发生在改名后、登记前，或库被重置）
    await scan_orphans(ctx)

    # 3) 已发布但远端未删除的，重试删除（含 cleanup_pending）
    for row in await db.files_in_states(dbm.DELETE_PENDING_STATES):
        await delete_remote(ctx, row)

    # 4) 上一轮崩溃遗留的 running 批次收尾
    await db.finalize_stale_batches(exclude_id=current_batch_id)


async def scan_orphans(ctx: Ctx) -> None:
    cfg, db, sftp = ctx.cfg, ctx.db, ctx.sftp
    recovery_batch: dict[str, int] = {}

    async def recovery_batch_id() -> int:
        if "id" not in recovery_batch:
            recovery_batch["id"] = await db.create_batch(note="recovery-orphan")
        return recovery_batch["id"]

    async def register(claim_path: str, filename: str, batch_hint: Optional[int]) -> None:
        if await db.find_by_claim_path(claim_path):
            return  # 已登记（唯一约束兜底），不重复归档
        batch_id = batch_hint
        if batch_id is None or await db.get_batch(batch_id) is None:
            batch_id = await recovery_batch_id()
        size = None
        try:
            attrs = await sftp.run(lambda s, p: s.stat(p), claim_path)
            size = attrs.size
        except Exception:  # noqa: BLE001
            pass
        fid = await db.insert_file(
            batch_id,
            filename,
            origin_path=f"{cfg.drop_dir}/{filename}",
            claim_path=claim_path,
            size=size,
            status=dbm.CLAIMED,
        )
        logger.info("登记远端孤儿认领件: %s -> file id=%s", claim_path, fid)
        await process_file(ctx, await db.get_file(fid))

    for name, is_dir in await sftp.listdir(cfg.claim_dir):
        if is_dir and name.startswith("batch_"):
            m = re.fullmatch(r"batch_(\d+)", name)
            hint = int(m.group(1)) if m else None
            sub = f"{cfg.claim_dir}/{name}"
            for fname, f_is_dir in await sftp.listdir(sub):
                if not f_is_dir:
                    await register(f"{sub}/{fname}", fname, hint)
        elif not is_dir:
            await register(f"{cfg.claim_dir}/{name}", name, None)


# ---------------------------------------------------------------- 一轮采集


async def scan_and_claim(ctx: Ctx, batch_id: int) -> int:
    """扫描投递目录并逐件登记+改名认领。返回本轮新认领件数。"""
    cfg, db, sftp = ctx.cfg, ctx.db, ctx.sftp
    found = await sftp.list_csv(cfg.drop_dir, cfg.file_suffix)
    if not found:
        return 0
    claim_root = f"{cfg.claim_dir}/batch_{batch_id}"
    await sftp.makedirs(claim_root)
    claimed = 0
    for name, size in found:
        origin = f"{cfg.drop_dir}/{name}"
        claim_path = f"{claim_root}/{name}"
        fid = await db.insert_file(batch_id, name, origin, claim_path, size)
        try:
            await sftp.rename_checked(origin, claim_path)
        except asyncssh.SFTPNoSuchFile:
            # rename_checked 已确认认领目标也不存在：源件真正缺失，明确报告
            await db.update_file(fid, status=dbm.LOST, error="认领时源文件已不存在")
            continue
        except Exception as exc:  # noqa: BLE001 - 保持 claiming，留待续办重试
            await db.update_file(fid, error=f"认领失败(待重试): {exc}")
            continue
        await db.update_file(fid, status=dbm.CLAIMED)
        claimed += 1
    return claimed


async def run_round_inner(ctx: Ctx, batch_id: int) -> None:
    cfg, db = ctx.cfg, ctx.db
    try:
        # 先续办历史遗留，再采新件
        await resume_incomplete(ctx, current_batch_id=batch_id)
        claimed = await scan_and_claim(ctx, batch_id)

        # 演示钩子：认领完成后立刻退出进程，验证重启自动续办
        if claimed > 0 and cfg.crash_flag.exists():
            cfg.crash_flag.unlink(missing_ok=True)
            logger.error("演示：认领 %d 件后进程主动退出 (crash_after_claim)", claimed)
            os._exit(4)

        for row in await db.list_files(batch_id):
            if row["status"] in (dbm.CLAIMED, dbm.DOWNLOADING):
                await process_file(ctx, row)

        # 本轮发布但删除失败的，立即再试一次
        for row in await db.files_in_states(dbm.DELETE_PENDING_STATES):
            await delete_remote(ctx, row)
    finally:
        await db.finish_batch(batch_id)


async def run_round(ctx: Ctx, batch_id: int) -> None:
    """一轮完整采集；全局锁保证单进程同时只采一轮。"""
    async with ctx.lock:
        try:
            await run_round_inner(ctx, batch_id)
        except Exception:
            logger.exception("采集轮次 %s 异常结束（遗留事项将由续办接管）", batch_id)


async def startup_housekeeping(ctx: Ctx) -> None:
    """启动：等 SFTP 就绪 -> 续办中断事项 -> 投递目录有货则自动采一轮。"""
    cfg = ctx.cfg
    for attempt in range(60):
        try:
            await ctx.sftp.connect()
            break
        except Exception as exc:  # noqa: BLE001
            if attempt == 59:
                logger.error("SFTP 源不可达，启动续办推迟到触发时: %s", exc)
                return
            await asyncio.sleep(1)
    async with ctx.lock:
        try:
            await resume_incomplete(ctx, current_batch_id=None)
            if await ctx.sftp.list_csv(cfg.drop_dir, cfg.file_suffix):
                batch_id = await ctx.db.create_batch(note="startup-auto")
                await run_round_inner(ctx, batch_id)
        except Exception:
            logger.exception("启动续办失败，将由定时清理/手动触发继续")


async def janitor_loop(ctx: Ctx, interval: float) -> None:
    """定时续办：重试待清理删除与未完成件（与采集轮次互斥）。"""
    while True:
        await asyncio.sleep(interval)
        if ctx.lock.locked():
            continue
        async with ctx.lock:
            try:
                await resume_incomplete(ctx, current_batch_id=None)
            except Exception:
                logger.warning("定时续办失败（下周期重试）", exc_info=True)
