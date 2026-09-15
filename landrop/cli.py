"""Command-line interface for the first LanDrop milestone."""

from __future__ import annotations

import argparse
import errno
from pathlib import Path
import sys

from .network import (
    NetworkDiscoveryError,
    discover_interfaces,
    format_interfaces,
    select_interface,
)
from .server import ServerGroup


DEFAULT_PORT = 8000
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SHARED_DIRECTORY = PROJECT_ROOT / "shared"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="LanDrop",
        description="在可信的 Windows Private 局域网中提供临时只读文件下载。",
    )
    parser.add_argument(
        "--shared-dir",
        type=Path,
        default=DEFAULT_SHARED_DIRECTORY,
        help="共享目录（默认：项目中的 shared 目录）",
    )
    parser.add_argument(
        "--interface",
        help="存在多个 LAN 候选时，指定接口名或具体 IPv4",
    )
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="只显示网络检测结果，不启动服务",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        interfaces = discover_interfaces()
    except NetworkDiscoveryError as exc:
        print(f"网络检测失败：{exc}", file=sys.stderr)
        return 1

    print("Windows 网络检测结果：")
    print(format_interfaces(interfaces))
    if args.diagnose:
        return 0

    try:
        interface = select_interface(interfaces, args.interface)
        shared_directory = args.shared_dir.expanduser().resolve(strict=True)
    except NetworkDiscoveryError as exc:
        print(f"无法启动：{exc}", file=sys.stderr)
        return 1
    except FileNotFoundError:
        print(f"无法启动：共享目录不存在：{args.shared_dir}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"无法读取共享目录：{exc}", file=sys.stderr)
        return 1

    if not shared_directory.is_dir():
        print(f"无法启动：共享路径不是目录：{shared_directory}", file=sys.stderr)
        return 1

    try:
        servers = ServerGroup(interface.address, DEFAULT_PORT, shared_directory)
    except OSError as exc:
        if exc.errno in {errno.EADDRINUSE, 10048} or getattr(exc, "winerror", None) == 10048:
            print(f"无法启动：端口 {DEFAULT_PORT} 已被占用。", file=sys.stderr)
        elif getattr(exc, "winerror", None) == 10013:
            print(
                f"无法启动：没有权限监听 {interface.address}:{DEFAULT_PORT}。",
                file=sys.stderr,
            )
        else:
            print(f"无法启动服务器：{exc}", file=sys.stderr)
        return 1

    print()
    print("LanDrop 只读下载服务已启动")
    print(f"共享目录：{shared_directory}")
    print(f"电脑本机：http://127.0.0.1:{DEFAULT_PORT}/")
    print(f"手机访问：http://{interface.address}:{DEFAULT_PORT}/")
    print(f"网络接口：{interface.alias}（{interface.category}）")
    print("只允许 GET/HEAD；不提供上传、删除或重命名。")
    print("按 Ctrl+C 停止服务。")

    try:
        servers.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止 LanDrop……")
    finally:
        servers.close()
    print("服务已停止，监听端口已关闭。")
    return 0
