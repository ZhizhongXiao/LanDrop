"""Bounded, read-only Windows network and firewall diagnostics."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from .network import POWERSHELL


NETWORK_DETAILS_TIMEOUT_SECONDS = 10
FIREWALL_TIMEOUT_SECONDS = 10

_NETWORK_DETAILS_SCRIPT = r"""
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$items = Get-NetIPConfiguration -All -ErrorAction Stop | ForEach-Object {
    $config = $_
    [PSCustomObject]@{
        alias = [string]$config.InterfaceAlias
        interface_index = [int]$config.InterfaceIndex
        description = [string]$config.InterfaceDescription
        status = [string]$config.NetAdapter.Status
        link_speed = [string]$config.NetAdapter.LinkSpeed
        ipv4 = @($config.IPv4Address | ForEach-Object { [string]$_.IPAddress })
        gateways = @($config.IPv4DefaultGateway | ForEach-Object { [string]$_.NextHop })
        dns = @($config.DNSServer.ServerAddresses | ForEach-Object { [string]$_ })
    }
}
[PSCustomObject]@{ adapters = @($items) } | ConvertTo-Json -Depth 5 -Compress
"""

_FIREWALL_SCRIPT = r"""
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$profiles = Get-NetFirewallProfile -ErrorAction Stop | ForEach-Object {
    [PSCustomObject]@{
        name = [string]$_.Name
        enabled = [bool]$_.Enabled
        default_inbound = [string]$_.DefaultInboundAction
        default_outbound = [string]$_.DefaultOutboundAction
    }
}
$enabledRules = @(Get-NetFirewallRule -PolicyStore ActiveStore -Enabled True -Direction Inbound -ErrorAction Stop)
$allApplications = @($enabledRules | Get-NetFirewallApplicationFilter -ErrorAction Stop)
$allPorts = @($enabledRules | Get-NetFirewallPortFilter -ErrorAction Stop)
$allAddresses = @($enabledRules | Get-NetFirewallAddressFilter -ErrorAction Stop)
$allInterfaces = @($enabledRules | Get-NetFirewallInterfaceFilter -ErrorAction Stop)

function Group-ByInstanceId {
    param([object[]]$Items)
    $table = @{}
    foreach ($item in $Items) {
        $key = [string]$item.InstanceID
        if (-not $table.ContainsKey($key)) {
            $table[$key] = [System.Collections.ArrayList]::new()
        }
        [void]$table[$key].Add($item)
    }
    return ,$table
}

$applicationsById = Group-ByInstanceId $allApplications
$portsById = Group-ByInstanceId $allPorts
$addressesById = Group-ByInstanceId $allAddresses
$interfacesById = Group-ByInstanceId $allInterfaces

$rules = $enabledRules | ForEach-Object {
        $rule = $_
        $key = [string]$rule.InstanceID
        $applications = @($applicationsById[$key])
        $ports = @($portsById[$key])
        $addresses = @($addressesById[$key])
        $interfaces = @($interfacesById[$key])
        if ($applications.Count -eq 0) { $applications = @($null) }
        if ($ports.Count -eq 0) { $ports = @($null) }
        foreach ($application in $applications) {
            foreach ($port in $ports) {
                $program = if ($application) { [string]$application.Program } else { 'Any' }
                $protocol = if ($port) { [string]$port.Protocol } else { 'Any' }
                $localPort = if ($port) { [string]$port.LocalPort } else { 'Any' }
                $isBroadBlock = ([string]$rule.Action -eq 'Block')
                if ($program -eq 'Any' -and $localPort -eq 'Any' -and -not $isBroadBlock) {
                    continue
                }
                [PSCustomObject]@{
                    name = [string]$rule.Name
                    display_name = [string]$rule.DisplayName
                    profile = [string]$rule.Profile
                    action = [string]$rule.Action
                    program = $program
                    protocol = $protocol
                    local_port = $localPort
                    local_address = @($addresses | ForEach-Object { [string]$_.LocalAddress })
                    remote_address = @($addresses | ForEach-Object { [string]$_.RemoteAddress })
                    interface_alias = @($interfaces | ForEach-Object { [string]$_.InterfaceAlias })
                }
            }
        }
    }
[PSCustomObject]@{ profiles = @($profiles); rules = @($rules) } |
    ConvertTo-Json -Depth 6 -Compress
