"""FastAPI 入口：网页触发采集、查询逐件进度、下载原件。"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from . import core
from .config import settings
from .db import Db, DOWNLOADABLE_STATES
from .sftp_ops import SftpClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("collector.main")

STATIC = Path(__file__).parent / "static" / "index.html"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    # 上次进程退出可能遗留半截临时文件，统一清理（正式归档件不受影响）
    for p in settings.tmp_dir.glob("*.part"):
        p.unlink(missing_ok=True)
    # 演示标记属一次性信号，随进程退出失效
    settings.receipt_lost_marker.unlink(missing_ok=True)

    db = Db(str(settings.db_path))
    await db.open()
    ctx = core.Ctx(settings, db, SftpClient(settings))
    app.state.ctx = ctx

    startup = asyncio.create_task(core.startup_housekeeping(ctx))
    janitor = asyncio.create_task(core.janitor_loop(ctx, settings.cleanup_retry_interval))
    logger.info(
        "collector 启动: sftp=%s:%s drop=%s claim=%s data=%s",
        settings.sftp_host, settings.sftp_port, settings.drop_dir,
        settings.claim_dir, settings.data_dir,
    )
    try:
        yield
    finally:
        for t in (startup, janitor, ctx.current_task):
            if t is not None:
                t.cancel()
        await ctx.sftp.close()
        await db.close()


app = FastAPI(title="仪器 CSV 采集归档", lifespan=lifespan)


def ctx_of(request: Request) -> core.Ctx:
    return request.app.state.ctx


# ---------------------------------------------------------------- 页面


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


# ---------------------------------------------------------------- 采集触发


@app.post("/api/collect")
async def collect(request: Request):
    """触发一轮采集；已有轮次在进行时，不重复采集，直接返回当前进度。"""
    ctx = ctx_of(request)
    task = ctx.current_task
    if task is not None and not task.done():
        return JSONResponse(
            {"started": False, "reason": "already_running",
             "progress": await build_progress(ctx)}
        )

    batch_id = await ctx.db.create_batch(note="manual")
    ctx.current_task = asyncio.create_task(core.run_round(ctx, batch_id))
    ctx.current_task.add_done_callback(_log_task_result)
    return {"started": True, "batch_id": batch_id,
            "progress": await build_progress(ctx)}


def _log_task_result(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("采集任务异常: %r", exc)


# ---------------------------------------------------------------- 进度查询


async def build_progress(ctx: core.Ctx) -> dict:
    db = ctx.db
    batch = await db.latest_batch()
    files = await db.list_files(batch["id"]) if batch else []
    for f in files:
        f["downloadable"] = bool(
            f["status"] in DOWNLOADABLE_STATES and f["local_path"]
        )
    return {
        "running": ctx.lock.locked(),
        "crash_armed": settings.crash_flag.exists(),
        "lose_receipt_armed": bool(ctx.sftp.lose_next_receipt),
        "batch": batch,
        "counts": await db.batch_status_counts(batch["id"]) if batch else {},
        "files": files,
        "batches": await db.list_batches(20),
    }


@app.get("/api/progress")
async def progress(request: Request):
    return await build_progress(ctx_of(request))


@app.get("/api/config")
async def config():
    """非敏感配置展示（源地址由部署固定，页面只读）。"""
    return {
        "sftp_host": settings.sftp_host,
        "sftp_port": settings.sftp_port,
        "sftp_user": settings.sftp_user,
        "drop_dir": settings.drop_dir,
        "claim_dir": settings.claim_dir,
        "file_suffix": settings.file_suffix,
    }


@app.get("/api/batches")
async def batches(request: Request):
    return {"batches": await ctx_of(request).db.list_batches(50)}


@app.get("/api/batches/{batch_id}/files")
async def batch_files(batch_id: int, request: Request):
    ctx = ctx_of(request)
    if await ctx.db.get_batch(batch_id) is None:
        raise HTTPException(404, "批次不存在")
    files = await ctx.db.list_files(batch_id)
    for f in files:
        f["downloadable"] = bool(
            f["status"] in DOWNLOADABLE_STATES and f["local_path"]
        )
    return {"files": files}


# ---------------------------------------------------------------- 原件下载


@app.get("/api/files/{file_id}/download")
async def download(file_id: int, request: Request):
    ctx = ctx_of(request)
    row = await ctx.db.get_file(file_id)
    if row is None:
        raise HTTPException(404, "文件不存在")
    if row["status"] not in DOWNLOADABLE_STATES or not row["local_path"]:
        raise HTTPException(409, f"当前状态不可下载: {row['status']}")
    path = Path(row["local_path"])
    if not path.is_file():
        raise HTTPException(410, "本地原件缺失")
    return FileResponse(path, filename=row["filename"], media_type="text/csv")


# ---------------------------------------------------------------- 演示钩子


@app.post("/api/demo/crash-after-claim")
async def arm_crash():
    """武装“认领后退出”演示：下一轮采集在认领完成、尚未下载时退出进程，
    由容器重启策略拉起后自动续办。"""
    settings.crash_flag.parent.mkdir(parents=True, exist_ok=True)
    settings.crash_flag.write_text("armed\n")
    return {"armed": True}


@app.delete("/api/demo/crash-after-claim")
async def disarm_crash():
    settings.crash_flag.unlink(missing_ok=True)
    return {"armed": False}


@app.post("/api/demo/lose-receipt")
async def arm_lose_receipt(request: Request):
    """武装“认领回执丢失”演示：下一次认领改名在服务器生效后，客户端按回执
    随断线丢失处理（重试将报源路径不存在），用于验证同名重投下的认领恢复。"""
    ctx = ctx_of(request)
    settings.receipt_lost_marker.unlink(missing_ok=True)
    ctx.sftp.lose_next_receipt = True
    return {"armed": True}


@app.delete("/api/demo/lose-receipt")
async def disarm_lose_receipt(request: Request):
    ctx_of(request).sftp.lose_next_receipt = False
    settings.receipt_lost_marker.unlink(missing_ok=True)
    return {"armed": False}
