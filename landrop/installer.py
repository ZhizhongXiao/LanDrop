"""First-install transaction for the frozen Phase 8B contract."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
from typing import Callable

from .install_contract import (
    InstallPaths,
    new_transaction_id,
    transaction_directory_name,
    validate_install_child,
    validate_transaction_directory,
    is_reparse_object,
)
from .install_lock import InstallLifecycleLock
from .install_state import (
    InstallHistoryLog,
    InstallRecord,
    InstallationStateStore,
    TransactionRecord,
)
from .payload_manifest import PayloadManifest, verify_payload
from .system_integration import (
    FirstInstallIntegration,
    IntegrationPlan,
    installed_size_kib,
)


class FirstInstallError(RuntimeError):
    """The first-install transaction could not be completed safely."""


@dataclass(frozen=True, slots=True)
class FirstInstallOptions:
    desktop_shortcut: bool = False


@dataclass(frozen=True, slots=True)
class FirstInstallOutcome:
    result: str
    verified: bool
    rollback_attempted: bool
    rollback_succeeded: bool | None
    message: str
    warning: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "result": self.result,
            "verified": self.verified,
            "rollback_attempted": self.rollback_attempted,
            "rollback_succeeded": self.rollback_succeeded,
            "message": self.message,
            "warning": self.warning,
        }


class FirstInstallService:
    """Commit one new current-user installation or roll it back completely."""

    def __init__(
        self,
        *,
        paths: InstallPaths,
        payload_root: Path,
        manifest: PayloadManifest,
        integration: FirstInstallIntegration,
        self_check: Callable[[Path], bool] | None = None,
        state_store: InstallationStateStore | None = None,
        history: InstallHistoryLog | None = None,
        lifecycle_lock: InstallLifecycleLock | None = None,
    ) -> None:
        self.paths = paths
        self.payload_root = Path(payload_root)
        self.manifest = manifest
        self.integration = integration
        self.self_check = self_check or run_installed_self_check
        self.state_store = state_store or InstallationStateStore(paths)
        self.history = history or InstallHistoryLog(paths)
        self.lifecycle_lock = lifecycle_lock or InstallLifecycleLock(paths)

    def install(
        self,
        options: FirstInstallOptions,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> FirstInstallOutcome:
        if not isinstance(options, FirstInstallOptions):
            raise TypeError("首次安装选项类型无效。")
        report = progress or (lambda _stage: None)
        acquired = False
        plan: IntegrationPlan | None = None
        transaction: TransactionRecord | None = None
        product_root_created = False
        integration_started = False
        committed = False
        try:
            report("acquiring_lock")
            acquired = self.lifecycle_lock.acquire(0.0)
            if not acquired:
                raise FirstInstallError("LanDrop 正在维护，请稍后重试。")

            report("preflight")
            self._assert_first_install_target_empty()
            verify_payload(self.payload_root, self.manifest)
            self._assert_required_payload_files(self.payload_root)
            plan = IntegrationPlan.create(
                self.paths,
                version=self.manifest.version,
                estimated_size_kib=max(
                    1,
                    (sum(entry.size for entry in self.manifest.files) + 1023) // 1024,
                ),
                desktop_enabled=options.desktop_shortcut,
            )
            self.integration.assert_absent(plan)

            transaction_id = new_transaction_id()
            staging_name = transaction_directory_name(
                "staging", self.manifest.version, transaction_id
            )
            transaction = TransactionRecord.create(
                transaction_id=transaction_id,
                kind="install",
                target_version=self.manifest.version,
                target_build_id=self.manifest.build_id,
                staging_directory=staging_name,
                integration_snapshot=plan.snapshot_json(),
            )
            self.paths.install_root.mkdir(parents=True, exist_ok=False)
            product_root_created = True
            self.state_store.write_transaction(transaction)

            report("staging")
            staging = validate_transaction_directory(
                self.paths.install_root / staging_name,
                self.paths,
            )
            staging.mkdir()
            _copy_manifest_payload(self.payload_root, staging, self.manifest)
            verify_payload(staging, self.manifest)
            self._assert_required_payload_files(staging)

            report("app_switch")
            _activate_staged_first_install(staging, self.paths)
            transaction = transaction.advance("app_switched")
            self.state_store.write_transaction(transaction)

            report("self_check")
            if not self.self_check(self.paths.main_executable):
                raise FirstInstallError("正式 app 路径的 LanDrop --self-check 失败。")

            report("integration_write")
            integration_started = True
            self.integration.write(plan)
            transaction = transaction.advance("integration_written")
            self.state_store.write_transaction(transaction)

            report("integration_verify")
            self.integration.verify(plan)
            transaction = transaction.advance("integration_verified")
            self.state_store.write_transaction(transaction)

            report("commit")
            if self.state_store.read_install() is not None:
                raise FirstInstallError("首次安装 commit 前意外出现 install.json。")
            installed_size_kib(self.paths.install_root)
            install_record = InstallRecord.create(
                self.paths,
                version=self.manifest.version,
                build_id=self.manifest.build_id,
                installed_at=transaction.created_at,
                updated_at=transaction.updated_at,
            )
            self.state_store.write_install(install_record)
            committed = True

            warning = ""
            try:
                self.history.append(
                    "installed",
                    version=self.manifest.version,
                    result="ok",
                    transaction_id=transaction.transaction_id,
                )
            except Exception as exc:
                warning = f"安装成功，但写入安装历史失败：{exc}"
            self.state_store.remove_transaction()
            report("completed")
            return FirstInstallOutcome(
                result="success-with-warning" if warning else "success",
                verified=True,
                rollback_attempted=False,
                rollback_succeeded=None,
                message="LanDrop 已完成当前用户首次安装。",
                warning=warning,
            )
        except Exception as exc:
            report("rollback")
            rollback_errors = self._rollback_first_install(
                plan,
                product_root_created=product_root_created,
                integration_started=integration_started,
                committed=committed,
            )
            rollback_succeeded = not rollback_errors
            if transaction is not None:
                self._record_rollback_history(transaction, rollback_succeeded, rollback_errors)
            detail = f"{type(exc).__name__}: {exc}"
            if rollback_errors:
                detail += "；回滚残留：" + "；".join(rollback_errors)
            return FirstInstallOutcome(
                result="failed" if rollback_succeeded else "partial",
                verified=False,
                rollback_attempted=product_root_created or plan is not None,
                rollback_succeeded=rollback_succeeded,
                message=detail[:1000],
            )
        finally:
            self.lifecycle_lock.close()

    def _assert_first_install_target_empty(self) -> None:
        validate_install_child(self.paths.install_root, self.paths, allow_root=True)
        if os.path.lexists(self.paths.install_root):
            raise FirstInstallError(
                "固定安装根已经存在；本轮仅支持真实首次安装，不覆盖或升级。"
            )

    @staticmethod
    def _assert_required_payload_files(root: Path) -> None:
        required = (root / "app" / "LanDrop.exe", root / "maintenance" / "Uninstall.exe")
        if any(not path.is_file() or is_reparse_object(path) for path in required):
            raise FirstInstallError("payload 缺少 app/LanDrop.exe 或 maintenance/Uninstall.exe。")

    def _rollback_first_install(
        self,
        plan: IntegrationPlan | None,
        *,
        product_root_created: bool,
        integration_started: bool,
        committed: bool,
    ) -> list[str]:
        errors: list[str] = []
        if plan is not None and integration_started:
            try:
                self.integration.rollback(plan)
            except Exception as exc:
                errors.append(f"系统集成：{exc}")
        if committed:
            try:
                self.state_store.remove_install()
            except Exception as exc:
                errors.append(f"install.json：{exc}")
        if product_root_created:
            try:
                _remove_created_install_root(self.paths)
            except Exception as exc:
                errors.append(f"程序根：{exc}")
        return errors

    def _record_rollback_history(
        self,
        transaction: TransactionRecord,
        succeeded: bool,
        errors: list[str],
    ) -> None:
        try:
            self.history.append(
                "rollback_started",
                version=transaction.target_version,
                result="first_install_failed",
                transaction_id=transaction.transaction_id,
            )
            self.history.append(
                "rollback_completed",
                version=transaction.target_version,
                result="ok" if succeeded else "partial",
                transaction_id=transaction.transaction_id,
                details={"residual_count": len(errors)},
            )
        except Exception:
            pass


def run_installed_self_check(executable: Path, *, timeout_seconds: float = 60.0) -> bool:
    target = Path(executable)
    if not target.is_file() or is_reparse_object(target):
        return False
    try:
        completed = subprocess.run(
            [str(target), "--self-check"],
            cwd=target.parent,
            check=False,
            timeout=timeout_seconds,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _copy_manifest_payload(
    source_root: Path,
    destination_root: Path,
    manifest: PayloadManifest,
) -> None:
    for entry in manifest.files:
        relative = Path(*entry.relative_path.split("/"))
        source = source_root / relative
        destination = destination_root / relative
        if not source.is_file() or is_reparse_object(source):
            raise FirstInstallError(f"payload 来源文件不安全：{entry.relative_path}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _activate_staged_first_install(staging: Path, paths: InstallPaths) -> None:
    app_source = staging / "app"
    maintenance_source = staging / "maintenance"
    if paths.app_directory.exists() or paths.maintenance_directory.exists():
        raise FirstInstallError("正式 app 或 maintenance 路径在提交前已经存在。")
    os.replace(app_source, paths.app_directory)
    os.replace(maintenance_source, paths.maintenance_directory)
    try:
        staging.rmdir()
    except OSError as exc:
        raise FirstInstallError(f"staging 激活后仍包含未知内容：{exc}") from exc


def _remove_created_install_root(paths: InstallPaths) -> None:
    root = validate_install_child(paths.install_root, paths, allow_root=True)
    if not os.path.lexists(root):
        return
    if is_reparse_object(root):
        raise FirstInstallError("拒绝清理 reparse 产品根。")
    for directory, directory_names, file_names in os.walk(root, topdown=False, followlinks=False):
        current = Path(directory)
        if is_reparse_object(current):
            raise FirstInstallError(f"程序根包含 reparse 目录：{current}")
        for name in (*directory_names, *file_names):
            child = current / name
            if is_reparse_object(child):
                raise FirstInstallError(f"程序根包含 reparse object：{child}")
    shutil.rmtree(root)
