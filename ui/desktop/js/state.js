"use strict";

const $ = id => document.getElementById(id);
let busy = false;
let configurationInitialized = false;
let refreshing = false;
let countdownActive = false;
let countdownTarget = 0;
let trustedRefreshTimer = null;
let trustedRefreshStopTimer = null;

const reasonLabels = {
  manual_stop: "用户手动停止",
  app_exit: "退出 LanDrop",
  window_closed: "关闭窗口（历史）",
  deadline_no_active: "正常到期",
  deadline_transfers_completed: "到期后传输完成",
  grace_timeout: "传输宽限耗尽",
  system_resume: "睡眠恢复",
  client_disconnect: "客户机主动断开",
  network_changed: "网络环境变化",
  network_category_unavailable: "无法确认网络类别",
  storage_error: "文件或磁盘错误",
  server_error: "服务器异常",
  size_limit: "超过大小限制",
  authentication: "未通过认证",
  csrf: "请求校验失败",
  insufficient_space: "磁盘空间不足",
  missing_content_length: "缺少内容长度",
  missing_file: "未选择文件",
  deadline_expired: "会话到期"
};

function showError(message) {
  $("notice").textContent = message || "";
  $("notice").className = message ? "notice error" : "notice";
}

function showInfo(message) {
  $("notice").textContent = message || "";
  $("notice").className = "notice";
}

function reasonLabel(reason) {
  return reasonLabels[reason] || reason || "";
}

function formatReasons(reasons) {
  return Object.entries(reasons || {})
    .map(([key, count]) => `${reasonLabel(key)}：${count}`)
    .join("；");
}

