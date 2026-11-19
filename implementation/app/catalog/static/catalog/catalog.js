const filters = document.querySelector("#catalog-filters");

filters?.elements.namedItem("deal_type").addEventListener("change", () => {
  filters.elements.namedItem("price_min").value = "";
  filters.elements.namedItem("price_max").value = "";
  filters.requestSubmit();
});

for (const image of document.querySelectorAll("[data-preview]")) {
  const showFallback = () => {
    image.hidden = true;
    image.nextElementSibling.hidden = false;
  };
  image.addEventListener("error", showFallback, { once: true });
  if (image.complete && image.naturalWidth === 0) {
    showFallback();
  }
}

for (const gallery of document.querySelectorAll("[data-gallery]")) {
  const controls = gallery.querySelector("[data-controls]");
  if (!controls) continue;

  const slides = gallery.querySelectorAll("[data-slide]");
  const previous = controls.querySelector("[data-previous]");
  const next = controls.querySelector("[data-next]");
  const position = controls.querySelector("[data-position]");
  let index = 0;

  const showSlide = () => {
    slides.forEach((slide, number) => { slide.hidden = number !== index; });
    previous.disabled = index === 0;
    next.disabled = index === slides.length - 1;
    position.textContent = `${index + 1} из ${slides.length}`;
  };

  previous.addEventListener("click", () => {
    index -= 1;
    showSlide();
  });
  next.addEventListener("click", () => {
    index += 1;
    showSlide();
  });
  showSlide();
  controls.hidden = false;
}
