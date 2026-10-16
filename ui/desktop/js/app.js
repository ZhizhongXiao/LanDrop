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

async function writeClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (_error) {
    const area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    try {
      document.body.appendChild(area);
      area.select();
      return document.execCommand("copy");
    } catch (_fallbackError) {
      return false;
    } finally {
      area.remove();
    }
  }
}

async function copySessionValue(elementId, successMessage) {
  const value = $(elementId).textContent.trim();
  if (!value || value === "—") return;
  const copied = await writeClipboard(value);
  if (copied) showInfo(successMessage);
  else showError("无法写入剪贴板，请稍后重试。");
}

async function copyDiagnosticReport() {
  try {
    const result = await window.pywebview.api.get_diagnostic_report();
    if (!result.ok) {
      setNotice("settingsNotice", result.error, true);
      return;
    }
    const copied = await writeClipboard(result.text);
    setNotice(
      "settingsNotice",
      copied
        ? "诊断信息已复制；不包含配对码、凭据或收发目录。"
        : "无法写入剪贴板，请稍后重试。",
      !copied
    );
  } catch (error) {
    setNotice("settingsNotice", `复制诊断信息失败：${error}`, true);
  }
}

for (const tab of document.querySelectorAll(".tab")) {
  tab.addEventListener("click", () => navigateTo(tab.dataset.page));
}

for (const button of document.querySelectorAll(".chooser")) {
  button.addEventListener("click", async () => {
    const input = $(button.dataset.target);
    showInfo("");
    try {
      const result = await window.pywebview.api.choose_directory(input.value);
      if (!result.ok) showError(result.error);
      else if (!result.cancelled) {
        input.value = result.path;
        showInfo("目录已更新；保存设置后才会持久化。");
      }
    } catch (error) {
      showError(`选择目录失败：${error}`);
    }
  });
}

$("start").addEventListener("click", async () => {
  busy = true;
  showInfo("正在检测 Private 网络并启动服务……");
  try {
    await refresh();
    showInfo("正在检测 Private 网络并启动服务……");
    const result = await window.pywebview.api.start_service({
      shared_directory: $("shared").value,
      receive_directory: $("received").value,
      max_upload_mb: $("limit").value,
      interface_selector: $("interfaceSelector").value
    });
    if (!result.ok) showError(result.error);
    else if (result.warning) showInfo(`服务已启动，但${result.warning}`);
    else showInfo("服务已启动。");
    render(result.state);
    if (result.ok && result.state.running) startTrustedRefreshWindow();
  } catch (error) {
    showError(`启动服务失败：${error}`);
  } finally {
    busy = false;
    await refresh();
  }
});

$("saveSettings").addEventListener("click", async () => {
  if (busy) return;
  busy = true;
  const button = $("saveSettings");
  const settingsControls = [
    $("shared"), $("received"), $("limit"), $("interfaceSelector"),
    $("refreshInterfaces"), ...document.querySelectorAll(".chooser")
  ];
  const settings = {
    shared_directory: $("shared").value,
    receive_directory: $("received").value,
    max_upload_mb: $("limit").value
  };
  settingsControls.forEach(control => { control.disabled = true; });
  button.disabled = true;
  button.textContent = "保存中…";
  try {
    const result = await window.pywebview.api.save_settings(settings);
    if (!result.ok) showToast(result.error || "保存设置失败。", { isError: true });
    else {
      $("settingsPreviewShared").textContent = settings.shared_directory || "尚未设置";
      $("settingsPreviewReceived").textContent = settings.receive_directory || "尚未设置";
      showToast("目录和单文件上传上限已保存。");
    }
  } catch (error) {
    showToast(`保存设置失败：${error}`, { isError: true });
  } finally {
    busy = false;
    button.textContent = "保存设置";
    const serviceRunning = $("badge").classList.contains("running");
    settingsControls.forEach(control => { control.disabled = serviceRunning; });
    button.disabled = serviceRunning;
  }
});

$("stop").addEventListener("click", async () => {
  busy = true;
  showInfo("正在停止服务……");
  try {
    await refresh();
    const result = await window.pywebview.api.stop_service();
    if (!result.ok) showError(result.error);
    else {
      render(result.state);
      showInfo("服务已停止。");
    }
  } catch (error) {
    showError(`停止服务失败：${error}`);
  } finally {
    busy = false;
    await refresh();
  }
});

$("resetDeadline").addEventListener("click", async () => {
  busy = true;
  showInfo("正在重置服务时限……");
  try {
    await refresh();
    const result = await window.pywebview.api.reset_deadline();
    if (!result.ok) showError(result.error);
    else showInfo("服务时限已重置为 5 分钟。");
    if (result.state) render(result.state);
  } catch (error) {
    showError(`重置服务时限失败：${error}`);
  } finally {
    busy = false;
    await refresh();
  }
});

