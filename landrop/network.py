"""Read-only Windows LAN interface discovery."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import ipaddress
import json
import os
import socket
import struct
import subprocess
import winreg


POWERSHELL = os.path.join(
    os.environ.get("SystemRoot", r"C:\Windows"),
    "System32",
    "WindowsPowerShell",
    "v1.0",
    "powershell.exe",
)

_PROFILE_SCRIPT = r"""
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$items = Get-NetConnectionProfile -ErrorAction Stop |
    ForEach-Object {
        [PSCustomObject]@{
            alias = [string]$_.InterfaceAlias
            interface_index = [int]$_.InterfaceIndex
            category = [string]$_.NetworkCategory
            connectivity = [string]$_.IPv4Connectivity
            name = [string]$_.Name
        }
    }
@($items) | ConvertTo-Json -Compress
"""

_BENCHMARK_NETWORK = ipaddress.ip_network("198.18.0.0/15")
_ERROR_INSUFFICIENT_BUFFER = 122

_EXCLUDED_WORDS = (
    "meta",
    "tun",
    "tap",
    "vpn",
    "virtual",
    "vmware",
    "hyper-v",
    "vethernet",
    "loopback",
    "bluetooth",
    "wi-fi direct",
    "wifi direct",
    "本地连接*",
    "蓝牙",
    "虚拟",
)


class NetworkDiscoveryError(RuntimeError):
    """Raised when a safe LAN interface cannot be determined."""


@dataclass(frozen=True, slots=True)
class LanInterface:
    alias: str
    interface_index: int
    address: str
    category: str
    connectivity: str
    has_gateway: bool | None
    description: str

    @property
    def is_private_profile(self) -> bool:
        return self.category.casefold() == "private"

    @property
    def is_public_profile(self) -> bool:
        return self.category.casefold() == "public"

    @property
    def is_excluded(self) -> bool:
        text = f"{self.alias} {self.description}".casefold()
        try:
            address = ipaddress.ip_address(self.address)
        except ValueError:
            return True
        return address in _BENCHMARK_NETWORK or any(
            word.casefold() in text for word in _EXCLUDED_WORDS
        )

    @property
    def is_lan_ipv4(self) -> bool:
        try:
            address = ipaddress.ip_address(self.address)
        except ValueError:
            return False
        return (
            address.version == 4
            and address.is_private
            and not address.is_loopback
            and not address.is_link_local
            and not address.is_unspecified
        )


def discover_interfaces() -> list[LanInterface]:
    """Return active IPv4 configurations without changing system settings."""
    if os.name != "nt":
        raise NetworkDiscoveryError("LanDrop 第一阶段目前只支持 Windows。")
    if not os.path.isfile(POWERSHELL):
        raise NetworkDiscoveryError(f"找不到 Windows PowerShell：{POWERSHELL}")

    profiles = _read_connection_profiles()
    wifi_profile = _read_wifi_registry_profile() if not profiles else None
    names = dict(socket.if_nameindex())
    interfaces: list[LanInterface] = []
    for address, interface_index in _read_ipv4_table():
        profile = profiles.get(interface_index, {})
        native_name = names.get(interface_index) or f"接口 {interface_index}"
        if not profile and wifi_profile and native_name.casefold().startswith("wireless"):
            profile = wifi_profile
        interfaces.append(
            LanInterface(
                alias=str(profile.get("alias") or native_name),
                interface_index=interface_index,
                address=address,
                category=str(profile.get("category") or "Unknown"),
                connectivity=str(profile.get("connectivity") or "Unknown"),
                has_gateway=None,
                description=str(profile.get("name") or ""),
            )
        )
    return interfaces


def _read_wifi_registry_profile() -> dict[str, object] | None:
    """Fallback for restricted shells: match the active SSID to NetworkList."""
    netsh = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "netsh.exe")
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            [netsh, "wlan", "show", "interfaces"],
            check=False,
            capture_output=True,
            timeout=10,
            creationflags=creation_flags,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None

    output = completed.stdout.decode("oem", errors="replace")
    ssid = ""
    for line in output.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip().casefold() == "ssid":
            ssid = value.strip()
            break
    if not ssid:
        return None

    profiles_path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\NetworkList\Profiles"
    category_names = {0: "Public", 1: "Private", 2: "DomainAuthenticated"}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, profiles_path) as profiles_key:
            count = winreg.QueryInfoKey(profiles_key)[0]
            for index in range(count):
                child_name = winreg.EnumKey(profiles_key, index)
                with winreg.OpenKey(profiles_key, child_name) as child_key:
                    profile_name = str(winreg.QueryValueEx(child_key, "ProfileName")[0])
                    if profile_name != ssid:
                        continue
                    category = int(winreg.QueryValueEx(child_key, "Category")[0])
                    return {
                        "alias": "WLAN",
                        "category": category_names.get(category, "Unknown"),
                        "connectivity": "Connected",
                        "name": profile_name,
                    }
    except (OSError, ValueError):
        return None
    return None


def _read_connection_profiles() -> dict[int, dict[str, object]]:
    """Read Network List profiles; failure becomes Unknown and remains fail-closed."""
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            [
                POWERSHELL,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                _PROFILE_SCRIPT,
            ],
            check=False,
            capture_output=True,
            encoding="utf-8-sig",
            errors="replace",
            timeout=15,
            creationflags=creation_flags,
        )
    except (OSError, subprocess.SubprocessError):
        return {}

    if completed.returncode != 0:
        return {}

    output = completed.stdout.strip()
    try:
        records = json.loads(output or "[]")
    except json.JSONDecodeError:
        return {}

    if isinstance(records, dict):
        records = [records]

    profiles: dict[int, dict[str, object]] = {}
    for record in records:
        try:
            profiles[int(record["interface_index"])] = record
        except (KeyError, TypeError, ValueError):
            continue
    return profiles


class _MibIpAddrRow(ctypes.Structure):
    _fields_ = (
        ("address", wintypes.DWORD),
        ("interface_index", wintypes.DWORD),
        ("mask", wintypes.DWORD),
        ("broadcast", wintypes.DWORD),
        ("reassembly_size", wintypes.DWORD),
        ("unused_1", wintypes.USHORT),
        ("type", wintypes.USHORT),
    )


def _read_ipv4_table() -> list[tuple[str, int]]:
    """Enumerate IPv4 addresses through iphlpapi without CIM or admin rights."""
    get_ip_addr_table = ctypes.WinDLL("iphlpapi").GetIpAddrTable
    get_ip_addr_table.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.ULONG), wintypes.BOOL)
    get_ip_addr_table.restype = wintypes.DWORD

    size = wintypes.ULONG(0)
    result = get_ip_addr_table(None, ctypes.byref(size), False)
    if result not in (0, _ERROR_INSUFFICIENT_BUFFER):
        raise NetworkDiscoveryError(f"Windows IPv4 枚举失败，错误代码：{result}")

    buffer = ctypes.create_string_buffer(size.value)
    result = get_ip_addr_table(buffer, ctypes.byref(size), False)
    if result != 0:
        raise NetworkDiscoveryError(f"Windows IPv4 枚举失败，错误代码：{result}")

    count = ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD)).contents.value
    first_row = ctypes.addressof(buffer) + ctypes.sizeof(wintypes.DWORD)
    row_array = (_MibIpAddrRow * count).from_address(first_row)
    return [
        (socket.inet_ntoa(struct.pack("=L", row.address)), int(row.interface_index))
        for row in row_array
    ]


def usable_lan_interfaces(interfaces: list[LanInterface]) -> list[LanInterface]:
    """Exclude loopback, non-LAN and known virtual/TUN adapters."""
    return [item for item in interfaces if item.is_lan_ipv4 and not item.is_excluded]


def select_interface(
    interfaces: list[LanInterface], selector: str | None = None
) -> LanInterface:
    """Choose one Private LAN interface, asking the caller to resolve ambiguity."""
    candidates = usable_lan_interfaces(interfaces)
    if selector:
        pattern = selector.casefold()
        matches = [
            item
            for item in candidates
            if item.address == selector or item.alias.casefold() == pattern
        ]
        if not matches:
            raise NetworkDiscoveryError(
                f"未找到指定的活动 LAN 接口或 IPv4：{selector}"
            )
        if len(matches) > 1:
            exact_ip = [item for item in matches if item.address == selector]
            if len(exact_ip) == 1:
                matches = exact_ip
            else:
                raise NetworkDiscoveryError(
                    f"接口“{selector}”有多个 IPv4，请改用具体 IPv4 地址指定。"
                )
        chosen = matches[0]
        _require_private(chosen)
        return chosen

    private = [item for item in candidates if item.is_private_profile]
    if len(private) == 1:
        return private[0]
    if len(private) > 1:
        choices = "\n".join(
            f"  - {item.alias}: {item.address}（{item.description}）"
            for item in private
        )
        raise NetworkDiscoveryError(
            "检测到多个可信 LAN 候选，程序不会静默猜测。\n"
            f"{choices}\n"
            "请使用 --interface <接口名或IPv4> 明确选择。"
        )

    public = [item for item in candidates if item.is_public_profile]
    if public:
        details = "、".join(f"{item.alias} {item.address}" for item in public)
        raise NetworkDiscoveryError(
            f"当前 LAN 被 Windows 标记为 Public（{details}）。\n"
            "LanDrop 不支持在 Public 网络中启动传输服务。请仅在你信任的网络中，"
            "自行通过 Windows 设置确认并调整网络类别后重试。"
        )

    if candidates:
        details = "、".join(
            f"{item.alias} {item.address} [{item.category}]" for item in candidates
        )
        raise NetworkDiscoveryError(
            f"无法确认网络属于 Private：{details}\n"
            "为避免意外暴露，LanDrop 已拒绝启动。"
        )

    raise NetworkDiscoveryError(
        "没有找到可用的物理 LAN IPv4。请检查 Wi-Fi/热点连接；"
        "TUN、VPN、Wi-Fi Direct 和蓝牙网络不会作为候选。"
    )


def _require_private(interface: LanInterface) -> None:
    if interface.is_public_profile:
        raise NetworkDiscoveryError(
            f"接口 {interface.alias} ({interface.address}) 属于 Public 网络，拒绝启动。"
        )
    if not interface.is_private_profile:
        raise NetworkDiscoveryError(
            f"接口 {interface.alias} ({interface.address}) 的网络类别为 "
            f"{interface.category}，无法确认是 Private，拒绝启动。"
        )


def format_interfaces(interfaces: list[LanInterface]) -> str:
    """Create a readable diagnostic table."""
    if not interfaces:
        return "未检测到活动 IPv4 接口。"
    headers = ("接口", "IPv4", "类别", "连通性", "网关", "用途")
    rows = []
    for item in interfaces:
        role = "排除" if item.is_excluded or not item.is_lan_ipv4 else "LAN 候选"
        rows.append(
            (
                item.alias,
                item.address,
                item.category,
                item.connectivity,
                "有" if item.has_gateway else ("无" if item.has_gateway is False else "未知"),
                role,
            )
        )
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]

    def line(values: tuple[str, ...]) -> str:
        return "  ".join(value.ljust(widths[index]) for index, value in enumerate(values))

    separator = tuple("-" * width for width in widths)
    return "\n".join([line(headers), line(separator), *(line(row) for row in rows)])
