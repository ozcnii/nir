export function mountPreviews(root) {
  for (const image of root.querySelectorAll("[data-preview]")) {
    const slot = image.closest(".result-photo");
    if (slot && !image.complete) {
      slot.classList.add("is-pending");
      image.addEventListener("load", () => slot.classList.remove("is-pending"), { once: true });
    }
    const showFallback = () => {
      slot?.classList.remove("is-pending");
      image.hidden = true;
      image.nextElementSibling.hidden = false;
    };
    image.addEventListener("error", showFallback, { once: true });
    if (image.hasAttribute("src") && image.complete && image.naturalWidth === 0) {
      showFallback();
    }
  }
}

function restrictNumberInput(input) {
  const syntax = {
    decimal: /^(?:\d+(?:[.,]\d{0,2})?)?$/,
    integer: /^\d*$/,
    signed: /^-?\d*$/,
  }[input.dataset.numberKind];
  let accepted = syntax.test(input.value) ? input.value : "";
  input.addEventListener("beforeinput", (event) => {
    if (syntax.test(input.value)) accepted = input.value;
    if (!event.isComposing && event.data && /[^\d\s.,-]/.test(event.data)) event.preventDefault();
  });
  input.addEventListener("input", (event) => {
    if (event.isComposing) return;
    const next = input.value.replace(/\s/g, "");
    if (syntax.test(next)) accepted = next;
    else event.stopPropagation();
    if (input.value !== accepted) input.value = accepted;
  });
  input.addEventListener("blur", () => {
    if (/^[.,-]$/.test(input.value) || /[.,]$/.test(input.value)) {
      input.value = input.value.slice(0, -1);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }
  });
}

function validRange(group) {
  const limits = [...group.querySelectorAll("[data-number-kind]")];
  const problem = group.querySelector("[data-range-problem]");
  const values = limits.map((input) =>
    input.value ? Number(input.value.replace(",", ".")) : null);
  const incomplete = limits.some((input, index) =>
    Number.isNaN(values[index]) || /[.,]$/.test(input.value));
  const outOfRange = limits.some((input, index) =>
    values[index] !== null &&
    (values[index] < Number(input.dataset.numberMin) || values[index] > Number(input.dataset.numberMax)));
  const inverted = values.every((value) => value !== null) && values[0] > values[1];
  problem.textContent = outOfRange ? "Число вне допустимого диапазона."
    : inverted ? "«От» должно быть меньше или равно «До»." : "";
  problem.hidden = !problem.textContent;
  return !incomplete && !outOfRange && !inverted;
}


