"""Persistent browser trust using high-entropy credentials stored as hashes."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import threading
from typing import Iterator


MAX_TRUSTED_CLIENTS = 20


@dataclass(frozen=True, slots=True)
class TrustedClient:
    client_id: str
    label: str
    created_at: str
    device_name: str = ""
    device_type: str = "未知设备"
    operating_system: str = "未知系统"
    browser: str = "未知浏览器"
    device_model: str = ""
    browser_engine: str = "未知"


@dataclass(frozen=True, slots=True)
class PreparedCredential:
    """A browser credential that has not been made durable yet."""

    client: TrustedClient
    credential: str = dataclass_field(repr=False)
    record: dict[str, object] = dataclass_field(repr=False)


class CredentialPersistence:
    """Marks a prepared credential durable only after its caller commits."""

    def __init__(self) -> None:
        self.committed = False

    def commit(self) -> None:
        self.committed = True


class CredentialStore:
    """Small atomic JSON store; raw browser tokens are never persisted."""

    def __init__(self, data_directory: Path) -> None:
        self.data_directory = data_directory
        self.path = data_directory / "credentials.json"
        self._lock = threading.RLock()

    def issue(
        self,
        user_agent: str,
        device_name: str = "",
        client_hints: dict[str, str] | None = None,
    ) -> tuple[TrustedClient, str]:
        prepared = self.prepare(user_agent, device_name, client_hints)
        with self.persist_prepared(prepared) as persistence:
            persistence.commit()
        return prepared.client, prepared.credential

    def prepare(
        self,
        user_agent: str,
        device_name: str = "",
        client_hints: dict[str, str] | None = None,
    ) -> PreparedCredential:
        """Build a credential without changing the persistent trust file."""
        client_id = secrets.token_urlsafe(9)
        token = secrets.token_urlsafe(32)
        created_at = datetime.now(timezone.utc).isoformat()
        clean_name = " ".join(device_name.split())[:40]
        details = describe_user_agent(user_agent, client_hints)
        label = clean_name or details["device_label"]
        record = {
            "client_id": client_id,
            "label": label,
            "device_name": clean_name,
            "device_type": details["device_type"],
            "operating_system": details["operating_system"],
            "browser": details["browser"],
            "device_model": details["device_model"],
            "browser_engine": details["browser_engine"],
            "created_at": created_at,
            "token_hash": _token_hash(token),
        }
        return PreparedCredential(
            client=_client_from_record(record),
            credential=f"{client_id}.{token}",
            record=record,
        )

    @contextmanager
    def persist_prepared(
        self,
        prepared: PreparedCredential,
    ) -> Iterator[CredentialPersistence]:
        """Persist one prepared credential with compensating rollback.

        The store lock remains held until the caller confirms that its related
        in-memory state has also committed.  If that confirmation never
        arrives, the exact previous trust set is restored atomically.
        """
        with self._lock:
            previous = self._load()
            records = [*previous, dict(prepared.record)][-MAX_TRUSTED_CLIENTS:]
            self._save(records)
            persistence = CredentialPersistence()
            try:
                yield persistence
            finally:
                if not persistence.committed:
                    self._save(previous)

    def verify(self, credential: str | None) -> TrustedClient | None:
        if not credential or "." not in credential:
            return None
        client_id, token = credential.split(".", 1)
        if not client_id or not token:
            return None
        expected = _token_hash(token)
        with self._lock:
            for record in self._load():
                if record.get("client_id") != client_id:
                    continue
                stored = str(record.get("token_hash") or "")
                if secrets.compare_digest(stored, expected):
                    return _client_from_record(record)
        return None

    def list_clients(self) -> list[TrustedClient]:
        with self._lock:
            return [
                _client_from_record(item)
                for item in self._load()
                if item.get("client_id")
            ]

    def revoke(self, client_id: str) -> bool:
        with self._lock:
            records = self._load()
            remaining = [item for item in records if item.get("client_id") != client_id]
            if len(remaining) == len(records):
                return False
            self._save(remaining)
            return True

    def revoke_all(self) -> int:
        with self._lock:
            count = len(self._load())
            self._save([])
            return count

    def _load(self) -> list[dict[str, object]]:
        try:
            content = self.path.read_text(encoding="utf-8")
            payload = json.loads(content)
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"无法读取可信客户机记录：{exc}") from exc
        records = payload.get("clients", []) if isinstance(payload, dict) else []
        return records if isinstance(records, list) else []

    def _save(self, records: list[dict[str, object]]) -> None:
        self.data_directory.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f".{secrets.token_hex(6)}.tmp")
        payload = {"version": 3, "clients": records}
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as output:
                json.dump(payload, output, ensure_ascii=False, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _client_from_record(record: dict[str, object]) -> TrustedClient:
    """Read both new structured records and legacy raw User-Agent labels."""
    stored_label = str(record.get("label") or "浏览器")
    has_structured_details = any(
        record.get(key)
        for key in ("device_type", "operating_system", "browser")
    )
    if has_structured_details:
        stored_browser = str(record.get("browser") or "未知浏览器")
        if not record.get("browser_engine") and stored_browser.startswith("Google Chrome "):
            stored_browser = stored_browser.replace(
                "Google Chrome ",
                "Chromium 系浏览器（名称未公开） ",
                1,
            )
        details = {
            "device_label": stored_label,
            "device_type": str(record.get("device_type") or "未知设备"),
            "operating_system": str(record.get("operating_system") or "未知系统"),
            "browser": stored_browser,
            "device_model": str(record.get("device_model") or ""),
            "browser_engine": str(record.get("browser_engine") or ""),
        }
        if (
            not details["device_model"]
            and details["operating_system"].startswith("Android")
            and stored_label not in {"Android 手机", "Android 平板", "浏览器设备"}
            and not record.get("device_name")
        ):
            details["device_model"] = stored_label
            details["device_label"] = f"Android {details['device_type']}"
        if not details["browser_engine"]:
            details["browser_engine"] = _browser_engine(details["browser"], details["operating_system"])
    else:
        details = describe_user_agent(stored_label)
    device_name = str(record.get("device_name") or "")
    label = device_name or details["device_label"]
    return TrustedClient(
        client_id=str(record.get("client_id") or ""),
        label=label,
        created_at=str(record.get("created_at") or ""),
        device_name=device_name,
        device_type=details["device_type"],
        operating_system=details["operating_system"],
        browser=details["browser"],
        device_model=details["device_model"],
        browser_engine=details["browser_engine"],
    )


def describe_user_agent(
    user_agent: str,
    client_hints: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return conservative, human-readable browser and device details."""
    value = " ".join((user_agent or "").split())[:512]
    operating_system = "未知系统"
    device_type = "未知设备"
    device_label = "浏览器设备"
    device_model = ""

    match = re.search(r"(?:iPhone )?OS ([0-9_]+)", value)
    if "iPhone" in value:
        version = match.group(1).replace("_", ".") if match else ""
        operating_system = f"iOS {version}".strip()
        device_type, device_label = "手机", "iPhone"
    elif "iPad" in value:
        version = match.group(1).replace("_", ".") if match else ""
        operating_system = f"iPadOS {version}".strip()
        device_type, device_label = "平板", "iPad"
    elif "Android" in value:
        version_match = re.search(r"Android\s+([^;\)]+)", value)
        version = version_match.group(1).strip() if version_match else ""
        operating_system = f"Android {version}".strip()
        device_type = "手机" if "Mobile" in value else "平板"
        model_match = re.search(
            r"Android[^;\)]*;\s*([^;\)]+?)(?:\s+Build/[^;\)]*)?[;\)]",
            value,
        )
        model = model_match.group(1).strip() if model_match else ""
        if model in {"K", "wv"}:
            model = ""
        device_model = model
        device_label = f"Android {device_type}"
    elif "Windows NT" in value:
        windows_match = re.search(r"Windows NT\s+([0-9.]+)", value)
        windows_version = windows_match.group(1) if windows_match else ""
        windows_names = {"10.0": "Windows 10/11", "6.3": "Windows 8.1", "6.1": "Windows 7"}
        operating_system = windows_names.get(windows_version, "Windows")
        device_type, device_label = "电脑", "Windows 电脑"
    elif "Mac OS X" in value:
        mac_match = re.search(r"Mac OS X\s+([0-9_\.]+)", value)
        version = mac_match.group(1).replace("_", ".") if mac_match else ""
        operating_system = f"macOS {version}".strip()
        device_type, device_label = "电脑", "Mac"
    elif "Linux" in value:
        operating_system = "Linux"
        device_type, device_label = "电脑", "Linux 电脑"

    browser_patterns = (
        (r"Via/([0-9.]+)", "Via"),
        (r"SamsungBrowser/([0-9.]+)", "Samsung Internet"),
        (r"Edg(?:A|iOS)?/([0-9.]+)", "Microsoft Edge"),
        (r"OPR/([0-9.]+)", "Opera"),
        (r"CriOS/([0-9.]+)", "Google Chrome"),
        (r"FxiOS/([0-9.]+)", "Mozilla Firefox"),
        (r"Firefox/([0-9.]+)", "Mozilla Firefox"),
    )
    browser = "未知浏览器"
    for pattern, name in browser_patterns:
        browser_match = re.search(pattern, value)
        if browser_match:
            browser = f"{name} {browser_match.group(1).split('.', 1)[0]}"
            break
    else:
        safari_match = re.search(r"Version/([0-9.]+).+Safari/", value)
        if safari_match:
            browser = f"Safari {safari_match.group(1).split('.', 1)[0]}"
        else:
            chrome_match = re.search(r"Chrome/([0-9.]+)", value)
            if chrome_match:
                browser = (
                    "Chromium 系浏览器（名称未公开） "
                    f"{chrome_match.group(1).split('.', 1)[0]}"
                )

    hinted_browser = _browser_from_client_hints((client_hints or {}).get("brands", ""))
    if hinted_browser:
        browser = hinted_browser
    hinted_model = (client_hints or {}).get("model", "").strip().strip('"')
    if hinted_model:
        device_model = hinted_model[:80]

    if device_type == "未知设备" and value and len(value) <= 80:
        device_label = value
    return {
        "device_label": device_label,
        "device_type": device_type,
        "operating_system": operating_system,
        "browser": browser,
        "device_model": device_model,
        "browser_engine": _browser_engine(browser, operating_system),
    }


def _browser_from_client_hints(brands: str) -> str:
    entries = re.findall(r'"([^"]+)"\s*;\s*v="([^"]+)"', brands)
    candidates = {
        "Microsoft Edge": "Microsoft Edge",
        "Google Chrome": "Google Chrome",
        "Opera": "Opera",
        "Samsung Internet": "Samsung Internet",
    }
    for brand, version in entries:
        if brand in candidates:
            return f"{candidates[brand]} {version.split('.', 1)[0]}"
    for brand, version in entries:
        if brand == "Chromium":
            return f"Chromium 系浏览器（名称未公开） {version.split('.', 1)[0]}"
    return ""


def _browser_engine(browser: str, operating_system: str) -> str:
    if operating_system.startswith(("iOS", "iPadOS")):
        return "WebKit（iOS 系统限制）"
    if "Firefox" in browser:
        return "Gecko"
    if "Safari" in browser:
        return "WebKit"
    if any(name in browser for name in ("Chrome", "Chromium", "Edge", "Opera", "Via", "Samsung")):
        return "Blink / Chromium"
    return "未知"
