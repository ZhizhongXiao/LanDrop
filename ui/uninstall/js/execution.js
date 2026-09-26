"use strict";

const executionTitle = document.getElementById("executionTitle");
const executionMessage = document.getElementById("executionMessage");
const executionResiduals = document.getElementById("executionResiduals");
const next = document.getElementById("next");
let executionPoll = null;

async function closeExecutionWindow() {
  await window.pywebview.api.close_window();
}

function showExecutionFailure(message) {
  executionTitle.textContent = "卸载无法继续";
  executionMessage.textContent = message;
  executionResiduals.hidden = false;
  executionResiduals.classList.add("error");
  executionResiduals.textContent = "程序与用户数据尚未按本次请求完成清理。";
  next.disabled = false;
  next.textContent = "关闭";
  next.onclick = closeExecutionWindow;
}

async function pollExecution() {
  const status = await window.pywebview.api.get_uninstall_status();
  if (status.phase !== "finished") {
    executionPoll = window.setTimeout(pollExecution, 250);
    return;
  }
  const outcome = status.outcome || {};
  executionTitle.textContent = outcome.finalization_pending
    ? "等待最终清理"
    : (outcome.complete ? "卸载完成" : "卸载未完全完成");
  executionMessage.textContent = outcome.message || "卸载流程已结束。";
  const residuals = outcome.residuals || [];
  if (residuals.length) {
    executionResiduals.hidden = false;
    executionResiduals.classList.add("error");
    executionResiduals.textContent = `残留：${residuals.join("；")}`;
  }
  next.disabled = false;
  next.textContent = outcome.finalization_pending ? "关闭并完成清理" : "关闭";
  next.onclick = closeExecutionWindow;
}

window.addEventListener("pywebviewready", async () => {
  const context = await window.pywebview.api.get_context();
  if (!context.ok || context.mode !== "temporary") {
    showExecutionFailure(context.error || "临时卸载界面模式不匹配。");
    return;
  }
  document.getElementById("uninstallVersion").textContent = context.version
    ? `LanDrop ${context.version} · Uninstall`
    : "LanDrop Uninstall";
  const started = await window.pywebview.api.start_execution();
  if (!started.ok) {
    showExecutionFailure(started.error || "无法开始卸载。");
    return;
  }
  await pollExecution();
});
