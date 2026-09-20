"""Phase 7 local stability acceptance for the real LanDrop service stack.

This script deliberately uses temporary content, credentials, and session logs.
It exercises the real WSGI server and TCP 8000, but skips the asynchronous deep
firewall diagnostic because that path has its own regression coverage.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from pathlib import Path
import socket
import statistics
import sys
import tempfile
import threading
import time
from typing import Any
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from landrop.network import (  # noqa: E402
    LanInterface,
    discover_interfaces,
    select_interface,
    usable_lan_interfaces,
)
from landrop.service import ServiceController, ServiceSnapshot  # noqa: E402
from landrop.trust import CredentialStore  # noqa: E402


def _ready_diagnostics(_port: int, _program: str) -> dict[str, object]:
    return {
        "status": "ready",
        "message": "稳定性脚本跳过异步深度诊断。",
        "network": {"status": "ready", "adapters": []},
        "firewall": {
            "status": "ready",
            "level": "unknown",
            "message": "本脚本不重复扫描防火墙。",
            "evidence": [],
        },
    }


def _private_interfaces() -> list[LanInterface]:
    return [
        item
        for item in usable_lan_interfaces(discover_interfaces())
        if item.is_private_profile
    ]


def _choose_interface(selector: str | None) -> LanInterface:
    interfaces = discover_interfaces()
    if selector:
        return select_interface(interfaces, selector)
    private = [
        item
        for item in usable_lan_interfaces(interfaces)
        if item.is_private_profile
    ]
    if len(private) == 1:
        return private[0]
    if not private:
        raise RuntimeError("没有检测到可用于验收的 Private LAN endpoint。")
    choices = "\n".join(
        f"  {item.alias} · {item.description} · {item.address} · {item.category}"
        for item in private
    )
    raise RuntimeError(
        "检测到多个 Private LAN endpoint，请使用 --interface 指定 IPv4：\n"
        f"{choices}"
    )


def _port_open(port: int) -> bool:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _wait_port(port: int, expected: bool, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_open(port) is expected:
            return
        time.sleep(0.05)
    state = "监听" if expected else "关闭"
    raise RuntimeError(f"TCP {port} 未在 {timeout:.1f}s 内进入预期状态：{state}")


def _request_root(url: str) -> None:
    request = Request(url, method="HEAD")
    with urlopen(request, timeout=3) as response:  # noqa: S310 - loopback only
        if response.status != 200:
            raise RuntimeError(f"本机 HEAD 验证返回 HTTP {response.status}")


def _landrop_threads() -> list[str]:
    return sorted(
        thread.name
        for thread in threading.enumerate()
        if thread.name.startswith("LanDrop-")
    )


def _wait_threads_clear(timeout: float = 3.0) -> list[str]:
    deadline = time.monotonic() + timeout
    remaining = _landrop_threads()
    while remaining and time.monotonic() < deadline:
        time.sleep(0.05)
        remaining = _landrop_threads()
    return remaining


def _controller(
    data_directory: Path,
    *,
    duration_seconds: float = 300,
    grace_seconds: float = 60,
) -> ServiceController:
    return ServiceController(
        CredentialStore(data_directory),
        duration_seconds=duration_seconds,
        grace_seconds=grace_seconds,
        diagnostics_factory=_ready_diagnostics,
    )


def _assert_running(state: ServiceSnapshot, interface: LanInterface) -> None:
    if not state.running or state.phase != "running":
        raise RuntimeError(f"服务未进入运行状态：{state.phase} / {state.message}")
    if state.bound_ipv4 != interface.address:
        raise RuntimeError(
            f"监听 endpoint 改变：预期 {interface.address}，实际 {state.bound_ipv4}"
        )


def run_stability(
    *,
    cycles: int,
    resets: int,
    expiry_cycles: int,
    port: int,
    selector: str | None,
) -> None:
    if _port_open(port):
        raise RuntimeError(
            f"TCP {port} 已被占用。请先从托盘退出正在运行的 LanDrop。"
        )

    interface = _choose_interface(selector)
    print(
        "验收 endpoint："
        f"{interface.alias} · {interface.description} · "
        f"{interface.address} · {interface.category}"
    )
    print(
        f"计划：真实启停 {cycles} 次；倒计时重置 {resets} 次；"
        f"自然到期 {expiry_cycles} 次。"
    )

    start_times: list[float] = []
    stop_times: list[float] = []
    with tempfile.TemporaryDirectory(prefix="LanDrop-P7-stability-") as raw_root:
        root = Path(raw_root)
        shared = root / "shared"
        received = root / "received"
        shared.mkdir()
        received.mkdir()
        (shared / "stability-check.txt").write_text(
            "LanDrop phase 7 stability check\n", encoding="utf-8"
        )

        controller = _controller(root / "data")
        try:
            for cycle in range(1, cycles + 1):
                started = time.perf_counter()
                state = controller.start(
                    shared,
                    received,
                    1000,
                    interface_selector=interface.address,
                )
                start_times.append(time.perf_counter() - started)
                _assert_running(state, interface)
                _wait_port(port, True)
                _request_root(state.local_url)

                if cycle == 1:
                    revision = state.deadline_revision
                    for _ in range(resets):
                        state = controller.reset_deadline()
                        revision += 1
                        if state.deadline_revision != revision:
                            raise RuntimeError("倒计时重置 revision 未按预期递增。")
                        if not state.running:
                            raise RuntimeError("倒计时重置意外停止了服务。")

                stopped = time.perf_counter()
                state = controller.stop("manual_stop")
                stop_times.append(time.perf_counter() - stopped)
                if state.running or state.phase != "stopped":
                    raise RuntimeError(f"第 {cycle} 次停止未完成：{state.phase}")
                _wait_port(port, False)
                remaining = _wait_threads_clear()
                if remaining:
                    raise RuntimeError(
                        f"第 {cycle} 次停止后仍有 LanDrop 线程：{remaining}"
                    )
                print(f"启停 {cycle:02d}/{cycles}：通过")
        finally:
            controller.stop("manual_stop")

        expiry_controller = _controller(
            root / "expiry-data",
            duration_seconds=0.5,
            grace_seconds=0.2,
        )
        try:
            for cycle in range(1, expiry_cycles + 1):
                state = expiry_controller.start(
                    shared,
                    received,
                    1000,
                    interface_selector=interface.address,
                )
                _assert_running(state, interface)
                _wait_port(port, True)
                deadline = time.monotonic() + 4
                while expiry_controller.snapshot().running and time.monotonic() < deadline:
                    time.sleep(0.05)
                state = expiry_controller.snapshot()
                if state.running or state.stop_reason != "deadline_no_active":
                    raise RuntimeError(
                        f"自然到期 {cycle} 状态异常："
                        f"{state.phase} / {state.stop_reason or '-'}"
                    )
                _wait_port(port, False)
                remaining = _wait_threads_clear()
                if remaining:
                    raise RuntimeError(
                        f"自然到期 {cycle} 后仍有 LanDrop 线程：{remaining}"
                    )
                print(f"自然到期 {cycle:02d}/{expiry_cycles}：通过")
        finally:
            expiry_controller.stop("manual_stop")

    print("\n自动稳定性验收通过。")
    print(
        "启动耗时：平均 "
        f"{statistics.fmean(start_times):.3f}s，最大 {max(start_times):.3f}s"
    )
    print(
        "停止耗时：平均 "
        f"{statistics.fmean(stop_times):.3f}s，最大 {max(stop_times):.3f}s"
    )
    print(f"最终 TCP {port}：{'仍在监听' if _port_open(port) else '已关闭'}")
    print(f"最终 LanDrop 线程：{_landrop_threads() or '无'}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LanDrop 第七阶段本机稳定性验收")
    parser.add_argument("--cycles", type=int, default=30, help="真实启停次数")
    parser.add_argument("--resets", type=int, default=20, help="首次会话重置次数")
    parser.add_argument(
        "--expiry-cycles", type=int, default=3, help="短会话自然到期次数"
    )
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--interface", help="多个 Private endpoint 时指定 IPv4")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.cycles < 1 or args.resets < 0 or args.expiry_cycles < 1:
        raise SystemExit("cycles/expiry-cycles 必须大于 0，resets 不能小于 0。")
    try:
        run_stability(
            cycles=args.cycles,
            resets=args.resets,
            expiry_cycles=args.expiry_cycles,
            port=args.port,
            selector=args.interface,
        )
    except Exception as exc:
        print(f"稳定性验收失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
