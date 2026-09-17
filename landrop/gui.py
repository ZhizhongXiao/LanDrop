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
            )
            return {"ok": True, "state": snapshot.to_dict()}
        except (ServiceError, TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc), "state": self._controller.snapshot().to_dict()}

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
                result["error"] = "该可信设备已不存在。"
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
    section { margin-bottom: 14px; padding: 20px; background: #fff; border: 1px solid #e4e9f1; border-radius: 14px; box-shadow: 0 5px 18px rgba(21, 35, 62, .05); }
    h2 { margin: 0 0 16px; font-size: 17px; }
    label { display: block; margin: 14px 0 6px; color: #344054; font-size: 13px; font-weight: 600; }
    .path-row { display: grid; grid-template-columns: 1fr auto; gap: 8px; }
    input { min-width: 0; width: 100%; border: 1px solid #cfd6e1; border-radius: 9px; padding: 10px 12px; color: #172033; background: #fff; font: inherit; }
    input:focus { outline: 2px solid #bfd2ff; border-color: #3974e8; }
    input:disabled { color: #667085; background: #f2f4f7; }
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
  </style>
</head>
<body>
<main>
  <header>
    <div><h1>LanDrop</h1><p class="subtitle">可信局域网文件传输</p></div>
    <span id="badge" class="badge">已停止</span>
  </header>

  <section>
    <h2>文件目录</h2>
    <label for="shared">手机下载目录</label>
    <div class="path-row">
      <input id="shared" autocomplete="off">
      <button class="secondary chooser" data-target="shared">选择…</button>
    </div>
    <label for="received">手机上传接收目录</label>
    <div class="path-row">
      <input id="received" autocomplete="off">
      <button class="secondary chooser" data-target="received">选择…</button>
    </div>
    <label for="limit">单文件上传上限（MB）</label>
    <input id="limit" type="number" min="1" max="10000" value="1000">
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
      <dt>手机地址</dt><dd id="lanUrl">—</dd>
      <dt>本机地址</dt><dd id="localUrl">—</dd>
      <dt>网络接口</dt><dd id="network">—</dd>
      <dt>本次配对码</dt><dd id="pairing" class="code">—</dd>
      <dt>已配对设备</dt><dd id="paired">0</dd>
      <dt>活动请求流</dt><dd id="active">0</dd>
      <dt>文件任务</dt><dd id="statistics">—</dd>
      <dt>下载请求流</dt><dd id="streamStatistics">—</dd>
      <dt>停止原因</dt><dd id="stopReason">—</dd>
      <dt>失败分类</dt><dd id="failureReasons">—</dd>
      <dt>拒绝分类</dt><dd id="rejectionReasons">—</dd>
    </dl>
    <div class="actions">
      <button id="open" class="secondary" disabled>在浏览器中打开</button>
      <button id="resetDeadline" class="secondary" disabled>重置为 5 分钟</button>
    </div>
  </section>

  <section>
    <h2>可信设备</h2>
    <div id="clients" class="clients"><span class="subtitle">正在读取…</span></div>
    <div class="actions">
      <button id="refreshClients" class="secondary">刷新列表</button>
      <button id="revokeAll" class="danger">撤销全部信任</button>
    </div>
  </section>

  <section class="security">
    服务只会在 Windows 确认为 Private 的 LAN 接口上启动，并同时监听本机回环地址。
    关闭窗口会隐藏到系统托盘；请在托盘菜单中选择“退出 LanDrop”以停止服务并关闭端口。窗口不承担文件传输。
  </section>
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
    $('network').textContent = state.interface ? `${state.interface}（${state.network_category}）` : '—';
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
    if (!configurationInitialized) {
      if (state.shared_directory) $('shared').value = state.shared_directory;
      if (state.receive_directory) $('received').value = state.receive_directory;
      if (state.max_upload_mb) $('limit').value = state.max_upload_mb;
      configurationInitialized = true;
    }
    for (const input of [$('shared'), $('received'), $('limit')]) input.disabled = running || busy;
    for (const button of document.querySelectorAll('.chooser')) button.disabled = running || busy;
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
    storage_error: '文件或磁盘错误', server_error: '服务器异常', size_limit: '超过大小限制',
    authentication: '未通过认证', csrf: '请求校验失败', insufficient_space: '磁盘空间不足',
    missing_content_length: '缺少内容长度', missing_file: '未选择文件',
    deadline_expired: '会话到期'
  };

  function reasonLabel(reason) { return reasonLabels[reason] || reason || ''; }

  function formatReasons(reasons) {
    return Object.entries(reasons || {}).map(([key, count]) => `${reasonLabel(key)}：${count}`).join('；');
  }

  function renderClients(clients) {
    const root = $('clients');
    const expandedClients = new Set(
      [...root.querySelectorAll('.client-details[open]')].map(item => item.dataset.clientId)
    );
    root.replaceChildren();
    if (!clients.length) {
      const empty = document.createElement('span');
      empty.className = 'subtitle'; empty.textContent = '当前没有可信设备。'; root.appendChild(empty);
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
        ['客户端标识', client.client_id]
      ];
      for (const [label, value] of detailRows) {
        const key = document.createElement('span'); key.className = 'client-detail-label'; key.textContent = label;
        const content = document.createElement('span'); content.className = 'client-detail-value'; content.textContent = value;
        grid.append(key, content);
      }
      details.append(summary, grid);
      const revoke = document.createElement('button'); revoke.className = 'secondary'; revoke.textContent = '撤销信任';
      revoke.addEventListener('click', async () => {
        if (!confirm(`确定撤销设备“${id.textContent}”的信任吗？`)) return;
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
      max_upload_mb: $('limit').value
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
  $('revokeAll').addEventListener('click', async () => {
    if (!confirm('确定撤销全部可信设备吗？所有浏览器下次访问都需要重新配对。')) return;
    const result = await window.pywebview.api.revoke_all_trusted_clients();
    if (!result.ok) showError(result.error); else renderClients(result.clients);
  });

  window.addEventListener('pywebviewready', async () => {
    await refresh();
    await refreshClients();
    setInterval(tickCountdown, 200);
    setInterval(refresh, 1000);
  });
</script>
</body>
</html>
"""
