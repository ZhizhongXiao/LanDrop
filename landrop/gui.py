"""pywebview desktop control window for LanDrop."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Any
import webbrowser

from .cli import (
    DEFAULT_DATA_DIRECTORY,
    DEFAULT_MAX_UPLOAD_MIB,
    DEFAULT_RECEIVE_DIRECTORY,
    DEFAULT_SHARED_DIRECTORY,
)
from .service import ServiceController, ServiceError
from .trust import CredentialStore


class DesktopApi:
    """Small bridge exposed only to the local desktop control page."""

    def __init__(self, controller: ServiceController, webview_module: Any) -> None:
        self._controller = controller
        self._webview = webview_module
        self._window: Any | None = None

    def attach_window(self, window: Any) -> None:
        self._window = window

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
            max_upload_mib = int(options.get("max_upload_mib", DEFAULT_MAX_UPLOAD_MIB))
            snapshot = self._controller.start(
                str(options.get("shared_directory", "")),
                str(options.get("receive_directory", "")),
                max_upload_mib,
            )
            return {"ok": True, "state": snapshot.to_dict()}
        except (ServiceError, TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc), "state": self._controller.snapshot().to_dict()}

    def stop_service(self) -> dict[str, object]:
        try:
            return {"ok": True, "state": self._controller.stop().to_dict()}
        except Exception as exc:
            return {"ok": False, "error": f"停止服务失败：{exc}"}

    def open_transfer_page(self) -> dict[str, object]:
        url = self._controller.snapshot().local_url
        if not url:
            return {"ok": False, "error": "服务尚未启动。"}
        try:
            opened = webbrowser.open(url)
        except webbrowser.Error as exc:
            return {"ok": False, "error": f"无法打开浏览器：{exc}"}
        return {"ok": bool(opened), "error": "" if opened else "系统未能打开浏览器。"}


def main() -> int:
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

    controller = ServiceController(CredentialStore(DEFAULT_DATA_DIRECTORY))
    api = DesktopApi(controller, webview)
    window = webview.create_window(
        "LanDrop",
        html=DESKTOP_HTML,
        js_api=api,
        width=720,
        height=690,
        min_size=(620, 600),
        background_color="#f4f7fb",
        text_select=True,
    )
    api.attach_window(window)
    try:
        webview.start()
    finally:
        controller.stop()
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
    .notice { min-height: 22px; margin-top: 12px; color: #475467; font-size: 14px; }
    .notice.error { color: #b42318; }
    .security { color: #667085; font-size: 13px; line-height: 1.6; }
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
    <label for="limit">单文件上传上限（MiB）</label>
    <input id="limit" type="number" min="1" max="10240" value="1024">
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
      <dt>手机地址</dt><dd id="lanUrl">—</dd>
      <dt>本机地址</dt><dd id="localUrl">—</dd>
      <dt>网络接口</dt><dd id="network">—</dd>
      <dt>本次配对码</dt><dd id="pairing" class="code">—</dd>
    </dl>
    <div class="actions">
      <button id="open" class="secondary" disabled>在浏览器中打开</button>
    </div>
  </section>

  <section class="security">
    服务只会在 Windows 确认为 Private 的 LAN 接口上启动，并同时监听本机回环地址。
    关闭窗口会停止服务并关闭端口；窗口不承担文件传输。
  </section>
</main>
<script>
  const $ = id => document.getElementById(id);
  let busy = false;
  let configurationInitialized = false;

  function showError(message) {
    $('notice').textContent = message || '';
    $('notice').className = message ? 'notice error' : 'notice';
  }

  function render(state) {
    const running = Boolean(state.running);
    $('badge').textContent = running ? '运行中' : (state.phase === 'error' ? '异常' : '已停止');
    $('badge').className = 'badge ' + (running ? 'running' : (state.phase === 'error' ? 'error' : ''));
    $('message').textContent = state.message || '—';
    $('lanUrl').textContent = state.lan_url || '—';
    $('localUrl').textContent = state.local_url || '—';
    $('network').textContent = state.interface ? `${state.interface}（${state.network_category}）` : '—';
    $('pairing').textContent = state.pairing_code || '—';
    if (!configurationInitialized) {
      if (state.shared_directory) $('shared').value = state.shared_directory;
      if (state.receive_directory) $('received').value = state.receive_directory;
      if (state.max_upload_mib) $('limit').value = state.max_upload_mib;
      configurationInitialized = true;
    }
    for (const input of [$('shared'), $('received'), $('limit')]) input.disabled = running || busy;
    for (const button of document.querySelectorAll('.chooser')) button.disabled = running || busy;
    $('start').disabled = running || busy;
    $('stop').disabled = !running || busy;
    $('open').disabled = !running || busy;
  }

  async function refresh() {
    try {
      const result = await window.pywebview.api.get_state();
      if (result.ok) render(result.state);
    } catch (error) { showError(String(error)); }
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
    busy = true; showError(''); await refresh();
    const result = await window.pywebview.api.start_service({
      shared_directory: $('shared').value,
      receive_directory: $('received').value,
      max_upload_mib: $('limit').value
    });
    busy = false;
    if (!result.ok) showError(result.error);
    render(result.state);
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

  window.addEventListener('pywebviewready', async () => {
    await refresh();
    setInterval(refresh, 1200);
  });
</script>
</body>
</html>
"""
