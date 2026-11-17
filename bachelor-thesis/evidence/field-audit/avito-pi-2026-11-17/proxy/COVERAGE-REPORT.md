# Avito field coverage

- **Rooms sale:** rendered; inspected one observed listing. Hydration shows room area 12.5 m², 2 rooms in the flat, floor 1/3, price 752,777 ₽, 3 photos, and property coordinates. Whole-flat area and numeric offered-room count were not exposed in the inspected params.
- **Monthly rent:** observed URL returned HTTP 429. One IP slider check reported passed, but the page remained restricted; no listing or rent terms were available. Daily rent, resale, and the previously observed pagination link were not attempted after the repeated restriction.
- No seller/contact data, descriptions, media, imports, or global settings were collected/changed. Proxy configuration is referenced from `proxy-launch-proof.json`; no external IP check was made.
- Structured values and exact hydration paths: [`coverage-result.json`](coverage-result.json).
