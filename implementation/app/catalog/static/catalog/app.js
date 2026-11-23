import { mountCatalog } from "./catalog.js?v=37";
import { mountMap } from "./map.js?v=13";

const mainId = "main-content";
const status = document.getElementById("app-status");
const error = document.getElementById("app-error");
const feedback = document.getElementById("app-feedback");
const feedbackText = feedback.querySelector("[data-feedback-text]");
const feedbackLink = feedback.querySelector("[data-feedback-compare]");
const feedbackClose = feedback.querySelector("[data-feedback-close]");
const route = /^(?:\/(?:catalog|map|market|favorites|compare)\/|\/properties\/[0-9]+\/|\/)$/;
const searchForms = "#catalog-filters, #map-search-form, .market-form";
const comparisonFields = {
  "compare-pair": ["pair"],
  "compare-differences": ["differences"],
  "compare-budget-trigger": ["budget", "priority"],
};
let page;
let request;
let saving = false;
let queuedNavigation;
let queuedFilterForm;
let currentURL = location.href;
let feedbackTimer;
let lastFeedback;
const saveQueue = new Map();
const scrollPositions = new Map();
const comparisonOffsets = new Map();
const filterDestinations = new WeakMap();

history.scrollRestoration = "manual";

function localPage(url) {
  return url.origin === location.origin && route.test(url.pathname);
}

function comparisonGroup(url) {
  return JSON.stringify([url.searchParams.get("deal") ?? "sale", ...url.searchParams.getAll("property_id")]);
}

function sharedComparisonRequest(url, control) {
  const fields = comparisonFields[control];
  if (url.pathname !== "/compare/" || !fields || !url.searchParams.has("property_id")) return;
  const pending = request?.sharedComparison;
  if (pending && comparisonGroup(pending) === comparisonGroup(url)) {
    for (const field of ["pair", "differences", "budget", "priority"]) {
      if (fields.includes(field)) continue;
      const value = pending.searchParams.get(field);
      if (value === null) url.searchParams.delete(field);
      else url.searchParams.set(field, value);
    }
  }
  return url;
}

function mount(feedFrames) {
  page = new AbortController();
  const { signal } = page;
  mountCatalog(signal, feedFrames);
  mountMap(signal);
  const form = document.querySelector(searchForms);
  if (!form) return;
  form.dataset.liveFilters = "";
  let timeout;
  const schedule = () => {
    clearTimeout(timeout);
    if (signal.aborted || (!saving && request && !request.filter)) return;
    if (saving) {
      queuedFilterForm = form;
      return;
    }
    if (request?.filter) request.controller.abort();
    timeout = setTimeout(() => {
      if (!signal.aborted && saving) {
        queuedFilterForm = form;
        return;
      }
      if (!signal.aborted && !saving && (!request || request.filter) &&
          !form.querySelector("button[type='submit']:disabled")) {
        form.requestSubmit();
      }
    }, 450);
  };
  for (const event of ["input", "change"]) {
    form.addEventListener(event, ({ target }) => {
      if (target.name) schedule();
    });
  }
  form.addEventListener("filters:ready", schedule);
  signal.addEventListener("abort", () => clearTimeout(timeout), { once: true });
}

function showError(message) {
  const dialogError = document.querySelector("[data-compare-request-error]");
  const target = dialogError?.closest("dialog[open]") ? dialogError : error;
  for (const region of [error, dialogError]) {
    if (!region) continue;
    region.textContent = region === target ? message : "";
    region.hidden = !region.textContent;
  }
}

function clearFeedback() {
  clearTimeout(feedbackTimer);
  feedback.hidden = true;
  feedbackText.textContent = "";
}

feedbackClose.addEventListener("click", () => {
  document.getElementById(lastFeedback?.id)?.focus({ preventScroll: true });
  clearFeedback();
});
window.addEventListener("scroll", () => {
  if (!feedback.hidden && feedback.dataset.kind === "success") clearFeedback();
}, { passive: true, capture: true });