"""


def inspect_system(port: int = 8000, program: str | None = None) -> dict[str, object]:
    """Run bounded diagnostics; every failure becomes data instead of an exception."""
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="LanDrop-Diagnostic") as pool:
        network_future = pool.submit(inspect_network_details)
        firewall_future = pool.submit(inspect_firewall, port, program or sys.executable)
        network = network_future.result()
        firewall = firewall_future.result()
    complete = network.get("status") == "ready" and firewall.get("status") == "ready"
    return {
        "status": "ready" if complete else "unknown",
        "message": (
            "网络与防火墙诊断已更新。"
            if complete
            else "部分诊断无法确定，请查看具体项目。"
        ),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "network": network,
        "firewall": firewall,
    }


def inspect_network_details() -> dict[str, object]:
    result, error = _run_powershell_json(
        _NETWORK_DETAILS_SCRIPT,
        timeout=NETWORK_DETAILS_TIMEOUT_SECONDS,
    )
    if error:
        return {"status": "unknown", "message": error, "adapters": []}
    adapters = result.get("adapters", []) if isinstance(result, dict) else []
    if isinstance(adapters, dict):
        adapters = [adapters]
    return {"status": "ready", "message": "网络详细信息已更新。", "adapters": adapters}


def inspect_firewall(port: int, program: str) -> dict[str, object]:
    result, error = _run_powershell_json(
        _FIREWALL_SCRIPT,
        timeout=FIREWALL_TIMEOUT_SECONDS,
    )
    if error:
        return _unknown_firewall(error)
    if not isinstance(result, dict):
        return _unknown_firewall("Windows 防火墙查询返回了无法识别的数据。")
    return _classify_firewall(result, port, program)


def _classify_firewall(
    result: dict[str, Any], port: int, program: str
) -> dict[str, object]:
    profiles = result.get("profiles") or []
    rules = result.get("rules") or []
    if isinstance(profiles, dict):
        profiles = [profiles]
    if isinstance(rules, dict):
        rules = [rules]

    target_program = _normalized_program(program)
    evidence: list[dict[str, object]] = []
    exact_port_allow = 0
    program_allow = 0
    broad_allow = 0
    relevant_blocks = 0

    for rule in rules:
        if not isinstance(rule, dict) or not _private_profile_applies(rule.get("profile")):
            continue
        protocol = str(rule.get("protocol") or "Any")
        if protocol.casefold() not in {"tcp", "6", "any"}:
            continue
        port_scope = _port_scope(str(rule.get("local_port") or "Any"), port)
        program_value = str(rule.get("program") or "Any")
        normalized_program = _normalized_program(program_value)
        program_match = bool(target_program) and normalized_program == target_program
        program_any = not normalized_program
        if port_scope == "none" or (not program_any and not program_match):
            continue

        action = str(rule.get("action") or "Unknown")
        if action.casefold() == "block":
            relevant_blocks += 1
            kind = "block"
        elif action.casefold() == "allow":
            if port_scope == "exact":
                exact_port_allow += 1
            if program_match:
                program_allow += 1
            if port_scope != "exact" and not program_match:
                broad_allow += 1
            kind = (
                "exact_port_allow"
                if port_scope == "exact"
                else ("program_allow" if program_match else "broad_allow")
            )
        else:
            continue
        if len(evidence) < 30:
            evidence.append(
                {
                    "kind": kind,
                    "name": str(rule.get("display_name") or rule.get("name") or "未命名规则"),
                    "profile": str(rule.get("profile") or "Unknown"),
                    "action": action,
                    "program": program_value,
                    "protocol": protocol,
                    "local_port": str(rule.get("local_port") or "Any"),
                }
            )

    private_profile = next(
        (
            profile
            for profile in profiles
            if isinstance(profile, dict)
            and str(profile.get("name") or "").casefold() == "private"
        ),
        {},
    )
    private_enabled = private_profile.get("enabled") if private_profile else None
    if relevant_blocks:
        level = "warning"
        message = "发现可能影响 LanDrop 的入站阻止规则，请检查证据。"
    elif exact_port_allow or program_allow:
        level = "ok"
        message = "发现明确的 Private 入站允许证据。"
    elif broad_allow:
        level = "info"
        message = "只发现较宽泛的允许证据，最终可达性仍以实机连接为准。"
    else:
        level = "warning"
        message = "未发现明确的 TCP 8000 或当前程序 Private 允许规则。"

    return {
        "status": "ready",
        "level": level,
        "message": message,
        "private_profile_enabled": private_enabled,
        "private_default_inbound": private_profile.get("default_inbound", "Unknown"),
        "exact_port_allow": exact_port_allow,
        "program_allow": program_allow,
        "broad_allow": broad_allow,
        "relevant_blocks": relevant_blocks,
        "evidence": evidence,
    }


def _unknown_firewall(message: str) -> dict[str, object]:
    return {
        "status": "unknown",
        "level": "unknown",
        "message": message,
        "private_profile_enabled": None,
        "private_default_inbound": "Unknown",
        "exact_port_allow": 0,
        "program_allow": 0,
        "broad_allow": 0,
        "relevant_blocks": 0,
        "evidence": [],
    }


def _private_profile_applies(value: object) -> bool:
    text = str(value or "").casefold()
    return "private" in text or text in {"any", "all"}


def _normalized_program(value: str) -> str:
    text = os.path.expandvars(value.strip().strip('"'))
    if text.casefold() in {"", "any", "*"}:
        return ""
    try:
        return str(Path(text).resolve()).casefold()
    except OSError:
        return text.casefold()


def _port_scope(value: str, target: int) -> str:
    text = value.strip()
    if text.casefold() in {"any", "*"}:
        return "any"
    for part in text.split(","):
        item = part.strip()
        if item == str(target):
            return "exact"
        separator = "-" if "-" in item else (":" if ":" in item else "")
        if separator:
            start, _, end = item.partition(separator)
            try:
                if int(start) <= target <= int(end):
                    return "broad"
            except ValueError:
                continue
    return "none"


def _run_powershell_json(
    script: str,
    *,
    timeout: float,
) -> tuple[object, str]:
    if os.name != "nt" or not os.path.isfile(POWERSHELL):
        return {}, "当前环境无法运行 Windows PowerShell 诊断。"
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
                script,
            ],
            check=False,
            capture_output=True,
            encoding="utf-8-sig",
            errors="replace",
            timeout=timeout,
            creationflags=creation_flags,
        )
    except subprocess.TimeoutExpired:
        return {}, f"诊断查询超过 {timeout:g} 秒，已终止。"
    except (OSError, subprocess.SubprocessError) as exc:
        return {}, f"无法执行诊断查询：{exc}"
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        return {}, detail or "诊断查询未成功。"
    try:
        return json.loads(completed.stdout.strip() or "{}"), ""
    except json.JSONDecodeError:
        return {}, "诊断查询返回了无法解析的数据。"
