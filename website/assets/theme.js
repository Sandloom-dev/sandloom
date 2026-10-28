/* Shared color preference and navigation for the entire Sandloom site. */
(() => {
  if (window.__sandloomSiteReady) return;
  window.__sandloomSiteReady = true;
  const key = "sandloom:theme";
  const system = window.matchMedia("(prefers-color-scheme: dark)");
  const desktop = window.matchMedia("(min-width: 1220px)");
  const read = () => {
    try { return localStorage.getItem(key); } catch { return null; }
  };
  const resolve = () => read() === "dark" || (read() !== "light" && system.matches) ? "dark" : "light";
  const prepare = (theme) => {
    document.documentElement.dataset.sandloomTheme = theme;
    try {
      localStorage.setItem("/docs/.__palette", JSON.stringify({
        index: theme === "dark" ? 1 : 0,
        color: {scheme: theme === "dark" ? "slate" : "default", primary: "custom", accent: "custom",
          media: `(prefers-color-scheme: ${theme})`}
      }));
    } catch { /* The theme also works without persistent storage. */ }
  };
  const apply = (theme) => {
    prepare(theme);
    if (document.body.hasAttribute("data-md-color-scheme")) {
      const scheme = theme === "dark" ? "slate" : "default";
      const radio = document.querySelector(`input.md-option[data-md-color-scheme="${scheme}"]`);
      if (radio && !radio.checked) radio.click();
      document.body.dataset.mdColorScheme = scheme;
    }
    document.querySelectorAll("[data-sandloom-theme-toggle]").forEach(button => {
      const label = `Switch to ${theme === "dark" ? "light" : "dark"} mode`;
      button.setAttribute("aria-label", label);
      button.title = label;
    });
  };
  const menu = () => document.querySelector("#sl-site-menu");
  const syncMenuState = () => {
    const open = !!menu()?.open;
    document.documentElement.classList.toggle("sl-menu-open", open);
    document.querySelectorAll("[data-sandloom-menu-toggle]").forEach(button => button.setAttribute("aria-expanded", String(open)));
  };
  const normalizedPath = value => value.replace(/\/+$/, "") || "/";
  const syncSiteSection = () => {
    const path = normalizedPath(location.pathname);
    const section = path === "/docs" || path.startsWith("/docs/") ? "docs"
      : path === "/examples" || path.startsWith("/examples/") ? "examples"
      : path === "/" ? "home" : null;
    document.querySelectorAll("[data-sandloom-section]").forEach(link => {
      if (link.dataset.sandloomSection !== section) { link.removeAttribute("aria-current"); return; }
      const exact = normalizedPath(new URL(link.href).pathname) === path;
      link.setAttribute("aria-current", exact ? "page" : "location");
    });
  };
  const wired = new WeakSet();
  const syncNavigation = () => {
    syncSiteSection();
    const dialog = menu();
    if (dialog && !wired.has(dialog)) {
      dialog.addEventListener("close", syncMenuState);
      wired.add(dialog);
    }
    document.querySelectorAll(".sl-doc-tree a").forEach(link => {
      const current = new URL(link.href).pathname === location.pathname;
      if (!current) { link.removeAttribute("aria-current"); return; }
      link.setAttribute("aria-current", "page");
      for (let group = link.closest("details"); group; group = group.parentElement.closest("details")) group.open = true;
    });
    syncMenuState();
    apply(resolve());
  };
  prepare(resolve());
  document.addEventListener("DOMContentLoaded", () => {
    syncNavigation();
    // Delegated handlers survive Zensical's instant page navigation.
    document.addEventListener("click", event => {
      if (!(event.target instanceof Element)) return;
      const target = event.target;
      if (target.closest("[data-sandloom-menu-toggle]")) {
        const dialog = menu();
        if (!dialog) return;
        if (dialog.open) dialog.close();
        else {
          dialog.showModal();
          dialog.querySelector("[data-sandloom-menu-close]").focus();
          dialog.querySelector('[aria-current="page"]')?.scrollIntoView({block: "nearest"});
        }
        syncMenuState();
      } else if (target.closest("[data-sandloom-menu-close]")) {
        menu()?.close();
      } else if (target.closest("[data-sandloom-menu-search]")) {
        menu()?.close();
        document.querySelector(".sl-header .sl-search button")?.click();
      } else if (target.closest("[data-sandloom-theme-toggle]")) {
        const theme = document.documentElement.dataset.sandloomTheme === "dark" ? "light" : "dark";
        try { localStorage.setItem(key, theme); } catch { /* In-memory fallback. */ }
        apply(theme);
      } else if (target.closest(".sl-site-menu a") && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) {
        menu()?.close();
      }
    });
    if (window.document$) window.document$.subscribe(syncNavigation);
  });
  desktop.addEventListener("change", event => { if (event.matches) menu()?.close(); });
  window.addEventListener("popstate", syncNavigation);
  system.addEventListener("change", () => { if (!read()) apply(resolve()); });
  window.addEventListener("storage", event => { if (event.key === key || event.key === null) apply(resolve()); });
})();
