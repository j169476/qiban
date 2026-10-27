// Synthetic first-run browser checks. No real credentials or platform requests.
import assert from 'node:assert/strict';
import {readFile,mkdir,writeFile} from 'node:fs/promises';
import {spawnSync} from 'node:child_process';
import {createRequire} from 'node:module';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const require=createRequire(import.meta.url),{chromium}=require(process.env.QIBAN_PLAYWRIGHT_MODULE||'playwright');
const output=process.env.QIBAN_UI_OUTPUT||path.join(root,'.dev/v094/browser');await mkdir(output,{recursive:true});
const rendered=spawnSync(process.env.QIBAN_PYTHON||path.join(root,'.venv/Scripts/python.exe'),['-c','import json;from jinja2 import Environment,FileSystemLoader;e=Environment(loader=FileSystemLoader("src/companion_service/templates"));print(json.dumps({kind:e.get_template("index.html").render(kind=kind,authenticated=False,configured=False,csrf="",counterpart="http://qiban.test",kinds=[]) for kind in ("admin","role")}))'],{cwd:root,encoding:'utf8'});
assert.equal(rendered.status,0,rendered.stderr);const html=JSON.parse(rendered.stdout),errors=[];
const browser=await chromium.launch({headless:true,...(process.env.QIBAN_BROWSER?{executablePath:process.env.QIBAN_BROWSER}:{})});
try{
 for(const locale of ['zh-CN','en-US']){
  const ctx=await browser.newContext({locale,viewport:{width:390,height:1000}}),page=await ctx.newPage();
  page.on('pageerror',error=>errors.push(String(error)));
  let validations=0,submissions=0,mode='valid';
  await ctx.route('**/*',async route=>{
   const url=new URL(route.request().url()),p=url.pathname;let body,status=200,contentType='text/html';
   if(p.startsWith('/static/')){assert.match(p,/^\/static\/[\w.-]+$/);body=await readFile(path.join(root,'src/companion_service',p));contentType=({'.css':'text/css','.js':'text/javascript','.svg':'image/svg+xml','.png':'image/png'})[path.extname(p)];}
   else if(p==='/setup/validate'){
    validations++;contentType='application/json';
    body=mode==='valid'?{valid:true}:{error:'setup_ticket_expired',message:'初始化链接已过期，请在管理聊天重新调用 service_setup'};if(mode!=='valid')status=403;
   }else if(p==='/setup'){
    submissions++;assert.equal(route.request().postDataJSON().ticket,'synthetic-once-token');contentType='application/json';
    if(mode==='used'){status=403;body={error:'setup_ticket_used',message:'初始化已完成，请返回首页登录；不要重复提交初始化表单'};}
    else body={configured:true,role_started:false};
   }else body=html[p==='/role'?'role':'admin'];
   await route.fulfill({status,contentType,body:typeof body==='object'&&!Buffer.isBuffer(body)?JSON.stringify(body):body});
  });
  await page.goto('http://qiban.test/');assert.equal(await page.locator('#setup').isVisible(),false);
  assert.equal(await page.locator('#setup input').isDisabled(),true);assert.equal(validations,0);
  await page.evaluate(()=>document.querySelector('#setup').dispatchEvent(new Event('submit',{cancelable:true})));
  assert.equal(submissions,0);await page.screenshot({path:path.join(output,`setup-no-ticket-${locale}.png`),fullPage:true});
  mode='expired';await page.goto('http://qiban.test/#setup=synthetic-once-token');await page.reload();
  await page.waitForFunction(()=>document.querySelector('#setup-guidance').textContent.includes('过期')||document.querySelector('#setup-guidance').textContent.includes('expired'));
  assert.equal(await page.locator('#setup').isVisible(),false);assert.equal(new URL(page.url()).hash,'');
  mode='valid';await page.goto('http://qiban.test/#setup=synthetic-once-token');await page.reload();await page.locator('#setup').waitFor({state:'visible'});
  assert.equal(await page.locator('#setup input').isDisabled(),false);assert.equal(submissions,0);
  await page.screenshot({path:path.join(output,`setup-valid-${locale}.png`),fullPage:true});
  await page.locator('#setup input').fill('synthetic-password-2026');
  mode='used';await page.locator('#setup button').click();await page.locator('#setup').waitFor({state:'hidden'});
  assert.equal(submissions,1);assert.match(await page.locator('#setup-guidance').textContent(),locale==='zh-CN'?/已完成/:/already complete/);
  await page.screenshot({path:path.join(output,`setup-used-${locale}.png`),fullPage:true});
  await page.goto('http://qiban.test/role#setup=synthetic-once-token');assert.equal(await page.locator('#setup').count(),0);
  await ctx.close();
 }
 assert.deepEqual(errors,[]);await writeFile(path.join(output,'setup-checks.json'),JSON.stringify({ok:true,languages:2,scenarios:['no ticket','disabled submit','expired ticket','verified link','late duplicate submission','role cannot setup'],errors},null,2));
 console.log(JSON.stringify({ok:true,errors,output}));
}finally{await browser.close();}
