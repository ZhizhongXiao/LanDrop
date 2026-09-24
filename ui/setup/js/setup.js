"use strict";

const result = document.getElementById("previewResult");
const desktop = document.getElementById("desktopShortcut");
const desktopSummary = document.getElementById("desktopSummary");
let pollTimer = null;
let setupDisposition = "first_install";
let setupBlockedMessage = "";

function setResult(message, failed = false) {
  result.textContent = message;
  result.classList.toggle("error", failed);
}

function stageLabel(stage) {
  return ({
    acquiring_lock: "正在取得安装生命周期锁…",
    preflight: "正在检查安装边界和系统入口…",
    staging: "正在解包并验证程序文件…",
    app_switch: "正在建立正式程序目录…",
    self_check: "正在验证正式 LanDrop 程序…",
    integration_write: "正在写入当前用户系统入口…",
    integration_verify: "正在读回验证系统入口…",
    commit: "正在提交 install.json…",
    cleanup: "正在安全清理旧版本程序文件…",
    rollback: "安装失败，正在撤销本轮修改…",
    completed: "安装完成。"
  })[stage] || "正在安装…";
}

async function pollInstall(next, back) {
  const status = await window.pywebview.api.get_install_status();
  if (!status.ok) {
    setResult(status.error || "无法读取安装状态。", true);
    next.disabled = false;
    back.disabled = false;
    return;
  }
  if (status.phase !== "finished") {
    setResult(stageLabel(status.stage));
    pollTimer = window.setTimeout(() => pollInstall(next, back), 250);
    return;
  }
  const outcome = status.outcome || {};
  const succeeded = outcome.verified === true && ["success", "success-with-warning"].includes(outcome.result);
  setResult(succeeded ? `${outcome.message}${outcome.warning ? ` ${outcome.warning}` : ""}` : (outcome.message || "安装失败。"), !succeeded);
  next.disabled = false;
  next.textContent = succeeded ? "完成" : "关闭";
  next.onclick = async event => {
    event.stopImmediatePropagation();
    await window.pywebview.api.close_window();
  };
}

const wizardOptions = {
  finishLabel: "安装",
  onFinish: async ({ next, back }) => {
    if (next.dataset.started === "true") return;
    if (["downgrade_blocked", "untrusted"].includes(setupDisposition)) {
      setResult(setupBlockedMessage || "当前安装状态不允许继续。", true);
      return;
    }
    next.dataset.started = "true";
    next.disabled = true;
    back.disabled = true;
    setResult("正在开始安装…");
    const response = await window.pywebview.api.start_install({
      desktop_shortcut: desktop.checked
    });
    if (!response.ok) {
      setResult(response.error || "无法开始安装。", true);
      next.dataset.started = "false";
      next.disabled = false;
      back.disabled = false;
      return;
    }
    await pollInstall(next, back);
  }
};

createWizard(wizardOptions);

desktop.addEventListener("change", () => {
  desktopSummary.textContent = desktop.checked ? "创建" : "不创建";
});

window.addEventListener("pywebviewready", async () => {
  const status = await window.pywebview.api.get_status();
  if (!status.ok) {
    setResult(status.error || "无法读取安装信息。", true);
    return;
  }
  document.getElementById("installRoot").textContent = status.install_root;
  document.getElementById("setupVersion").textContent = `LanDrop ${status.version} · Setup`;
  setupDisposition = status.disposition || "untrusted";
  setupBlockedMessage = status.message || "当前安装状态无法继续。";
  const firstInstall = setupDisposition === "first_install";
  desktop.disabled = status.desktop_shortcut_enabled !== true;
  desktop.checked = firstInstall && status.desktop_shortcut_default === true;
  desktopSummary.textContent = firstInstall
    ? (desktop.checked ? "创建" : "不创建")
    : "保持当前状态";
  const actionLabels = {
    first_install: "安装",
    upgrade: "升级",
    already_installed: status.pending_cleanup?.length ? "重试清理" : "检查完成",
    downgrade_blocked: "不可降级",
    incomplete: "恢复并继续",
    untrusted: "无法继续"
  };
  wizardOptions.finishLabel = actionLabels[setupDisposition] || "无法继续";
  document.getElementById("setupModeMessage").textContent = status.message;
  document.getElementById("setupActionSummary").textContent = firstInstall
    ? `安装 LanDrop ${status.version}`
    : setupDisposition === "upgrade"
      ? `从 ${status.current_version} 升级到 ${status.version}`
      : status.message;
});
