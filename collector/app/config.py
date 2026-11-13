"""集中式配置：源地址等参数只来自环境变量，网页不接受任何源端参数。"""
from __future__ import annotations

import os
from pathlib import Path


class Settings:
    def __init__(self) -> None:
        # SFTP 源（由部署配置固定，采集器只通过 SFTP 协议访问源端）
        self.sftp_host: str = os.getenv("SFTP_HOST", "sftp")
        self.sftp_port: int = int(os.getenv("SFTP_PORT", "22"))
        self.sftp_user: str = os.getenv("SFTP_USER", "instrument")
        self.sftp_password: str = os.getenv("SFTP_PASSWORD", "instrument")
        # 远端目录：投递目录（仪器写入）与认领目录（采集器专用）
        self.drop_dir: str = os.getenv("SFTP_DROP_DIR", "/drop")
        self.claim_dir: str = os.getenv("SFTP_CLAIM_DIR", "/claim")
        # 本地数据目录
        self.data_dir: Path = Path(os.getenv("DATA_DIR", "/data"))
        # 待清理（远端删除失败）重试间隔，秒
        self.cleanup_retry_interval: float = float(os.getenv("CLEANUP_RETRY_INTERVAL", "15"))
        # 文件名过滤
        self.file_suffix: str = os.getenv("FILE_SUFFIX", ".csv")
        # 演示：认领改名“回执丢失”后、重试前的等待窗口（秒），供仪器同名重投
        self.receipt_lost_window: float = float(os.getenv("DEMO_RECEIPT_LOST_WINDOW", "6"))

    # ---- 派生路径 ----
    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def archive_dir(self) -> Path:
        return self.data_dir / "archive"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def flags_dir(self) -> Path:
        return self.data_dir / "flags"

    @property
    def crash_flag(self) -> Path:
        # 演示用：存在该标记文件时，采集器在“认领完成、尚未下载”时主动退出进程
        return self.flags_dir / "crash_after_claim"

    @property
    def receipt_lost_marker(self) -> Path:
        # 演示用：改名已在服务器生效、回执按“丢失”注入时创建该标记，
        # 演示脚本据此在重试前的窗口期内同名重投
        return self.flags_dir / "receipt_lost"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.tmp_dir, self.archive_dir, self.flags_dir):
            p.mkdir(parents=True, exist_ok=True)


settings = Settings()
