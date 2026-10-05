const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { chromium } = require('playwright');

(async () => {
  const [base, fixture] = process.argv.slice(2);
  assert(base && fixture, 'Provide the local Vite URL and generated QR fixture file');
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    await page.route(`${base}/__boundary_probe`, (route) => route.fulfill({
      contentType: 'text/html', body: '<!doctype html><html><body></body></html>',
    }));
    await page.goto(`${base}/__boundary_probe`);
    const results = await page.evaluate(async (qr) => {
      const { mount, unmount } = await import('/node_modules/.vite/deps/svelte.js');
      const { default: DeviceIcon } = await import('/src/lib/DeviceIcon.svelte');
      const target = document.createElement('div');
      document.body.append(target);
      window.__didaBoundaryExecuted = false;
      const icon = mount(DeviceIcon, {
        target,
        props: { type: '</svg><img src=x onerror="window.__didaBoundaryExecuted=true">' },
      });
      await new Promise((resolve) => setTimeout(resolve, 50));
      const iconSafe = target.querySelectorAll('svg').length === 1 && !target.querySelector('img,script,foreignObject');
      unmount(icon);
      for (const markup of qr) {
        target.innerHTML = markup;
        await new Promise((resolve) => setTimeout(resolve, 50));
        if (target.querySelector('script,img,foreignObject,[onload],[onerror]')) return false;
      }
      const safe = iconSafe && !window.__didaBoundaryExecuted;
      target.remove();
      return safe;
    }, JSON.parse(readFileSync(fixture, 'utf8')));
    assert(results, 'Untrusted icon or QR content reached an executable HTML sink');
    console.log('PASS: actual Svelte icon and generated QR SVG boundaries in Chromium');
  } finally {
    await browser.close();
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
