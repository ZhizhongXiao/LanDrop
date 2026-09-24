"use strict";

function createWizard(options = {}) {
  const pages = [...document.querySelectorAll(".wizard-page")];
  const steps = [...document.querySelectorAll(".wizard-step")];
  const back = document.getElementById("back");
  const next = document.getElementById("next");
  let current = 0;

  function render() {
    pages.forEach((page, index) => page.classList.toggle("active", index === current));
    steps.forEach((step, index) => {
      step.classList.toggle("active", index === current);
      step.classList.toggle("done", index < current);
    });
    back.disabled = current === 0;
    next.textContent = current === pages.length - 1 ? (options.finishLabel || "完成") : "继续";
    next.classList.toggle("danger", current === pages.length - 1 && options.dangerFinish);
  }

  back.addEventListener("click", () => {
    if (current > 0) current -= 1;
    render();
  });

  next.addEventListener("click", async () => {
    if (current < pages.length - 1) {
      current += 1;
      render();
      return;
    }
    if (typeof options.onFinish === "function") {
      await options.onFinish({ back, next, current });
      return;
    }
    const note = document.getElementById("previewResult");
    if (note) note.textContent = options.previewMessage || "这是界面草图，尚未执行任何系统操作。";
  });

  render();
  return { render, back, next };
}
