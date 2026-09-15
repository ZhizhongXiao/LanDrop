"""Command-line interface for LanDrop."""

from __future__ import annotations

import argparse
import errno
import os
from pathlib import Path
import sys

from .network import (
    NetworkDiscoveryError,
    discover_interfaces,
    format_interfaces,
    select_interface,
)
from .server import ServerGroup
from .trust import CredentialStore
from .web import WebConfig, create_application


DEFAULT_PORT = 8000
DEFAULT_MAX_UPLOAD_MIB = 1024
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SHARED_DIRECTORY = PROJECT_ROOT / "shared"
DEFAULT_RECEIVE_DIRECTORY = PROJECT_ROOT / "received"
DEFAULT_DATA_DIRECTORY = Path(
    os.environ.get("LOCALAPPDATA") or PROJECT_ROOT / ".landrop-data"
) / "LanDrop"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="LanDrop",
        description="在可信的 Windows Private 局域网中安全上传和下载文件。",
    )
    parser.add_argument(
        "--shared-dir",
        type=Path,
        default=DEFAULT_SHARED_DIRECTORY,
        help="下载共享目录（默认：项目中的 shared 目录）",
    )
    parser.add_argument(
        "--receive-dir",
        type=Path,
        default=DEFAULT_RECEIVE_DIRECTORY,
        help="上传接收目录（默认：项目中的 received 目录）",
    )
    parser.add_argument(
        "--max-upload-mib",
        type=int,
        default=DEFAULT_MAX_UPLOAD_MIB,
        help=f"单文件上传上限 MiB（默认：{DEFAULT_MAX_UPLOAD_MIB}）",
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
    management = parser.add_mutually_exclusive_group()
    management.add_argument(
        "--trusted-clients",
        action="store_true",
        help="列出已配对浏览器，不启动服务",
    )
    management.add_argument(
        "--forget-trusted",
        metavar="CLIENT_ID|all",
        help="取消一个或全部浏览器信任，不启动服务",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    credentials = CredentialStore(DEFAULT_DATA_DIRECTORY)
    if args.trusted_clients or args.forget_trusted:
        return _manage_trusted_clients(args, credentials)
    if not 1 <= args.max_upload_mib <= 10_240:
        print("错误：上传上限必须在 1 到 10240 MiB 之间。", file=sys.stderr)
        return 2

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
        shared_directory = _existing_directory(args.shared_dir, "共享")
        receive_directory = _existing_directory(args.receive_dir, "接收")
    except (NetworkDiscoveryError, RuntimeError) as exc:
        print(f"无法启动：{exc}", file=sys.stderr)
        return 1

    max_upload_bytes = args.max_upload_mib * 1024 * 1024
    application, pairing_code = create_application(
        WebConfig(
            shared_directory=shared_directory,
            receive_directory=receive_directory,
            max_upload_bytes=max_upload_bytes,
            credentials=credentials,
        )
    )
    try:
        servers = ServerGroup(interface.address, DEFAULT_PORT, application)
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
    print("LanDrop 双向传输服务已启动")
    print(f"下载目录：{shared_directory}")
    print(f"接收目录：{receive_directory}")
    print(f"上传上限：{args.max_upload_mib} MiB")
    print(f"电脑本机：http://127.0.0.1:{DEFAULT_PORT}/")
    print(f"手机访问：http://{interface.address}:{DEFAULT_PORT}/")
    print(f"网络接口：{interface.alias}（{interface.category}）")
    print(f"本次配对码：{pairing_code}")
    print("新浏览器需要输入配对码；已信任浏览器会自动识别。")
    print("按 Ctrl+C 停止服务。")

    try:
        servers.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止 LanDrop……")
    finally:
        servers.close()
    print("服务已停止，监听端口已关闭。")
    return 0


def _existing_directory(path: Path, label: str) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise RuntimeError(f"{label}目录不存在：{path}") from exc
    except OSError as exc:
        raise RuntimeError(f"无法读取{label}目录：{exc}") from exc
    if not resolved.is_dir():
        raise RuntimeError(f"{label}路径不是目录：{resolved}")
    return resolved


def _manage_trusted_clients(args: argparse.Namespace, store: CredentialStore) -> int:
    try:
        if args.trusted_clients:
            clients = store.list_clients()
            if not clients:
                print("当前没有已配对浏览器。")
                return 0
            for client in clients:
                print(f"{client.client_id}  {client.created_at}  {client.label}")
            return 0

        selector = args.forget_trusted
        if selector == "all":
            count = store.revoke_all()
            print(f"已取消 {count} 个浏览器的信任。")
            return 0
        if store.revoke(selector):
            print(f"已取消浏览器信任：{selector}")
            return 0
        print(f"未找到可信客户端：{selector}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
