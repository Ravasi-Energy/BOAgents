import assert from 'node:assert/strict';

export const encodedImageSources = [
  '/%61pi/backend/health?bo01_encoded_image=1',
  '/api%2fbackend%2fhealth?bo01_encoded_image=2',
  '/%2561pi/backend/health?bo01_encoded_image=3',
  '/asset/..%2fapi/backend/health?bo01_encoded_image=4',
];

// The caller seeds a real document and opens its real viewer before invoking
// this regression. Observe actual browser requests; never replace API replies.
export async function verifyEncodedMarkdownImages(page, check, { filename, viewport, imageRequests, role }) {
  const modal = page.getByRole('dialog', { name: filename, exact: true });
  await modal.waitFor();
  await page.waitForTimeout(800);
  for (let i = 0; i < encodedImageSources.length; i++) {
    await check((role ? role + ' ' : '') + viewport + ' encoded variant ' + i + ' refused without request', async () => {
      assert.equal(await modal.locator('[data-md-image=blocked][aria-label="encoded-' + i + '"]').count(), 1);
      assert.equal(imageRequests.filter(x => x.vp === viewport && (!role || x.role === role) && x.url.includes('bo01_encoded_image=' + (i + 1))).length, 0);
    });
  }
}

export const validPercentImageSources = ['/bo01-100%25.png', '/bo01-%2525.png'];

export async function verifyValidPercentMarkdownImages(page, check, { filename, viewport, imageRequests, imageResponses, role }) {
  const modal = page.getByRole('dialog', { name: filename, exact: true });
  for (let i = 0; i < validPercentImageSources.length; i++) {
    await check((role ? role + ' ' : '') + viewport + ' valid percent asset ' + i + ' reaches real 404 fallback', async () => {
      const alt = 'valid-percent-' + i;
      assert.equal(await modal.locator('[data-md-image=blocked][aria-label="' + alt + '"]').count(), 0);
      const image = modal.locator('img[alt="' + alt + '"]');
      if (await image.count()) await image.scrollIntoViewIfNeeded();
      const fallback = modal.locator('[data-md-image=error][aria-label="' + alt + '"]');
      await fallback.waitFor({ timeout: 5000 });
      assert(imageRequests.some(r => r.vp === viewport && (!role || r.role === role) && new URL(r.url).pathname === validPercentImageSources[i]));
      assert(imageResponses.some(r => r.vp === viewport && (!role || r.role === role) && new URL(r.url).pathname === validPercentImageSources[i] && r.status === 404));
    });
  }
}
