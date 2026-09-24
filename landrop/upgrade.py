"""Transactional A-to-B upgrades and committed-payload cleanup."""

from __future__ import annotations

from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import re
import shutil
from typing import Callable

from .install_contract import (
    InstallPaths,
    is_reparse_object,
    new_transaction_id,
    transaction_directory_name,
    validate_install_child,
    validate_transaction_directory,
)
from .install_lock import InstallLifecycleLock
from .install_state import (
    InstallHistoryLog,
    InstallRecord,
    InstallationStateStore,
    TransactionRecord,
)
from .installer import _copy_manifest_payload, run_installed_self_check
from .payload_manifest import PayloadFile, PayloadManifest, verify_payload
from .system_integration import (
    IntegrationPlan,
    UpgradeIntegration,
    UpgradeIntegrationSnapshot,
)


_VERSION_PATTERN = re.compile(r"(0|[1-9][0-9]*)(?:\.(0|[1-9][0-9]*)){1,3}\Z")
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class UpgradeError(RuntimeError):
    """An installed state is unsafe or an upgrade could not be committed."""


@dataclass(frozen=True, slots=True)
class SetupInspection:
    disposition: str
    message: str
    current_version: str = ""
    pending_cleanup: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class UpgradeOutcome:
    result: str
    verified: bool
    committed: bool
    rollback_attempted: bool
    rollback_succeeded: bool | None
    cleanup_pending: bool
    message: str
    warning: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "result": self.result,
            "verified": self.verified,
            "committed": self.committed,
            "rollback_attempted": self.rollback_attempted,
            "rollback_succeeded": self.rollback_succeeded,
            "cleanup_pending": self.cleanup_pending,
            "message": self.message,
            "warning": self.warning,
        }


@dataclass(frozen=True, slots=True)
class CleanupResult:
    attempted: int
    removed: int
    remaining: tuple[str, ...]
    errors: tuple[str, ...]


