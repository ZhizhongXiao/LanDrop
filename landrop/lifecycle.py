"""Session lifecycle, rolling pairing codes, and transfer statistics."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
import secrets
import threading
import time
import uuid
from typing import Callable


class SessionExpiredError(RuntimeError):
    """The session no longer accepts a new transfer."""


class TransferCancelledError(RuntimeError):
    """An active transfer was cancelled by the service lifecycle."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(slots=True)
class _ActiveTransfer:
    kind: str
    started_at: float
    byte_count: int = 0
    download_id: str = ""
    range_start: int = 0


@dataclass(slots=True)
class _DownloadTask:
    started_at: float
    expected_size: int
    completed_ranges: list[tuple[int, int]] = field(default_factory=list)
    active_streams: int = 0
    last_failure_reason: str = ""
    stream_failures: dict[str, int] = field(default_factory=dict)
    finalized: bool = False


@dataclass(slots=True)
class SessionStatistics:
    completed_downloads: int = 0
    completed_uploads: int = 0
    failed_downloads: int = 0
    failed_uploads: int = 0
    completed_download_streams: int = 0
    failed_download_streams: int = 0
    cancelled_download_streams: int = 0
    failed_download_bytes: int = 0
    failed_upload_bytes: int = 0
    downloaded_bytes: int = 0
    uploaded_bytes: int = 0
    completed_transfer_seconds: float = 0.0
    rejected_expired_requests: int = 0
    failures: dict[str, int] = field(default_factory=dict)
    stream_failures: dict[str, int] = field(default_factory=dict)
    stream_cancellations: dict[str, int] = field(default_factory=dict)
    rejections: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["downloaded_mb"] = round(self.downloaded_bytes / 1_000_000, 6)
        result["uploaded_mb"] = round(self.uploaded_bytes / 1_000_000, 6)
        result["failed_download_mb"] = round(self.failed_download_bytes / 1_000_000, 6)
        result["failed_upload_mb"] = round(self.failed_upload_bytes / 1_000_000, 6)
        for key in (
            "downloaded_bytes",
            "uploaded_bytes",
            "failed_download_bytes",
            "failed_upload_bytes",
        ):
            result.pop(key, None)
        total_bytes = self.downloaded_bytes + self.uploaded_bytes
        result["transferred_mb"] = round(total_bytes / 1_000_000, 2)
        result["average_mb_s"] = round(
            total_bytes / 1_000_000 / self.completed_transfer_seconds,
            2,
        ) if self.completed_transfer_seconds > 0 else 0.0
        return result


@dataclass(frozen=True, slots=True)
class LifecycleSnapshot:
    session_id: str
    deadline_revision: int
    phase: str
    remaining_seconds: int
    grace_remaining_seconds: int
    remaining_milliseconds: int
    grace_remaining_milliseconds: int
    pairing_code: str
    pairing_revision: int
    paired_devices: int
    active_transfers: int
    stop_reason: str
    should_close: bool
    statistics: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PairingCommit:
    """Prepared next pairing state, valid for exactly one revision."""

    session_id: str
    pairing_revision: int
    next_pairing_code: str
    next_qr_token: str = field(repr=False)


class TransferHandle:
    """Track one upload or download until completion or failure."""

    def __init__(self, lifecycle: SessionLifecycle, transfer_id: str) -> None:
        self._lifecycle = lifecycle
        self._transfer_id = transfer_id
        self._finished = False
        self._lock = threading.Lock()

    def add_bytes(self, amount: int) -> None:
        if amount <= 0:
            return
        self._lifecycle._add_bytes(self._transfer_id, amount)

    def check_cancelled(self) -> None:
        reason = self._lifecycle.cancel_reason()
        if reason:
            raise TransferCancelledError(reason)

    def complete(self) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
        self._lifecycle._finish_transfer(self._transfer_id, None)

    def fail(self, reason: str) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
        self._lifecycle._finish_transfer(self._transfer_id, reason)