$("openQrDialog").addEventListener("click", () => {
  if (!$("pairingQrLarge").getAttribute("src")) return;
  $("qrDialog").showModal();
});

$("copyPairing").addEventListener("click", () => copySessionValue("pairing", "配对码已复制。"));
$("copyLanUrl").addEventListener("click", () => copySessionValue("lanUrl", "客户机访问地址已复制。"));

for (const [buttonId, target, label] of [
  ["openSharedDirectory", "shared", "共享目录"],
  ["openReceivedDirectory", "received", "接收目录"]
]) {
  $(buttonId).addEventListener("click", async () => {
    try {
      const result = await window.pywebview.api.open_transfer_directory(target);
      if (result.ok) showInfo("已打开" + label + "。");
      else showError(result.error || "无法打开" + label + "。");
    } catch (error) {
      showError("打开" + label + "失败：" + error);
    }
  });
}

$("qrDialog").addEventListener("click", event => {
  if (event.target === $("qrDialog")) $("qrDialog").close();
});

$("confirmDialog").addEventListener("click", event => {
  if (event.target === $("confirmDialog")) $("confirmDialog").close("cancel");
});

$("refreshClients").addEventListener("click", () => refreshClients({ showFeedback: true }));
$("refreshInterfaces").addEventListener("click", () => refreshInterfaces({ showFeedback: true }));
$("interfaceSelector").addEventListener("change", () => {
  const selected = $("interfaceSelector").selectedOptions[0]?.textContent || "自动选择";
  showInfo(`已选择“${selected}”；下次启动服务时生效，不会写入持久设置。`);
});
$("openTransferSettingsCard").addEventListener("click", () => navigateTo("transferSettingsPage"));
$("returnMainFromTransferSettings").addEventListener("click", returnToMain);
$("openTrustedClients").addEventListener("click", async () => {
  navigateTo("trustedClientsPage");
  await refreshClients();
});
$("returnMainFromClients").addEventListener("click", returnToMain);
$("revokeAll").addEventListener("click", async () => {
  const confirmed = await confirmAction({
    title: "撤销全部信任",
    message: "确定撤销所有可信客户机吗？这些浏览器下次访问时都需要重新配对。",
    confirmLabel: "全部撤销"
  });
  if (!confirmed) return;
  try {
    const result = await window.pywebview.api.revoke_all_trusted_clients();
    if (!result.ok) showError(result.error);
    else {
      renderClients(result.clients);
      showInfo(result.count ? `已撤销 ${result.count} 台客户机的信任。` : "当前没有可信客户机需要撤销。");
    }
  } catch (error) {
    showError(`撤销全部信任失败：${error}`);
  }
});

$("refreshDiagnostics").addEventListener("click", async () => {
  $("refreshDiagnostics").disabled = true;
  setNotice("settingsNotice", "正在启动网络与防火墙诊断……");
  try {
    const result = await window.pywebview.api.refresh_diagnostics();
    if (!result.ok) setNotice("settingsNotice", result.error, true);
    else {
      render(result.state);
      setNotice("settingsNotice", "诊断已开始；可在“信息”页查看结果状态。检测在后台继续运行。");
    }
  } catch (error) {
    setNotice("settingsNotice", `启动诊断失败：${error}`, true);
  } finally {
    $("refreshDiagnostics").disabled = false;
  }
});

$("viewDiagnostics").addEventListener("click", () => {
  navigateTo("infoPage");
  $("diagnosticDetails").open = true;
  $("diagnosticDetails").scrollIntoView({ behavior: "smooth", block: "start" });
});

$("openLogs").addEventListener("click", async () => {
  try {
    const result = await window.pywebview.api.open_log_folder();
    setNotice("settingsNotice", result.ok ? "已打开日志文件夹。" : result.error, !result.ok);
  } catch (error) {
    setNotice("settingsNotice", `打开日志文件夹失败：${error}`, true);
  }
});

$("copyDiagnostics").addEventListener("click", copyDiagnosticReport);

for (const button of document.querySelectorAll(".settings-link")) {
  button.addEventListener("click", async () => {
    const targetLabels = { network: "网络设置", firewall: "防火墙设置", proxy: "代理设置" };
    try {
      const result = await window.pywebview.api.open_windows_settings(button.dataset.target);
      setNotice(
        "settingsNotice",
        result.ok ? `已请求打开 Windows ${targetLabels[button.dataset.target] || "设置"}。` : result.error,
        !result.ok
      );
    } catch (error) {
      setNotice("settingsNotice", `打开 Windows 设置失败：${error}`, true);
    }
  });
}

window.addEventListener("pywebviewready", async () => {
  await refresh();
  await refreshInterfaces();
  await refreshClients();
  setInterval(tickCountdown, 200);
  setInterval(refresh, 1000);
});
