"""Command-line interface for LanDrop."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
import sys
import time

from .network import (
    NetworkDiscoveryError,
    discover_interfaces,
    format_interfaces,
)
from .service import ServiceController, ServiceError
from .settings import (
    DEFAULT_MAX_UPLOAD_MB,
    SettingsError,
    SettingsStore,
    default_data_directory,
)
from .trust import CredentialStore


DEFAULT_PORT = 8000
DEFAULT_DATA_DIRECTORY = default_data_directory()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="LanDrop",
        description="在可信的 Windows Private 局域网中安全上传和下载文件。",
    )
    parser.add_argument(
        "--shared-dir",
        type=Path,
        default=None,
        help="提供客户机下载的目录（默认：用户 Downloads\\LanDrop\\Shared）",
    )
    parser.add_argument(
        "--receive-dir",
        type=Path,
        default=None,
        help="保存客户机上传文件的目录（默认：用户 Downloads\\LanDrop\\Received）",
    )
    parser.add_argument(
        "--max-upload-mb",
        type=int,
        default=None,
        help=f"单文件上传上限 MB（未指定时读取配置，初始为 {DEFAULT_MAX_UPLOAD_MB}）",
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
    parser.add_argument("--session-seconds", type=int, default=300, help=argparse.SUPPRESS)
    parser.add_argument("--grace-seconds", type=int, default=60, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_parser().parse_args(argv)
    credentials = CredentialStore(DEFAULT_DATA_DIRECTORY)
    if args.trusted_clients or args.forget_trusted:
        return _manage_trusted_clients(args, credentials)
    settings_store = SettingsStore(DEFAULT_DATA_DIRECTORY)
    settings = settings_store.load()
    if settings_store.last_warning:
        print(f"警告：{settings_store.last_warning}", file=sys.stderr)
    shared_option = args.shared_dir or settings.shared_directory
    receive_option = args.receive_dir or settings.receive_directory
    max_upload_mb = (
        args.max_upload_mb if args.max_upload_mb is not None else settings.max_upload_mb
    )
    if not 1 <= max_upload_mb <= 10_000:
        print("错误：上传上限必须在 1 到 10000 MB 之间。", file=sys.stderr)
        return 2
    if not 5 <= args.session_seconds <= 86_400 or not 0 <= args.grace_seconds <= 600:
        print("错误：测试会话需为 5–86400 秒，宽限需为 0–600 秒。", file=sys.stderr)
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
        settings_store.ensure_default_directories(settings)
        shared_directory = _existing_directory(shared_option, "下载来源")
        receive_directory = _existing_directory(receive_option, "上传保存")
    except (NetworkDiscoveryError, RuntimeError, SettingsError) as exc:
        print(f"无法启动：{exc}", file=sys.stderr)
        return 1

    controller = ServiceController(
        credentials,
        duration_seconds=args.session_seconds,
        grace_seconds=args.grace_seconds,
    )
    try:
        state = controller.start(
            shared_directory,
            receive_directory,
            max_upload_mb,
            args.interface,
        )
    except ServiceError as exc:
        print(f"无法启动：{exc}", file=sys.stderr)
        return 1

    print()
    print("LanDrop 双向传输服务已启动")
    print(f"提供下载的目录：{shared_directory}")
    print(f"保存上传的目录：{receive_directory}")
    print(f"上传上限：{max_upload_mb} MB")
    print(f"服务机本地访问：{state.local_url}")
    print(f"客户机访问：{state.lan_url}")
    network_name = (
        f" · {state.network_name}"
        if state.network_name and state.network_name != state.interface
        else ""
    )
    print(f"服务机监听网络：{state.interface}{network_name}（{state.network_category}）")
    print(f"本次配对码：{state.pairing_code}")
    print("配对码单次有效；新客户机配对后控制台会显示新码。")
    print(f"会话时长：{args.session_seconds} 秒；传输宽限：{args.grace_seconds} 秒")
    print("按 Ctrl+C 停止服务。")

    previous_code = state.pairing_code
    try:
        while state.running:
            time.sleep(1)
            state = controller.snapshot()
            if state.pairing_code and state.pairing_code != previous_code:
                previous_code = state.pairing_code
                print(f"\n新配对码：{previous_code}")
            remaining = (
                state.grace_remaining_seconds
                if state.phase == "grace"
                else state.remaining_seconds
            )
            label = "宽限剩余" if state.phase == "grace" else "会话剩余"
            print(f"\r{label}：{remaining:>3} 秒  活动传输：{state.active_transfers}", end="", flush=True)
    except KeyboardInterrupt:
        print("\n正在停止 LanDrop……")
        state = controller.stop("manual_stop")
    finally:
        if controller.snapshot().running:
            state = controller.stop("manual_stop")
    print(f"\n{state.message}")
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
                print(
                    f"{client.client_id}  {client.created_at}  {client.label}  "
                    f"{client.device_type} / {client.operating_system} / {client.browser}"
                )
            return 0

        selector = args.forget_trusted
        if selector == "all":
            count = store.revoke_all()
            print(f"已取消 {count} 个浏览器的信任。")
            return 0
        if store.revoke(selector):
            print(f"已取消浏览器信任：{selector}")
            return 0
        print(f"未找到可信客户机：{selector}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