class SessionLifecycle:
    """Thread-safe state machine for one temporary service session."""

    def __init__(
        self,
        duration_seconds: float = 300,
        grace_seconds: float = 60,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep_gap_seconds: float = 10,
    ) -> None:
        if duration_seconds <= 0 or grace_seconds < 0:
            raise ValueError("会话时长必须大于零，宽限时长不能为负数。")
        self._clock = clock
        self._duration = float(duration_seconds)
        self._grace = float(grace_seconds)
        self._sleep_gap = float(sleep_gap_seconds)
        self._lock = threading.RLock()
        now = self._clock()
        self._session_id = uuid.uuid4().hex
        self._deadline_revision = 1
        self._deadline = now + self._duration
        self._grace_deadline: float | None = None
        self._last_heartbeat = now
        self._phase = "running"
        self._stop_reason = ""
        self._should_close = False
        self._pairing_code = _new_pairing_code()
        self._pairing_revision = 1
        self._qr_token = _new_qr_token()
        self._paired_devices = 0
        self._active: dict[str, _ActiveTransfer] = {}
        self._download_tasks: dict[str, _DownloadTask] = {}
        self._statistics = SessionStatistics()
        self._cancel_reason = ""

    @property
    def pairing_code(self) -> str:
        with self._lock:
            return self._pairing_code

    @property
    def pairing_revision(self) -> int:
        with self._lock:
            return self._pairing_revision

    def pairing_invitation(self) -> tuple[str, int, str] | None:
        """Return the current in-memory QR secret for the local GUI only."""
        with self._lock:
            self._advance(self._clock())
            if self._phase != "running" or not self._qr_token:
                return None
            return self._session_id, self._pairing_revision, self._qr_token

    def prepare_pairing(self, kind: str, submitted: str) -> PairingCommit | None:
        """Validate a credential and prepare, but do not publish, its successor."""
        if kind not in {"code", "qr"}:
            raise ValueError("未知配对凭据类型。")
        with self._lock:
            self._advance(self._clock())
            if self._phase != "running":
                return None
            expected = self._pairing_code if kind == "code" else self._qr_token
            if not submitted or not secrets.compare_digest(submitted, expected):
                return None
            return PairingCommit(
                session_id=self._session_id,
                pairing_revision=self._pairing_revision,
                next_pairing_code=_new_pairing_code(),
                next_qr_token=_new_qr_token(),
            )

    def commit_pairing(self, prepared: PairingCommit) -> bool:
        """Publish a prepared pairing only if its session/revision is current."""
        with self._lock:
            self._advance(self._clock())
            if (
                self._phase != "running"
                or self._session_id != prepared.session_id
                or self._pairing_revision != prepared.pairing_revision
            ):
                return False
            self._paired_devices += 1
            self._pairing_revision += 1
            self._pairing_code = prepared.next_pairing_code
            self._qr_token = prepared.next_qr_token
            return True

    def activate(self) -> LifecycleSnapshot:
        """Anchor the deadline when the listener is ready, not during construction."""
        with self._lock:
            if self._phase != "running" or self._active:
                raise RuntimeError("只有尚未开始传输的新会话可以激活。")
            now = self._clock()
            self._deadline = now + self._duration
            self._grace_deadline = None
            self._last_heartbeat = now
            return self._snapshot_locked(now)

    def snapshot(self) -> LifecycleSnapshot:
        with self._lock:
            self._advance(self._clock())
            return self._snapshot_locked(self._clock())

    def heartbeat(self) -> LifecycleSnapshot:
        with self._lock:
            now = self._clock()
            if self._phase in {"running", "grace"}:
                gap = now - self._last_heartbeat
                if gap > self._sleep_gap:
                    self._stop_locked("system_resume", "system_resume")
            self._last_heartbeat = now
            self._advance(now)
            return self._snapshot_locked(now)

    def reset_deadline(self, seconds: float = 300) -> LifecycleSnapshot:
        if seconds <= 0:
            raise ValueError("重置时长必须大于零。")
        with self._lock:
            now = self._clock()
            self._advance(now)
            if self._phase != "running" or now >= self._deadline:
                raise SessionExpiredError("本次会话已经到期，无法重置倒计时。")
            self._deadline = now + seconds
            self._deadline_revision += 1
            return self._snapshot_locked(now)

    def accepts_new_requests(self) -> bool:
        with self._lock:
            now = self._clock()
            if (
                self._phase in {"running", "grace"}
                and now - self._last_heartbeat > self._sleep_gap
            ):
                self._stop_locked("system_resume", "system_resume")
            self._advance(now)
            allowed = self._phase == "running"
            if not allowed:
                self._statistics.rejected_expired_requests += 1
            return allowed

    def record_rejection(self, reason: str) -> None:
        with self._lock:
            self._statistics.rejections[reason] = (
                self._statistics.rejections.get(reason, 0) + 1
            )

    def consume_pairing_code(self, submitted: str) -> bool:
        prepared = self.prepare_pairing("code", submitted)
        return prepared is not None and self.commit_pairing(prepared)

    def register_download(self, download_id: str, expected_size: int) -> None:
        if not download_id or expected_size < 0:
            raise ValueError("下载任务标识和文件大小无效。")
        with self._lock:
            now = self._clock()
            self._advance(now)
            if self._phase != "running":
                raise SessionExpiredError("本次传输会话已到期。")
            self._download_tasks.setdefault(
                download_id,
                _DownloadTask(now, expected_size),
            )

    def download_task_status(self, download_id: str) -> str:
        """Return a browser-safe logical download state for batch sequencing."""
        with self._lock:
            task = self._download_tasks.get(download_id)
            if task is None:
                return "missing"
            covered = _covered_bytes(task.completed_ranges, task.expected_size)
            if task.finalized:
                return "completed" if covered >= task.expected_size else "failed"
            if task.active_streams:
                return "active"
            if task.last_failure_reason or task.completed_ranges:
                return "waiting_retry"
            return "pending"

    def begin_transfer(
        self,
        kind: str,
        *,
        download_id: str = "",
        expected_size: int = 0,
        range_start: int = 0,
    ) -> TransferHandle:
        if kind not in {"download", "upload"}:
            raise ValueError(f"未知传输类型：{kind}")
        with self._lock:
            now = self._clock()
            self._advance(now)
            if self._phase != "running":
                raise SessionExpiredError("本次传输会话已到期。")
            transfer_id = uuid.uuid4().hex
            if kind == "download":
                download_id = download_id or f"direct-{transfer_id}"
                task = self._download_tasks.setdefault(
                    download_id,
                    _DownloadTask(now, max(0, expected_size)),
                )
                if expected_size > task.expected_size:
                    task.expected_size = expected_size
                task.active_streams += 1
            self._active[transfer_id] = _ActiveTransfer(
                kind=kind,
                started_at=now,
                download_id=download_id,
                range_start=max(0, range_start),
            )
            return TransferHandle(self, transfer_id)

    def stop(self, reason: str, transfer_failure_reason: str | None = None) -> LifecycleSnapshot:
        with self._lock:
            self._stop_locked(reason, transfer_failure_reason or reason)
            return self._snapshot_locked(self._clock())

    def cancel_reason(self) -> str:
        with self._lock:
            return self._cancel_reason

    def _add_bytes(self, transfer_id: str, amount: int) -> None:
        with self._lock:
            if self._cancel_reason:
                raise TransferCancelledError(self._cancel_reason)
            transfer = self._active.get(transfer_id)
            if transfer is not None:
                transfer.byte_count += amount

    def _finish_transfer(self, transfer_id: str, failure_reason: str | None) -> None:
        with self._lock:
            transfer = self._active.pop(transfer_id, None)
            if transfer is None:
                return
            if transfer.kind == "download":
                self._finish_download_stream(transfer, failure_reason)
            elif failure_reason:
                self._record_failure("upload", failure_reason, transfer.byte_count)
            else:
                elapsed = max(0.000001, self._clock() - transfer.started_at)
                self._statistics.completed_transfer_seconds += elapsed
                self._statistics.completed_uploads += 1
                self._statistics.uploaded_bytes += transfer.byte_count
            self._advance(self._clock())

    def _finish_download_stream(
        self,
        transfer: _ActiveTransfer,
        failure_reason: str | None,
    ) -> None:
        task = self._download_tasks.get(transfer.download_id)
        if task is None:
            return
        task.active_streams = max(0, task.active_streams - 1)
        if transfer.byte_count:
            task.completed_ranges.append(
                (
                    transfer.range_start,
                    transfer.range_start + transfer.byte_count,
                )
            )
        if failure_reason:
            task.last_failure_reason = failure_reason
            if task.finalized:
                if failure_reason == "client_disconnect":
                    self._statistics.cancelled_download_streams += 1
                    self._statistics.stream_cancellations[failure_reason] = (
                        self._statistics.stream_cancellations.get(failure_reason, 0) + 1
                    )
                else:
                    self._statistics.failed_download_streams += 1
                    self._statistics.stream_failures[failure_reason] = (
                        self._statistics.stream_failures.get(failure_reason, 0) + 1
                    )
                return
            task.stream_failures[failure_reason] = (
                task.stream_failures.get(failure_reason, 0) + 1
            )
            return
        self._statistics.completed_download_streams += 1
        covered = _covered_bytes(task.completed_ranges, task.expected_size)
        if task.expected_size <= 0:
            task.expected_size = covered
        if not task.finalized and covered >= task.expected_size:
            task.finalized = True
            self._settle_download_stream_failures(task, succeeded=True)
            self._statistics.completed_downloads += 1
            self._statistics.downloaded_bytes += task.expected_size
            self._statistics.completed_transfer_seconds += max(
                0.000001,
                self._clock() - task.started_at,
            )

    def _record_failure(self, kind: str, reason: str, byte_count: int = 0) -> None:
        if kind != "upload":
            raise ValueError("逻辑下载失败应通过下载任务结算。")
        self._statistics.failed_uploads += 1
        self._statistics.failed_upload_bytes += byte_count
        self._statistics.failures[reason] = self._statistics.failures.get(reason, 0) + 1

    def _finalize_pending_downloads(self, reason: str) -> None:
        for task in self._download_tasks.values():
            if task.finalized:
                continue
            task.finalized = True
            failure_reason = reason or task.last_failure_reason or "client_disconnect"
            self._settle_download_stream_failures(task, succeeded=False)
            self._statistics.failed_downloads += 1
            self._statistics.failed_download_bytes += _covered_bytes(
                task.completed_ranges,
                task.expected_size,
            )
            self._statistics.failures[failure_reason] = (
                self._statistics.failures.get(failure_reason, 0) + 1
            )

    def _settle_download_stream_failures(
        self,
        task: _DownloadTask,
        *,
        succeeded: bool,
    ) -> None:
        for reason, count in task.stream_failures.items():
            if succeeded and reason == "client_disconnect":
                self._statistics.cancelled_download_streams += count
                self._statistics.stream_cancellations[reason] = (
                    self._statistics.stream_cancellations.get(reason, 0) + count
                )
                continue
            self._statistics.failed_download_streams += count
            self._statistics.stream_failures[reason] = (
                self._statistics.stream_failures.get(reason, 0) + count
            )

    def _advance(self, now: float) -> None:
        if self._phase == "running" and now >= self._deadline:
            if not self._active:
                self._stop_locked("deadline_no_active", "deadline_expired")
                return
            self._phase = "grace"
            self._grace_deadline = self._deadline + self._grace
        if self._phase == "grace":
            if not self._active:
                self._stop_locked("deadline_transfers_completed", None)
            elif self._grace_deadline is not None and now >= self._grace_deadline:
                self._stop_locked("grace_timeout", "grace_timeout")

    def _stop_locked(self, reason: str, transfer_failure_reason: str | None) -> None:
        if self._phase == "stopped":
            return
        self._phase = "stopped"
        self._stop_reason = reason
        self._should_close = True
        self._pairing_code = ""
        self._qr_token = ""
        if transfer_failure_reason and self._active:
            self._cancel_reason = transfer_failure_reason
            for transfer in self._active.values():
                if transfer.kind == "download":
                    self._finish_download_stream(transfer, transfer_failure_reason)
                else:
                    self._record_failure(
                        "upload",
                        transfer_failure_reason,
                        transfer.byte_count,
                    )
            self._active.clear()
        self._finalize_pending_downloads(transfer_failure_reason or "")

    def _snapshot_locked(self, now: float) -> LifecycleSnapshot:
        remaining = math.ceil(max(0.0, self._deadline - now)) if self._phase == "running" else 0
        remaining_ms = math.ceil(max(0.0, self._deadline - now) * 1000) if self._phase == "running" else 0
        grace_remaining = 0
        grace_remaining_ms = 0
        if self._phase == "grace" and self._grace_deadline is not None:
            grace_remaining = math.ceil(max(0.0, self._grace_deadline - now))
            grace_remaining_ms = math.ceil(max(0.0, self._grace_deadline - now) * 1000)
        return LifecycleSnapshot(
            session_id=self._session_id,
            deadline_revision=self._deadline_revision,
            phase=self._phase,
            remaining_seconds=remaining,
            grace_remaining_seconds=grace_remaining,
            remaining_milliseconds=remaining_ms,
            grace_remaining_milliseconds=grace_remaining_ms,
            pairing_code=self._pairing_code,
            pairing_revision=self._pairing_revision,
            paired_devices=self._paired_devices,
            active_transfers=len(self._active),
            stop_reason=self._stop_reason,
            should_close=self._should_close,
            statistics=self._statistics.to_dict(),
        )


def _covered_bytes(ranges: list[tuple[int, int]], expected_size: int) -> int:
    normalized = []
    upper_bound = expected_size if expected_size > 0 else None
    for start, end in ranges:
        start = max(0, start)
        end = max(start, end)
        if upper_bound is not None:
            start = min(start, upper_bound)
            end = min(end, upper_bound)
        if end > start:
            normalized.append((start, end))
    if not normalized:
        return 0
    normalized.sort()
    total = 0
    current_start, current_end = normalized[0]
    for start, end in normalized[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
            continue
        total += current_end - current_start
        current_start, current_end = start, end
    return total + current_end - current_start


def _new_pairing_code() -> str:
    return f"{secrets.randbelow(100_000_000):08d}"


def _new_qr_token() -> str:
    return secrets.token_urlsafe(32)