function showFeedback(action, message, kind = "success", compareLimit = false) {
  const previous = action.append && !feedback.hidden ? lastFeedback : null;
  clearTimeout(feedbackTimer);
  feedback.dataset.kind = kind === "error" || previous?.kind === "error" ? "error" : "success";
  feedbackText.textContent = previous && previous.message !== message
    ? `${previous.message} ${message}` : message;
  status.textContent = feedbackText.textContent;
  const deal = compareLimit ? action.deal : previous?.deal;
  feedbackLink.hidden = !deal;
  if (deal) feedbackLink.href = `/compare/?deal=${encodeURIComponent(deal)}`;
  feedbackClose.hidden = feedback.dataset.kind !== "error";
  feedback.hidden = false;
  lastFeedback = { message, kind, deal, id: action.id };
  if (feedback.dataset.kind === "success") feedbackTimer = setTimeout(clearFeedback, 4200);
}

function selectionMessage(action) {
  if (action.kind === "favorite") {
    return action.value === "save" ? "Добавлено в избранное." : "Убрано из избранного.";
  }
  return action.value === "save" ? "Добавлено в сравнение." : "Убрано из сравнения.";
}

function applySelection(next, destination, action) {
  if (!action.partial) return false;
  const current = new URL(currentURL);
  if (destination.pathname !== current.pathname || destination.search !== current.search) return false;

  const navItem = next.querySelector("[data-comparison-nav]");
  if (navItem) {
    document.querySelector("[data-comparison-nav]").replaceChildren(...navItem.childNodes);
  }
  const oldButton = document.getElementById(action.id);
  const newButton = next.getElementById(action.id);
  const notice = next.querySelector("[data-app-message]");
  if (oldButton && newButton?.matches("[data-selection-button]")) {
    const wasFocused = document.activeElement === oldButton;
    const button = document.importNode(newButton, true);
    oldButton.replaceWith(button);
    if (wasFocused) button.focus({ preventScroll: true });
  }
  if (notice || !newButton || newButton.value === action.value) {
    showFeedback(action, notice?.textContent.trim() || "Изменение не подтверждено.",
      "error", notice?.dataset.appMessage === "compare-limit");
  } else {
    showFeedback(action, selectionMessage(action));
  }
  return true;
}

async function swapStyles(next, signal) {
  const current = new Map(
    [...document.querySelectorAll("link[data-page-style]")].map((link) => [link.href, link]),
  );
  const added = [];
  const loads = [];
  for (const source of next.querySelectorAll("link[data-page-style]")) {
    const href = new URL(source.getAttribute("href"), location.origin).href;
    if (current.delete(href)) continue;
    const link = document.importNode(source, true);
    link.media = "not all";
    loads.push(new Promise((resolve) => {
      link.addEventListener("load", resolve, { once: true });
      link.addEventListener("error", resolve, { once: true });
      signal.addEventListener("abort", resolve, { once: true });
    }));
    added.push(link);
    document.head.append(link);
  }
  await Promise.all(loads);
  if (signal.aborted) {
    for (const link of added) link.remove();
    return;
  }
  for (const link of added) link.removeAttribute("media");
  for (const link of current.values()) link.remove();
}

function filterState() {
  const active = document.activeElement;
  const search = active.closest(".location-choice-panel:not([hidden])")?.querySelector("input");
  return {
    focus: active.id,
    range: active instanceof HTMLInputElement ? [active.selectionStart, active.selectionEnd] : null,
    panel: document.querySelector(".filter-panel")?.open,
    advanced: [...document.querySelectorAll(".advanced-filters")].map((section) => section.open),
    locationSearch: search ? { id: search.id, query: search.value } : null,
  };
}

function restoreFocus(id, range) {
  const target = document.getElementById(id);
  if (!target) return;
  target.focus({ preventScroll: true });
  if (range?.[0] !== null && range?.[0] !== undefined && target instanceof HTMLInputElement) {
    target.setSelectionRange(...range);
  }
}

function adjacentSelectionButton(button) {
  const group = button?.closest(".result-grid, [data-comparison-scroll]");
  if (!group) return;
  const actionURL = button.form.getAttribute("action");
  const actions = [...group.querySelectorAll("[data-selection-button][value='remove']")]
    .filter((action) => action.form.getAttribute("action") === actionURL);
  const index = actions.indexOf(button);
  return (actions[index + 1] ?? actions[index - 1])?.id;
}

