"use strict";

const executionTitle = document.getElementById("executionTitle");
const executionMessage = document.getElementById("executionMessage");
const executionResiduals = document.getElementById("executionResiduals");
const firewallCleanup = document.getElementById("firewallCleanup");
const openFirewallAllowedApps = document.getElementById("openFirewallAllowedApps");
const firewallOpenStatus = document.getElementById("firewallOpenStatus");
const next = document.getElementById("next");
let executionPoll = null;

async function closeExecutionWindow() {
  await window.pywebview.api.close_window();
}

async function openAllowedAppsPage() {
  openFirewallAllowedApps.disabled = true;
  firewallOpenStatus.textContent = "";
  firewallOpenStatus.classList.remove("error");
  try {
    const result = await window.pywebview.api.open_firewall_allowed_apps();
    if (!result.ok) {
      firewallOpenStatus.textContent = result.error || "无法打开 Windows 防火墙页面。";
      firewallOpenStatus.classList.add("error");
      return;
    }
    firewallOpenStatus.textContent = "已打开页面。";
  } catch (error) {
    firewallOpenStatus.textContent = error instanceof Error ? error.message : "无法打开 Windows 防火墙页面。";
    firewallOpenStatus.classList.add("error");
  } finally {
    openFirewallAllowedApps.disabled = false;
  }
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
  if (outcome.complete || outcome.finalization_pending) {
    firewallCleanup.hidden = false;
  }
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

openFirewallAllowedApps.addEventListener("click", openAllowedAppsPage);

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
