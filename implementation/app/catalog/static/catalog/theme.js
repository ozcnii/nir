(() => {
  const key = "catalog-theme";
  const root = document.documentElement;
  const apply = (theme) => {
    if (theme === "light" || theme === "dark") root.dataset.theme = theme;
    else delete root.dataset.theme;
  };

  try {
    apply(localStorage.getItem(key));
  } catch {
    // The default follows the device when browser storage is unavailable.
  }

  document.addEventListener("DOMContentLoaded", () => {
    const menu = document.querySelector("[data-theme-menu]");
    const summary = menu.querySelector("summary");
    const current = root.dataset.theme ?? "system";
    menu.querySelector(`[value="${current}"]`).checked = true;
    menu.hidden = false;

    const close = () => {
      menu.open = false;
      summary.focus();
    };
    menu.addEventListener("change", ({ target }) => {
      apply(target.value);
      try {
        localStorage.setItem(key, target.value);
      } catch {
        // The choice still applies on the current page.
      }
    });
    menu.addEventListener("click", ({ target, detail }) => {
      if (detail && target.matches("input")) close();
    });
    menu.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && menu.open) close();
    });
    document.addEventListener("click", ({ target }) => {
      if (!menu.contains(target)) menu.open = false;
    });
  });
})();