function restoreComparisonOffset(offset) {
  const table = document.querySelector("[data-comparison-scroll]");
  if (table && offset !== undefined) table.scrollLeft = offset;
}

function viewPosition() {
  return {
    y: scrollY,
    tableOffset: document.querySelector("[data-comparison-scroll]")?.scrollLeft,
    feedFrames: new Map([...document.querySelectorAll("[data-gallery-key]")]
      .map((gallery) => [gallery.dataset.galleryKey, Number(gallery.dataset.galleryIndex ?? 0)])),
  };
}

function applyPage(next, url, options, position) {
  const content = next.getElementById(mainId);
  const nav = next.querySelector(".site-nav");
  if (!content || !nav) return false;
  const editing = (options.filter && !options.switchView) || options.preserveView ? filterState() : null;
  const { tableOffset } = position;
  const previousURL = new URL(currentURL);
  if (tableOffset !== undefined && !previousURL.searchParams.has("pair")) {
    comparisonOffsets.set(comparisonGroup(previousURL), tableOffset);
  }
  const completingPair = options.selection?.kind === "comparison" &&
    document.querySelector(".comparison-single") && content.querySelector("[data-comparison-scroll]");
  const adjacentFocus = options.selection?.value === "remove" &&
    adjacentSelectionButton(document.getElementById(options.focus));
  scrollPositions.set(currentURL, position);
  const backLink = content.querySelector("[data-restore-view]");
  if (backLink) {
    const backURL = new URL(backLink.getAttribute("href"), url);
    if (localPage(backURL) && backURL.pathname === previousURL.pathname) {
      scrollPositions.set(backURL.href, position);
    }
  }
  const restored = options.pop || options.restoreView ? scrollPositions.get(url.href) : null;
  page.abort();
  document.getElementById(mainId).replaceWith(document.importNode(content, true));
  document.querySelector(".site-nav").replaceChildren(...nav.childNodes);
  document.title = next.title;
  if (!options.pop && url.href !== currentURL) history.pushState(null, "", url);
  currentURL = url.href;
  mount(restored?.feedFrames ?? (previousURL.pathname === url.pathname ? position.feedFrames : undefined));
  if (editing) {
    const panel = document.querySelector(".filter-panel");
    const advanced = document.querySelectorAll(".advanced-filters");
    if (panel && editing.panel !== undefined && !panel.hasAttribute("data-filter-errors")) {
      panel.open = editing.panel;
    }
    advanced.forEach((section, index) => {
      if (editing.advanced[index] !== undefined && !section.querySelector(".errorlist")) {
        section.open = editing.advanced[index];
      }
    });
    if (editing.locationSearch) {
      document.getElementById(editing.locationSearch.id)?.dispatchEvent(
        new CustomEvent("location:restore-search", { detail: editing.locationSearch.query }),
      );
    }
    const switchingDeal = options.preserveView?.startsWith("compare-deal-");
    const budgetUpdate = options.preserveView === "compare-budget-trigger";
    const range = budgetUpdate ? null : editing.range;
    const focus = range?.[0] != null ? editing.focus : options.preserveView || editing.focus;
    if (!document.querySelector("#compare-budget-dialog[open]")) restoreFocus(focus, range);
    if (options.preserveView && !switchingDeal && tableOffset !== undefined) {
      const pairChanged = previousURL.searchParams.get("pair") !== url.searchParams.get("pair");
      const offset = pairChanged
        ? (url.searchParams.has("pair") ? 0 : (comparisonOffsets.get(comparisonGroup(url)) ?? 0))
        : tableOffset;
      restoreComparisonOffset(offset);
    }
    scrollTo(0, position.y);
    status.textContent = document.querySelector("#results-heading, #map-results-title, #market-median")?.textContent
      ?? document.querySelector("main h2")?.textContent ?? "";
  } else if (options.saving) {
    const table = completingPair && document.querySelector("[data-comparison-scroll]");
    const currentButton = document.getElementById(options.focus);
    const focus = adjacentFocus && !currentButton?.matches("[value='remove']")
      ? adjacentFocus
      : options.focus;
    restoreComparisonOffset(tableOffset);
    if (table) table.focus({ preventScroll: true });
    else if (focus && document.getElementById(focus)) restoreFocus(focus);
    else (document.querySelector(".favorites-empty a") ??
      document.getElementById(mainId)).focus({ preventScroll: true });
    const tableAboveViewport = table && table.getBoundingClientRect().top + scrollY < position.y;
    scrollTo(0, tableAboveViewport ? 0 : position.y);
    status.textContent = document.activeElement.matches("[data-selection-button]")
      ? document.activeElement.title || document.activeElement.textContent.trim()
      : "";
  } else {
    document.querySelector("main").focus({ preventScroll: true });
    restoreComparisonOffset(restored?.tableOffset);
    scrollTo(0, restored?.y ?? 0);
  }
  if (!options.filter && !options.saving && url.hash) {
    const anchor = document.getElementById(url.hash.slice(1));
    anchor?.focus({ preventScroll: true });
    anchor?.scrollIntoView();
  }
  return true;
}

