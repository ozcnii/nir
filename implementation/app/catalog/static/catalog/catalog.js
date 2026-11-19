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
