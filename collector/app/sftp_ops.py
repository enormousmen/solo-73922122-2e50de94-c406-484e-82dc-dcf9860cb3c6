"""SFTP 访问层：采集器对源端的全部操作只走 SFTP 协议。

带自动重连：连接类错误（断线）会重连并重试；协议类错误（如文件不存在）
直接抛出，由上层状态机处理。
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import stat
from typing import Any, Awaitable, Callable, Optional

import asyncssh
from asyncssh.sftp import FILEXFER_TYPE_REGULAR

from .config import Settings

CHUNK = 256 * 1024
RECONNECTABLE = (asyncssh.DisconnectError, asyncssh.ConnectionLost, OSError, EOFError)


class SftpClient:
    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self._conn: Optional[asyncssh.SSHClientConnection] = None
        self._sftp: Optional[asyncssh.SFTPClient] = None

    async def connect(self) -> None:
        self._conn = await asyncssh.connect(
            self.cfg.sftp_host,
            port=self.cfg.sftp_port,
            username=self.cfg.sftp_user,
            password=self.cfg.sftp_password,
            known_hosts=None,  # 演示环境关闭主机密钥校验；生产应固定 known_hosts
            connect_timeout=10,
            login_timeout=10,
        )
        self._sftp = await self._conn.start_sftp_client()

    async def close(self) -> None:
        conn, self._conn, self._sftp = self._conn, None, None
        if conn is not None:
            conn.close()
            try:
                await conn.wait_closed()
            except Exception:
                pass

    async def _reconnect(self) -> None:
        await self.close()
        delay = 0.5
        last: Optional[Exception] = None
        for _ in range(5):
            try:
                await self.connect()
                return
            except Exception as exc:  # noqa: BLE001 - 重连阶段一律重试
                last = exc
                await asyncio.sleep(delay)
                delay = min(delay * 2, 8.0)
        raise last  # type: ignore[misc]

    async def run(self, op: Callable[..., Awaitable[Any]], *args: Any) -> Any:
        """执行一个 SFTP 操作；断线时重连并重试（操作本身需可重入）。"""
        last: Optional[Exception] = None
        for attempt in range(4):
            try:
                if self._sftp is None:
                    await self._reconnect()
                return await op(self._sftp, *args)
            except asyncssh.SFTPNoConnection as exc:
                # 连接已断（注意：它是 SFTPError 子类，必须先于它捕获）
                last = exc
                await self._reconnect()
            except asyncssh.SFTPError:
                raise  # 协议层错误（如 NoSuchFile）：与连接无关，不重试
            except RECONNECTABLE as exc:
                last = exc
                await self._reconnect()
        raise last  # type: ignore[misc]

    # ---------- 基础操作 ----------
    async def exists(self, path: str) -> bool:
        try:
            await self.run(lambda s, p: s.stat(p), path)
            return True
        except asyncssh.SFTPNoSuchFile:
            return False

    async def list_csv(self, path: str, suffix: str) -> list[tuple[str, Optional[int]]]:
        """列出投递目录中的目标文件（跳过临时点文件与子目录）。"""
        try:
            entries = await self.run(lambda s, p: s.readdir(p), path)
        except asyncssh.SFTPNoSuchFile:
            return []
        out: list[tuple[str, Optional[int]]] = []
        for e in entries:
            name = e.filename
            if name.startswith(".") or not name.lower().endswith(suffix):
                continue
            ftype = e.attrs.type
            if ftype is not None and ftype != FILEXFER_TYPE_REGULAR:
                continue
            if ftype is None and e.attrs.permissions is not None and not stat.S_ISREG(
                e.attrs.permissions
            ):
                continue
            out.append((name, e.attrs.size))
        return sorted(out)

    async def listdir(self, path: str) -> list[tuple[str, bool]]:
        """返回 (名称, 是否目录)；目录不存在时返回空。"""
        try:
            entries = await self.run(lambda s, p: s.readdir(p), path)
        except asyncssh.SFTPNoSuchFile:
            return []
        out = []
        for e in entries:
            if e.filename in (".", ".."):
                continue
            is_dir = e.attrs.type == asyncssh.sftp.FILEXFER_TYPE_DIRECTORY
            out.append((e.filename, is_dir))
        return out

    async def makedirs(self, path: str) -> None:
        cur = ""
        for part in [p for p in path.split("/") if p]:
            cur += "/" + part
            if not await self.exists(cur):
                try:
                    await self.run(lambda s, p: s.mkdir(p), cur)
                except asyncssh.SFTPFailure:
                    pass  # 已存在

    async def rename_checked(self, src: str, dst: str) -> None:
        """投递目录 -> 认领目录的改名。断线重试时若改名其实已成功，视为成功。"""
        try:
            await self.run(lambda s, a, b: s.rename(a, b), src, dst)
            return
        except asyncssh.SFTPError:
            if await self.exists(dst) and not await self.exists(src):
                return
            raise

    async def remove_checked(self, path: str) -> None:
        """删除远端认领件；已不存在视为成功（幂等）。"""
        try:
            await self.run(lambda s, p: s.remove(p), path)
        except asyncssh.SFTPNoSuchFile:
            pass

    async def download(
        self,
        remote_path: str,
        local_tmp: str,
        progress: Optional[Callable[[int], Awaitable[None]]] = None,
    ) -> tuple[str, int]:
        """流式下载到本地临时文件，边下边算 SHA-256，结束后 fsync。

        断线重试会从头重下（临时文件截断），保证内容完整。
        返回 (sha256_hex, 字节数)。
        """
        sha = hashlib.sha256()
        done = 0

        async def op(sftp: asyncssh.SFTPClient, rp: str) -> None:
            nonlocal sha, done
            sha = hashlib.sha256()
            done = 0
            async with sftp.open(rp, "rb") as rf:
                with open(local_tmp, "wb") as lf:
                    while True:
                        chunk = await rf.read(CHUNK)
                        if not chunk:
                            break
                        lf.write(chunk)
                        sha.update(chunk)
                        done += len(chunk)
                        if progress is not None:
                            await progress(done)
                    lf.flush()
                    os.fsync(lf.fileno())

        await self.run(op, remote_path)
        return sha.hexdigest(), done
