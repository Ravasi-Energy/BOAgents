import assert from 'node:assert/strict';

// Runs against the real People page and API. The caller supplies its real
// authenticated page and records every independent assertion, including red ones.
export async function verifyPersonCreateKeyboard(page, check, { origin, viewport, draft }) {
  const trigger = page.getByRole('button', { name: '+ Add person', exact: true });
  const name = page.getByPlaceholder('Sarah Chen');
  const cancel = page.getByRole('button', { name: 'Cancel', exact: true });
  await trigger.focus();
  await page.keyboard.press('Enter');
  await name.waitFor();
  await check(viewport+' modal has an accessible dialog name', async()=>{
    assert.equal(await page.getByRole('dialog', { name:'Add person', exact:true }).count(),1);
  });
  await check(viewport+' initial focus enters the name field', async()=>{
    assert(await name.evaluate(e=>document.activeElement===e));
  });
  await check(viewport+' Shift Tab stays inside the dialog', async()=>{
    await name.focus(); await page.keyboard.press('Shift+Tab');
    assert(await cancel.evaluate(e=>document.activeElement===e));
  });
  await check(viewport+' Tab wraps from Cancel to the name field', async()=>{
    await cancel.focus(); await page.keyboard.press('Tab');
    assert(await name.evaluate(e=>document.activeElement===e));
  });
  await check(viewport+' Escape closes the dialog', async()=>{
    await name.focus(); await page.keyboard.press('Escape');
    await page.waitForTimeout(100);
    assert.equal(await name.isVisible(),false);
  });
  // Preserve independent red observations instead of timing out after Escape.
  if(await name.isVisible()) await cancel.click();
  await check(viewport+' closing restores focus to the invoking button', async()=>{
    assert(await trigger.evaluate(e=>document.activeElement===e));
  });
  await trigger.click(); await name.fill(draft);
  // Slow the real request through CDP, preserving its real backend and response.
  // This exposes the pending state without replacing the API or response body.
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Network.enable');
  await cdp.send('Network.emulateNetworkConditions', {
    offline:false, latency:400, downloadThroughput:-1, uploadThroughput:-1,
  });
  const response = page.waitForResponse(r=>r.url()===origin+'/api/backend/people'&&r.request().method()==='POST');
  await page.getByRole('button',{name:'Add person',exact:true}).click();
  await check(viewport+' Escape preserves the dialog while its real request is pending', async()=>{
    assert(await cancel.isDisabled());
    await page.keyboard.press('Escape');
    assert(await name.isVisible());
  });
  assert.equal((await response).status(),409);
  await page.getByText(/Scrierea ar revoca propriul acces/).waitFor();
  await cdp.detach();
  await check(viewport+' refused create is announced and preserves the draft', async()=>{
    const alert = page.getByRole('alert').filter({hasText:'Scrierea ar revoca propriul acces'});
    assert.equal(await alert.count(),1);
    assert.equal(await name.inputValue(),draft);
  });
}
