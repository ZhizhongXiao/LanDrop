"""pywebview desktop control window for LanDrop."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from typing import Any
import webbrowser

from .cli import (
    DEFAULT_DATA_DIRECTORY,
    DEFAULT_MAX_UPLOAD_MB,
    DEFAULT_RECEIVE_DIRECTORY,
    DEFAULT_SHARED_DIRECTORY,
)
from .coordinator import ActionCoordinator
from .notifications import WindowsToastBackend
from .service import ServiceController, ServiceError
from .tray import LanDropTray, TrayUnavailableError
from .trust import CredentialStore


class DesktopApi:
    """Small bridge exposed only to the local desktop control page."""

    def __init__(
        self,
        controller: ServiceController,
        credentials: CredentialStore,
        webview_module: Any,
    ) -> None:
        self._controller = controller
        self._credentials = credentials
        self._webview = webview_module
        self._window: Any | None = None
        self._coordinator: ActionCoordinator | None = None

    def attach_window(self, window: Any) -> None:
        self._window = window

    def attach_coordinator(self, coordinator: ActionCoordinator) -> None:
        self._coordinator = coordinator

    def get_state(self) -> dict[str, object]:
        state = self._controller.snapshot().to_dict()
        if not state["shared_directory"]:
            state["shared_directory"] = str(DEFAULT_SHARED_DIRECTORY)
        if not state["receive_directory"]:
            state["receive_directory"] = str(DEFAULT_RECEIVE_DIRECTORY)
        return {"ok": True, "state": state}

    def choose_directory(self, current: str) -> dict[str, object]:
        if self._controller.snapshot().running:
            return {"ok": False, "error": "请先停止服务，再修改目录。"}
        if self._window is None:
            return {"ok": False, "error": "窗口尚未准备完成。"}
        initial = current if current and Path(current).is_dir() else str(Path.home())
        try:
            file_dialog = getattr(self._webview, "FileDialog", None)
            dialog_type = (
                file_dialog.FOLDER
                if file_dialog is not None
                else self._webview.FOLDER_DIALOG
            )
            selected = self._window.create_file_dialog(
                dialog_type,
                directory=initial,
                allow_multiple=False,
            )
        except Exception as exc:
            return {"ok": False, "error": f"无法打开目录选择器：{exc}"}
        if not selected:
            return {"ok": True, "cancelled": True}
        return {"ok": True, "path": str(selected[0])}

    def start_service(self, options: dict[str, object]) -> dict[str, object]:
        try:
            max_upload_mb = int(options.get("max_upload_mb", DEFAULT_MAX_UPLOAD_MB))
            if self._coordinator is None:
                raise RuntimeError("桌面运行时尚未准备完成。")
            snapshot = self._coordinator.start_from_gui(
                str(options.get("shared_directory", "")),
                str(options.get("receive_directory", "")),
                max_upload_mb,
                str(options.get("interface_selector") or "") or None,
            )
            return {"ok": True, "state": snapshot.to_dict()}
        except (ServiceError, TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc), "state": self._controller.snapshot().to_dict()}

    def list_interfaces(self) -> dict[str, object]:
        try:
            return {"ok": True, "interfaces": self._controller.available_interfaces()}
        except Exception as exc:
            return {"ok": False, "error": f"无法刷新网络接口：{exc}", "interfaces": []}

    def stop_service(self) -> dict[str, object]:
        try:
            if self._coordinator is None:
                raise RuntimeError("桌面运行时尚未准备完成。")
            return {"ok": True, "state": self._coordinator.stop_from_ui().state.to_dict()}
        except Exception as exc:
            return {"ok": False, "error": f"停止服务失败：{exc}"}

    def reset_deadline(self) -> dict[str, object]:
        try:
            if self._coordinator is None:
                raise RuntimeError("桌面运行时尚未准备完成。")
            result = self._coordinator.reset_from_tray()
            if not result.applied:
                return {"ok": False, "error": result.message, "state": result.state.to_dict()}
            return {"ok": True, "state": result.state.to_dict()}
        except ServiceError as exc:
            return {"ok": False, "error": str(exc), "state": self._controller.snapshot().to_dict()}

    def open_transfer_page(self) -> dict[str, object]:
        url = self._controller.snapshot().local_url
        if not url:
            return {"ok": False, "error": "服务尚未启动。"}
        try:
            opened = webbrowser.open(url)
        except webbrowser.Error as exc:
            return {"ok": False, "error": f"无法打开浏览器：{exc}"}
        return {"ok": bool(opened), "error": "" if opened else "系统未能打开浏览器。"}

    def refresh_diagnostics(self) -> dict[str, object]:
        try:
            return {"ok": True, "state": self._controller.refresh_diagnostics().to_dict()}
        except Exception as exc:
            return {"ok": False, "error": f"无法刷新诊断：{exc}"}

    def open_windows_settings(self, target: str) -> dict[str, object]:
        targets = {
            "network": "ms-settings:network-status",
            "firewall": "windowsdefender://network/",
            "proxy": "ms-settings:network-proxy",
        }
        uri = targets.get(target)
        if uri is None:
            return {"ok": False, "error": "未知的 Windows 设置入口。"}
        try:
            os.startfile(uri)  # type: ignore[attr-defined]
        except (OSError, AttributeError) as exc:
            return {"ok": False, "error": f"无法打开 Windows 设置：{exc}"}
        return {"ok": True}

    def list_trusted_clients(self) -> dict[str, object]:
        try:
            clients = [
                {
                    "client_id": client.client_id,
                    "created_at": client.created_at,
                    "label": client.label,
                    "device_name": client.device_name,
                    "device_type": client.device_type,
                    "operating_system": client.operating_system,
                    "browser": client.browser,
                    "device_model": client.device_model,
                    "browser_engine": client.browser_engine,
                }
                for client in self._credentials.list_clients()
            ]
            return {"ok": True, "clients": clients}
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc), "clients": []}

    def revoke_trusted_client(self, client_id: str) -> dict[str, object]:
        try:
            revoked = self._credentials.revoke(client_id)
            result = self.list_trusted_clients()
            result["revoked"] = revoked
            if not revoked and result.get("ok"):
                result["ok"] = False
                result["error"] = "该可信客户机已不存在。"
            return result
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc), "clients": []}

    def revoke_all_trusted_clients(self) -> dict[str, object]:
        try:
            count = self._credentials.revoke_all()
            return {"ok": True, "count": count, "clients": []}
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc), "clients": []}


class _WindowDispatcher:
    """Centralize every pywebview window mutation away from callback threads."""

    def __init__(self, window: Any) -> None:
        self._window = window

    def hide_window(self) -> None:
        self._window.hide()

    def show_window(self) -> None:
        self._window.show()
        self._window.restore()

    def destroy_window(self) -> None:
        self._window.destroy()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="LanDrop Desktop")
    parser.add_argument("--session-seconds", type=int, default=300)
    parser.add_argument("--grace-seconds", type=int, default=60)
    args = parser.parse_args(argv)
    if not 5 <= args.session_seconds <= 86_400 or not 0 <= args.grace_seconds <= 600:
        parser.error("session-seconds 需为 5–86400，grace-seconds 需为 0–600")
    if os.name != "nt":
        print("LanDrop 桌面版目前只支持 Windows。", file=sys.stderr)
        return 1
    try:
        import webview
    except ImportError:
        print(
            "缺少 pywebview。请先运行：python -m pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 1

    credentials = CredentialStore(DEFAULT_DATA_DIRECTORY)
    controller = ServiceController(
        credentials,
        duration_seconds=args.session_seconds,
        grace_seconds=args.grace_seconds,
        emit_performance_timings=True,
    )
    api = DesktopApi(controller, credentials, webview)
    window = webview.create_window(
        "LanDrop",
        html=DESKTOP_HTML,
        js_api=api,
        width=720,
        height=780,
        min_size=(620, 680),
        background_color="#f4f7fb",
        text_select=True,
    )
    api.attach_window(window)
    window_dispatcher = _WindowDispatcher(window)
    coordinator = ActionCoordinator(controller, window_dispatcher)
    api.attach_coordinator(coordinator)
    toasts = WindowsToastBackend(
        lambda action, session_id, revision: coordinator.submit_toast_action(
            action,
            session_id=session_id,
            deadline_revision=revision,
        )
    )
    try:
        toasts.start()
    except Exception as exc:
        print(f"[通知] Windows Toast 不可用：{exc}", file=sys.stderr)
    else:
        coordinator.bind_toasts(toasts)
        coordinator.start_expiry_monitor(
            toasts.send_expiry_reminder,
            toasts.send_service_stopped,
        )

    tray = LanDropTray(
        state_provider=controller.snapshot,
        open_window=coordinator.submit_show_window,
        reset=coordinator.reset_from_tray,
        stop_service=coordinator.stop_from_ui,
        exit_application=lambda: coordinator.request_exit("app_exit"),
    )
    coordinator.bind_tray(tray)
    coordinator.set_state_listener(lambda _state: tray.refresh())
    try:
        tray.start()
    except TrayUnavailableError as exc:
        print(f"[托盘] {exc}；关闭窗口将退出 LanDrop。", file=sys.stderr)

    def on_window_closing() -> bool:
        return coordinator.close_request(tray_ready=tray.ready)

    window.events.closing += on_window_closing
    try:
        webview.start()
    finally:
        coordinator.request_exit("app_exit")
    return 0


DESKTOP_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>LanDrop</title>
  <style>
    :root { color-scheme: light; font-family: "Segoe UI", "Microsoft YaHei UI", sans-serif; }
    * { box-sizing: border-box; }
    body { margin: 0; color: #172033; background: #f4f7fb; }
    main { width: min(660px, calc(100% - 36px)); margin: 28px auto; }
    header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; }
    h1 { margin: 0; font-size: 28px; letter-spacing: -.5px; }
    .subtitle { margin: 5px 0 0; color: #667085; font-size: 14px; }
    .badge { border-radius: 999px; padding: 7px 12px; background: #e9edf4; color: #475467; font-weight: 600; }
    .badge.running { background: #dcfae6; color: #087443; }
    .badge.error { background: #fee4e2; color: #b42318; }
    .tabs { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; margin-bottom: 16px; padding: 4px; border-radius: 11px; background: #e9edf4; }
    .tab { color: #475467; background: transparent; }
    .tab.active { color: #175cd3; background: #fff; box-shadow: 0 1px 4px rgba(21, 35, 62, .12); }
    .page { display: none; }
    .page.active { display: block; }
    section { margin-bottom: 14px; padding: 20px; background: #fff; border: 1px solid #e4e9f1; border-radius: 14px; box-shadow: 0 5px 18px rgba(21, 35, 62, .05); }
    h2 { margin: 0 0 16px; font-size: 17px; }
    label { display: block; margin: 14px 0 6px; color: #344054; font-size: 13px; font-weight: 600; }
    .path-row { display: grid; grid-template-columns: 1fr auto; gap: 8px; }
    input, select { min-width: 0; width: 100%; border: 1px solid #cfd6e1; border-radius: 9px; padding: 10px 12px; color: #172033; background: #fff; font: inherit; }
    input:focus, select:focus { outline: 2px solid #bfd2ff; border-color: #3974e8; }
    input:disabled, select:disabled { color: #667085; background: #f2f4f7; }
    button { border: 0; border-radius: 9px; padding: 10px 16px; font: inherit; font-weight: 600; cursor: pointer; }
    button:disabled { cursor: default; opacity: .5; }
    .secondary { color: #344054; background: #eef2f7; }
    .primary { color: #fff; background: #2468da; }
    .danger { color: #fff; background: #d92d20; }
    .actions { display: flex; gap: 10px; margin-top: 18px; }
    .actions button { flex: 1; padding: 12px; }
    .status-grid { display: grid; grid-template-columns: 118px 1fr; gap: 10px 16px; margin: 0; }
    dt { color: #667085; }
    dd { min-width: 0; margin: 0; overflow-wrap: anywhere; font-weight: 600; }
    .code { color: #175cd3; font-size: 23px; letter-spacing: 3px; }
    .countdown { font-size: 24px; color: #175cd3; }
    .countdown.warning { color: #b54708; }
    .notice { min-height: 22px; margin-top: 12px; color: #475467; font-size: 14px; }
    .notice.error { color: #b42318; }
    .security { color: #667085; font-size: 13px; line-height: 1.6; }
    .clients { display: grid; gap: 10px; }
    .client { display: grid; grid-template-columns: 1fr auto; gap: 12px; align-items: center; padding: 12px; border: 1px solid #e4e9f1; border-radius: 10px; }
    .client-main { min-width: 0; }
    .client-id { font-weight: 700; }
    .client-meta { margin-top: 4px; color: #667085; font-size: 12px; overflow-wrap: anywhere; }
    .client-details { margin-top: 8px; color: #475467; font-size: 12px; }
    .client-details summary { width: fit-content; cursor: pointer; color: #175cd3; user-select: none; }
    .client-detail-grid { display: grid; grid-template-columns: 86px 1fr; gap: 5px 10px; margin-top: 8px; }
    .client-detail-label { color: #667085; }
    .client-detail-value { overflow-wrap: anywhere; }
    .diagnostic-list { display: grid; gap: 10px; }
    .diagnostic-item { padding: 12px; border: 1px solid #e4e9f1; border-radius: 9px; background: #f9fafb; }
    .diagnostic-title { font-weight: 700; }
    .diagnostic-meta { margin-top: 4px; color: #667085; font-size: 12px; white-space: pre-wrap; overflow-wrap: anywhere; }
    .diagnostic-ok { color: #087443; }
    .diagnostic-warning { color: #b54708; }
    .diagnostic-unknown { color: #667085; }
    .settings-actions { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  </style>
</head>
<body>
<main>
  <header>
    <div><h1>LanDrop</h1><p class="subtitle">服务机 ↔ 客户机 · 可信局域网传输</p></div>
    <span id="badge" class="badge">已停止</span>
  </header>

  <nav class="tabs" aria-label="LanDrop 页面">
    <button class="tab active" data-page="mainPage">主控</button>
    <button class="tab" data-page="infoPage">信息</button>
    <button class="tab" data-page="settingsPage">设置</button>
  </nav>

  <div id="mainPage" class="page active">

  <section>
    <h2>文件目录</h2>
    <label for="shared">共享目录（客户机下载）</label>
    <div class="path-row">
      <input id="shared" autocomplete="off">
      <button class="secondary chooser" data-target="shared">选择…</button>
    </div>
    <label for="received">接收目录（客户机上传）</label>
    <div class="path-row">
      <input id="received" autocomplete="off">
      <button class="secondary chooser" data-target="received">选择…</button>
    </div>
    <label for="limit">单文件上传上限（MB）</label>
    <input id="limit" type="number" min="1" max="10000" value="1000">
    <label for="interfaceSelector">监听网络</label>
    <div class="path-row">
      <select id="interfaceSelector">
        <option value="">自动选择（仅一个可用 Private 网络）</option>
      </select>
      <button id="refreshInterfaces" class="secondary">刷新网络</button>
    </div>
    <div class="actions">
      <button id="start" class="primary">启动服务</button>
      <button id="stop" class="danger" disabled>停止服务</button>
    </div>
    <div id="notice" class="notice"></div>
  </section>

  <section>
    <h2>当前状态</h2>
    <dl class="status-grid">
      <dt>状态</dt><dd id="message">服务未启动</dd>
      <dt>剩余时间</dt><dd id="countdown" class="countdown">—</dd>
      <dt>客户机访问</dt><dd id="lanUrl">—</dd>
      <dt>服务机本地</dt><dd id="localUrl">—</dd>
      <dt>监听网络</dt><dd id="network">—</dd>
      <dt>本次配对码</dt><dd id="pairing" class="code">—</dd>
      <dt>已配对客户机</dt><dd id="paired">0</dd>
      <dt>活动请求流</dt><dd id="active">0</dd>
      <dt>文件任务</dt><dd id="statistics">—</dd>
      <dt>下载请求流</dt><dd id="streamStatistics">—</dd>
      <dt>停止原因</dt><dd id="stopReason">—</dd>
      <dt>失败分类</dt><dd id="failureReasons">—</dd>
      <dt>拒绝分类</dt><dd id="rejectionReasons">—</dd>
    </dl>
    <div class="actions">
      <button id="open" class="secondary" disabled>在服务机浏览器中打开</button>
      <button id="resetDeadline" class="secondary" disabled>重置为 5 分钟</button>
    </div>
  </section>

  <section>
    <h2>可信客户机</h2>
    <div id="clients" class="clients"><span class="subtitle">正在读取…</span></div>
    <div class="actions">
      <button id="refreshClients" class="secondary">刷新列表</button>
      <button id="revokeAll" class="danger">撤销全部信任</button>
    </div>
  </section>

  <section class="security">
    服务机是运行 LanDrop 的电脑；客户机是通过浏览器连接的手机或另一台电脑。
    服务仅在 Windows Private LAN 上启动；127.0.0.1 只供服务机本地访问。
    关闭窗口会隐藏到系统托盘；请在托盘菜单中选择“退出 LanDrop”以停止服务并关闭端口。窗口不承担文件传输。
  </section>
  </div>

  <div id="infoPage" class="page">
    <section>
      <h2>当前服务机网络基线</h2>
      <dl class="status-grid">
        <dt>服务端绑定基线</dt><dd id="endpointSummary">—</dd>
        <dt>服务端网络校验</dt><dd id="endpointHealth">—</dd>
        <dt>诊断状态</dt><dd id="diagnosticStatus">尚未检测</dd>
        <dt>服务端监听地址</dt><dd id="listenSummary">—</dd>
        <dt>传输摘要</dt><dd id="transferSummary">—</dd>
      </dl>
    </section>
    <section>
      <h2>服务机 IPv4 接口</h2>
      <div id="interfaceList" class="diagnostic-list"><span class="subtitle">尚未检测。</span></div>
      <p class="subtitle">127.0.0.1 是服务机内部回环地址，只供服务机本地访问，不是 LAN 候选。无 IPv4 的断开适配器不会进入此列表。</p>
    </section>
    <section>
      <h2>服务机网络详细信息</h2>
      <div id="adapterDetails" class="diagnostic-list"><span class="subtitle">正在等待异步诊断。</span></div>
    </section>
    <section>
      <h2>Windows 防火墙</h2>
      <div id="firewallSummary" class="diagnostic-unknown">尚未检测。</div>
      <div id="firewallEvidence" class="diagnostic-list" style="margin-top: 12px"></div>
    </section>
  </div>

  <div id="settingsPage" class="page">
    <section>
      <h2>诊断与恢复</h2>
      <p class="security">重新检测只刷新 LanDrop 的只读诊断信息，不会改变网络类别、防火墙规则、代理、VPN 或静态 IP。</p>
      <div class="actions">
        <button id="refreshDiagnostics" class="primary">重新检测</button>
      </div>
      <div id="settingsNotice" class="notice"></div>
    </section>
    <section>
      <h2>Windows 设置入口</h2>
      <div class="settings-actions">
        <button class="secondary settings-link" data-target="network">网络设置</button>
        <button class="secondary settings-link" data-target="firewall">防火墙设置</button>
        <button class="secondary settings-link" data-target="proxy">代理设置</button>
        <button class="secondary" id="returnMain">返回主控</button>
      </div>
    </section>
    <section class="security">
      LanDrop 只检测、解释并提供 Windows 官方设置入口。是否修改系统配置始终由用户决定。
    </section>
  </div>
</main>
<script>
  const $ = id => document.getElementById(id);
  let busy = false;
  let configurationInitialized = false;
  let refreshing = false;
  let countdownActive = false;
  let countdownTarget = 0;
  let trustedRefreshTimer = null;
  let trustedRefreshStopTimer = null;

  function showError(message) {
    $('notice').textContent = message || '';
    $('notice').className = message ? 'notice error' : 'notice';
  }

  function showInfo(message) {
    $('notice').textContent = message || '';
    $('notice').className = 'notice';
  }

  function render(state) {
    const running = Boolean(state.running);
    $('badge').textContent = state.phase === 'grace' ? '传输宽限' : (running ? '运行中' : (state.phase === 'error' ? '异常' : '已停止'));
    $('badge').className = 'badge ' + (running ? 'running' : (state.phase === 'error' ? 'error' : ''));
    $('message').textContent = state.message || '—';
    $('lanUrl').textContent = state.lan_url || '—';
    $('localUrl').textContent = state.local_url || '—';
    const networkName = state.network_name && state.network_name !== state.interface
      ? ` · ${state.network_name}` : '';
    $('network').textContent = state.interface
      ? `${state.interface}${networkName}（${state.network_category || '类别未知'}）`
      : '—';
    $('pairing').textContent = String(state.pairing_code || '').replace(/[^0-9]/g, '') || '—';
    const milliseconds = state.phase === 'grace'
      ? (state.grace_remaining_milliseconds ?? Number(state.grace_remaining_seconds || 0) * 1000)
      : (state.remaining_milliseconds ?? Number(state.remaining_seconds || 0) * 1000);
    countdownActive = running;
    countdownTarget = performance.now() + Math.max(0, Number(milliseconds || 0));
    tickCountdown();
    $('paired').textContent = String(state.paired_devices || 0);
    $('active').textContent = String(state.active_transfers || 0);
    const stats = state.statistics || {};
    const completed = (stats.completed_downloads || 0) + (stats.completed_uploads || 0);
    const failed = (stats.failed_downloads || 0) + (stats.failed_uploads || 0);
    $('statistics').textContent = `${completed} 成功 / ${failed} 失败；${Number(stats.transferred_mb || 0).toFixed(2)} MB；平均 ${Number(stats.average_mb_s || 0).toFixed(2)} MB/s`;
    const completedStreams = stats.completed_download_streams || 0;
    const failedStreams = stats.failed_download_streams || 0;
    const cancelledStreams = stats.cancelled_download_streams || 0;
    const streamReasons = formatReasons(stats.stream_failures);
    const cancellationReasons = formatReasons(stats.stream_cancellations);
    $('streamStatistics').textContent = `${completedStreams} 完成 / ${failedStreams} 失败 / ${cancelledStreams} 浏览器取消${streamReasons ? `；失败：${streamReasons}` : ''}${cancellationReasons ? `；取消：${cancellationReasons}` : ''}`;
    $('stopReason').textContent = reasonLabel(state.stop_reason) || '—';
    $('failureReasons').textContent = formatReasons(stats.failures) || '—';
    $('rejectionReasons').textContent = formatReasons(stats.rejections) || (stats.rejected_expired_requests ? `会话到期：${stats.rejected_expired_requests}` : '—');
    renderDiagnostics(state, stats);
    if (!configurationInitialized) {
      if (state.shared_directory) $('shared').value = state.shared_directory;
      if (state.receive_directory) $('received').value = state.receive_directory;
      if (state.max_upload_mb) $('limit').value = state.max_upload_mb;
      configurationInitialized = true;
    }
    for (const input of [$('shared'), $('received'), $('limit'), $('interfaceSelector')]) input.disabled = running || busy;
    for (const button of document.querySelectorAll('.chooser')) button.disabled = running || busy;
    $('refreshInterfaces').disabled = running || busy;
    $('start').disabled = running || busy;
    $('stop').disabled = !running || busy;
    $('open').disabled = !running || busy;
    $('resetDeadline').disabled = state.phase !== 'running' || busy;
    if (!running) stopTrustedRefreshWindow();
  }

  function formatTime(totalSeconds) {
    const value = Math.max(0, Number(totalSeconds || 0));
    const minutes = Math.floor(value / 60);
    const seconds = value % 60;
    return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
  }

  function tickCountdown() {
    if (!countdownActive) {
      $('countdown').textContent = '—';
      $('countdown').className = 'countdown';
      return;
    }
    const milliseconds = Math.max(0, countdownTarget - performance.now());
    const seconds = Math.ceil(milliseconds / 1000);
    $('countdown').textContent = formatTime(seconds);
    $('countdown').className = 'countdown ' + (seconds <= 60 ? 'warning' : '');
  }

  const reasonLabels = {
    manual_stop: '用户手动停止', app_exit: '退出 LanDrop', window_closed: '关闭窗口（历史）', deadline_no_active: '正常到期',
    deadline_transfers_completed: '到期后传输完成', grace_timeout: '传输宽限耗尽',
    system_resume: '睡眠恢复', client_disconnect: '客户端主动断开',
    network_changed: '网络环境变化',
    storage_error: '文件或磁盘错误', server_error: '服务器异常', size_limit: '超过大小限制',
    authentication: '未通过认证', csrf: '请求校验失败', insufficient_space: '磁盘空间不足',
    missing_content_length: '缺少内容长度', missing_file: '未选择文件',
    deadline_expired: '会话到期'
  };

  function reasonLabel(reason) { return reasonLabels[reason] || reason || ''; }

  function formatReasons(reasons) {
    return Object.entries(reasons || {}).map(([key, count]) => `${reasonLabel(key)}：${count}`).join('；');
  }

  function renderDiagnostics(state, stats) {
    const endpointNetworkName = state.network_name && state.network_name !== state.interface
      ? ` · ${state.network_name}` : '';
    const endpoint = state.interface_index
      ? `#${state.interface_index} · ${state.bound_ipv4 || '地址未知'} · ${state.network_category || '类别未知'} · ${state.interface || '未命名接口'}${endpointNetworkName}`
      : '尚未建立服务端绑定基线';
    $('endpointSummary').textContent = endpoint;
    const endpointLabels = {
      healthy: '正常', confirming: '正在二次确认', changed: '已变化', inactive: '未启用'
    };
    $('endpointHealth').textContent = `${endpointLabels[state.endpoint_status] || state.endpoint_status || '未启用'}${state.endpoint_detail ? `；${state.endpoint_detail}` : ''}`;
    $('listenSummary').textContent = state.running
      ? `客户机访问：${state.lan_url || '—'}；服务机本地：${state.local_url || '—'}`
      : '端口未监听';
    $('transferSummary').textContent = `${Number(stats.transferred_mb || 0).toFixed(2)} MB；平均 ${Number(stats.average_mb_s || 0).toFixed(2)} MB/s；活动请求流 ${state.active_transfers || 0}`;

    const diagnostics = state.diagnostics || {};
    const diagnosticLabels = { checking: '检测中', ready: '已更新', unknown: '无法确定', idle: '尚未检测' };
    $('diagnosticStatus').textContent = `${diagnosticLabels[diagnostics.status] || diagnostics.status || '尚未检测'}${diagnostics.message ? `；${diagnostics.message}` : ''}`;
    const network = diagnostics.network || {};
    renderDiagnosticItems(
      $('interfaceList'),
      network.interfaces || [],
      item => ({
        title: `${item.alias || '未命名接口'}${item.description && item.description !== item.alias ? ` · ${item.description}` : ''} · ${item.address || '无 IPv4'}`,
        meta: `InterfaceIndex ${item.interface_index || '—'} · ${item.category || 'Unknown'} · ${item.connectivity || 'Unknown'} · ${item.address === '127.0.0.1' ? '服务机回环，仅供本地访问' : (item.role === 'excluded' ? '不作为 LAN 候选' : 'LAN 候选')}`
      }),
      network.discovery_error || '未检测到接口。'
    );
    renderDiagnosticItems(
      $('adapterDetails'),
      network.adapters || [],
      item => ({
        title: `${item.alias || '未命名接口'} · ${item.status || '状态未知'} · ${item.link_speed || '速率未知'}`,
        meta: `InterfaceIndex ${item.interface_index || '—'}\nIPv4：${listText(item.ipv4)}\n网关：${listText(item.gateways)}\nDNS：${listText(item.dns)}`
      }),
      network.message || '详细信息不可用。'
    );

    const firewall = diagnostics.firewall || {};
    const firewallStatus = diagnosticLabels[firewall.status] || firewall.status || '尚未检测';
    $('firewallSummary').textContent = `${firewallStatus}；${firewall.message || '—'}${firewall.status === 'ready' ? `；精确端口允许 ${firewall.exact_port_allow || 0}；程序允许 ${firewall.program_allow || 0}；宽泛允许 ${firewall.broad_allow || 0}；相关阻止 ${firewall.relevant_blocks || 0}` : ''}`;
    $('firewallSummary').className = `diagnostic-${firewall.level === 'ok' ? 'ok' : (firewall.level === 'warning' ? 'warning' : 'unknown')}`;
    renderDiagnosticItems(
      $('firewallEvidence'),
      firewall.evidence || [],
      item => ({
        title: `${item.action || 'Unknown'} · ${item.name || '未命名规则'}`,
        meta: `${item.profile || 'Unknown'} · ${item.protocol || 'Any'}:${item.local_port || 'Any'} · ${item.program || 'Any'}`
      }),
      firewall.status === 'checking' ? '正在读取规则……' : '没有可展示的相关规则证据。'
    );
  }

  function renderDiagnosticItems(root, items, formatter, emptyMessage) {
    root.replaceChildren();
    if (!items.length) {
      const empty = document.createElement('span');
      empty.className = 'subtitle'; empty.textContent = emptyMessage; root.appendChild(empty);
      return;
    }
    for (const item of items) {
      const view = formatter(item);
      const row = document.createElement('div'); row.className = 'diagnostic-item';
      const title = document.createElement('div'); title.className = 'diagnostic-title'; title.textContent = view.title;
      const meta = document.createElement('div'); meta.className = 'diagnostic-meta'; meta.textContent = view.meta;
      row.append(title, meta); root.appendChild(row);
    }
  }

  function listText(value) {
    if (Array.isArray(value)) return value.join(', ') || '—';
    return value ? String(value) : '—';
  }

  function renderClients(clients) {
    const root = $('clients');
    const expandedClients = new Set(
      [...root.querySelectorAll('.client-details[open]')].map(item => item.dataset.clientId)
    );
    root.replaceChildren();
    if (!clients.length) {
      const empty = document.createElement('span');
      empty.className = 'subtitle'; empty.textContent = '当前没有可信客户机。'; root.appendChild(empty);
      return;
    }
    for (const client of clients) {
      const row = document.createElement('div'); row.className = 'client';
      const main = document.createElement('div'); main.className = 'client-main';
      const id = document.createElement('div'); id.className = 'client-id';
      id.textContent = client.device_name || client.label || '未命名设备';
      const meta = document.createElement('div'); meta.className = 'client-meta';
      const created = client.created_at ? new Date(client.created_at).toLocaleString('zh-CN', { hour12: false }) : '时间未知';
      meta.textContent = `${client.operating_system || '未知系统'} · ${client.browser || '未知浏览器'} · ${created}`;
      const details = document.createElement('details'); details.className = 'client-details';
      details.dataset.clientId = client.client_id;
      details.open = expandedClients.has(client.client_id);
      const summary = document.createElement('summary'); summary.textContent = 'ⓘ 详细信息';
      const grid = document.createElement('div'); grid.className = 'client-detail-grid';
      const detailRows = [
        ['设备类型', client.device_type || '未知'],
        ['报告型号', client.device_model || '浏览器未提供'],
        ['操作系统', client.operating_system || '未知'],
        ['浏览器', client.browser || '未知'],
        ['浏览器内核', client.browser_engine || '未知'],
        ['配对时间', created],
        ['客户端标识（浏览器）', client.client_id]
      ];
      for (const [label, value] of detailRows) {
        const key = document.createElement('span'); key.className = 'client-detail-label'; key.textContent = label;
        const content = document.createElement('span'); content.className = 'client-detail-value'; content.textContent = value;
        grid.append(key, content);
      }
      details.append(summary, grid);
      const revoke = document.createElement('button'); revoke.className = 'secondary'; revoke.textContent = '撤销信任';
      revoke.addEventListener('click', async () => {
        if (!confirm(`确定撤销客户机“${id.textContent}”的信任吗？`)) return;
        const result = await window.pywebview.api.revoke_trusted_client(client.client_id);
        if (!result.ok) showError(result.error); else renderClients(result.clients);
      });
      main.append(id, meta, details); row.append(main, revoke); root.appendChild(row);
    }
  }

  async function refreshClients() {
    const result = await window.pywebview.api.list_trusted_clients();
    if (!result.ok) showError(result.error); else renderClients(result.clients);
  }

  async function refreshInterfaces() {
    const selector = $('interfaceSelector');
    const previous = selector.value;
    const result = await window.pywebview.api.list_interfaces();
    if (!result.ok) { showError(result.error); return; }
    selector.replaceChildren();
    const automatic = document.createElement('option');
    automatic.value = ''; automatic.textContent = '自动选择（仅一个可用 Private 网络）';
    selector.appendChild(automatic);
    for (const item of result.interfaces || []) {
      if (item.role === 'excluded') continue;
      const option = document.createElement('option');
      option.value = item.address || '';
      const usable = String(item.category || '').toLowerCase() === 'private';
      option.disabled = !usable;
      const alias = item.alias || '未命名接口';
      const profileName = item.description && item.description !== alias ? ` · ${item.description}` : '';
      option.textContent = `${alias}${profileName} · ${item.address || '无 IPv4'} · ${item.category || '类别未知'}${usable ? '' : ' · 不可选'}`;
      selector.appendChild(option);
    }
    if ([...selector.options].some(option => option.value === previous && !option.disabled)) {
      selector.value = previous;
    }
  }

  function startTrustedRefreshWindow() {
    stopTrustedRefreshWindow();
    refreshClients();
    trustedRefreshTimer = setInterval(refreshClients, 5000);
    trustedRefreshStopTimer = setTimeout(stopTrustedRefreshWindow, 60000);
  }

  function stopTrustedRefreshWindow() {
    if (trustedRefreshTimer !== null) clearInterval(trustedRefreshTimer);
    if (trustedRefreshStopTimer !== null) clearTimeout(trustedRefreshStopTimer);
    trustedRefreshTimer = null;
    trustedRefreshStopTimer = null;
  }

  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    try {
      const result = await window.pywebview.api.get_state();
      if (result.ok) render(result.state);
    } catch (error) { showError(String(error)); }
    finally { refreshing = false; }
  }

  function showPage(pageId) {
    for (const page of document.querySelectorAll('.page')) page.classList.toggle('active', page.id === pageId);
    for (const tab of document.querySelectorAll('.tab')) tab.classList.toggle('active', tab.dataset.page === pageId);
  }

  for (const tab of document.querySelectorAll('.tab')) {
    tab.addEventListener('click', () => showPage(tab.dataset.page));
  }

  for (const button of document.querySelectorAll('.chooser')) {
    button.addEventListener('click', async () => {
      const input = $(button.dataset.target);
      showError('');
      const result = await window.pywebview.api.choose_directory(input.value);
      if (!result.ok) showError(result.error);
      else if (!result.cancelled) input.value = result.path;
    });
  }

  $('start').addEventListener('click', async () => {
    busy = true;
    showInfo('正在检测 Private 网络并启动服务……');
    await refresh();
    showInfo('正在检测 Private 网络并启动服务……');
    const result = await window.pywebview.api.start_service({
      shared_directory: $('shared').value,
      receive_directory: $('received').value,
      max_upload_mb: $('limit').value,
      interface_selector: $('interfaceSelector').value
    });
    busy = false;
    if (!result.ok) showError(result.error);
    else showInfo('');
    render(result.state);
    if (result.ok && result.state.running) startTrustedRefreshWindow();
  });

  $('stop').addEventListener('click', async () => {
    busy = true; showError(''); await refresh();
    const result = await window.pywebview.api.stop_service();
    busy = false;
    if (!result.ok) showError(result.error); else render(result.state);
  });

  $('open').addEventListener('click', async () => {
    const result = await window.pywebview.api.open_transfer_page();
    if (!result.ok) showError(result.error);
  });

  $('resetDeadline').addEventListener('click', async () => {
    busy = true; showError(''); await refresh();
    const result = await window.pywebview.api.reset_deadline();
    busy = false;
    if (!result.ok) showError(result.error);
    render(result.state);
  });

  $('refreshClients').addEventListener('click', refreshClients);
  $('refreshInterfaces').addEventListener('click', refreshInterfaces);
  $('revokeAll').addEventListener('click', async () => {
    if (!confirm('确定撤销全部可信客户机吗？所有客户机浏览器下次访问都需要重新配对。')) return;
    const result = await window.pywebview.api.revoke_all_trusted_clients();
    if (!result.ok) showError(result.error); else renderClients(result.clients);
  });

  $('refreshDiagnostics').addEventListener('click', async () => {
    $('refreshDiagnostics').disabled = true;
    $('settingsNotice').textContent = '已开始异步诊断；可切换到“信息”页查看进度。';
    const result = await window.pywebview.api.refresh_diagnostics();
    $('refreshDiagnostics').disabled = false;
    if (!result.ok) $('settingsNotice').textContent = result.error;
    else render(result.state);
  });

  for (const button of document.querySelectorAll('.settings-link')) {
    button.addEventListener('click', async () => {
      const result = await window.pywebview.api.open_windows_settings(button.dataset.target);
      $('settingsNotice').textContent = result.ok ? '' : result.error;
    });
  }
  $('returnMain').addEventListener('click', () => showPage('mainPage'));

  window.addEventListener('pywebviewready', async () => {
    await refresh();
    await refreshInterfaces();
    await refreshClients();
    setInterval(tickCountdown, 200);
    setInterval(refresh, 1000);
  });
</script>
</body>
</html>
"""
