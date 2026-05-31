// Скриншоты сервисов-аналогов для рисунков главы 1.
//
// Запуск (из корня репозитория):
//   node scripts/screenshots.mjs
//
// Использует системный Google Chrome в видимом (headful) режиме, локаль
// ru-RU и московский часовой пояс, чтобы выглядеть как обычный пользователь.
// Авито и Циан могут показать CAPTCHA / антибот — для них окно остаётся
// открытым PAUSE_MS миллисекунд, чтобы можно было пройти проверку вручную,
// после чего скриншот снимается автоматически.
//
// Результат: thesis/screenshots/<name>.png (перезаписывает существующие).

import { chromium } from 'playwright';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const OUT_DIR = resolve(__dirname, '..', 'thesis', 'screenshots');

// Сколько ждать после загрузки, чтобы успеть вручную закрыть баннеры cookie
// или пройти CAPTCHA перед снимком (мс).
const PAUSE_MS = Number(process.env.PAUSE_MS ?? 8000);

const TARGETS = [
  { name: 'avito',    url: 'https://www.avito.ru/rossiya/nedvizhimost' },
  { name: 'cian',     url: 'https://www.cian.ru/' },
  { name: 'domclick', url: 'https://domclick.ru/' },
  { name: 'yandex',   url: 'https://realty.yandex.ru/' },
  { name: 'restate',  url: 'https://www.restate.ru/' },
];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const browser = await chromium.launch({
  channel: 'chrome',          // системный Chrome, а не bundled chromium
  headless: false,            // видимое окно — меньше шансов на бан
});

const context = await browser.newContext({
  viewport: { width: 1366, height: 900 },
  locale: 'ru-RU',
  timezoneId: 'Europe/Moscow',
  userAgent:
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 ' +
    '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
});

const results = [];

for (const { name, url } of TARGETS) {
  const page = await context.newPage();
  const out = resolve(OUT_DIR, `${name}.png`);
  try {
    console.log(`\n→ ${name}: ${url}`);
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
    // Дать догрузиться динамике и плавно проскроллить, имитируя пользователя.
    await sleep(2500);
    await page.mouse.wheel(0, 600);
    await sleep(1200);
    await page.mouse.wheel(0, -600);
    console.log(`  ждём ${PAUSE_MS} мс (закрой баннеры / пройди CAPTCHA, если есть)…`);
    await sleep(PAUSE_MS);
    await page.screenshot({ path: out }); // видимая область (не full page)
    console.log(`  ✓ сохранено: ${out}`);
    results.push({ name, ok: true });
  } catch (err) {
    console.log(`  ✗ ошибка: ${err.message}`);
    results.push({ name, ok: false, error: err.message });
  } finally {
    await page.close();
  }
}

await browser.close();

console.log('\n===== Итог =====');
for (const r of results) {
  console.log(`${r.ok ? '✓' : '✗'} ${r.name}${r.ok ? '' : ' — ' + r.error}`);
}
const failed = results.filter((r) => !r.ok).map((r) => r.name);
if (failed.length) {
  console.log(`\nНе удалось: ${failed.join(', ')} — сними вручную.`);
}
