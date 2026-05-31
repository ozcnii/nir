// Повторная попытка по сайтам, забанившим автоматизацию (Домклик, Restate).
// Маскирует navigator.webdriver и не подменяет User-Agent (реальный Chrome).
import { chromium } from 'playwright';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const OUT_DIR = resolve(__dirname, '..', 'thesis', 'screenshots');
const PAUSE_MS = Number(process.env.PAUSE_MS ?? 12000);

const TARGETS = [
  { name: 'domclick', url: 'https://domclick.ru/' },
  { name: 'restate',  url: 'https://www.restate.ru/' },
];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const browser = await chromium.launch({ channel: 'chrome', headless: false });
const context = await browser.newContext({
  viewport: { width: 1366, height: 900 },
  locale: 'ru-RU',
  timezoneId: 'Europe/Moscow',
});
// Прячем признак автоматизации до загрузки любой страницы.
await context.addInitScript(() => {
  Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
});

for (const { name, url } of TARGETS) {
  const page = await context.newPage();
  const out = resolve(OUT_DIR, `${name}.png`);
  try {
    console.log(`\n→ ${name}: ${url}`);
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
    await sleep(3000);
    await page.reload({ waitUntil: 'domcontentloaded' }); // антибот часто пускает со 2-го раза
    await sleep(3000);
    await page.mouse.wheel(0, 500);
    await sleep(1000);
    await page.mouse.wheel(0, -500);
    console.log(`  ждём ${PAUSE_MS} мс (пройди CAPTCHA вручную, если есть)…`);
    await sleep(PAUSE_MS);
    await page.screenshot({ path: out });
    console.log(`  ✓ сохранено: ${out}`);
  } catch (err) {
    console.log(`  ✗ ошибка: ${err.message}`);
  } finally {
    await page.close();
  }
}
await browser.close();
console.log('\nГотово. Проверь содержимое thesis/screenshots/domclick.png и restate.png.');