export function mountCatalog(signal, feedFrames) {
  const filters = document.querySelector("#catalog-filters, #map-search-form");
  const budget = document.querySelector("#compare-budget");
  if (budget) restrictNumberInput(budget);
  const budgetDialog = document.getElementById("compare-budget-dialog");
  if (budgetDialog?.showModal) {
    const summary = document.getElementById("compare-budget-trigger");
    const disclosure = summary.parentElement;
    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.id = summary.id;
    trigger.className = summary.className;
    trigger.append(...summary.childNodes);
    trigger.setAttribute("aria-label", summary.getAttribute("aria-label"));
    budgetDialog.append(disclosure.querySelector(".decision-body"));
    disclosure.replaceWith(trigger);
    trigger.setAttribute("aria-haspopup", "dialog");
    trigger.setAttribute("aria-controls", budgetDialog.id);
    trigger.addEventListener("click", () => budgetDialog.showModal());
    if (budget.hasAttribute("aria-invalid")) {
      trigger.focus({ preventScroll: true });
      budgetDialog.showModal();
      budget.focus({ preventScroll: true });
    }
  }
  for (const chart of document.querySelectorAll("[data-price-chart]")) {
    const markers = [...chart.querySelectorAll("[data-price-marker]")];
    const value = chart.querySelector("[data-price-value]");
    if (!markers.length || !value) continue;
    let current;
    const showPrice = (marker) => {
      current?.setAttribute("aria-pressed", "false");
      current = marker;
      marker.setAttribute("aria-pressed", "true");
      value.textContent = marker.querySelector("title").textContent.trim();
    };
    for (const marker of markers) {
      marker.setAttribute("role", "button");
      marker.setAttribute("tabindex", "0");
      marker.setAttribute("aria-pressed", "false");
      marker.addEventListener("click", () => showPrice(marker));
      marker.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        showPrice(marker);
      });
    }
    showPrice(markers.at(-1));
    value.hidden = false;
  }
  const priceDeal = document.querySelector("[data-price-deal]");
  if (priceDeal) {
    const charts = document.querySelectorAll("[data-price-chart]");
    const showChart = () => {
      for (const chart of charts) chart.hidden = chart.dataset.priceChart !== priceDeal.value;
    };
    for (const label of document.querySelectorAll("[data-price-deal-label]")) label.hidden = true;
    priceDeal.addEventListener("change", showChart);
    showChart();
    priceDeal.hidden = false;
  }
  const region = document.querySelector("select[name='region_id']");

  const filterPanel = filters?.closest(".filter-panel");
  if (filterPanel?.matches("[data-filter-panel]:not([data-filter-errors])")) {
    const narrow = matchMedia("(max-width: 55rem)");
    const layoutFilters = () => { filterPanel.open = !narrow.matches; };
    layoutFilters();
    narrow.addEventListener("change", layoutFilters, { signal });
  }
  const additional = document.getElementById("catalog-advanced");
  const moreButton = filters?.querySelector(".more-filters-toggle");
  if (additional && moreButton) {
    const syncAdditional = () => moreButton.setAttribute("aria-expanded", String(additional.open));
    moreButton.hidden = false;
    additional.querySelector("summary").hidden = true;
    moreButton.addEventListener("click", () => { additional.open = !additional.open; });
    additional.addEventListener("toggle", syncAdditional);
    syncAdditional();
  }

  function watchFilterEdits(form, update) {
    const values = () => new URLSearchParams(
      Array.from(form.elements)
        .filter((field) => field.name && field.name !== "bbox")
        .map((field) => [field.name, field.value]),
    ).toString();
    const applied = values();
    const refresh = () => update(values() !== applied);
    form.addEventListener("input", refresh);
    form.addEventListener("change", refresh);
    window.addEventListener("pageshow", refresh, { signal });
    refresh();
    return refresh;
  }

  const marketForm = document.querySelector(".market-form");
  for (const form of [filters, marketForm]) {
    if (!form) continue;
    const deal = form.elements.namedItem("deal_type");
    const category = form.elements.namedItem("category");
    const saleOptions = [...category.querySelectorAll("[data-sale-only]")];
    deal.addEventListener("change", () => {
      const rental = deal.value !== "sale";
      if (rental && saleOptions.some((option) => option.selected)) {
        category.value = "flat";
        category.dispatchEvent(new Event("change", { bubbles: true }));
      }
      for (const option of saleOptions) {
        option.hidden = rental;
        option.disabled = rental;
      }
    });
  }
  if (filters) {
    watchFilterEdits(filters, (dirty) => { filters.dataset.draft = String(dirty); });
    const groups = [...filters.querySelectorAll("fieldset:has([data-range-problem])")];
    for (const group of groups) {
      for (const input of group.querySelectorAll("[data-number-kind]")) restrictNumberInput(input);
      group.addEventListener("input", () => validRange(group));
    }
    filters.addEventListener("submit", (event) => {
      if (!groups.map(validRange).every(Boolean)) event.preventDefault();
    });
  }

  const menus = [...document.querySelectorAll(".sort-menu")];
  if (menus.length) {
    document.addEventListener("click", ({ target }) => {
      for (const menu of menus) {
        if (!menu.contains(target)) menu.open = false;
      }
    }, { signal });
  }
  for (const menu of menus) {
    menu.addEventListener("focusout", ({ relatedTarget }) => {
      if (relatedTarget && !menu.contains(relatedTarget)) menu.open = false;
    });
    menu.addEventListener("keydown", (event) => {
      if (event.key !== "Escape" || !menu.open) return;
      event.preventDefault();
      menu.open = false;
      menu.querySelector("summary").focus();
    });
  }
  for (const link of document.querySelectorAll("[data-edit-filters]")) {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      filterPanel.open = true;
      const field = filters.querySelector("select[name='category']");
      const target = document.getElementById(`${field.id}-choice`) ?? field;
      target.focus({ preventScroll: true });
      target.scrollIntoView({ block: "nearest" });
    });
  }

  function searchableLocation(select, searchLabel, minimumOptions) {
    const template = document.querySelector("#location-choice-template");
    const choice = template.content.firstElementChild.cloneNode(true);
    const toggle = choice.querySelector("button");
    const value = toggle.querySelector("span");
    const panel = choice.querySelector(".location-choice-panel");
    const search = panel.querySelector("input");
    const options = panel.querySelector("[role='group']");
    const status = panel.querySelector("[role='status']");
    const label = select.labels[0];
    const normalize = (text) => text.toLocaleLowerCase("ru").replaceAll("ё", "е");
    toggle.id = `${select.id}-choice`;
    value.id = `${select.id}-value`;
    label.id ||= `${select.id}-label`;
    panel.id = `${select.id}-options`;
    search.id = `${select.id}-search`;
    toggle.setAttribute("aria-controls", panel.id);
    toggle.setAttribute("aria-labelledby", `${label.id} ${value.id}`);
    search.setAttribute("aria-label", searchLabel);
    search.placeholder = searchLabel;
    options.setAttribute("aria-labelledby", label.id);
    select.after(choice);

    function close(restoreFocus = false) {
      panel.hidden = true;
      toggle.setAttribute("aria-expanded", "false");
      if (restoreFocus) toggle.focus();
    }

    function renderOptions() {
      const terms = normalize(search.value).trim().split(/\s+/).filter(Boolean);
      const matches = Array.from(select.options).filter((option) =>
        !option.disabled && terms.every((term) => normalize(option.text).includes(term)));
      options.replaceChildren(...matches.map((option) => {
        const button = document.createElement("button");
        button.type = "button";
        button.id = `${select.id}-option-${option.index}`;
        button.tabIndex = -1;
        button.textContent = option.text;
        button.setAttribute("aria-pressed", String(option.selected));
        button.addEventListener("click", () => {
          if (select.value !== option.value) {
            select.value = option.value;
            select.dispatchEvent(new Event("change", { bubbles: true }));
          }
          close(true);
        });
        return button;
      }));
      options.scrollTop = 0;
      status.textContent = matches.length ? "" : "Ничего не найдено";
    }

    function sync() {
      const useSearch = !select.disabled && select.options.length >= minimumOptions;
      if (!useSearch) close();
      select.hidden = useSearch;
      choice.hidden = !useSearch;
      label.htmlFor = useSearch ? toggle.id : select.id;
      value.textContent = select.selectedOptions[0]?.text ?? "";
      if (!panel.hidden) renderOptions();
    }

    function open(query) {
      if (choice.hidden) return;
      toggle.setAttribute("aria-expanded", "true");
      panel.hidden = false;
      search.value = query;
      renderOptions();
    }
    toggle.addEventListener("click", () => {
      if (!panel.hidden) return close();
      open("");
      search.focus();
    });
    search.addEventListener("location:restore-search", ({ detail }) => open(detail));
    search.addEventListener("input", renderOptions);
    choice.addEventListener("keydown", (event) => {
      if (panel.hidden) return;
      if (event.key === "Escape") {
        event.preventDefault();
        close(true);
      } else if (event.key === "Enter" && event.target === search) {
        event.preventDefault();
        if (options.childElementCount === 1) options.firstElementChild.click();
      } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        const next = event.target === search
          ? (event.key === "ArrowDown" ? options.firstElementChild : options.lastElementChild)
          : (event.key === "ArrowDown" ? event.target.nextElementSibling : event.target.previousElementSibling ?? search);
        if (next === search || next?.parentElement === options) next.focus();
      }
    });
    choice.addEventListener("focusout", ({ relatedTarget }) => {
      if (relatedTarget && !choice.contains(relatedTarget)) close();
    });
    document.addEventListener("pointerdown", ({ target }) => {
      if (!choice.contains(target)) close();
    }, { signal });
    select.addEventListener("change", sync);
    const observer = new MutationObserver(sync);
    observer.observe(select, { attributeFilter: ["disabled"], childList: true });
    signal.addEventListener("abort", () => observer.disconnect(), { once: true });
    sync();
  }

  let locationRequest;
  signal.addEventListener("abort", () => locationRequest?.abort(), { once: true });
  async function updateLocationOptions({ reset = false } = {}) {
    locationRequest?.abort();
    const request = new AbortController();
    locationRequest = request;
    const options = [
      ["district", "Любой район"],
      ["metro", "Любая станция"],
    ];
    const status = filters.querySelector("[data-location-status]");
    const submit = filters.querySelector("button[type='submit']");
    const previous = Object.fromEntries(options.map(([name]) => [name, reset ? "" : filters.elements.namedItem(name).value]));
    submit.disabled = true;
    for (const [name, label] of options) {
      const select = filters.elements.namedItem(name);
      if (reset) select.replaceChildren(new Option(label, ""));
      select.disabled = true;
    }
    status.dataset.kind = "progress";
    status.textContent = "Обновляем список районов и станций…";
    const params = new URLSearchParams({
      deal_type: filters.elements.namedItem("deal_type").value,
      region_id: region.value,
    });
    try {
      const response = await fetch(`${filters.dataset.locationOptions}?${params}`, { signal: request.signal });
      if (!response.ok) throw new Error();
      const locations = await response.json();
      if (request.signal.aborted) return;
      for (const [name, label] of options) {
        const select = filters.elements.namedItem(name);
        const names = locations[name];
        select.replaceChildren(new Option(names.length ? label : "Нет данных", ""));
        select.append(...names.map((value) => new Option(value, value)));
        select.value = names.includes(previous[name]) ? previous[name] : "";
        select.disabled = !names.length;
      }
      status.textContent = "";
    } catch {
      if (request.signal.aborted) return;
      for (const [name] of options) {
        filters.elements.namedItem(name).disabled = false;
      }
      status.dataset.kind = "error";
      status.textContent = "Районы и станции временно недоступны.";
    } finally {
      if (locationRequest === request) {
        locationRequest = null;
        submit.disabled = false;
        filters.dispatchEvent(new Event("filters:ready"));
      }
    }
  }

  function updateRegion() {
    const bbox = filters.elements.namedItem("bbox");
    if (bbox) bbox.value = "";
    updateLocationOptions({ reset: true });
  }

  if (region) searchableLocation(region, "Найти регион", 2);
  if (filters) {
    const deal = filters.elements.namedItem("deal_type");
    const category = filters.elements.namedItem("category");
    const roomContext = (value) => ["room", "house"].includes(value) ? value : "flat";
    let previousRoomContext = roomContext(category.value);
    const roomMin = filters.elements.namedItem("rooms_min");
    const roomMax = filters.elements.namedItem("rooms_max");
    const roomPresets = filters.querySelector("[data-room-presets]");
    const roomChoices = [...roomPresets.querySelectorAll("button")];
    const syncRoomPresets = () => {
      roomPresets.hidden = ["house", "room"].includes(category.value);
      for (const choice of roomChoices) {
        const preset = choice.dataset.rooms;
        const minimum = preset.replace("+", "");
        const maximum = preset.endsWith("+") ? "" : minimum;
        choice.setAttribute("aria-pressed", String(roomMin.value === minimum && roomMax.value === maximum));
      }
    };
    for (const choice of roomChoices) {
      choice.id = `rooms-quick-${choice.dataset.rooms || "any"}`;
      choice.addEventListener("click", () => {
        const preset = choice.dataset.rooms;
        const isSelected = choice.getAttribute("aria-pressed") === "true";
        roomMin.value = isSelected ? "" : preset.replace("+", "");
        roomMax.value = isSelected || preset.endsWith("+") ? "" : roomMin.value;
        if (!isSelected && preset && category.value === "all") {
          category.value = "flat";
          category.dispatchEvent(new Event("change", { bubbles: true }));
        }
        roomMin.dispatchEvent(new Event("change", { bubbles: true }));
      });
    }
    for (const input of [roomMin, roomMax]) {
      input.addEventListener("input", syncRoomPresets);
      input.addEventListener("change", syncRoomPresets);
    }
    syncRoomPresets();
    const priceUnits = JSON.parse(document.querySelector("#search-price-units").textContent);
    searchableLocation(filters.elements.namedItem("district"), "Найти район", 8);
    searchableLocation(filters.elements.namedItem("metro"), "Найти станцию", 8);
    region.addEventListener("change", updateRegion);
    filters.addEventListener("submit", (event) => {
      if (locationRequest) event.preventDefault();
    });
    deal.addEventListener("change", () => {
      filters.elements.namedItem("price_min").value = "";
      filters.elements.namedItem("price_max").value = "";
      filters.querySelector("[data-price-label]").textContent = `Цена, ${priceUnits[deal.value]}`;
      updateLocationOptions();
    });
    category.addEventListener("change", ({ target }) => {
      const context = roomContext(target.value);
      if (context !== previousRoomContext) {
        roomMin.value = "";
        roomMax.value = "";
        previousRoomContext = context;
      }
      filters.querySelector("[data-rooms-label]").textContent = target.value === "room" ? "Предлагаемых комнат" : "Комнаты";
      filters.querySelector("[data-area-label]").textContent = ({ house: "Площадь дома, м²", room: "Площадь комнаты, м²" })[target.value] ?? "Площадь, м²";
      filters.querySelector("[data-room-count-help]").hidden = target.value !== "all";
      syncRoomPresets();
    });
  }

  mountPreviews(document);

  for (const gallery of document.querySelectorAll("[data-gallery]")) {
    const controls = gallery.querySelector("[data-controls]");
    if (!controls) continue;

    const slides = gallery.querySelectorAll("[data-slide]");
    const previous = controls.querySelector("[data-previous]");
    const next = controls.querySelector("[data-next]");
    const position = controls.querySelector("[data-position]");
    const thumbnails = gallery.querySelector("[data-gallery-thumbnails]");
    const choices = thumbnails?.querySelectorAll("[data-gallery-thumbnail]");
    const feed = gallery.hasAttribute("data-feed-gallery");
    const previousFrame = feedFrames?.get(gallery.dataset.galleryKey) ?? 0;
    let index = previousFrame < slides.length ? previousFrame : 0;
    let thumbnailTabStop;

    const setThumbnailTabStop = (choice) => {
      if (!choice || choice === thumbnailTabStop) return;
      if (thumbnailTabStop) thumbnailTabStop.tabIndex = -1;
      thumbnailTabStop = choice;
      choice.tabIndex = 0;
    };

    const revealThumbnail = (choice) => {
      if (!choice || thumbnails.hidden) return;
      const strip = thumbnails.getBoundingClientRect();
      const thumb = choice.getBoundingClientRect();
      if (thumb.left < strip.left) thumbnails.scrollLeft += thumb.left - strip.left;
      if (thumb.right > strip.right) thumbnails.scrollLeft += thumb.right - strip.right;
    };

    const showSlide = () => {
      if (gallery.dataset.galleryKey) gallery.dataset.galleryIndex = String(index);
      slides.forEach((slide, number) => { slide.hidden = number !== index; });
      const image = slides[index].querySelector("[data-gallery-src]");
      if (image && !image.hasAttribute("src")) image.src = image.dataset.gallerySrc;
      if (!feed) {
        previous.disabled = index === 0;
        next.disabled = index === slides.length - 1;
      }
      position.textContent = `${index + 1} из ${slides.length}`;
      choices?.forEach((choice, number) => {
        if (number === index) choice.setAttribute("aria-current", "true");
        else choice.removeAttribute("aria-current");
      });
      const selected = choices?.[index];
      if (!thumbnails?.contains(document.activeElement)) setThumbnailTabStop(selected);
      revealThumbnail(selected);
    };

    previous.addEventListener("click", () => {
      index = (index + slides.length - 1) % slides.length;
      showSlide();
    });
    next.addEventListener("click", () => {
      index = (index + 1) % slides.length;
      showSlide();
    });
    choices?.forEach((choice, number) => {
      choice.addEventListener("focus", () => {
        setThumbnailTabStop(choice);
        revealThumbnail(choice);
      });
      choice.addEventListener("keydown", (event) => {
        if (event.altKey || event.ctrlKey || event.metaKey) return;
        const last = choices.length - 1;
        let target;
        switch (event.key) {
          case "ArrowLeft": target = Math.max(0, number - 1); break;
          case "ArrowRight": target = Math.min(last, number + 1); break;
          case "Home": target = 0; break;
          case "End": target = last; break;
          default: return;
        }
        event.preventDefault();
        choices[target].focus({ preventScroll: true });
      });
      choice.addEventListener("click", () => {
        index = number;
        showSlide();
      });
    });
    if (feed) {
      let start;
      let swiped = false;
      gallery.addEventListener("click", (event) => {
        if (swiped && event.target.closest("a.gallery-image")) event.preventDefault();
      }, true);
      gallery.addEventListener("touchstart", ({ touches }) => {
        swiped = false;
        start = touches.length === 1 ? touches[0] : null;
      }, { passive: true });
      gallery.addEventListener("touchend", ({ changedTouches }) => {
        if (!start) return;
        const { clientX, clientY } = changedTouches[0];
        const x = clientX - start.clientX;
        const y = clientY - start.clientY;
        start = null;
        if (Math.abs(x) < 45 || Math.abs(x) < Math.abs(y) * 1.4) return;
        swiped = true;
        index = (index + slides.length - Math.sign(x)) % slides.length;
        showSlide();
      }, { passive: true });
    }
    showSlide();
    controls.hidden = false;
    if (thumbnails) thumbnails.hidden = false;
  }

  const tableRegion = document.querySelector("[data-comparison-scroll]");
  if (tableRegion) {
    const hint = document.querySelector("[data-comparison-hint]");
    const resize = new ResizeObserver(() => {
      hint.hidden = tableRegion.scrollWidth === tableRegion.clientWidth;
    });
    resize.observe(tableRegion);
    signal.addEventListener("abort", () => resize.disconnect(), { once: true });
  }

  for (const opener of document.querySelectorAll("[data-open-gallery]")) {
    const dialog = document.getElementById(opener.dataset.openGallery);
    if (dialog?.showModal) {
      opener.hidden = false;
      opener.addEventListener("click", () => dialog.showModal());
    }
  }
}