async function openPage(url, options = {}) {
  if (saving) {
    if (options.saving && options.preserveView?.startsWith("compare-")) {
      saveQueue.set(Symbol(), { url, options });
    } else if (!options.saving && !options.filter) {
      queuedNavigation = { url, options };
    }
    return;
  }
  const sharedComparison = sharedComparisonRequest(url, options.preserveView);
  request?.controller.abort();
  window.dispatchEvent(new CustomEvent("catalog:navigation-start", {
    detail: { filtering: Boolean(options.filter && !options.switchView) },
  }));
  const controller = new AbortController();
  const action = options.selection;
  if (!action) clearFeedback();
  request = { controller, filter: options.filter, selectionId: action?.id, sharedComparison };
  saving = Boolean(options.saving);
  const target = document.getElementById(action?.id || mainId) ?? document.getElementById(mainId);
  target.setAttribute("aria-busy", "true");
  status.textContent = "";
  showError("");
  try {
    const response = await fetch(url, {
      method: options.saving ? "POST" : "GET",
      body: options.body,
      signal: controller.signal,
    });
    const destination = new URL(response.url);
    if (url.hash && !destination.hash) destination.hash = url.hash;
    const next = new DOMParser().parseFromString(await response.text(), "text/html");
    if (controller.signal.aborted || (options.saving && queuedNavigation?.options.pop)) return;
    if (action && (!response.ok || !localPage(destination))) {
      showFeedback(action, "Изменение не подтверждено. Обновите страницу перед повтором.", "error");
      return;
    }
    if (!response.ok && response.status !== 400 &&
        (options.saving || options.filter || sharedComparison)) {
      showError(options.saving
        ? "Не удалось подтвердить сохранение. Попробуйте ещё раз."
        : "Не удалось обновить данные. Попробуйте ещё раз.");
      return;
    }
    if (localPage(destination)) {
      if (action && applySelection(next, destination, action)) return;
      const position = viewPosition();
      if (next.getElementById(mainId)) await swapStyles(next, controller.signal);
      if (controller.signal.aborted) return;
      if (applyPage(next, destination, options, position)) {
        if (action && !document.querySelector("[data-app-message]")) {
          showFeedback(action, selectionMessage(action));
        }
        return;
      }
    }
    if (options.saving) {
      showError("Не удалось обновить страницу. Проверьте, сохранилось ли изменение.");
    } else {
      location.assign(destination);
    }
  } catch {
    if (!controller.signal.aborted) {
      if (options.pop) location.reload();
      else if (action) showFeedback(action, "Не удалось получить ответ. Обновите страницу перед повтором.", "error");
      else showError("Не удалось загрузить данные. Попробуйте ещё раз.");
    }
  } finally {
    if (request?.controller === controller) {
      target.removeAttribute("aria-busy");
      request = null;
      saving = false;
      window.dispatchEvent(new Event("catalog:navigation-end"));
      const entry = saveQueue.entries().next().value;
      if (entry) {
        const [key, queued] = entry;
        saveQueue.delete(key);
        openPage(queued.url, queued.options);
      } else {
        const navigation = queuedNavigation;
        const filter = queuedFilterForm;
        queuedNavigation = null;
        queuedFilterForm = null;
        if (navigation) openPage(navigation.url, navigation.options);
        else if (filter?.isConnected) filter.dispatchEvent(new Event("filters:ready"));
      }
    }
  }
}

