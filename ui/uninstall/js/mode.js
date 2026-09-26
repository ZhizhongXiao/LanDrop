"use strict";

// The temporary onefile process loads a fresh WebView. Mark that mode before
// the body is parsed so the first painted navigation never falls back to the
// installed uninstaller's step 1.
const uninstallMode = new URLSearchParams(window.location.search).get("mode");
if (uninstallMode === "temporary") {
  document.documentElement.classList.add("uninstall-temporary");
}
