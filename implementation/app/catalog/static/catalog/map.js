import { mountPreviews } from "./catalog.js?v=17";

let leaflet;

function loadLeaflet() {
  if (globalThis.L) return Promise.resolve(globalThis.L);
  leaflet ??= new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js";
    script.integrity = "sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=";
    script.crossOrigin = "anonymous";
    script.addEventListener("load", () => resolve(globalThis.L), { once: true });
    script.addEventListener("error", () => {
      script.remove();
      leaflet = null;
      reject(new Error("Leaflet is unavailable"));
    }, { once: true });
    document.head.append(script);
  });
  return leaflet;
}

function boundsQuery(view) {
  const wrap = (lon) => ((lon + 180) % 360 + 360) % 360 - 180;
  const world = view.getEast() - view.getWest() >= 360;
  return [
    Math.max(-90, view.getSouth()),
    world ? -180 : wrap(view.getWest()),
    Math.min(90, view.getNorth()),
    world ? 180 : wrap(view.getEast()),
  ].map((value) => value.toFixed(6)).join(",");
}

export async function mountMap(signal) {
  const stateNode = document.querySelector("#map-state");
  if (!stateNode) return;
  const state = JSON.parse(stateNode.textContent);
  const { bounds, scope, unit } = state;
  let { points } = state;
  const status = document.querySelector("#map-status");
  const initialURL = location.href;
  const mapError = document.querySelector("#map-error");
  const form = document.querySelector("#map-search-form");
  const region = form.elements.namedItem("region_id");
  const mountedRegion = region.value;
  const reset = document.querySelector("#map-reset-area");
  let L;
  try {
    L = await loadLeaflet();
  } catch {
    if (!signal.aborted) status.textContent = "Не удалось загрузить карту. Список объектов доступен.";
    return;
  }
  if (signal.aborted || location.href !== initialURL) return;
  const map = L.map("property-map", {
    scrollWheelZoom: false,
    zoomControl: false,
    trackResize: false,
    worldCopyJump: true,
  });
  map.attributionControl.setPrefix('<a href="https://leafletjs.com/">Leaflet</a>');
  const target = map.getContainer();
  let pinchDelta = 0;
  let lastPinch = 0;
  target.addEventListener("wheel", (event) => {
    if (!event.ctrlKey) return;
    event.preventDefault();
    if (event.timeStamp - lastPinch > 180) pinchDelta = 0;
    lastPinch = event.timeStamp;
    pinchDelta += event.deltaY;
    if (Math.abs(pinchDelta) < 50) return;
    map.setZoomAround(map.mouseEventToContainerPoint(event), map.getZoom() - Math.sign(pinchDelta));
    pinchDelta = 0;
  }, { passive: false });
  if (!navigator.maxTouchPoints) {
    // Safari reports trackpad pinch as a gesture rather than a wheel event.
    let gestureZoom = map.getZoom();
    target.addEventListener("gesturestart", (event) => {
      event.preventDefault();
      gestureZoom = map.getZoom();
    }, { passive: false });
    target.addEventListener("gesturechange", (event) => {
      event.preventDefault();
      const anchor = Number.isFinite(event.clientX) && Number.isFinite(event.clientY)
        ? map.mouseEventToContainerPoint(event)
        : map.getSize().divideBy(2);
      map.setZoomAround(anchor, gestureZoom + Math.round(Math.log2(event.scale)));
    }, { passive: false });
    target.addEventListener("gestureend", (event) => event.preventDefault(), { passive: false });
  }
  L.control.zoom({
    zoomInTitle: "Приблизить",
    zoomOutTitle: "Отдалить",
  }).addTo(map);
  const layer = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    referrerPolicy: "strict-origin-when-cross-origin",
    attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  });
  layer.on("loading", () => { mapError.textContent = ""; });
  layer.on("tileerror", () => {
    mapError.textContent = "Подложка карты частично недоступна. Метки и список работают.";
  });
  layer.addTo(map);

  const markers = L.layerGroup().addTo(map);
  const number = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 });
  const price = (item) => item.amount === null
    ? "Цена неизвестна"
    : `${item.lower_bound ? "от " : ""}${number.format(Number(item.amount))} ${unit}`;

  function drawMarkers() {
    const groups = new Map();
    const cellSize = 42;
    const zoom = map.getZoom();
    for (const point of points) {
      const pixel = map.project([point.lat, point.lon], zoom);
      const key = `${Math.floor(pixel.x / cellSize)},${Math.floor(pixel.y / cellSize)}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(point);
    }
    markers.clearLayers();
    for (const group of groups.values()) {
      const first = group[0];
      const title = group.length === 1 ? `${first.title}, ${first.address}` : `Объектов рядом: ${group.length}`;
      const markerText = document.createElement("span");
      markerText.textContent = group.length === 1 ? "•" : String(group.length);
      const icon = L.divIcon({ className: "listing-pin", html: markerText, iconSize: [30, 30] });
      const marker = L.marker([first.lat, first.lon], { icon, title }).addTo(markers);
      marker.getElement().setAttribute("aria-label", title);
      marker.bindPopup(() => {
        const popup = document.createElement("ul");
        popup.className = "map-popup";
        for (const item of group) {
          const line = document.createElement("li");
          const link = document.createElement("a");
          link.href = item.url;
          link.textContent = item.title;
          const details = document.createElement("p");
          const area = typeof item.area === "string" ? `${number.format(Number(item.area))} м² · ` : "";
          details.textContent = `${area}${price(item)} · ${item.address}`;
          line.append(link, details);
          popup.append(line);
        }
        return popup;
      });
    }
  }

  let timer;
  let pending;
  let lastBounds = new URLSearchParams(location.search).get("bbox");
  let popupPan = false;
  let fitted = false;
  let resizing = false;
  let navigating = false;
  let applyingFilters = false;
  const cancel = () => {
    clearTimeout(timer);
    pending?.abort();
  };
  signal.addEventListener("abort", () => {
    cancel();
    resize.disconnect();
    map.remove();
  }, { once: true });
  window.addEventListener("catalog:navigation-start", ({ detail }) => {
    navigating = true;
    applyingFilters = detail.filtering;
    cancel();
  }, { signal });
  window.addEventListener("catalog:navigation-end", () => {
    navigating = false;
    applyingFilters = false;
  }, { signal });

  async function updateArea(nextBounds) {
    if (navigating || form.dataset.draft === "true" || nextBounds === lastBounds) return;
    const controller = new AbortController();
    pending = controller;
    const query = new URLSearchParams(scope);
    query.set("bbox", nextBounds);
    const startURL = location.href;
    const url = new URL(location.pathname, location.origin);
    url.search = query;
    status.textContent = "";
    try {
      const response = await fetch(url, { signal: controller.signal });
      if (!response.ok) throw new Error("Area request failed");
      const next = new DOMParser().parseFromString(await response.text(), "text/html");
      if (signal.aborted || controller.signal.aborted || navigating || location.href !== startURL ||
          form.dataset.draft === "true") return;
      const nextState = next.querySelector("#map-state");
      const results = next.querySelector(".map-results");
      if (!nextState || !results) throw new Error("Map results were absent");
      points = JSON.parse(nextState.textContent).points;
      stateNode.textContent = nextState.textContent;
      drawMarkers();
      const section = document.importNode(results, true);
      const previousSection = document.querySelector(".map-results");
      const active = document.activeElement;
      const listOffset = previousSection.querySelector(".map-results-list")?.scrollTop;
      const restoreFocus = previousSection.contains(active);
      previousSection.replaceWith(section);
      mountPreviews(section);
      const list = section.querySelector(".map-results-list");
      if (list && listOffset !== undefined) list.scrollTop = listOffset;
      if (restoreFocus) {
        const result = [...section.querySelectorAll(".map-result-link")]
          .find((link) => link.pathname === active.pathname);
        const target = result ?? section.querySelector("#map-results-title");
        target.focus({ preventScroll: true });
        target.scrollIntoView({ block: "nearest", inline: "nearest" });
      }
      for (const selector of [".search-toolbar", ".view-switch"]) {
        const oldBlock = document.querySelector(selector);
        const newBlock = next.querySelector(selector);
        if (oldBlock && newBlock) oldBlock.replaceWith(document.importNode(newBlock, true));
      }
      form.elements.namedItem("bbox").value = nextBounds;
      reset.hidden = false;
      lastBounds = nextBounds;
      history.replaceState(history.state, "", url);
      window.dispatchEvent(new Event("catalog:map-area"));
      const count = section.querySelector(".map-results-count")?.textContent;
      status.textContent = count ? `Объектов на карте: ${count}` : section.querySelector("h2").textContent;
      status.classList.add("sr-only");
    } catch {
      if (!controller.signal.aborted && !signal.aborted) {
        status.classList.remove("sr-only");
        status.textContent = "Не удалось обновить результаты. Переместите карту, чтобы повторить.";
      }
    }
  }

  const resize = new ResizeObserver(() => {
    if (!target.clientHeight || !target.clientWidth) return;
    resizing = true;
    if (fitted) {
      map.invalidateSize({ pan: false });
    } else {
      if (bounds) {
        const [south, west, north, east] = bounds;
        const region = [[south, west], [north, east < west ? east + 360 : east]];
        map.fitBounds(region, { animate: false });
      } else {
        map.fitBounds(points.map(({ lat, lon }) => [lat, lon]), { maxZoom: 15, padding: [24, 24] });
      }
      fitted = true;
      drawMarkers();
    }
    resizing = false;
  });
  resize.observe(target);
  map.on("zoomend", () => { if (fitted) drawMarkers(); });
  map.on("autopanstart", () => { popupPan = true; });
  map.on("movestart", () => { if (!resizing) cancel(); });
  map.on("moveend", () => {
    if (resizing || (navigating && !applyingFilters)) {
      popupPan = false;
      return;
    }
    if (popupPan) {
      popupPan = false;
      return;
    }
    cancel();
    if (region.value !== mountedRegion) return;
    const nextBounds = boundsQuery(map.getBounds());
    form.elements.namedItem("bbox").value = nextBounds;
    if (applyingFilters) {
      form.dispatchEvent(new Event("filters:ready"));
    } else if (form.dataset.draft !== "true") {
      timer = setTimeout(() => updateArea(nextBounds), 250);
    }
  });
}
