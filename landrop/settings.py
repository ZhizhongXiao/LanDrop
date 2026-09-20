"""Persistent, user-scoped settings for LanDrop."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import uuid


CONFIG_VERSION = 1
DEFAULT_MAX_UPLOAD_MB = 1000
MIN_UPLOAD_MB = 1
MAX_UPLOAD_MB = 10_000


class SettingsError(RuntimeError):
    """A settings operation failed in a way the user can act on."""


@dataclass(frozen=True, slots=True)
class AppSettings:
    shared_directory: Path
    receive_directory: Path
    max_upload_mb: int = DEFAULT_MAX_UPLOAD_MB

    def to_json(self) -> dict[str, object]:
        return {
            "version": CONFIG_VERSION,
            "shared_directory": str(self.shared_directory),
            "receive_directory": str(self.receive_directory),
            "max_upload_mb": self.max_upload_mb,
        }


def default_data_directory() -> Path:
    """Return the per-user LanDrop data root without using the source tree."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "LanDrop"
    if os.name == "nt":
        return Path.home() / "AppData" / "Local" / "LanDrop"
    return Path.home() / ".local" / "share" / "LanDrop"


def downloads_directory() -> Path:
    """Resolve the Windows Downloads known folder, with a conservative fallback."""
    if os.name == "nt":
        try:
            return _windows_known_folder("374DE290-123F-4565-9164-39C4925E467B")
        except OSError:
            pass
    return Path.home() / "Downloads"


def default_settings(downloads: Path | None = None) -> AppSettings:
    root = (downloads or downloads_directory()) / "LanDrop"
    return AppSettings(
        shared_directory=root / "Shared",
        receive_directory=root / "Received",
        max_upload_mb=DEFAULT_MAX_UPLOAD_MB,
    )


class SettingsStore:
    """Load and atomically save the deliberately small settings schema."""

    def __init__(
        self,
        data_directory: Path | None = None,
        *,
        downloads: Path | None = None,
    ) -> None:
        self.data_directory = Path(data_directory or default_data_directory())
        self.path = self.data_directory / "config.json"
        self.defaults = default_settings(downloads)
        self.last_warning = ""

    def load(self) -> AppSettings:
        self.last_warning = ""
        if not self.path.exists():
            return self.defaults
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("配置根节点必须是 JSON 对象")
            version = raw.get("version", CONFIG_VERSION)
            if isinstance(version, bool) or not isinstance(version, int):
                raise ValueError("配置版本必须是整数")
            if version != CONFIG_VERSION:
                raise ValueError(f"不支持的配置版本：{version}")
            return AppSettings(
                shared_directory=_configured_path(
                    raw.get("shared_directory"), self.defaults.shared_directory
                ),
                receive_directory=_configured_path(
                    raw.get("receive_directory"), self.defaults.receive_directory
                ),
                max_upload_mb=_configured_limit(
                    raw.get("max_upload_mb"), self.defaults.max_upload_mb
                ),
            )
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            ValueError,
            SettingsError,
        ) as exc:
            self.last_warning = f"配置文件无法使用，已回退到安全默认值：{exc}"
            return self.defaults

    def save(self, settings: AppSettings) -> None:
        validated = AppSettings(
            shared_directory=_required_absolute_path(settings.shared_directory, "下载来源目录"),
            receive_directory=_required_absolute_path(settings.receive_directory, "上传保存目录"),
            max_upload_mb=_required_limit(settings.max_upload_mb),
        )
        temporary = self.data_directory / f".config.{secrets.token_hex(8)}.tmp"
        try:
            self.data_directory.mkdir(parents=True, exist_ok=True)
            with temporary.open("x", encoding="utf-8", newline="\n") as output:
                json.dump(validated.to_json(), output, ensure_ascii=False, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            raise SettingsError(f"无法保存 LanDrop 设置：{exc}") from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def ensure_default_directories(self, settings: AppSettings | None = None) -> None:
        """Create only the app-owned default directories when they are selected."""
        selected = settings or self.load()
        for candidate, default in (
            (selected.shared_directory, self.defaults.shared_directory),
            (selected.receive_directory, self.defaults.receive_directory),
        ):
            if _same_path(candidate, default):
                try:
                    candidate.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    raise SettingsError(f"无法创建默认目录 {candidate}：{exc}") from exc


def _configured_path(value: object, fallback: Path) -> Path:
    if value is None:
        return fallback
    if not isinstance(value, str) or not value.strip():
        raise ValueError("目录设置必须是非空字符串")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise ValueError("目录设置必须使用绝对路径")
    return candidate


def _configured_limit(value: object, fallback: int) -> int:
    if value is None:
        return fallback
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("上传上限必须是整数")
    return _required_limit(value)


def _required_absolute_path(value: Path, label: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise SettingsError(f"{label}必须使用绝对路径。")
    return candidate


def _required_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettingsError("上传上限必须是整数。")
    if not MIN_UPLOAD_MB <= value <= MAX_UPLOAD_MB:
        raise SettingsError(f"上传上限必须在 {MIN_UPLOAD_MB} 到 {MAX_UPLOAD_MB} MB 之间。")
    return value


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


class _Guid(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _windows_known_folder(folder_id: str) -> Path:
    folder_guid = _Guid.from_buffer_copy(uuid.UUID(folder_id).bytes_le)
    raw_path = ctypes.c_void_p()
    shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
    ole32 = ctypes.windll.ole32  # type: ignore[attr-defined]
    shell32.SHGetKnownFolderPath.argtypes = [
        ctypes.POINTER(_Guid),
        wintypes.DWORD,
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.CoTaskMemFree.restype = None
    result = shell32.SHGetKnownFolderPath(
        ctypes.byref(folder_guid), 0, None, ctypes.byref(raw_path)
    )
    if result != 0 or not raw_path.value:
        raise OSError(result, "Windows 无法解析 Downloads Known Folder")
    try:
        return Path(ctypes.wstring_at(raw_path.value))
    finally:
        ole32.CoTaskMemFree(raw_path)
