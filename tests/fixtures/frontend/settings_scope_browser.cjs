// Exercise account boundaries using real settings writes and a failed GET.
const {chromium}=require(process.env.FLEET_PLAYWRIGHT_MODULE);
const assert=require('node:assert/strict');
const [origin,scenario]=process.argv.slice(2);
(async()=>{
 const browser=await chromium.launch({channel:process.env.FLEET_BROWSER_CHANNEL||'chrome',headless:true});
 try {
  const context=await browser.newContext({viewport:{width:1440,height:1000}});
  const login=await context.request.post(origin+'/api/accounts/login',{headers:{Origin:origin},data:{email:'admin@example.test',password:'safe-password-123!'}});
  assert.equal(login.status(),200);
  const api=origin+'/api/platform/v1/settings';
  const owner='another-owner';
  const node={node_id:'same-node',label:'相同节点',enabled:true,capabilities:{}};
  for(const suffix of ['', '?owner_id='+owner]) {
   const saved=await context.request.put(api+'/nodes/same-node'+suffix,{headers:{Origin:origin},data:{expected_digest:'',value:node}});
   assert.equal(saved.status(),200);
  }
  const page=await context.newPage();page.setDefaultTimeout(8000);
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(origin+'/settings/nodes');
  await page.getByRole('button',{name:/相同节点/}).click();
  await page.getByLabel('节点名称',{exact:true}).fill('旧账号的编辑草稿');
  await page.locator('.settings-owner-scope summary').click();
  let reject=true;
  await page.route('**/api/platform/v1/settings?owner_id='+owner,r=>reject
   ?r.fulfill({status:503,json:{error:'unavailable',detail:'目标账号暂时不可用'}}):r.continue());
  await page.getByLabel('账号 ID',{exact:true}).fill(owner);
  await page.getByRole('button',{name:'加载账号配置',exact:true}).click();
  await page.getByText('目标账号暂时不可用',{exact:true}).waitFor();
  if(scenario==='failed-switch')assert.equal(await page.getByRole('button',{name:'保存配置',exact:true}).count(),0,'failed owner switch must not expose the previous owner form with a new write target');
  // Recover the target account, then switch while a save receipt is held.
  reject=false;await page.getByRole('button',{name:'加载账号配置',exact:true}).click();
  await page.getByRole('button',{name:/相同节点/}).click();
  await page.getByLabel('节点名称',{exact:true}).fill('目标账号保存');
  let releaseSave;const held=new Promise(resolve=>{releaseSave=resolve;});
  let saved;const committed=new Promise(resolve=>{saved=resolve;});
  await page.route('**/api/platform/v1/settings/nodes/same-node?owner_id='+owner,async r=>{
   const response=await r.fetch();saved();await held;return r.fulfill({response});
  });
  await page.getByRole('button',{name:'保存配置',exact:true}).click();
  await committed;
  await page.getByLabel('账号 ID',{exact:true}).fill('');
  await page.getByRole('button',{name:'加载账号配置',exact:true}).click();
  await page.getByRole('button',{name:/相同节点/}).click();
  await page.getByLabel('节点名称',{exact:true}).fill('当前账号尚未保存');
  const receipt=page.waitForResponse(r=>r.request().method()==='PUT');
  releaseSave(); await receipt;
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  assert.equal(await page.getByLabel('节点名称',{exact:true}).inputValue(),'当前账号尚未保存','a stale save receipt must not reload and erase the new owner editor');
  assert.deepEqual(errors,[]);
  console.log('PASS: owner failure isolation and late-save fence');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
