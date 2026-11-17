# Avito via isolated SOCKS5 browser — field report

**Captured:** 2026-11-17 01:03 UTC. Read-only public-page inspection; no login, seller contact, or application/database changes.

## Proxy configuration and access

- The local `.pi/mcp.json` configures Playwright MCP `@playwright/mcp@0.0.85` with `--isolated`, `--headless`, and `--proxy-server socks5://127.0.0.1:10808`. This is the concrete launch-configuration evidence used for this audit. `process.argv` could not be read from page-context JavaScript, and the external egress IP was not independently checked; therefore proxy routing is configuration-backed, not independently egress-verified.
- A fresh isolated context opened the previously observed house-sale category URL: [Samara houses for sale](https://www.avito.ru/samara/doma_dachi_kottedzhi/prodam-ASgBAgICAUSUA9AQ?cd=1). Initial navigation returned **HTTP 439** and the title `Доступ ограничен: проверка безопасности`. The browser then observed `POST /web/3/firewallPow/get` and `/web/3/firewallPow/verify`, both HTTP 200; the normal listing DOM subsequently rendered. No manual challenge interaction was needed after that transition. The 439 response remains the recorded initial document status; I do not represent it as a 200 page load.
- The rendered listing showed **1,934** results and pagination **1–5, …, 30** (30 pages; next link to page 2).
- The first observed card was `Дом 59,2 м² на участке 6 сот., Самара`, ID **8457143068**. Its observed card link opened the item detail page successfully.

## Public listing observed

- **Item ID:** `8457143068`; hydration `externalId` is `null`. For the existing source contract, derive `external_id` from the observed public item ID; `source_code` remains the application constant `avito`.
- **Type/deal:** house for sale (`doma_dachi_kottedzhi`; page says “на продажу” / “Купить”).
- **Address:** Самарская обл., Волжский р-н, сельское поселение Черноречье, пос. Чапаевка, КП Бавария парк.
- **Coordinates:** `item.geo.coords` = **53.178563, 50.427189** (zoom 16); paired with the above item address.
- **House fields:** 59.2 m²; **2 structured rooms**; 1 floor; land 6 сот.; land category ИЖС; build year 2026. The public description separately says “2 изолированные спальни”; that is descriptive text, not a distinct structured bedroom field. No apartment floor number applies.
- **Asking price:** **6,250,000 ₽**, sale price, no period/postfix. Mortgage figures shown separately on the page are not treated as listing price.
- **Description:** 2,260 characters in public hydration `buyerItem.item.description`; visible description section text is 2,051 characters. The text itself is omitted from the saved result.
- **Photos:** 28 public `1280x960` URLs in `imageUrls`; URLs are saved in `proxy-result.json`. No media downloaded or hashed.
- **Publication/removal signals:** public `sortFormatedDate` says `сегодня в 01:26`; `isActive=true`, `isArchived/isBlocked/isClosed/isDeleted/isExpired/isFinished/isRejected/isInModeration=false`. `finishTime` is 2026-12-07 05:48:04 UTC, a scheduled finish timestamp, not evidence of removal. No removal timestamp/signal was observed.
- **Price history:** observed public endpoint `/web/1/realty/price/history/8457143068?categoryID=25&microcatID=3809&offset=0&limit=40` returned HTTP 200. It reported 6,250,000 ₽ at 2026-10-01 02:59:20 UTC (`diff=150000`) and 6,100,000 ₽ at 2026-09-29 06:08:21 UTC (`diff=0`). These API records are preserved in `proxy-result.json`.

## Mapping notes

Exact accepted-schema mapping is in [AVITO-CONTRACT-AUDIT.md](../AVITO-CONTRACT-AUDIT.md). House area 59.20 m², rooms 2, floors_total 1, floor null, land_area_m2 600.00; deal_type belongs to ads. Land category, build year, title and source publication timestamps do not introduce new database columns. Current price 6250000.00 / listed enters our observation history under the accepted rules. The source historical price events are evidence only; importing them into our price_history was not approved. No diff field is added.

## Evidence files

- `proxy-result.json` — structured public values, source paths and price-history data.
- `proxy-listing.png`, `proxy-listing-snapshot.yml` — rendered category listing and pagination.
- `proxy-house.png`, `proxy-house-snapshot.yml` — house detail page.
- `proxy-security-20261117.png` — full-page capture taken after the initial 439 response had already transitioned to the normal listing; it is **not** a screenshot of the original security page.
