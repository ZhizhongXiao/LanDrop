"use strict";

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
    if (result.ok) {
      render(result.state);
      if (result.warning) showError(result.warning);
    }
  } catch (error) {
    showError(String(error));
  } finally {
    refreshing = false;
  }
}

function showPage(pageId) {
  const selectedPage = $(pageId);
  const parentPageId = selectedPage?.dataset.parentPage || pageId;
  for (const page of document.querySelectorAll(".page")) {
    page.classList.toggle("active", page.id === pageId);
  }
  let activeTab = null;
  for (const tab of document.querySelectorAll(".tab")) {
    const active = tab.dataset.page === parentPageId;
    tab.classList.toggle("active", active);
    tab.toggleAttribute("aria-current", active);
    if (active) activeTab = tab;
  }
  $("workspaceTitle").textContent = selectedPage?.dataset.title || activeTab?.dataset.title || "LanDrop";
  $("workspaceSubtitle").textContent = selectedPage?.dataset.subtitle || activeTab?.dataset.subtitle || "";
  window.scrollTo({ top: 0, behavior: "smooth" });
}

const pageIds = new Set([...document.querySelectorAll(".page")].map(page => page.id));
let navigationIndex = 0;

function navigateTo(pageId) {
  if (!pageIds.has(pageId)) pageId = "mainPage";
  const currentPageId = document.querySelector(".page.active")?.id;
  if (currentPageId === pageId) return;

  if (pageId === "mainPage") {
    if (navigationIndex > 0) history.back();
    else showPage("mainPage");
    return;
  }

  if (navigationIndex > 0) {
    history.replaceState(
      { landropPage: pageId, landropIndex: 1 },
      "",
      `#${pageId}`
    );
  } else {
    navigationIndex = 1;
    history.pushState(
      { landropPage: pageId, landropIndex: 1 },
      "",
      `#${pageId}`
    );
  }
  showPage(pageId);
}

function returnToMain() {
  if (navigationIndex > 0) history.back();
  else navigateTo("mainPage");
}

history.scrollRestoration = "manual";
const initialHashPage = window.location.hash.slice(1);
history.replaceState(
  { landropPage: "mainPage", landropIndex: 0 },
  "",
  "#mainPage"
);
showPage("mainPage");
if (pageIds.has(initialHashPage) && initialHashPage !== "mainPage") {
  navigateTo(initialHashPage);
}

window.addEventListener("popstate", event => {
  const pageId = pageIds.has(event.state?.landropPage)
    ? event.state.landropPage
    : "mainPage";
  navigationIndex = Number(event.state?.landropIndex || 0);
  showPage(pageId);
  if (pageId === "trustedClientsPage") refreshClients();
});

async function copyDiagnosticReport() {
  const result = await window.pywebview.api.get_diagnostic_report();
  if (!result.ok) {
    $("settingsNotice").textContent = result.error;
    return;
  }
  let copied = false;
  try {
    await navigator.clipboard.writeText(result.text);
    copied = true;
  } catch (_error) {
    const area = document.createElement("textarea");
    area.value = result.text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    copied = document.execCommand("copy");
    area.remove();
  }
  $("settingsNotice").textContent = copied
    ? "诊断信息已复制；不包含配对码、凭据或收发目录。"
    : "无法写入剪贴板，请稍后重试。";
}

for (const tab of document.querySelectorAll(".tab")) {
  tab.addEventListener("click", () => navigateTo(tab.dataset.page));
}

for (const button of document.querySelectorAll(".chooser")) {
  button.addEventListener("click", async () => {
    const input = $(button.dataset.target);
    showError("");
    const result = await window.pywebview.api.choose_directory(input.value);
    if (!result.ok) showError(result.error);
    else if (!result.cancelled) input.value = result.path;
  });
}

