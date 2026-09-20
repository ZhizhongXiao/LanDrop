"""Safe file-system operations for LanDrop uploads and downloads."""

from __future__ import annotations

from dataclasses import dataclass
import errno
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import threading
import time
import unicodedata
from typing import BinaryIO, Callable


COPY_CHUNK_SIZE = 1024 * 1024
MINIMUM_FREE_SPACE = 10 * 1024 * 1024
MAX_FILENAME_LENGTH = 180
ORPHAN_PART_MIN_AGE_SECONDS = 24 * 60 * 60

_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")
_LANDROP_PART_FILE = re.compile(r"^\..+\.[0-9a-f]{24}\.part$")
_destination_lock = threading.Lock()


class StorageError(RuntimeError):
    """Base class for user-facing storage errors."""


class InvalidFilenameError(StorageError):
    """The client supplied a filename that cannot be stored safely."""


class UploadTooLargeError(StorageError):
    """The upload exceeds the configured file size limit."""


class InsufficientSpaceError(StorageError):
    """The receive volume does not have enough free space."""


@dataclass(frozen=True, slots=True)
class ListedFile:
    relative_path: str
    size: int


@dataclass(frozen=True, slots=True)
class UploadResult:
    filename: str
    size: int
    renamed: bool


@dataclass(frozen=True, slots=True)
class PartCleanupResult:
    removed: int
    skipped: int
    errors: tuple[str, ...]


def sanitize_filename(raw_filename: str) -> str:
    """Return one Windows-safe basename while preserving useful Unicode."""
    if not raw_filename or not raw_filename.strip():
        raise InvalidFilenameError("文件名为空。")

    # Browsers normally send a basename, but older clients may include a fake
    # Windows path. Only the final component is ever considered.
    basename = raw_filename.replace("\\", "/").rsplit("/", 1)[-1]
    basename = unicodedata.normalize("NFC", basename)
    basename = _INVALID_FILENAME.sub("_", basename)
    basename = _WHITESPACE.sub(" ", basename).strip(" .")
    if not basename or basename in {".", ".."}:
        raise InvalidFilenameError("文件名清理后为空。")

    suffix = Path(basename).suffix[:20]
    stem = basename[: -len(suffix)] if suffix else basename
    if stem.upper() in _WINDOWS_RESERVED:
        stem = f"_{stem}"
    allowed_stem = MAX_FILENAME_LENGTH - len(suffix)
    basename = f"{stem[:allowed_stem]}{suffix}".rstrip(" .")
    if not basename:
        raise InvalidFilenameError("无法生成安全文件名。")
    return basename


def ensure_free_space(directory: Path, expected_bytes: int) -> None:
    """Require room for the request plus a small safety reserve."""
    free = shutil.disk_usage(directory).free
    required = max(0, expected_bytes) + MINIMUM_FREE_SPACE
    if free < required:
        raise InsufficientSpaceError(
            f"磁盘空间不足：至少需要 {format_size(required)}，"
            f"当前可用 {format_size(free)}。"
        )


def save_upload(
    source: BinaryIO,
    raw_filename: str,
    receive_directory: Path,
    max_bytes: int,
    *,
    progress: Callable[[int], None] | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> UploadResult:
    """Stream an upload through a unique .part file and atomically finalize it."""
    safe_name = sanitize_filename(raw_filename)
    root = receive_directory.resolve(strict=True)
    part_path = root / f".{safe_name}.{secrets.token_hex(12)}.part"
    size = 0
    try:
        with part_path.open("xb") as output:
            while True:
                if check_cancelled is not None:
                    check_cancelled()
                chunk = source.read(COPY_CHUNK_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise UploadTooLargeError(
                        f"文件超过 {format_size(max_bytes)} 的上传上限。"
                    )
                output.write(chunk)
                if progress is not None:
                    progress(len(chunk))
            output.flush()
            os.fsync(output.fileno())

        with _destination_lock:
            destination = _available_destination(root, safe_name)
            renamed = destination.name != safe_name
            part_path.rename(destination)
        return UploadResult(destination.name, size, renamed)
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            raise InsufficientSpaceError("写入过程中磁盘空间不足。") from exc
        raise StorageError(f"无法保存上传文件：{exc}") from exc
    finally:
        try:
            part_path.unlink(missing_ok=True)
        except OSError:
            pass


def cleanup_orphaned_upload_parts(
    receive_directory: Path,
    *,
    minimum_age_seconds: float = ORPHAN_PART_MIN_AGE_SECONDS,
) -> PartCleanupResult:
    """Delete only direct-child regular files matching LanDrop's temp format."""
    root = receive_directory.resolve(strict=True)
    if not root.is_dir():
        raise StorageError(f"接收路径不是目录：{root}")
    removed = 0
    skipped = 0
    errors: list[str] = []
    try:
        candidates = list(root.iterdir())
    except OSError as exc:
        raise StorageError(f"无法检查上传保存目录中的临时文件：{exc}") from exc

    for candidate in candidates:
        if not _LANDROP_PART_FILE.fullmatch(candidate.name):
            continue
        try:
            metadata = candidate.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                skipped += 1
                continue
            if time.time() - metadata.st_mtime < max(0.0, minimum_age_seconds):
                skipped += 1
                continue
            candidate.unlink()
            removed += 1
        except OSError as exc:
            errors.append(f"{candidate.name}: {exc}")
    return PartCleanupResult(removed, skipped, tuple(errors))


def list_shared_files(shared_directory: Path) -> list[ListedFile]:
    """List regular files that resolve beneath the shared root."""
    root = shared_directory.resolve(strict=True)
    files: list[ListedFile] = []
    for candidate in root.rglob("*"):
        if candidate.name.endswith(".part"):
            continue
        try:
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(root) or not resolved.is_file():
                continue
            relative = candidate.relative_to(root).as_posix()
            files.append(ListedFile(relative, resolved.stat().st_size))
        except (OSError, ValueError):
            continue
    return sorted(files, key=lambda item: item.relative_path.casefold())


def resolve_shared_file(shared_directory: Path, relative_path: str) -> Path:
    """Resolve an existing regular file without allowing root escape."""
    root = shared_directory.resolve(strict=True)
    if "\x00" in relative_path or "\\" in relative_path:
        raise InvalidFilenameError("请求路径包含非法字符。")
    parts = Path(relative_path.replace("/", os.sep)).parts
    if any(part == ".." for part in parts):
        raise InvalidFilenameError("请求路径不能超出提供下载的目录。")
    candidate = root.joinpath(*parts).resolve(strict=False)
    if not candidate.is_relative_to(root):
        raise InvalidFilenameError("请求路径不能超出提供下载的目录。")
    if candidate.name.endswith(".part") or not candidate.is_file():
        raise FileNotFoundError(relative_path)
    return candidate


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1000
    return f"{size} B"


def _available_destination(root: Path, filename: str) -> Path:
    candidate = root / filename
    if not candidate.exists():
        return candidate
    suffix = candidate.suffix
    stem = candidate.name[: -len(suffix)] if suffix else candidate.name
    counter = 1
    while True:
        candidate = root / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1
