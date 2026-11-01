import assert from 'node:assert/strict';
// Real document read and modal interactions; no DOM/style/API replacement.
export async function verifyCompanyDocumentKeyboard(page,check,{filename,viewport,capture}){
 const trigger=page.getByRole('button',{name:'View',exact:true}), close=page.getByRole('button',{name:'Close',exact:true});
 const modal=page.locator('.fixed.inset-0').filter({hasText:filename}),body=modal.locator('div.overflow-y-auto').first();
 async function open(){await trigger.focus();await page.keyboard.press('Enter');await close.waitFor();await page.waitForTimeout(100)}
 await open();
 await check(viewport+' document viewer has a named modal dialog',async()=>assert.equal(await page.getByRole('dialog',{name:filename,exact:true}).count(),1));
 await check(viewport+' document initial focus enters Close',async()=>assert(await close.evaluate(e=>document.activeElement===e)));
 await check(viewport+' ShiftTab from Close wraps to scroll body',async()=>{await close.focus();await page.keyboard.press('Shift+Tab');assert(await body.evaluate(e=>document.activeElement===e))});
 await check(viewport+' Tab from scroll body wraps to Close',async()=>{await body.focus();await page.keyboard.press('Tab');assert(await close.evaluate(e=>document.activeElement===e))});
 await check(viewport+' long document is keyboard scrollable inside modal',async()=>{await body.evaluate(e=>e.scrollTop=0);await body.focus();await page.keyboard.press('PageDown');await page.waitForTimeout(150);const g=await body.evaluate(e=>({focused:document.activeElement===e,scrollTop:e.scrollTop,scrollHeight:e.scrollHeight,clientHeight:e.clientHeight}));assert(g.focused&&g.scrollHeight>g.clientHeight&&g.scrollTop>0,JSON.stringify(g));return g});
 await check(viewport+' document content uses real Markdown image policy',async()=>{assert.equal(await modal.locator('[data-md-image=blocked][aria-label="company-remote"]').count(),1);assert.equal(await modal.locator('[data-md-image=blocked][data-md-reason=api]').count(),1);await modal.locator('[data-md-image=error][aria-label="company-missing"]').waitFor();assert.equal(await modal.locator('img').count(),0);assert.equal(await page.evaluate(()=>!!window.__bo01_doc_xss),false)});
 await capture(page,viewport);
 await close.focus();await page.keyboard.press('Escape');await page.waitForTimeout(100);
 await check(viewport+' Escape closes document',async()=>assert.equal(await close.isVisible(),false));
 if(await close.isVisible())await close.click();
 await check(viewport+' Escape restores focus to invoking View',async()=>assert(await trigger.evaluate(e=>document.activeElement===e)));
 await open();await close.click();
 await check(viewport+' Close button restores focus to invoking View',async()=>assert(await trigger.evaluate(e=>document.activeElement===e)));
}