class UpgradeService:
    """Upgrade one validated installed app directory without mixed overlay."""

    def __init__(
        self,
        *,
        paths: InstallPaths,
        payload_root: Path,
        manifest: PayloadManifest,
        integration: UpgradeIntegration,
        self_check: Callable[[Path], bool] | None = None,
        state_store: InstallationStateStore | None = None,
        history: InstallHistoryLog | None = None,
        lifecycle_lock: InstallLifecycleLock | None = None,
        executable_running: Callable[[Path], bool] | None = None,
        checkpoint: Callable[[str], None] | None = None,
        remove_tree: Callable[[Path, InstallPaths], None] | None = None,
    ) -> None:
        self.paths = paths
        self.payload_root = Path(payload_root)
        self.manifest = manifest
        self.integration = integration
        self.self_check = self_check or run_installed_self_check
        self.state_store = state_store or InstallationStateStore(paths)
        self.history = history or InstallHistoryLog(paths)
        self.lifecycle_lock = lifecycle_lock or InstallLifecycleLock(paths)
        self.executable_running = executable_running or is_executable_running
        self.checkpoint = checkpoint or (lambda _point: None)
        self.remove_tree = remove_tree or _remove_transaction_tree

    def upgrade(
        self,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> UpgradeOutcome:
        report = progress or (lambda _stage: None)
        transaction: TransactionRecord | None = None
        current: InstallRecord | None = None
        snapshot: UpgradeIntegrationSnapshot | None = None
        staging: Path | None = None
        rollback: Path | None = None
        app_switched = False
        integration_started = False
        commit_started = False
        committed = False
        acquired = False
        warning_parts: list[str] = []
        try:
            # Read-only preliminary classification improves error messages; every
            # authority decision is repeated after taking the lifecycle lock.
            preliminary = inspect_setup_state(
                self.paths,
                self.manifest,
                state_store=self.state_store,
            )
            if preliminary.disposition not in {
                "upgrade",
                "already_installed",
                "incomplete",
            }:
                raise UpgradeError(preliminary.message)

            report("acquiring_lock")
            acquired = self.lifecycle_lock.acquire(0.0)
            if not acquired:
                raise UpgradeError("LanDrop 正在维护，请稍后重试。")

            report("preflight")
            if os.path.lexists(self.paths.transaction_state_path):
                recovered = self._recover_interrupted_upgrade(report)
                if recovered is not None:
                    return recovered
            self.state_store.ensure_normal_start_allowed()
            current = self.state_store.read_install(required=True)
            assert current is not None
            _validate_installed_layout(self.paths, current)

            if current.pending_cleanup:
                cleanup = retry_pending_cleanup(
                    self.paths,
                    state_store=self.state_store,
                    history=self.history,
                    remove_tree=self.remove_tree,
                )
                if cleanup.remaining:
                    raise UpgradeError(
                        "旧 payload 清理仍未完成，不能开始下一次升级："
                        + "；".join(cleanup.errors)
                    )
                current = self.state_store.read_install(required=True)
                assert current is not None

            comparison = compare_versions(self.manifest.version, current.version)
            if comparison == 0:
                if self.manifest.build_id != current.build_id:
                    raise UpgradeError(
                        "检测到同版本但不同 build id；不支持覆盖或修复安装。"
                    )
                return UpgradeOutcome(
                    result="already-installed",
                    verified=True,
                    committed=False,
                    rollback_attempted=False,
                    rollback_succeeded=None,
                    cleanup_pending=False,
                    message=f"当前版本 {current.version} 已安装，无需重复覆盖。",
                )
            if comparison < 0:
                raise UpgradeError(
                    f"不支持从 {current.version} 自动降级到 {self.manifest.version}。"
                )
            if self.executable_running(self.paths.main_executable):
                raise UpgradeError("LanDrop 正在运行；请从托盘退出后重试升级。")

            verify_payload(self.payload_root, self.manifest)
            app_manifest = _subtree_manifest(self.manifest, "app")
            maintenance_manifest = _subtree_manifest(self.manifest, "maintenance")
            old_plan = IntegrationPlan.create(
                self.paths,
                version=current.version,
                estimated_size_kib=1,
                desktop_enabled=False,
            )
            snapshot = self.integration.snapshot(old_plan)

            transaction_id = new_transaction_id()
            staging_name = transaction_directory_name(
                "staging", self.manifest.version, transaction_id
            )
            rollback_name = transaction_directory_name(
                "rollback", current.version, transaction_id
            )
            transaction = TransactionRecord.create(
                transaction_id=transaction_id,
                kind="upgrade",
                source_version=current.version,
                source_build_id=current.build_id,
                target_version=self.manifest.version,
                target_build_id=self.manifest.build_id,
                staging_directory=staging_name,
                rollback_directory=rollback_name,
                integration_snapshot=snapshot.to_json(),
            )
            self.state_store.write_transaction(transaction)
            try:
                self._append_history(
                    "upgrade_started",
                    current,
                    transaction,
                    result="started",
                )
            except Exception as exc:
                warning_parts.append(f"安装历史写入失败：{exc}")

            report("staging")
            staging = validate_transaction_directory(
                self.paths.install_root / staging_name,
                self.paths,
            )
            rollback = validate_transaction_directory(
                self.paths.install_root / rollback_name,
                self.paths,
            )
            staging.mkdir()
            _copy_manifest_payload(
                self.payload_root / "app",
                staging / "app",
                app_manifest,
            )
            _copy_manifest_payload(
                self.payload_root / "maintenance",
                staging / "maintenance",
                maintenance_manifest,
            )
            self.checkpoint("before_staging_verify")
            verify_payload(staging / "app", app_manifest)
            verify_payload(staging / "maintenance", maintenance_manifest)
            if self.executable_running(self.paths.main_executable):
                raise UpgradeError("LanDrop 在升级提交前仍在运行。")

            report("app_switch")
            rollback.mkdir()
            self.checkpoint("before_app_to_rollback")
            _rename_owned_directory(
                self.paths.app_directory,
                rollback / "app",
                self.paths,
            )
            self.checkpoint("before_maintenance_to_rollback")
            _rename_owned_directory(
                self.paths.maintenance_directory,
                rollback / "maintenance",
                self.paths,
            )
            self.checkpoint("before_staging_to_app")
            _rename_owned_directory(
                staging / "app",
                self.paths.app_directory,
                self.paths,
            )
            self.checkpoint("before_staging_maintenance_to_live")
            _rename_owned_directory(
                staging / "maintenance",
                self.paths.maintenance_directory,
                self.paths,
            )
            verify_payload(self.paths.app_directory, app_manifest)
            verify_payload(self.paths.maintenance_directory, maintenance_manifest)
            app_switched = True
            transaction = transaction.advance("app_switched")
            self.state_store.write_transaction(transaction)

            report("self_check")
            if not self.self_check(self.paths.main_executable):
                raise UpgradeError("新版正式 app 路径的 LanDrop --self-check 失败。")

            report("integration_write")
            integration_started = True
            estimated_size = max(
                1,
                (sum(entry.size for entry in self.manifest.files) + 1023) // 1024,
            )
            self.integration.update_registration(
                snapshot,
                version=self.manifest.version,
                estimated_size_kib=estimated_size,
            )
            transaction = transaction.advance("integration_written")
            self.state_store.write_transaction(transaction)

            report("integration_verify")
            self.integration.verify_upgrade(
                snapshot,
                version=self.manifest.version,
                estimated_size_kib=estimated_size,
            )
            transaction = transaction.advance("integration_verified")
            self.state_store.write_transaction(transaction)

            report("commit")
            self.checkpoint("before_install_commit")
            commit_started = True
            committed_record = InstallRecord.create(
                self.paths,
                version=self.manifest.version,
                build_id=self.manifest.build_id,
                installed_at=current.installed_at,
                updated_at=transaction.updated_at,
            )
            self.state_store.write_install(committed_record)
            committed = True
            try:
                self._append_history(
                    "upgrade_committed",
                    current,
                    transaction,
                    result="ok",
                )
                self.history.append(
                    "installed",
                    version=committed_record.version,
                    result="upgrade",
                    from_version=current.version,
                    to_version=committed_record.version,
                    transaction_id=transaction.transaction_id,
                )
            except Exception as exc:
                warning_parts.append(f"安装历史写入失败：{exc}")

            report("cleanup")
            try:
                self.checkpoint("before_committed_cleanup")
                cleanup_pending, cleanup_warning = self._finalize_committed_cleanup(
                    committed_record,
                    transaction,
                    staging,
                    rollback,
                )
                if cleanup_warning:
                    warning_parts.append(cleanup_warning)
            except Exception:
                # transaction.json deliberately remains authoritative until
                # rollback is gone or pending_cleanup has been published.
                raise
            if cleanup_pending:
                report("completed")
                return UpgradeOutcome(
                    result="success-with-warning",
                    verified=True,
                    committed=True,
                    rollback_attempted=False,
                    rollback_succeeded=None,
                    cleanup_pending=True,
                    message=f"LanDrop 已升级到 {self.manifest.version}。",
                    warning="；".join(warning_parts),
                )

            try:
                self.history.append(
                    "old_payload_removed",
                    version=self.manifest.version,
                    result="ok",
                    from_version=current.version,
                    to_version=self.manifest.version,
                    transaction_id=transaction.transaction_id,
                )
            except Exception as exc:
                warning_parts.append(f"清理历史写入失败：{exc}")
            report("completed")
            return UpgradeOutcome(
                result="success-with-warning" if warning_parts else "success",
                verified=True,
                committed=True,
                rollback_attempted=False,
                rollback_succeeded=None,
                cleanup_pending=False,
                message=f"LanDrop 已升级到 {self.manifest.version}。",
                warning="；".join(warning_parts),
            )
        except Exception as exc:
            if committed:
                return UpgradeOutcome(
                    result="partial",
                    verified=False,
                    committed=True,
                    rollback_attempted=False,
                    rollback_succeeded=None,
                    cleanup_pending=bool(
                        self.state_store.read_install(required=True).pending_cleanup
                    ),
                    message=f"升级已提交，但提交后收尾失败：{type(exc).__name__}: {exc}"[:1000],
                )
            report("rollback")
            rollback_errors = self._rollback_precommit(
                current=current,
                transaction=transaction,
                snapshot=snapshot,
                staging=staging,
                rollback=rollback,
                app_switched=app_switched,
                integration_started=integration_started,
                commit_started=commit_started,
            )
            succeeded = not rollback_errors
            detail = f"{type(exc).__name__}: {exc}"
            if rollback_errors:
                detail += "；回滚残留：" + "；".join(rollback_errors)
            return UpgradeOutcome(
                result="failed" if succeeded else "partial",
                verified=False,
                committed=False,
                rollback_attempted=transaction is not None,
                rollback_succeeded=succeeded,
                cleanup_pending=False,
                message=detail[:1000],
            )
        finally:
            if acquired:
                self.lifecycle_lock.close()

    def _finalize_committed_cleanup(
        self,
        committed_record: InstallRecord,
        transaction: TransactionRecord,
        staging: Path,
        rollback: Path,
    ) -> tuple[bool, str]:
        """Transfer ownership before deleting the transaction journal."""
        if os.path.lexists(staging):
            self.remove_tree(staging, self.paths)
        try:
            if os.path.lexists(rollback):
                self.remove_tree(rollback, self.paths)
        except Exception as exc:
            pending_names = tuple(
                dict.fromkeys((*committed_record.pending_cleanup, rollback.name))
            )
            pending_record = InstallRecord.create(
                self.paths,
                version=committed_record.version,
                build_id=committed_record.build_id,
                installed_at=committed_record.installed_at,
                pending_cleanup=pending_names,
            )
            self.state_store.write_install(pending_record)
            confirmed = self.state_store.read_install(required=True)
            if confirmed is None or confirmed.pending_cleanup != pending_names:
                raise UpgradeError("pending_cleanup 发布后读回不一致。")
            self.state_store.remove_transaction()
            try:
                self.history.append(
                    "cleanup_pending",
                    version=pending_record.version,
                    result="pending",
                    from_version=transaction.source_version,
                    to_version=pending_record.version,
                    transaction_id=transaction.transaction_id,
                    details={"directory": rollback.name},
                )
            except Exception:
                pass
            return True, f"旧 payload 将稍后重试清理：{exc}"

        if rollback.name in committed_record.pending_cleanup:
            remaining = tuple(
                name for name in committed_record.pending_cleanup if name != rollback.name
            )
            cleaned_record = InstallRecord.create(
                self.paths,
                version=committed_record.version,
                build_id=committed_record.build_id,
                installed_at=committed_record.installed_at,
                pending_cleanup=remaining,
            )
            self.state_store.write_install(cleaned_record)
            confirmed = self.state_store.read_install(required=True)
            if confirmed is None or confirmed.pending_cleanup != remaining:
                raise UpgradeError("pending_cleanup 清理后读回不一致。")
        self.state_store.remove_transaction()
        return False, ""

    def _recover_interrupted_upgrade(
        self,
        report: Callable[[str], None],
    ) -> UpgradeOutcome | None:
        """Resolve a trusted leftover upgrade journal while the lock is held.

        A transaction whose install.json still names A is rolled back to A, then
        the caller starts a fresh A-to-B attempt.  If install.json already names
        B, the atomic commit won the crash race; B is retained and only the old
        payload cleanup is completed or registered for a later retry.
        """
        transaction = self.state_store.read_transaction(required=True)
        assert transaction is not None
        if transaction.kind != "upgrade":
            raise UpgradeError(
                "检测到未完成的首次安装事务；本轮升级恢复器拒绝解释该状态。"
            )
        if (
            transaction.target_version != self.manifest.version
            or transaction.target_build_id != self.manifest.build_id
        ):
            raise UpgradeError(
                "残留升级事务与当前 Setup payload 不匹配；请重新运行发起该事务的 Setup。"
            )
        if not transaction.source_version or not transaction.source_build_id:
            raise UpgradeError("残留升级事务缺少源版本身份。")
        if not transaction.rollback_directory:
            raise UpgradeError("残留升级事务缺少 rollback 目录身份。")

        snapshot = UpgradeIntegrationSnapshot.from_json(
            transaction.integration_snapshot
        )
        self._validate_recovery_snapshot(transaction, snapshot)
        current = self.state_store.read_install(required=True)
        assert current is not None
        staging = validate_transaction_directory(
            self.paths.install_root / transaction.staging_directory,
            self.paths,
        )
        rollback = validate_transaction_directory(
            self.paths.install_root / transaction.rollback_directory,
            self.paths,
        )
        app_manifest = _subtree_manifest(self.manifest, "app")
        maintenance_manifest = _subtree_manifest(self.manifest, "maintenance")

        if (
            current.version == transaction.target_version
            and current.build_id == transaction.target_build_id
        ):
            if transaction.stage != "integration_verified":
                raise UpgradeError("install.json 已是目标版本，但事务尚未完成系统集成验证。")
            verify_payload(self.paths.app_directory, app_manifest)
            verify_payload(self.paths.maintenance_directory, maintenance_manifest)
            estimated_size = max(
                1,
                (sum(entry.size for entry in self.manifest.files) + 1023) // 1024,
            )
            self.integration.verify_upgrade(
                snapshot,
                version=transaction.target_version,
                estimated_size_kib=estimated_size,
            )
            cleanup_pending, warning = self._finalize_committed_cleanup(
                current,
                transaction,
                staging,
                rollback,
            )
            try:
                self.history.append(
                    "upgrade_committed",
                    version=current.version,
                    result="recovered",
                    from_version=transaction.source_version,
                    to_version=current.version,
                    transaction_id=transaction.transaction_id,
                )
            except Exception:
                pass
            report("completed")
            return UpgradeOutcome(
                result="success-with-warning" if warning else "already-installed",
                verified=True,
                committed=True,
                rollback_attempted=False,
                rollback_succeeded=None,
                cleanup_pending=cleanup_pending,
                message=f"已恢复并确认 LanDrop {current.version} 的升级提交。",
                warning=warning,
            )

        if (
            current.version != transaction.source_version
            or current.build_id != transaction.source_build_id
        ):
            raise UpgradeError("残留事务与当前权威 install.json 的源/目标身份均不一致。")

        report("rollback")
        _restore_source_payload(
            self.paths,
            staging,
            rollback,
            app_manifest,
            maintenance_manifest,
        )

        self.integration.restore_upgrade(snapshot)
        if os.path.lexists(staging):
            self.remove_tree(staging, self.paths)
        if os.path.lexists(rollback):
            self.remove_tree(rollback, self.paths)
        _validate_installed_layout(self.paths, current)
        self.state_store.remove_transaction()
        try:
            self.history.append(
                "rollback_completed",
                version=current.version,
                result="recovered",
                from_version=current.version,
                to_version=transaction.target_version,
                transaction_id=transaction.transaction_id,
            )
        except Exception:
            pass
        return None

    def _validate_recovery_snapshot(
        self,
        transaction: TransactionRecord,
        snapshot: UpgradeIntegrationSnapshot,
    ) -> None:
        expected = IntegrationPlan.create(
            self.paths,
            version=str(transaction.source_version),
            estimated_size_kib=snapshot.registration.estimated_size_kib,
            desktop_enabled=snapshot.desktop_shortcut is not None,
        )
        if snapshot.start_menu_shortcut != expected.start_menu_shortcut:
            raise UpgradeError("升级快照中的开始菜单对象不属于固定安装。")
        if snapshot.desktop_shortcut_path != expected.desktop_shortcut.path:
            raise UpgradeError("升级快照中的桌面快捷方式路径不属于固定安装。")
        if snapshot.desktop_shortcut not in {None, expected.desktop_shortcut}:
            raise UpgradeError("升级快照中的桌面快捷方式对象不属于固定安装。")
        if snapshot.run_command not in {None, expected.run_command}:
            raise UpgradeError("升级快照中的 HKCU Run 值不属于固定安装。")
        if snapshot.registration != expected.registration:
            raise UpgradeError("升级快照中的卸载登记不属于源版本安装。")

    def _rollback_precommit(
        self,
        *,
        current: InstallRecord | None,
        transaction: TransactionRecord | None,
        snapshot: UpgradeIntegrationSnapshot | None,
        staging: Path | None,
        rollback: Path | None,
        app_switched: bool,
        integration_started: bool,
        commit_started: bool,
    ) -> list[str]:
        errors: list[str] = []
        if current is None or transaction is None:
            return errors
        try:
            if staging is not None and rollback is not None:
                _restore_source_payload(
                    self.paths,
                    staging,
                    rollback,
                    _subtree_manifest(self.manifest, "app"),
                    _subtree_manifest(self.manifest, "maintenance"),
                )
            elif app_switched:
                raise UpgradeError("缺少受控 staging/rollback 路径，无法恢复 A。")
        except Exception as restore_error:
            errors.append(f"payload 恢复：{restore_error}")

        if integration_started and snapshot is not None:
            try:
                self.integration.restore_upgrade(snapshot)
            except Exception as restore_error:
                errors.append(f"系统集成：{restore_error}")

        if commit_started:
            try:
                self.state_store.write_install(current)
            except Exception as restore_error:
                errors.append(f"install.json：{restore_error}")

        if not errors:
            if staging is not None and staging.exists():
                try:
                    self.remove_tree(staging, self.paths)
                except Exception as cleanup_error:
                    errors.append(f"staging：{cleanup_error}")
            if rollback is not None and rollback.exists():
                try:
                    self.remove_tree(rollback, self.paths)
                except Exception as cleanup_error:
                    errors.append(f"rollback：{cleanup_error}")
        if not errors:
            try:
                self.state_store.remove_transaction()
            except Exception as state_error:
                errors.append(f"transaction.json：{state_error}")

        try:
            self.history.append(
                "rollback_started",
                version=current.version,
                result="upgrade_failed",
                from_version=current.version,
                to_version=self.manifest.version,
                transaction_id=transaction.transaction_id,
            )
            self.history.append(
                "rollback_completed",
                version=current.version,
                result="ok" if not errors else "partial",
                from_version=current.version,
                to_version=self.manifest.version,
                transaction_id=transaction.transaction_id,
                details={"residual_count": len(errors)},
            )
        except Exception:
            pass
        return errors

    def _append_history(
        self,
        event: str,
        record: InstallRecord,
        transaction: TransactionRecord,
        *,
        result: str,
    ) -> None:
        self.history.append(
            event,
            version=transaction.target_version,
            result=result,
            from_version=record.version,
            to_version=transaction.target_version,
            transaction_id=transaction.transaction_id,
        )


def inspect_setup_state(
    paths: InstallPaths,
    manifest: PayloadManifest,
    *,
    state_store: InstallationStateStore | None = None,
) -> SetupInspection:
    store = state_store or InstallationStateStore(paths)
    if os.path.lexists(paths.transaction_state_path):
        try:
            transaction = store.read_transaction(required=True)
        except Exception as exc:
            return SetupInspection("untrusted", f"安装事务损坏或不可解释：{exc}")
        assert transaction is not None
        return SetupInspection(
            "incomplete",
            f"检测到未完成的 {transaction.kind} 事务（{transaction.stage}）。",
        )
    try:
        current = store.read_install()
    except Exception as exc:
        return SetupInspection("untrusted", f"install.json 损坏或不可解释：{exc}")
    if current is None:
        if os.path.lexists(paths.install_root):
            return SetupInspection("untrusted", "安装根存在但缺少权威 install.json。")
        return SetupInspection("first_install", "尚未安装 LanDrop。")
    try:
        _validate_installed_layout(paths, current)
        comparison = compare_versions(manifest.version, current.version)
    except Exception as exc:
        return SetupInspection("untrusted", f"已有安装状态不可信：{exc}")
    if comparison < 0:
        disposition = "downgrade_blocked"
        message = f"不支持从 {current.version} 自动降级到 {manifest.version}。"
    elif comparison == 0 and manifest.build_id != current.build_id:
        disposition = "build_mismatch"
        message = "检测到同版本但不同 build id；不支持覆盖或修复安装。"
    elif comparison == 0:
        disposition = "already_installed"
        message = f"当前版本 {current.version} 已安装。"
    else:
        disposition = "upgrade"
        message = f"可从 {current.version} 升级到 {manifest.version}。"
    return SetupInspection(
        disposition,
        message,
        current_version=current.version,
        pending_cleanup=current.pending_cleanup,
    )


def retry_pending_cleanup(
    paths: InstallPaths,
    *,
    state_store: InstallationStateStore | None = None,
    history: InstallHistoryLog | None = None,
    remove_tree: Callable[[Path, InstallPaths], None] | None = None,
) -> CleanupResult:
    store = state_store or InstallationStateStore(paths)
    audit = history or InstallHistoryLog(paths)
    remover = remove_tree or _remove_transaction_tree
    record = store.read_install()
    if record is None or not record.pending_cleanup:
        return CleanupResult(0, 0, (), ())
    remaining: list[str] = []
    errors: list[str] = []
    removed = 0
    for name in record.pending_cleanup:
        if not name.startswith(".rollback-"):
            remaining.append(name)
            errors.append(f"拒绝非 rollback pending_cleanup：{name}")
            continue
        target = validate_transaction_directory(paths.install_root / name, paths)
        try:
            remover(target, paths)
            removed += 1
        except Exception as exc:
            remaining.append(name)
            errors.append(f"{name}：{exc}")
    if tuple(remaining) != record.pending_cleanup:
        updated = InstallRecord.create(
            paths,
            version=record.version,
            build_id=record.build_id,
            installed_at=record.installed_at,
            pending_cleanup=tuple(remaining),
        )
        try:
            store.write_install(updated)
        except Exception as exc:
            errors.append(f"pending_cleanup 状态更新失败：{exc}")
            try:
                audit.append(
                    "cleanup_pending",
                    version=record.version,
                    result="state_update_failed",
                    details={
                        "removed_count": removed,
                        "remaining_count": len(record.pending_cleanup),
                    },
                )
            except Exception:
                pass
            return CleanupResult(
                attempted=len(record.pending_cleanup),
                removed=removed,
                remaining=record.pending_cleanup,
                errors=tuple(errors),
            )
        try:
            audit.append(
                "cleanup_completed",
                version=record.version,
                result="ok" if not remaining else "partial",
                details={"removed_count": removed, "remaining_count": len(remaining)},
            )
        except Exception:
            pass
    elif errors:
        try:
            audit.append(
                "cleanup_pending",
                version=record.version,
                result="retry_failed",
                details={
                    "attempted_count": len(record.pending_cleanup),
                    "remaining_count": len(remaining),
                    "errors": errors,
                },
            )
        except Exception:
            pass
    return CleanupResult(
        attempted=len(record.pending_cleanup),
        removed=removed,
        remaining=tuple(remaining),
        errors=tuple(errors),
    )


def compare_versions(left: str, right: str) -> int:
    left_key = _version_key(left)
    right_key = _version_key(right)
    return (left_key > right_key) - (left_key < right_key)


def is_executable_running(executable: Path) -> bool:
    """Check only the exact installed executable path via native Windows APIs."""
    if os.name != "nt":
        return False
    target = os.path.normcase(os.path.abspath(executable))
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot == _INVALID_HANDLE_VALUE:
        raise UpgradeError(f"无法枚举运行进程（Windows 错误 {ctypes.get_last_error()}）。")
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        has_entry = bool(kernel32.Process32FirstW(snapshot, ctypes.byref(entry)))
        while has_entry:
            if entry.szExeFile.casefold() == Path(target).name.casefold():
                process = kernel32.OpenProcess(
                    _PROCESS_QUERY_LIMITED_INFORMATION,
                    False,
                    entry.th32ProcessID,
                )
                if process:
                    try:
                        size = wintypes.DWORD(32768)
                        buffer = ctypes.create_unicode_buffer(size.value)
                        if kernel32.QueryFullProcessImageNameW(
                            process, 0, buffer, ctypes.byref(size)
                        ) and os.path.normcase(os.path.abspath(buffer.value)) == target:
                            return True
                    finally:
                        kernel32.CloseHandle(process)
            has_entry = bool(kernel32.Process32NextW(snapshot, ctypes.byref(entry)))
    finally:
        kernel32.CloseHandle(snapshot)
    return False


def _validate_installed_layout(paths: InstallPaths, record: InstallRecord) -> None:
    root = validate_install_child(paths.install_root, paths, allow_root=True)
    if not root.is_dir() or is_reparse_object(root):
        raise UpgradeError("固定安装根不存在、不是目录或是 reparse object。")
    for directory in (paths.app_directory, paths.maintenance_directory, paths.metadata_directory):
        validate_install_child(directory, paths)
        if not directory.is_dir() or is_reparse_object(directory):
            raise UpgradeError(f"安装目录缺失或不安全：{directory.name}")
    for executable in (paths.main_executable, paths.uninstall_executable):
        validate_install_child(executable, paths)
        if not executable.is_file() or is_reparse_object(executable):
            raise UpgradeError(f"安装程序文件缺失或不安全：{executable}")
    allowed = {"app", "maintenance", "metadata", *record.pending_cleanup}
    for child in root.iterdir():
        if child.name not in allowed:
            raise UpgradeError(f"安装根包含无法解释的对象：{child.name}")
        if is_reparse_object(child):
            raise UpgradeError(f"安装根包含 reparse object：{child.name}")


def _subtree_manifest(manifest: PayloadManifest, subtree: str) -> PayloadManifest:
    prefix = f"{subtree}/"
    files = tuple(
        PayloadFile(entry.relative_path[len(prefix) :], entry.size, entry.sha256)
        for entry in manifest.files
        if entry.relative_path.startswith(prefix)
    )
    required = "LanDrop.exe" if subtree == "app" else "Uninstall.exe"
    if not files or not any(entry.relative_path == required for entry in files):
        raise UpgradeError(f"payload manifest 不包含完整 {subtree} 子树。")
    return PayloadManifest(
        product_id=manifest.product_id,
        version=manifest.version,
        build_id=manifest.build_id,
        files=files,
        schema_version=manifest.schema_version,
    )


def _restore_source_payload(
    paths: InstallPaths,
    staging: Path,
    rollback: Path,
    app_manifest: PayloadManifest,
    maintenance_manifest: PayloadManifest,
) -> None:
    for container in (staging, rollback):
        if os.path.lexists(container) and (
            not container.is_dir() or is_reparse_object(container)
        ):
            raise UpgradeError(f"事务容器不是安全的普通目录：{container}")
    components = (
        ("app", paths.app_directory, app_manifest),
        ("maintenance", paths.maintenance_directory, maintenance_manifest),
    )
    for name, live, target_manifest in components:
        staged = staging / name
        old = rollback / name
        if os.path.lexists(old):
            if not old.is_dir() or is_reparse_object(old):
                raise UpgradeError(f"旧 {name} rollback 不是安全的普通目录。")
            if os.path.lexists(live):
                if os.path.lexists(staged):
                    raise UpgradeError(
                        f"恢复 {name} 时 live 与 staging 同时存在，拒绝猜测。"
                    )
                _rename_owned_directory(live, staged, paths)
            _rename_owned_directory(old, live, paths)
            continue
        if not live.is_dir() or is_reparse_object(live):
            raise UpgradeError(f"旧 {name} 与 rollback 均缺失，无法恢复源版本。")
        if _payload_matches(live, target_manifest):
            raise UpgradeError(f"新版 {name} 已激活但旧 rollback 缺失。")


def _payload_matches(root: Path, manifest: PayloadManifest) -> bool:
    try:
        verify_payload(root, manifest)
    except Exception:
        return False
    return True


def _rename_owned_directory(source: Path, destination: Path, paths: InstallPaths) -> None:
    validate_install_child(source, paths)
    validate_install_child(destination, paths)
    if not source.is_dir() or is_reparse_object(source):
        raise UpgradeError(f"目录切换来源缺失或不安全：{source}")
    if os.path.lexists(destination):
        raise UpgradeError(f"目录切换目标已经存在：{destination}")
    os.replace(source, destination)


def _remove_transaction_tree(target: Path, paths: InstallPaths) -> None:
    directory = validate_transaction_directory(target, paths)
    if not os.path.lexists(directory):
        return
    if not directory.is_dir() or is_reparse_object(directory):
        raise UpgradeError(f"拒绝清理非普通事务目录：{directory}")
    for current_name, directory_names, file_names in os.walk(
        directory, topdown=False, followlinks=False
    ):
        current = Path(current_name)
        if is_reparse_object(current):
            raise UpgradeError(f"事务目录包含 reparse 目录：{current}")
        for name in (*directory_names, *file_names):
            child = current / name
            if is_reparse_object(child):
                raise UpgradeError(f"事务目录包含 reparse object：{child}")
    shutil.rmtree(directory)


def _version_key(value: str) -> tuple[int, int, int, int]:
    match = _VERSION_PATTERN.fullmatch(value)
    if match is None:
        raise UpgradeError(f"版本号不符合冻结的数字版本格式：{value!r}")
    parts = [int(part) for part in value.split(".")]
    return tuple((parts + [0] * 4)[:4])  # type: ignore[return-value]
