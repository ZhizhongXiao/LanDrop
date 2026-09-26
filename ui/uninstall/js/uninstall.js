"use strict";

const result = document.getElementById("previewResult");
const deleteConfig = document.getElementById("deleteConfig");
const deleteLogs = document.getElementById("deleteLogs");
const deleteTrusted = document.getElementById("deleteTrusted");

function setResult(message, failed = false) {
  result.textContent = message;
  result.classList.toggle("error", failed);
}

const wizard = createWizard({
  finishLabel: "卸载",
  dangerFinish: true,
  onFinish: async ({ next, back }) => {
    if (next.dataset.started === "true") return;
    next.dataset.started = "true";
    next.disabled = true;
    back.disabled = true;
    setResult("正在生成受绑定的临时卸载请求…");
    const response = await window.pywebview.api.start_uninstall({
      delete_config: deleteConfig.checked,
      delete_logs: deleteLogs.checked,
      delete_trusted_clients: deleteTrusted.checked
    });
    if (!response.ok) {
      setResult(response.error || "无法启动临时卸载器。", true);
      next.dataset.started = "false";
      next.disabled = false;
      back.disabled = false;
      return;
    }
    setResult("临时卸载器已启动，本窗口即将关闭。 ");
  }
});

window.addEventListener("pywebviewready", async () => {
  const context = await window.pywebview.api.get_context();
  if (!context.ok) {
    setResult(context.error || "无法读取卸载状态。", true);
    return;
  }
  document.getElementById("uninstallVersion").textContent = context.version
    ? `LanDrop ${context.version} · Uninstall`
    : "LanDrop Uninstall";
  if (context.mode !== "installed") {
    setResult("卸载界面模式不匹配，请从 Windows 卸载入口重新启动。", true);
    return;
  }
  document.getElementById("uninstallIntro").textContent =
    `将从 ${context.install_root} 移除 LanDrop ${context.version}。默认保留全部用户数据。`;
});
