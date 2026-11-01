import assert from 'node:assert/strict';
// Controlled transport failure only for the chat POST. Actual UI chunks and health API stay live.
// No generated assistant reply or provider execution is claimed.
export async function verifyDeferredAskOE(page,check,{scope,onAbort,onLegacyMatch}){
 const toggle=page.getByTitle('Ask OE about this page (Ctrl/Cmd + .)',{exact:true});
 const panel=page.getByRole('complementary',{name:'Ask OE',exact:true});
 const input=panel.locator('textarea');
 await check(scope+' closed panel stays absent until opened',async()=>assert.equal(await panel.count(),0));
 await toggle.click();await panel.waitFor();
 await check(scope+' opening keeps native input focus and empty send disabled',async()=>{
  assert.equal(await input.evaluate(e=>e===document.activeElement),true);
  assert.equal(await panel.getByRole('button',{name:'Send',exact:true}).isDisabled(),true);
 });
 const text='BO01 deferred renderer '+scope;
 await input.fill(text);await panel.getByRole('button',{name:'Close Ask OE',exact:true}).click();await panel.waitFor({state:'detached'});await toggle.click();
 await check(scope+' unsent draft survives close and reopen',async()=>assert.equal(await input.inputValue(),text));
 let aborted=0;const route='**/api/backend/chat';await page.route(route,async r=>{assert.equal(r.request().method(),'POST');aborted++;onAbort?.({path:new URL(r.request().url()).pathname,method:r.request().method(),reason:'intentional transport unavailable, no backend mutation'});await r.abort('failed');});
 try{
  await input.press('Enter');await panel.getByText(text,{exact:true}).waitFor();const legacyNodes=await panel.getByText(text,{exact:true}).evaluateAll(es=>es.map(e=>({tag:e.tagName,text:e.textContent,value:e instanceof HTMLTextAreaElement?e.value:null})));onLegacyMatch?.(legacyNodes);const rendered=panel.locator('p.whitespace-pre-wrap').filter({hasText:text});await rendered.waitFor({state:'visible'});
  await panel.getByText('Failed to fetch',{exact:false}).waitFor();
  await check(scope+' first message loads real renderer despite explicit transport failure',async()=>{assert.equal(aborted,1);assert.equal(await rendered.count(),1);assert.equal(await input.inputValue(),'');});
  await panel.getByRole('button',{name:'Close Ask OE',exact:true}).click();await panel.waitFor({state:'detached'});await toggle.click();
  await check(scope+' rendered message and error survive close and reopen',async()=>{assert.equal(await rendered.isVisible(),true);assert.equal(await panel.getByText('Failed to fetch',{exact:false}).isVisible(),true);});
  await panel.getByRole('button',{name:'New conversation',exact:true}).click();
  await check(scope+' new conversation clears transient message and error',async()=>{assert.equal(await panel.getByText(text,{exact:true}).count(),0);assert.equal(await panel.getByText('Failed to fetch',{exact:false}).count(),0);});
 }finally{await page.unroute(route);}
 return{panel};
}
