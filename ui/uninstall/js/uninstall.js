"use strict";

const result = document.getElementById("previewResult");
const deleteConfig = document.getElementById("deleteConfig");
const deleteLogs = document.getElementById("deleteLogs");
const deleteTrusted = document.getElementById("deleteTrusted");
let executionPoll = null;

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

async function pollExecution() {
  const status = await window.pywebview.api.get_uninstall_status();
  if (status.phase !== "finished") {
    executionPoll = window.setTimeout(pollExecution, 250);
    return;
  }
  const outcome = status.outcome || {};
  document.getElementById("executionTitle").textContent = outcome.finalization_pending
    ? "等待最终清理"
    : (outcome.complete ? "卸载完成" : "卸载未完全完成");
  document.getElementById("executionMessage").textContent = outcome.message || "卸载流程已结束。";
  const residuals = outcome.residuals || [];
  const residualPanel = document.getElementById("executionResiduals");
  if (residuals.length) {
    residualPanel.hidden = false;
    residualPanel.classList.add("error");
    residualPanel.textContent = `残留：${residuals.join("；")}`;
  }
  const next = document.getElementById("next");
  next.disabled = false;
  next.textContent = outcome.finalization_pending ? "关闭并完成清理" : "关闭";
  next.onclick = async event => {
    event.stopImmediatePropagation();
    await window.pywebview.api.close_window();
  };
}

function enterTemporaryMode() {
  document.querySelectorAll(".wizard-page").forEach(page => page.classList.remove("active"));
  document.querySelector(".wizard-steps").hidden = true;
  document.getElementById("executionPanel").hidden = false;
  document.getElementById("back").hidden = true;
  const next = document.getElementById("next");
  next.disabled = true;
  next.textContent = "正在卸载…";
}

window.addEventListener("pywebviewready", async () => {
  const context = await window.pywebview.api.get_context();
  if (!context.ok) {
    setResult(context.error || "无法读取卸载状态。", true);
    return;
  }
  document.getElementById("uninstallVersion").textContent = context.version
    ? `LanDrop ${context.version} · Uninstall`
    : "LanDrop Uninstall";
  if (context.mode === "temporary") {
    enterTemporaryMode();
    const started = await window.pywebview.api.start_execution();
    if (!started.ok) {
      document.getElementById("executionMessage").textContent = started.error || "无法开始卸载。";
      return;
    }
    await pollExecution();
    return;
  }
  document.getElementById("uninstallIntro").textContent =
    `将从 ${context.install_root} 移除 LanDrop ${context.version}。默认保留全部用户数据。`;
});
