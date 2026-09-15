"""Persistent browser trust using high-entropy credentials stored as hashes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import threading


MAX_TRUSTED_CLIENTS = 20


@dataclass(frozen=True, slots=True)
class TrustedClient:
    client_id: str
    label: str
    created_at: str


class CredentialStore:
    """Small atomic JSON store; raw browser tokens are never persisted."""

    def __init__(self, data_directory: Path) -> None:
        self.data_directory = data_directory
        self.path = data_directory / "credentials.json"
        self._lock = threading.RLock()

    def issue(self, label: str) -> tuple[TrustedClient, str]:
        client_id = secrets.token_urlsafe(9)
        token = secrets.token_urlsafe(32)
        created_at = datetime.now(timezone.utc).isoformat()
        clean_label = " ".join(label.split())[:80] or "浏览器"
        record = {
            "client_id": client_id,
            "label": clean_label,
            "created_at": created_at,
            "token_hash": _token_hash(token),
        }
        with self._lock:
            records = self._load()
            records.append(record)
            records = records[-MAX_TRUSTED_CLIENTS:]
            self._save(records)
        return TrustedClient(client_id, clean_label, created_at), f"{client_id}.{token}"

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
                    return TrustedClient(
                        client_id=client_id,
                        label=str(record.get("label") or "浏览器"),
                        created_at=str(record.get("created_at") or ""),
                    )
        return None

    def list_clients(self) -> list[TrustedClient]:
        with self._lock:
            return [
                TrustedClient(
                    client_id=str(item.get("client_id") or ""),
                    label=str(item.get("label") or "浏览器"),
                    created_at=str(item.get("created_at") or ""),
                )
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
            raise RuntimeError(f"无法读取可信客户端记录：{exc}") from exc
        records = payload.get("clients", []) if isinstance(payload, dict) else []
        return records if isinstance(records, list) else []

    def _save(self, records: list[dict[str, object]]) -> None:
        self.data_directory.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f".{secrets.token_hex(6)}.tmp")
        payload = {"version": 1, "clients": records}
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
