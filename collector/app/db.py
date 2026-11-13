"""SQLite 持久层：批次表 + 文件表（文件表即“来源索引”）。

状态机（files.status）：
  claiming         已登记、远端改名认领尚未确认
  claimed          已认领到 /claim/batch_<id>/ 下，等待下载
  downloading      正在下载（进程重启后按 claimed 处理，重新下载）
  published        原件完整落盘且来源索引已提交（可下载），远端认领件待删除
  cleanup_pending  远端删除失败：待清理（原件仍可下载）
  archived         远端认领件已删除（终态）
  lost             远端源件与认领件都缺失（无法采集，终态）
  failed           数据校验失败等不可自动恢复错误（终态）
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import aiosqlite

# 文件状态
CLAIMING = "claiming"
CLAIMED = "claimed"
DOWNLOADING = "downloading"
PUBLISHED = "published"
CLEANUP_PENDING = "cleanup_pending"
ARCHIVED = "archived"
LOST = "lost"
FAILED = "failed"

ACTIVE_STATES = (CLAIMING, CLAIMED, DOWNLOADING)          # 重启后需要续办
DELETE_PENDING_STATES = (PUBLISHED, CLEANUP_PENDING)      # 本地已发布、远端待删
DOWNLOADABLE_STATES = (PUBLISHED, CLEANUP_PENDING, ARCHIVED)

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    note        TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'running',   -- running / finished
    started_at  TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS files (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id          INTEGER NOT NULL REFERENCES batches(id),
    filename          TEXT NOT NULL,                -- 投递目录中的原始文件名
    origin_path       TEXT NOT NULL,                -- 来源路径（投递目录）
    remote_claim_path TEXT NOT NULL UNIQUE,         -- 认领后的远端路径（幂等键）
    size              INTEGER,
    bytes_done        INTEGER NOT NULL DEFAULT 0,
    sha256            TEXT,
    local_path        TEXT,
    status            TEXT NOT NULL,
    error             TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    published_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_files_batch ON files(batch_id);
CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Db:
    def __init__(self, path: str) -> None:
        self._path = path
        self.conn: Optional[aiosqlite.Connection] = None

    async def open(self) -> None:
        self.conn = await aiosqlite.connect(self._path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    # ---- 批次 ----
    async def create_batch(self, note: str = "") -> int:
        cur = await self.conn.execute(
            "INSERT INTO batches(note, status, started_at) VALUES(?, 'running', ?)",
            (note, utcnow()),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def finish_batch(self, batch_id: int) -> None:
        await self.conn.execute(
            "UPDATE batches SET status='finished', finished_at=? WHERE id=?",
            (utcnow(), batch_id),
        )
        await self.conn.commit()

    async def finalize_stale_batches(self, exclude_id: Optional[int] = None) -> None:
        """把上一轮崩溃遗留的 running 批次标记为 finished（其文件行由续办逻辑处理）。"""
        if exclude_id is None:
            await self.conn.execute(
                "UPDATE batches SET status='finished', finished_at=? WHERE status='running'",
                (utcnow(),),
            )
        else:
            await self.conn.execute(
                "UPDATE batches SET status='finished', finished_at=? "
                "WHERE status='running' AND id<>?",
                (utcnow(), exclude_id),
            )
        await self.conn.commit()

    async def list_batches(self, limit: int = 20) -> list[dict[str, Any]]:
        cur = await self.conn.execute(
            """
            SELECT b.*,
                   (SELECT COUNT(*) FROM files f WHERE f.batch_id = b.id) AS file_count
            FROM batches b ORDER BY b.id DESC LIMIT ?
            """,
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def get_batch(self, batch_id: int) -> Optional[dict[str, Any]]:
        cur = await self.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def latest_batch(self) -> Optional[dict[str, Any]]:
        cur = await self.conn.execute("SELECT * FROM batches ORDER BY id DESC LIMIT 1")
        row = await cur.fetchone()
        return dict(row) if row else None

    # ---- 文件 ----
    async def insert_file(
        self,
        batch_id: int,
        filename: str,
        origin_path: str,
        claim_path: str,
        size: Optional[int],
        status: str = CLAIMING,
    ) -> int:
        now = utcnow()
        cur = await self.conn.execute(
            """
            INSERT INTO files(batch_id, filename, origin_path, remote_claim_path,
                              size, status, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (batch_id, filename, origin_path, claim_path, size, status, now, now),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def update_file(self, file_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = utcnow()
        cols = ", ".join(f"{k}=?" for k in fields)
        await self.conn.execute(
            f"UPDATE files SET {cols} WHERE id=?", (*fields.values(), file_id)
        )
        await self.conn.commit()

    async def get_file(self, file_id: int) -> Optional[dict[str, Any]]:
        cur = await self.conn.execute("SELECT * FROM files WHERE id=?", (file_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def find_by_claim_path(self, claim_path: str) -> Optional[dict[str, Any]]:
        cur = await self.conn.execute(
            "SELECT * FROM files WHERE remote_claim_path=?", (claim_path,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def find_published_by_hash(
        self, sha256: str, filename: str
    ) -> Optional[dict[str, Any]]:
        cur = await self.conn.execute(
            """
            SELECT * FROM files
            WHERE sha256=? AND filename=? AND status IN (?,?,?)
            ORDER BY id LIMIT 1
            """,
            (sha256, filename, PUBLISHED, CLEANUP_PENDING, ARCHIVED),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def files_in_states(self, states: Iterable[str]) -> list[dict[str, Any]]:
        marks = ",".join("?" for _ in states)
        cur = await self.conn.execute(
            f"SELECT * FROM files WHERE status IN ({marks}) ORDER BY id",
            tuple(states),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def list_files(self, batch_id: Optional[int] = None) -> list[dict[str, Any]]:
        if batch_id is None:
            cur = await self.conn.execute("SELECT * FROM files ORDER BY id")
        else:
            cur = await self.conn.execute(
                "SELECT * FROM files WHERE batch_id=? ORDER BY id", (batch_id,)
            )
        return [dict(r) for r in await cur.fetchall()]

    async def batch_status_counts(self, batch_id: int) -> dict[str, int]:
        cur = await self.conn.execute(
            "SELECT status, COUNT(*) c FROM files WHERE batch_id=? GROUP BY status",
            (batch_id,),
        )
        return {r["status"]: r["c"] for r in await cur.fetchall()}
