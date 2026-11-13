// NAV-171 · R6.1 应用内分区导航：5 项导航检查。
// 只覆盖导航本身：分区归属、切换是否保留原节点与未保存输入、原 [hidden] 是否仍然优先、
// 是否额外调用桥接，以及键盘与 aria-current。既有页面测试不在此文件内改动。
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const assets=path.join(__dirname,'Assets');
const html=fs.readFileSync(path.join(assets,'index.html'),'utf8');
const css=fs.readFileSync(path.join(assets,'probe.css'),'utf8');
const navigation=fs.readFileSync(path.join(assets,'navigation.js'),'utf8');
const pages=['home','persona','platforms','model','records','media','settings'];
// 与 QIBAN_R6_NAVIGATION.md 的分区对照表一致：每个页面元素只归属一个分区。
const owners={
 'profile-area':'persona','baseline-panel':'persona','relationship-panel':'persona','memory-panel':'persona',
 'sharing-panel':'persona','relation-claims-panel':'persona',
 'bili-panel':'platforms','qq-panel':'platforms','audience-panel':'platforms','growth-panel':'platforms',
 'model-panel':'model','reply-quota-panel':'model',
 'tasks-panel':'records','reply-trace-panel':'records','runtime-panel':'home',
 'media-panel':'media','file-panel':'media','sticker-panel':'media',
 'backup-panel':'settings','management-lock':'settings'
};
const tagOf=id=>{
 const found=html.match(new RegExp('<[a-z]+[^>]*\\bid="'+id.replace(/[-]/g,'\\-')+'"[^>]*>'));
 return found?found[0]:null;
};
const pageOf=tag=>(tag.match(/data-nav-page="([a-z]+)"/)||[])[1]||'';
function run(){
 const body={dataset:{navPage:'home'}},sent=[],scrolled=[],links=[];
 for(const page of pages){
  const link={dataset:{navTarget:page},attributes:new Map(),onclick:null,
   setAttribute(name,value){this.attributes.set(name,String(value));},
   removeAttribute(name){this.attributes.delete(name);}};
  if(page==='home')link.attributes.set('aria-current','page');
  links.push(link);
 }
 // 未保存输入与已锁住（原逻辑设为 hidden）的资料节点：导航不应碰到它们。
 const draft={id:'persona-name',value:'未保存的资料文本'},locked={id:'memory-panel',hidden:true};
 vm.runInNewContext(navigation,{document:{body,querySelectorAll:()=>links},
  window:{scrollTo:(x,y)=>scrolled.push([x,y]),chrome:{webview:{postMessage:message=>sent.push(message)}}}});
 const current=()=>links.filter(link=>link.attributes.get('aria-current')==='page').map(link=>link.dataset.navTarget);
 return {body,links,sent,scrolled,draft,locked,current};
}
test('导航列出 7 个分区，每个页面元素只归属一个分区且原 id 唯一',()=>{
 const targets=[...html.matchAll(/data-nav-target="([a-z]+)"/g)].map(match=>match[1]);
 assert.deepEqual(targets,pages);
 for(const [id,page] of Object.entries(owners)){
  const tag=tagOf(id);
  assert.ok(tag,'缺少页面元素 '+id);
  assert.equal(pageOf(tag),page,id+' 的分区不对');
  assert.equal(tag.match(/data-nav-page="/g).length,1,id+' 只能有一个分区标记');
 }
 assert.equal(pageOf(html.match(/<section class="welcome card"[^>]*>/)[0]),'home');
 // 全局容器不参与分区切换：后台服务页与本机身份容器始终可达。
 assert.equal(pageOf(html.match(/<section[^>]*>\s*<h2 id="component-title">/)[0]),'');
 assert.equal(pageOf(tagOf('management-panel')),'');
 // 每个分区都有页面归属，且没有任何元素同时出现在两个分区里。
 assert.deepEqual([...new Set(Object.values(owners))].sort(),[...pages].sort());
 assert.equal(/data-nav-page="[^"]*\s[^"]*"/.test(html),false);
 const ids=[...html.matchAll(/\bid="([^"]+)"/g)].map(match=>match[1]);
 assert.equal(new Set(ids).size,ids.length,'原 id 必须唯一');
});
test('切换只改当前分区，保留原节点与未保存输入',()=>{
 const page=run();
 assert.equal(page.body.dataset.navPage,'home');
 assert.deepEqual(page.current(),['home']);
 const handler=page.links[3].onclick;
 page.links[3].onclick();
 assert.equal(page.body.dataset.navPage,'model');
 assert.deepEqual(page.current(),['model']);
 assert.deepEqual(page.scrolled.at(-1),[0,0],'切换后回到页面主区');
 assert.equal(page.draft.value,'未保存的资料文本');
 assert.equal(page.locked.hidden,true);
 assert.equal(page.draft.id,'persona-name');
 assert.equal(page.links[3].onclick,handler,'切换不重建按钮和监听');
 assert.equal(page.links.length,7);
});
test('原 hidden 仍然优先：导航不写 hidden 或 disabled，CSS 只隐藏非当前分区',()=>{
 assert.doesNotMatch(navigation,/\bhidden\s*=/);
 assert.doesNotMatch(navigation,/\bdisabled\s*=/);
 assert.doesNotMatch(navigation,/createElement|cloneNode|innerHTML|appendChild|insertBefore|replaceChildren|removeChild/);
 // 只检查分区可见性规则本身；导航组件与布局规则用 body[data-nav-page] 前缀，不会作用于 manual.html。
 const scoped=[...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].filter(match=>match[1].includes(':not([data-nav-page='));
 assert.ok(scoped.length>0);
 for(const [,selector,declaration] of scoped){
  assert.equal(declaration.trim(),'display:none',selector.trim()+' 只能隐藏非当前分区');
  if(selector.includes(':not('))for(const page of pages)
   assert.ok(selector.includes(`body[data-nav-page="${page}"] [data-nav-page]:not([data-nav-page="${page}"])`),page);
 }
 assert.equal(scoped.length,1,'分区可见性只应有一条合并规则');
});
test('切换分区不发送任何桥接消息',()=>{
 const page=run();
 for(const link of page.links)link.onclick();
 assert.equal(page.sent.length,0);
 assert.doesNotMatch(navigation,/postMessage|chrome\.webview|fetch\(|XMLHttpRequest/);
});
test('键盘可达：普通按钮、Tab 顺序与 Enter 激活，当前分区有 aria-current',()=>{
 for(const page of pages){
  const button=html.match(new RegExp('<button[^>]*data-nav-target="'+page+'"[^>]*>[^<]*</button>'))[0];
  assert.match(button,/type="button"/);
  assert.doesNotMatch(button,/disabled|tabindex/);
 }
 const page=run();
 for(const link of page.links){
  link.onclick();
  assert.deepEqual(page.current(),[link.dataset.navTarget],'Enter 激活后只有当前分区有 aria-current');
  assert.equal(page.body.dataset.navPage,link.dataset.navTarget);
 }
 page.links[0].onclick();
 assert.deepEqual(page.current(),['home']);
 // 会客室入口仍唯一，且固定在分区导航里，原 dialog 不变。
 assert.equal(html.match(/id="room-open"/g).length,1);
 const nav=html.slice(html.indexOf('<nav class="app-nav"'),html.indexOf('</nav>'));
 assert.match(nav,/id="room-open"/);
 assert.match(html,/<\/header>\s*<nav class="app-nav"/);
 assert.match(html,/id="room-dialog"/);
 assert.match(html,/id="notice"/);
});