function submitSelection(url, body, button) {
  const { id, value } = button;
  if (request?.selectionId === id || saveQueue.has(id)) return;
  const kind = url.pathname === "/favorites/" ? "favorite" : "comparison";
  const path = new URL(currentURL).pathname;
  const partial = path === "/catalog/" || path.startsWith("/properties/") ||
    (path === "/favorites/" && kind === "comparison");
  const selection = { id, value, kind, partial, append: saving, deal: body.get("deal") };
  const options = { saving: true, body, focus: id, selection };
  if (saving) {
    button.setAttribute("aria-busy", "true");
    saveQueue.set(id, { url, options });
  } else {
    openPage(url, options);
  }
}

document.addEventListener("click", (event) => {
  const link = event.target.closest("a[href]");
  if (!link || event.defaultPrevented || event.button || event.metaKey || event.ctrlKey ||
      event.shiftKey || event.altKey || link.download || (link.target && link.target !== "_self")) return;
  const url = new URL(link.href);
  if (!localPage(url) || (url.hash && url.pathname === location.pathname && url.search === location.search)) return;
  event.preventDefault();
  const { preserveView } = link.dataset;
  if (link.hasAttribute("data-filter-navigation") || preserveView === "catalog-sort") {
    const form = document.querySelector("form[data-live-filters]");
    if (form) {
      filterDestinations.set(form, { url, preserveView });
      if (saving) queuedFilterForm = form;
      else if (!form.querySelector("button[type='submit']:disabled")) form.requestSubmit();
      return;
    }
  }
  openPage(url, { preserveView, restoreView: link.hasAttribute("data-restore-view") });
});

document.addEventListener("submit", (event) => {
  const form = event.target;
  const destination = filterDestinations.get(form);
  filterDestinations.delete(form);
  if (event.defaultPrevented || !["get", "post"].includes(form.method) ||
      (form.target && form.target !== "_self")) return;
  const url = destination?.url ?? new URL(form.getAttribute("action") ?? location.href, location.href);
  if (!localPage(url)) return;
  const data = new FormData(form, event.submitter);
  const settingsFocus = form.id === "compare-settings" ? "compare-budget-trigger" :
    event.submitter?.dataset.preserveView;
  event.preventDefault();
  if (form.method === "post") {
    if (event.submitter?.matches("[data-selection-button]")) {
      submitSelection(url, data, event.submitter);
    } else {
      openPage(url, {
        saving: true,
        body: data,
        focus: event.submitter?.id ?? "",
        preserveView: settingsFocus,
      });
    }
    return;
  }
  const query = new URLSearchParams(data);
  for (const [key, value] of [...query]) {
    if (!value) query.delete(key);
  }
  if (destination?.preserveView === "catalog-sort") {
    query.delete("sort");
    if (url.searchParams.has("sort")) query.set("sort", url.searchParams.get("sort"));
  } else if (destination) {
    query.delete("bbox");
  }
  url.search = query;
  openPage(url, {
    filter: form.matches(searchForms),
    switchView: Boolean(destination && !destination.preserveView),
    preserveView: destination?.preserveView ?? settingsFocus,
  });
});

window.addEventListener("popstate", () => {
  const url = new URL(location.href);
  const previous = new URL(currentURL);
  if (url.pathname === previous.pathname && url.search === previous.search) {
    if (saving) queuedNavigation = null;
    currentURL = url.href;
    return;
  }
  openPage(url, { pop: true });
});

window.addEventListener("catalog:map-area", () => { currentURL = location.href; });

const navigation = document.querySelector(".site-nav");
new ResizeObserver(() => {
  const height = Math.ceil(navigation.getBoundingClientRect().height);
  document.documentElement.style.setProperty("--mobile-nav-measured", `${height}px`);
}).observe(navigation);

mount();
