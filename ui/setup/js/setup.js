"use strict";

const result = document.getElementById("previewResult");
const desktop = document.getElementById("desktopShortcut");
const desktopSummary = document.getElementById("desktopSummary");
let pollTimer = null;

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

createWizard({
  finishLabel: "安装",
  onFinish: async ({ next, back }) => {
    if (next.dataset.started === "true") return;
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
});

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
  desktop.checked = status.desktop_shortcut_default === true;
  desktopSummary.textContent = desktop.checked ? "创建" : "不创建";
});