function formatTime(totalSeconds) {
  const value = Math.max(0, Number(totalSeconds || 0));
  const minutes = Math.floor(value / 60);
  const seconds = value % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function tickCountdown() {
  if (!countdownActive) {
    $("countdown").textContent = "—";
    $("countdown").className = "countdown";
    return;
  }
  const milliseconds = Math.max(0, countdownTarget - performance.now());
  const seconds = Math.ceil(milliseconds / 1000);
  $("countdown").textContent = formatTime(seconds);
  $("countdown").className = `countdown${seconds <= 60 ? " warning" : ""}`;
}

function render(state) {
  const running = Boolean(state.running);
  const badge = $("badge");
  const badgeText = state.phase === "grace"
    ? "传输宽限"
    : (running ? "运行中" : (state.phase === "error" ? "异常" : "已停止"));
  badge.querySelector(".status-text").textContent = badgeText;
  badge.className = `status-pill${running ? " running" : ""}${state.phase === "error" ? " error" : ""}${state.phase === "grace" ? " grace" : ""}`;
  document.body.dataset.serviceState = state.phase || (running ? "running" : "stopped");

  $("message").textContent = state.message || "—";
  $("lanUrl").textContent = state.lan_url || "—";
  const networkName = state.network_name && state.network_name !== state.interface
    ? ` · ${state.network_name}` : "";
  $("network").textContent = state.interface
    ? `${state.interface}${networkName}（${state.network_category || "类别未知"}）`
    : "—";
  $("settingsPreviewNetwork").textContent = state.interface
    ? `${state.interface}${networkName} · ${state.network_category || "类别未知"}`
    : "尚未选择";
  $("settingsPreviewShared").textContent = state.shared_directory || "尚未设置";
  $("settingsPreviewReceived").textContent = state.receive_directory || "尚未设置";
  $("pairing").textContent = String(state.pairing_code || "").replace(/[^0-9]/g, "") || "—";
  const pairingQr = $("pairingQr");
  const pairingQrLarge = $("pairingQrLarge");
  const qrDialog = $("qrDialog");
  const qrDataUri = running ? String(state.pairing_qr_data_uri || "") : "";
  if (qrDataUri) {
    if (pairingQr.src !== qrDataUri) pairingQr.src = qrDataUri;
    if (pairingQrLarge.src !== qrDataUri) pairingQrLarge.src = qrDataUri;
    pairingQr.hidden = false;
    $("qrUnavailable").hidden = true;
    $("openQrDialog").disabled = false;
  } else {
    pairingQr.removeAttribute("src");
    pairingQrLarge.removeAttribute("src");
    pairingQr.hidden = true;
    $("qrUnavailable").hidden = false;
    $("openQrDialog").disabled = true;
    if (qrDialog.open) qrDialog.close();
  }

  const milliseconds = state.phase === "grace"
    ? (state.grace_remaining_milliseconds ?? Number(state.grace_remaining_seconds || 0) * 1000)
    : (state.remaining_milliseconds ?? Number(state.remaining_seconds || 0) * 1000);
  countdownActive = running;
  countdownTarget = performance.now() + Math.max(0, Number(milliseconds || 0));
  tickCountdown();

  $("paired").textContent = String(state.paired_devices || 0);
  $("active").textContent = String(state.active_transfers || 0);
  const stats = state.statistics || {};
  $("downloadStatistics").textContent = `${stats.completed_downloads || 0} 成功 / ${stats.failed_downloads || 0} 失败；${Number(stats.downloaded_mb || 0).toFixed(2)} MB`;
  $("uploadStatistics").textContent = `${stats.completed_uploads || 0} 成功 / ${stats.failed_uploads || 0} 失败；${Number(stats.uploaded_mb || 0).toFixed(2)} MB`;
  $("statistics").textContent = `${Number(stats.transferred_mb || 0).toFixed(2)} MB；平均 ${Number(stats.average_mb_s || 0).toFixed(2)} MB/s`;
  const completedStreams = stats.completed_download_streams || 0;
  const failedStreams = stats.failed_download_streams || 0;
  const cancelledStreams = stats.cancelled_download_streams || 0;
  const streamReasons = formatReasons(stats.stream_failures);
  const cancellationReasons = formatReasons(stats.stream_cancellations);
  $("streamStatistics").textContent = `${completedStreams} 完成 / ${failedStreams} 失败 / ${cancelledStreams} 浏览器取消${streamReasons ? `；失败：${streamReasons}` : ""}${cancellationReasons ? `；取消：${cancellationReasons}` : ""}`;
  $("stopReason").textContent = reasonLabel(state.stop_reason) || "—";
  $("failureReasons").textContent = formatReasons(stats.failures) || "—";
  $("rejectionReasons").textContent = formatReasons(stats.rejections)
    || (stats.rejected_expired_requests ? `会话到期：${stats.rejected_expired_requests}` : "—");
  renderDiagnostics(state, stats);

  if (!configurationInitialized) {
    if (state.shared_directory) $("shared").value = state.shared_directory;
    if (state.receive_directory) $("received").value = state.receive_directory;
    if (state.max_upload_mb) $("limit").value = state.max_upload_mb;
    configurationInitialized = true;
  }
  for (const input of [$("shared"), $("received"), $("limit"), $("interfaceSelector")]) {
    input.disabled = running || busy;
  }
  for (const button of document.querySelectorAll(".chooser")) button.disabled = running || busy;
  $("refreshInterfaces").disabled = running || busy;
  $("saveSettings").disabled = running || busy;
  $("start").disabled = running || busy;
  $("stop").disabled = !running || busy;
  $("resetDeadline").disabled = state.phase !== "running" || busy;
  if (!running) stopTrustedRefreshWindow();
}

function renderDiagnostics(state, stats) {
  const endpointNetworkName = state.network_name && state.network_name !== state.interface
    ? ` · ${state.network_name}` : "";
  const endpoint = state.interface_index
    ? `#${state.interface_index} · ${state.bound_ipv4 || "地址未知"} · ${state.network_category || "类别未知"} · ${state.interface || "未命名接口"}${endpointNetworkName}`
    : "尚未建立服务端绑定基线";
  $("endpointSummary").textContent = endpoint;
  const endpointLabels = {
    healthy: "正常",
    confirming: "正在二次确认",
    changed: "已变化",
    unavailable: "无法确认",
    inactive: "未启用"
  };
  $("endpointHealth").textContent = `${endpointLabels[state.endpoint_status] || state.endpoint_status || "未启用"}${state.endpoint_detail ? `；${state.endpoint_detail}` : ""}`;
  $("listenSummary").textContent = state.running
    ? `客户机访问：${state.lan_url || "—"}`
    : "端口未监听";
  $("transferSummary").textContent = `${Number(stats.transferred_mb || 0).toFixed(2)} MB；平均 ${Number(stats.average_mb_s || 0).toFixed(2)} MB/s；活动请求流 ${state.active_transfers || 0}`;

  const diagnostics = state.diagnostics || {};
  const diagnosticLabels = { checking: "检测中", ready: "已更新", unknown: "无法确定", idle: "尚未检测" };
  $("diagnosticStatus").textContent = `${diagnosticLabels[diagnostics.status] || diagnostics.status || "尚未检测"}${diagnostics.message ? `；${diagnostics.message}` : ""}`;
  const network = diagnostics.network || {};
  renderDiagnosticItems(
    $("interfaceList"),
    network.interfaces || [],
    item => ({
      title: `${item.alias || "未命名接口"}${item.description && item.description !== item.alias ? ` · ${item.description}` : ""} · ${item.address || "无 IPv4"}`,
      meta: `InterfaceIndex ${item.interface_index || "—"} · ${item.category || "Unknown"} · ${item.connectivity || "Unknown"} · ${item.address === "127.0.0.1" ? "服务机回环，仅供本地访问" : (item.role === "excluded" ? "不作为 LAN 候选" : "LAN 候选")}`
    }),
    network.discovery_error || "未检测到接口。"
  );
  renderDiagnosticItems(
    $("adapterDetails"),
    network.adapters || [],
    item => ({
      title: `${item.alias || "未命名接口"} · ${item.status || "状态未知"} · ${item.link_speed || "速率未知"}`,
      meta: `InterfaceIndex ${item.interface_index || "—"}\nIPv4：${listText(item.ipv4)}\n网关：${listText(item.gateways)}\nDNS：${listText(item.dns)}`
    }),
    network.message || "详细信息不可用。"
  );

  const firewall = diagnostics.firewall || {};
  const firewallStatus = diagnosticLabels[firewall.status] || firewall.status || "尚未检测";
  $("firewallSummary").textContent = `${firewallStatus}；${firewall.message || "—"}${firewall.status === "ready" ? `；精确端口允许 ${firewall.exact_port_allow || 0}；程序允许 ${firewall.program_allow || 0}；宽泛允许 ${firewall.broad_allow || 0}；相关阻止 ${firewall.relevant_blocks || 0}` : ""}`;
  $("firewallSummary").className = `diagnostic-${firewall.level === "ok" ? "ok" : (firewall.level === "warning" ? "warning" : "unknown")}`;
  renderDiagnosticItems(
    $("firewallEvidence"),
    firewall.evidence || [],
    item => ({
      title: `${item.action || "Unknown"} · ${item.name || "未命名规则"}`,
      meta: `${item.profile || "Unknown"} · ${item.protocol || "Any"}:${item.local_port || "Any"} · ${item.program || "Any"}`
    }),
    firewall.status === "checking" ? "正在读取规则……" : "没有可展示的相关规则证据。"
  );
}

function renderDiagnosticItems(root, items, formatter, emptyMessage) {
  root.replaceChildren();
  if (!items.length) {
    const empty = document.createElement("span");
    empty.className = "subtitle";
    empty.textContent = emptyMessage;
    root.appendChild(empty);
    return;
  }
  for (const item of items) {
    const view = formatter(item);
    const row = document.createElement("div");
    row.className = "diagnostic-item";
    const title = document.createElement("div");
    title.className = "diagnostic-title";
    title.textContent = view.title;
    const meta = document.createElement("div");
    meta.className = "diagnostic-meta";
    meta.textContent = view.meta;
    row.append(title, meta);
    root.appendChild(row);
  }
}

function listText(value) {
  if (Array.isArray(value)) return value.join(", ") || "—";
  return value ? String(value) : "—";
}

function renderClients(clients) {
  const root = $("clients");
  const expandedClients = new Set(
    [...root.querySelectorAll(".client-details[open]")].map(item => item.dataset.clientId)
  );
  root.replaceChildren();
  if (!clients.length) {
    const empty = document.createElement("span");
    empty.className = "subtitle";
    empty.textContent = "当前没有可信客户机。";
    root.appendChild(empty);
    return;
  }
  for (const client of clients) {
    const row = document.createElement("div");
    row.className = "client";
    const main = document.createElement("div");
    main.className = "client-main";
    const id = document.createElement("div");
    id.className = "client-id";
    id.textContent = client.device_name || client.label || "未命名设备";
    const meta = document.createElement("div");
    meta.className = "client-meta";
    const created = client.created_at
      ? new Date(client.created_at).toLocaleString("zh-CN", { hour12: false })
      : "时间未知";
    meta.textContent = `${client.operating_system || "未知系统"} · ${client.browser || "未知浏览器"} · ${created}`;
    const details = document.createElement("details");
    details.className = "client-details";
    details.dataset.clientId = client.client_id;
    details.open = expandedClients.has(client.client_id);
    const summary = document.createElement("summary");
    summary.textContent = "ⓘ 详细信息";
    const grid = document.createElement("div");
    grid.className = "client-detail-grid";
    const detailRows = [
      ["设备类型", client.device_type || "未知"],
      ["报告型号", client.device_model || "浏览器未提供"],
      ["操作系统", client.operating_system || "未知"],
      ["浏览器", client.browser || "未知"],
      ["浏览器内核", client.browser_engine || "未知"],
      ["配对时间", created],
      ["客户机标识（浏览器）", client.client_id]
    ];
    for (const [label, value] of detailRows) {
      const key = document.createElement("span");
      key.className = "client-detail-label";
      key.textContent = label;
      const content = document.createElement("span");
      content.className = "client-detail-value";
      content.textContent = value;
      grid.append(key, content);
    }
    details.append(summary, grid);
    const revoke = document.createElement("button");
    revoke.className = "button secondary compact";
    revoke.textContent = "撤销信任";
    revoke.addEventListener("click", async () => {
      if (!confirm(`确定撤销客户机“${id.textContent}”的信任吗？`)) return;
      const result = await window.pywebview.api.revoke_trusted_client(client.client_id);
      if (!result.ok) showError(result.error); else renderClients(result.clients);
    });
    main.append(id, meta, details);
    row.append(main, revoke);
    root.appendChild(row);
  }
}

async function refreshClients() {
  const result = await window.pywebview.api.list_trusted_clients();
  if (!result.ok) showError(result.error); else renderClients(result.clients);
}

async function refreshInterfaces() {
  const selector = $("interfaceSelector");
  const previous = selector.value;
  const result = await window.pywebview.api.list_interfaces();
  if (!result.ok) {
    showError(result.error);
    return;
  }
  selector.replaceChildren();
  const automatic = document.createElement("option");
  automatic.value = "";
  automatic.textContent = "自动选择（仅一个可用 Private 网络）";
  selector.appendChild(automatic);
  for (const item of result.interfaces || []) {
    if (item.role === "excluded") continue;
    const option = document.createElement("option");
    option.value = item.address || "";
    const usable = String(item.category || "").toLowerCase() === "private";
    option.disabled = !usable;
    const alias = item.alias || "未命名接口";
    const profileName = item.description && item.description !== alias ? ` · ${item.description}` : "";
    option.textContent = `${alias}${profileName} · ${item.address || "无 IPv4"} · ${item.category || "类别未知"}${usable ? "" : " · 不可选"}`;
    selector.appendChild(option);
  }
  if ([...selector.options].some(option => option.value === previous && !option.disabled)) {
    selector.value = previous;
  }
}
