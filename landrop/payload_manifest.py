"""Build and verify an exact manifest for a staged LanDrop payload."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import secrets
from typing import Iterable, Mapping

from .install_contract import PRODUCT_ID, is_reparse_object


PAYLOAD_MANIFEST_SCHEMA_VERSION = 1
_HASH_CHUNK_SIZE = 1024 * 1024
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


class PayloadManifestError(RuntimeError):
    """A payload manifest or the staged payload violates the frozen contract."""


@dataclass(frozen=True, slots=True)
class PayloadFile:
    relative_path: str
    size: int
    sha256: str

    def to_json(self) -> dict[str, object]:
        return {
            "path": self.relative_path,
            "size": self.size,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class PayloadManifest:
    product_id: str
    version: str
    build_id: str
    files: tuple[PayloadFile, ...]
    schema_version: int = PAYLOAD_MANIFEST_SCHEMA_VERSION

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "product_id": self.product_id,
            "version": self.version,
            "build_id": self.build_id,
            "files": [entry.to_json() for entry in self.files],
        }


def build_payload_manifest(
    payload_root: Path,
    *,
    version: str,
    build_id: str,
) -> PayloadManifest:
    """Hash every regular file below *payload_root* without following links."""
    root = _validated_payload_root(payload_root)
    entries = tuple(
        PayloadFile(relative, size, digest)
        for relative, path, size, digest in _inspect_payload_tree(root)
    )
    return _manifest_from_json(
        {
            "schema_version": PAYLOAD_MANIFEST_SCHEMA_VERSION,
            "product_id": PRODUCT_ID,
            "version": version,
            "build_id": build_id,
            "files": [entry.to_json() for entry in entries],
        }
    )


def write_payload_manifest(path: Path, manifest: PayloadManifest) -> None:
    """Atomically publish a build-time manifest outside the payload tree."""
    validated = _manifest_from_json(manifest.to_json())
    destination = Path(os.path.abspath(path))
    if os.path.lexists(destination) and is_reparse_object(destination):
        raise PayloadManifestError("payload manifest 目标不能是 reparse object。")
    temporary = destination.parent / f".{destination.stem}.{secrets.token_hex(8)}.tmp"
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("x", encoding="utf-8", newline="\n") as output:
            json.dump(validated.to_json(), output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    except OSError as exc:
        raise PayloadManifestError(f"无法写入 payload manifest：{exc}") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def read_payload_manifest(path: Path) -> PayloadManifest:
    source = Path(os.path.abspath(path))
    if not source.is_file() or is_reparse_object(source):
        raise PayloadManifestError("payload manifest 不存在、不是普通文件或是 reparse object。")
    try:
        if source.stat().st_size > _MAX_MANIFEST_BYTES:
            raise PayloadManifestError("payload manifest 超过 8 MiB 上限。")
        raw = json.loads(source.read_text(encoding="utf-8"))
    except PayloadManifestError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PayloadManifestError(f"无法读取 payload manifest：{exc}") from exc
    if not isinstance(raw, dict):
        raise PayloadManifestError("payload manifest 根节点必须是 JSON 对象。")
    return _manifest_from_json(raw)


def verify_payload(payload_root: Path, manifest: PayloadManifest) -> None:
    """Require an exact path/size/SHA-256 match for a staged payload."""
    root = _validated_payload_root(payload_root)
    validated = _manifest_from_json(manifest.to_json())
    expected = {entry.relative_path.casefold(): entry for entry in validated.files}
    actual_entries = tuple(_inspect_payload_tree(root))
    actual = {
        relative.casefold(): (relative, path, size, digest)
        for relative, path, size, digest in actual_entries
    }

    missing = sorted(expected[key].relative_path for key in expected.keys() - actual.keys())
    extra = sorted(actual[key][0] for key in actual.keys() - expected.keys())
    if missing or extra:
        raise PayloadManifestError(
            f"payload 文件集合不匹配；缺少 {missing}；多出 {extra}。"
        )
    for key, entry in expected.items():
        relative, _path, size, digest = actual[key]
        if relative != entry.relative_path:
            raise PayloadManifestError(f"payload 路径大小写不匹配：{relative}")
        if size != entry.size:
            raise PayloadManifestError(f"payload 文件大小不匹配：{relative}")
        if digest != entry.sha256:
            raise PayloadManifestError(f"payload SHA-256 不匹配：{relative}")


def _inspect_payload_tree(root: Path) -> Iterable[tuple[str, Path, int, str]]:
    inspected: list[tuple[str, Path, int, str]] = []
    try:
        for directory, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
            current = Path(directory)
            for name in list(directory_names):
                child = current / name
                if is_reparse_object(child):
                    raise PayloadManifestError(f"payload 包含目录 reparse object：{child}")
            for name in file_names:
                child = current / name
                if is_reparse_object(child) or not child.is_file():
                    raise PayloadManifestError(f"payload 包含非普通文件：{child}")
                relative = _normalized_relative_path(child.relative_to(root))
                before = child.stat()
                digest = _sha256_file(child)
                after = child.stat()
                if (
                    before.st_size != after.st_size
                    or before.st_mtime_ns != after.st_mtime_ns
                ):
                    raise PayloadManifestError(f"payload 文件在校验期间发生变化：{relative}")
                inspected.append((relative, child, after.st_size, digest))
    except PayloadManifestError:
        raise
    except OSError as exc:
        raise PayloadManifestError(f"无法枚举 payload：{exc}") from exc
    inspected.sort(key=lambda item: (item[0].casefold(), item[0]))
    folded = [item[0].casefold() for item in inspected]
    if len(folded) != len(set(folded)):
        raise PayloadManifestError("payload 包含 Windows 下大小写冲突的路径。")
    return inspected


def _manifest_from_json(raw: Mapping[str, object]) -> PayloadManifest:
    expected = {"schema_version", "product_id", "version", "build_id", "files"}
    _require_exact_keys(raw, expected, "payload manifest")
    schema = raw["schema_version"]
    if isinstance(schema, bool) or schema != PAYLOAD_MANIFEST_SCHEMA_VERSION:
        raise PayloadManifestError(f"不支持的 payload manifest schema：{schema!r}")
    product_id = _required_text(raw["product_id"], "product_id")
    if product_id != PRODUCT_ID:
        raise PayloadManifestError(f"payload product_id 不匹配：{product_id}")
    files_raw = raw["files"]
    if not isinstance(files_raw, list):
        raise PayloadManifestError("payload files 必须是数组。")
    entries: list[PayloadFile] = []
    seen: set[str] = set()
    for index, item in enumerate(files_raw):
        if not isinstance(item, dict):
            raise PayloadManifestError(f"payload files[{index}] 必须是对象。")
        _require_exact_keys(item, {"path", "size", "sha256"}, f"files[{index}]")
        relative = _normalized_relative_text(item["path"])
        folded = relative.casefold()
        if folded in seen:
            raise PayloadManifestError(f"payload manifest 包含重复路径：{relative}")
        seen.add(folded)
        size = item["size"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise PayloadManifestError(f"payload 文件大小无效：{relative}")
        digest = item["sha256"]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise PayloadManifestError(f"payload SHA-256 无效：{relative}")
        entries.append(PayloadFile(relative, size, digest))
    sorted_entries = sorted(entries, key=lambda entry: (entry.relative_path.casefold(), entry.relative_path))
    if entries != sorted_entries:
        raise PayloadManifestError("payload manifest 文件条目必须按路径排序。")
    return PayloadManifest(
        product_id=product_id,
        version=_required_text(raw["version"], "version"),
        build_id=_required_text(raw["build_id"], "build_id"),
        files=tuple(entries),
        schema_version=PAYLOAD_MANIFEST_SCHEMA_VERSION,
    )


def _normalized_relative_path(relative: Path) -> str:
    return _normalized_relative_text(relative.as_posix())


def _normalized_relative_text(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise PayloadManifestError("payload 相对路径必须是非空 POSIX 路径。")
    if len(value) > 32_000 or any(ord(character) < 32 for character in value):
        raise PayloadManifestError(f"payload 相对路径包含无效字符或过长：{value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or value != path.as_posix() or any(part in {"", ".", ".."} for part in path.parts):
        raise PayloadManifestError(f"payload 相对路径不安全：{value!r}")
    for part in path.parts:
        stem = part.split(".", 1)[0].upper()
        if (
            len(part) > 255
            or ":" in part
            or part.endswith((" ", "."))
            or stem in _WINDOWS_RESERVED_NAMES
        ):
            raise PayloadManifestError(f"payload 相对路径不适用于 Windows：{value!r}")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(_HASH_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise PayloadManifestError(f"{label} 必须是长度 1–256 的非空字符串。")
    if any(ord(character) < 32 for character in value):
        raise PayloadManifestError(f"{label} 不能包含控制字符。")
    return value


def _require_exact_keys(raw: Mapping[str, object], expected: set[str], label: str) -> None:
    actual = set(raw)
    if actual != expected:
        raise PayloadManifestError(
            f"{label} 字段不匹配；缺少 {sorted(expected - actual)}；"
            f"多出 {sorted(actual - expected)}。"
        )


def _validated_payload_root(payload_root: Path) -> Path:
    root = Path(os.path.abspath(payload_root))
    if not root.is_dir() or is_reparse_object(root):
        raise PayloadManifestError("payload 根不存在、不是目录或是 reparse object。")
    return root
