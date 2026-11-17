# Avito: read-only field audit

- **Observed:** 2026-11-17 00:44:45.616 UTC
- **Tool:** Pi Playwright MCP, fresh tab in isolated context `avito-thesis-audit`
- **Requested and actual URL:** <https://www.avito.ru/samara/kvartiry/prodam>
- **Result:** HTTP 429; title **«Доступ ограничен: проблема с IP»**. The rendered page says **«Страница не загрузилась из-за проблем с IP»** and explains that unusually high request volume was detected. It offers **«Пройти проверку»**. The challenge was not clicked; no retries or circumvention were attempted.

The requested listing page did not open, so no genuine property card, offer, pagination, or inline listing data could be inspected. All target property/ad fields are **unverified due to the access restriction**, not absent or empty. This includes offer ID/URL, deal and price semantics, property/address/area/room/floor/land attributes, description length, photos, listing status, and room/house/monthly/daily examples. No adapter mapping can be confirmed from this attempt.

The only observed fields and their rendered-page sources are the page title (`document.title`), the restriction heading (snapshot ref `e19`), restriction text (snapshot refs `e22`, `e25–e35` / `document.body.innerText`), the unclicked verification button (ref `e24`), and HTTP status 429 from the Playwright navigation result.

Evidence: screenshot [`avito-blocked.png`](avito-blocked.png); snapshot output `browser-output/page-2026-11-17T00-44-36-846Z.yml`; structured record [`result.json`](result.json). The browser was closed after recording the evidence.