$("start").addEventListener("click", async () => {
  busy = true;
  showInfo("正在检测 Private 网络并启动服务……");
  await refresh();
  showInfo("正在检测 Private 网络并启动服务……");
  const result = await window.pywebview.api.start_service({
    shared_directory: $("shared").value,
    receive_directory: $("received").value,
    max_upload_mb: $("limit").value,
    interface_selector: $("interfaceSelector").value
  });
  busy = false;
  if (!result.ok) showError(result.error);
  else if (result.warning) showInfo(`服务已启动，但${result.warning}`);
  else showInfo("");
  render(result.state);
  if (result.ok && result.state.running) startTrustedRefreshWindow();
});

$("saveSettings").addEventListener("click", async () => {
  showError("");
  const result = await window.pywebview.api.save_settings({
    shared_directory: $("shared").value,
    receive_directory: $("received").value,
    max_upload_mb: $("limit").value
  });
  if (!result.ok) showError(result.error);
  else showInfo("设置已保存；当前服务状态未改变。");
});

$("stop").addEventListener("click", async () => {
  busy = true;
  showError("");
  await refresh();
  const result = await window.pywebview.api.stop_service();
  busy = false;
  if (!result.ok) showError(result.error); else render(result.state);
});

$("resetDeadline").addEventListener("click", async () => {
  busy = true;
  showError("");
  await refresh();
  const result = await window.pywebview.api.reset_deadline();
  busy = false;
  if (!result.ok) showError(result.error);
  render(result.state);
});

$("openQrDialog").addEventListener("click", () => {
  if (!$("pairingQrLarge").getAttribute("src")) return;
  $("qrDialog").showModal();
});

$("qrDialog").addEventListener("click", event => {
  if (event.target === $("qrDialog")) $("qrDialog").close();
});

$("refreshClients").addEventListener("click", refreshClients);
$("refreshInterfaces").addEventListener("click", refreshInterfaces);
$("openTransferSettingsCard").addEventListener("click", () => navigateTo("transferSettingsPage"));
$("returnMainFromTransferSettings").addEventListener("click", returnToMain);
$("openTrustedClients").addEventListener("click", async () => {
  navigateTo("trustedClientsPage");
  await refreshClients();
});
$("returnMainFromClients").addEventListener("click", returnToMain);
$("revokeAll").addEventListener("click", async () => {
  if (!confirm("确定撤销全部可信客户机吗？所有客户机浏览器下次访问都需要重新配对。")) return;
  const result = await window.pywebview.api.revoke_all_trusted_clients();
  if (!result.ok) showError(result.error); else renderClients(result.clients);
});

$("refreshDiagnostics").addEventListener("click", async () => {
  $("refreshDiagnostics").disabled = true;
  $("settingsNotice").textContent = "已开始异步诊断；可切换到“信息”页查看进度。";
  const result = await window.pywebview.api.refresh_diagnostics();
  $("refreshDiagnostics").disabled = false;
  if (!result.ok) $("settingsNotice").textContent = result.error;
  else render(result.state);
});

$("viewDiagnostics").addEventListener("click", () => {
  navigateTo("infoPage");
  $("diagnosticDetails").open = true;
  $("diagnosticDetails").scrollIntoView({ behavior: "smooth", block: "start" });
});

$("openLogs").addEventListener("click", async () => {
  const result = await window.pywebview.api.open_log_folder();
  $("settingsNotice").textContent = result.ok ? "" : result.error;
});

$("copyDiagnostics").addEventListener("click", copyDiagnosticReport);

for (const button of document.querySelectorAll(".settings-link")) {
  button.addEventListener("click", async () => {
    const result = await window.pywebview.api.open_windows_settings(button.dataset.target);
    $("settingsNotice").textContent = result.ok ? "" : result.error;
  });
}

window.addEventListener("pywebviewready", async () => {
  await refresh();
  await refreshInterfaces();
  await refreshClients();
  setInterval(tickCountdown, 200);
  setInterval(refresh, 1000);
});
