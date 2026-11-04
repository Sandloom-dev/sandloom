// /docs and /redoc outside production: the theme switch in the header.
// System, light or dark, kept in this browser (localStorage, a convenience:
// without it the pages follow the system). Loaded in <head>, before the
// first paint, so a dark choice never flashes light.
(() => {
  const KEY = "sandloom-docs-theme";
  const MODES = ["system", "light", "dark"];
  const LABELS = { system: "Theme: system", light: "Theme: light", dark: "Theme: dark" };
  const root = document.documentElement;
  const media = matchMedia("(prefers-color-scheme: dark)");
  const read = () => {
    try {
      const mode = localStorage.getItem(KEY);
      return MODES.includes(mode) ? mode : "system";
    } catch {
      return "system";
    }
  };
  const isDark = (mode) => mode === "dark" || (mode === "system" && media.matches);
  let shown = null;
  const apply = () => {
    const mode = read();
    const dark = isDark(mode);
    root.classList.toggle("sl-dark", dark);
    root.classList.toggle("sl-light", !dark);
    root.classList.toggle("dark-mode", dark); // Swagger UI's own dark mode
    for (const button of document.querySelectorAll(".sl-theme")) {
      button.dataset.mode = mode;
      button.title = LABELS[mode];
      button.setAttribute("aria-label", LABELS[mode]);
    }
    if (shown !== dark) {
      shown = dark;
      document.dispatchEvent(new CustomEvent("sl-theme", { detail: { dark } }));
    }
  };
  window.slTheme = { dark: () => isDark(read()) };
  apply();
  media.addEventListener("change", apply);
  document.addEventListener("DOMContentLoaded", apply);
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".sl-theme")) return;
    const next = MODES[(MODES.indexOf(read()) + 1) % MODES.length];
    try {
      localStorage.setItem(KEY, next);
    } catch {
      // Not kept: this page still switches.
    }
    apply();
  });
})();
