"""Privacy-conscious rotating session event log."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import threading
from typing import Any


class SessionEventLog:
    def __init__(
        self,
        data_directory: Path,
        *,
        max_bytes: int = 1_000_000,
        backup_count: int = 3,
    ) -> None:
        self.directory = data_directory / "logs"
        self.path = self.directory / "sessions.jsonl"
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self._lock = threading.Lock()

    def record(
        self,
        lifecycle: Any,
        *,
        started_at: str,
        duration_seconds: float,
        grace_seconds: float,
    ) -> None:
        payload = {
            "version": 2,
            "session_id": lifecycle.session_id,
            "started_at": started_at,
            "stopped_at": datetime.now(timezone.utc).isoformat(),
            "configured_duration_seconds": duration_seconds,
            "configured_grace_seconds": grace_seconds,
            "stop_reason": lifecycle.stop_reason,
            "paired_devices": lifecycle.paired_devices,
            "statistics": lifecycle.statistics,
        }
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            logger = logging.getLogger(f"landrop.session.{id(self)}")
            logger.setLevel(logging.INFO)
            logger.propagate = False
            handler = RotatingFileHandler(
                self.path,
                maxBytes=self.max_bytes,
                backupCount=self.backup_count,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(handler)
            try:
                logger.info(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            finally:
                logger.removeHandler(handler)
                handler.close()
