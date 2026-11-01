import assert from 'node:assert/strict';
const destinations=new Set(['/','/memories','/review','/jobs','/artifacts','/watchlist','/people','/company-profile','/departments','/knowledge','/guide','/settings']);
export async function verifyNavigationOnDemand(page,check,{scope}){
 // Resource entries exclude the current document navigation and retain real RSC fetches.
 const prefetched=await page.evaluate(paths=>performance.getEntriesByType('resource').filter(e=>e.initiatorType==='fetch'&&paths.includes(new URL(e.name,location.href).pathname)).map(e=>({path:new URL(e.name,location.href).pathname,start:e.startTime,end:e.responseEnd})),[...destinations]);
 await check(scope+' closed navigation does not fetch unused destination documents',async()=>assert.deepEqual(prefetched,[]));
 const link=page.locator('aside').getByRole('link',{name:'Jobs',exact:true});
 const rect=await link.boundingBox();
 if(!rect||rect.x+rect.width<=1)await page.locator('header').getByRole('button',{name:'Open menu',exact:true}).click();
 await link.click();await page.getByRole('heading',{name:'Executive Jobs',exact:true}).waitFor();
 await check(scope+' actual Jobs navigation still reaches live workflow catalog',async()=>{assert.equal(new URL(page.url()).pathname,'/jobs');const r=await page.request.get(new URL('/api/backend/workflows',page.url()).href);assert.equal(r.status(),200);const d=await r.json();assert(d.workflows?.length>0,'real fixture workflow catalog required');await page.getByText('Loading jobs…',{exact:true}).waitFor({state:'hidden'});await page.getByText(d.workflows[0].title,{exact:true}).first().waitFor({state:'visible'});});
 if(page.viewportSize().width<1024){for(let i=0;i<60;i++){const r=await page.locator('aside').first().boundingBox();if(r&&r.x+r.width<=1)break;await page.waitForTimeout(50);}}
 return{prefetched};
}
