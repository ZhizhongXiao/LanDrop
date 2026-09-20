"use strict";

(() => {
  const storageKey = "landrop.ui.theme";
  const modes = ["system", "dark", "light"];
  const views = {
    system: { glyph: "◐", label: "跟随系统", next: "深色" },
    dark: { glyph: "☾", label: "深色", next: "浅色" },
    light: { glyph: "☀", label: "浅色", next: "跟随系统" }
  };
  let mode = "system";

  try {
    const stored = window.localStorage.getItem(storageKey);
    if (modes.includes(stored)) mode = stored;
  } catch (_error) {
    // file:// and hardened WebView profiles may disable localStorage.
  }

  function applyTheme() {
    if (mode === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.dataset.theme = mode;

    const view = views[mode];
    for (const button of document.querySelectorAll("[data-theme-toggle]")) {
      button.title = `主题：${view.label}；点击切换为${view.next}模式`;
      button.setAttribute("aria-label", button.title);
      const indicator = button.querySelector("[data-theme-indicator]");
      if (indicator) indicator.textContent = view.glyph;
    }
  }

  for (const button of document.querySelectorAll("[data-theme-toggle]")) {
    button.addEventListener("click", () => {
      mode = modes[(modes.indexOf(mode) + 1) % modes.length];
      try {
        window.localStorage.setItem(storageKey, mode);
      } catch (_error) {
        // The theme remains active for this window even without persistence.
      }
      applyTheme();
    });
  }

  applyTheme();
})();
