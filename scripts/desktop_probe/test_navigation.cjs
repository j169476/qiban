// NAV-171 · R6.1 应用内分区导航，LAYOUT-172 · 分区内的分组标签与顶栏“应用设置”菜单。
// 只覆盖显示层：分区/标签归属、切换是否保留原节点与未保存输入、原 [hidden] 是否仍然优先、
// 是否额外调用桥接，以及键盘与 aria 状态。业务脚本的测试不在此文件内改动。
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const assets=path.join(__dirname,'Assets');
const html=fs.readFileSync(path.join(assets,'index.html'),'utf8');
const css=fs.readFileSync(path.join(assets,'probe.css'),'utf8');
const navigation=fs.readFileSync(path.join(assets,'navigation.js'),'utf8');
const pages=['home','learn','context','model','persona','platforms','records','media','settings'];
// 每个页面元素只归属一个分区。
const owners={
 'workspace-learn':'learn','workspace-context':'context',
 'profile-panel':'persona','profile-area':'persona','baseline-panel':'persona','relationship-panel':'persona',
 'memory-panel':'persona','sharing-panel':'persona','relation-claims-panel':'persona',
 'bili-panel':'platforms','qq-panel':'platforms','audience-panel':'platforms','growth-panel':'platforms',
 'model-panel':'model','reply-quota-panel':'model',
 'tasks-panel':'records','reply-trace-panel':'records','notification-panel':'home',
 'media-panel':'media','file-panel':'media','sticker-panel':'media','sticker-detail-panel':'media',
 'backup-panel':'settings','backup-rail-local':'settings','backup-rail-source':'settings','lock-panel':'settings','management-lock':'settings'
};
const tagOf=id=>{
 const found=html.match(new RegExp('<[a-z]+[^>]*\\bid="'+id.replace(/[-]/g,'\\-')+'"[^>]*>'));
 return found?found[0]:null;
};
const pageOf=tag=>(tag.match(/data-nav-page="([a-z]+)"/)||[])[1]||'';
const pairs=attribute=>[...html.matchAll(new RegExp(attribute+'="([a-z]+)"','g'))].map(match=>match[1]);
// 顶栏、分组标签条与外壳结构直接从 HTML 读出来，测试不重复写一份期望值。
const tabGroups=[];
for(const match of html.matchAll(/data-tab-group="([a-z]+)" data-tab-target="([a-z]+)"/g)){
 const group=match[1];
 if(!tabGroups.some(entry=>entry.name===group))tabGroups.push({name:group,targets:[]});
 tabGroups.find(entry=>entry.name===group).targets.push(match[2]);
}
const defaults={};
for(const group of tabGroups){
 const found=new RegExp('data-tab-'+group.name+'="([a-z]+)"').exec(html);
 defaults[group.name]=found?found[1]:'';
}
function element(extra={}){
 const attributes=new Map();
 const node={dataset:{},attributes,kids:[],focused:0,scrolled:0,onclick:null,
  setAttribute(name,value){attributes.set(name,String(value));},
  getAttribute(name){return attributes.has(name)?attributes.get(name):null;},
  removeAttribute(name){attributes.delete(name);},
  focus(){this.focused+=1;},
  scrollIntoView(){this.scrolled+=1;},
  contains(other){return other===this||this.kids.includes(other);},
  querySelector(){return this.control||null;},
  ...extra};
 for(const [name,value] of Object.entries(node.dataset))if(value!==undefined&&value!==null)node.dataset[name]=String(value);
 return node;
}
function run(){
 const body=element({dataset:{navPage:'home',...Object.fromEntries(tabGroups.map(group=>['tab'+group.name[0].toUpperCase()+group.name.slice(1),defaults[group.name]]))}});
 const sent=[],scrolled=[],events=[],listeners={keydown:[],click:[]};
 const links=pages.map(page=>{
  const node=element({dataset:{navTarget:page}});
  if(page==='home')node.attributes.set('aria-current','page');
  return node;
 });
 const tabs=[];
 for(const group of tabGroups)for(const target of group.targets){
  const node=element({dataset:{tabGroup:group.name,tabTarget:target}});
  node.attributes.set('aria-pressed',String(target===defaults[group.name]));
  tabs.push(node);
 }
 const jump=element({dataset:{jump:'management-lock'}});
 const toggle=element();toggle.attributes.set('aria-expanded','false');
 const focusable=element();
 const menu=element({kids:[jump]});
 const lockArea=element({control:focusable});
 const controls=new Map([['app-settings',toggle],['app-settings-menu',menu],['management-lock',lockArea]]);
 const documentListeners={};
 const document={body,
  querySelectorAll:selector=>selector==='[data-nav-target]'?links:selector==='[data-tab-target]'?tabs:selector==='[data-jump]'?[jump]:[],
  getElementById:id=>controls.get(id)||null,
  addEventListener:(name,fn,capture)=>{documentListeners[name]=documentListeners[name]||[];documentListeners[name].push({fn,capture:!!capture});}};
 const window={scrollTo:(x,y)=>scrolled.push([x,y]),
  dispatchEvent:event=>{events.push(event);return true;}};
 class CustomEvent{constructor(type,init){this.type=type;this.detail=init?init.detail:undefined;}}
 // 未保存输入与已锁住（原逻辑设为 hidden）的资料节点：显示层不应碰到它们。
 const draft={id:'persona-name',value:'未保存的资料文本'},locked={id:'memory-panel',hidden:true};
 vm.runInNewContext(navigation,{document,window,CustomEvent,
  chrome:{webview:{postMessage:message=>sent.push(message)}}});
 const fire=(name,event)=>{for(const entry of documentListeners[name]||[])entry.fn(event);};
 const current=()=>links.filter(link=>link.attributes.get('aria-current')==='page').map(link=>link.dataset.navTarget);
 const pressed=group=>tabs.filter(tab=>tab.dataset.tabGroup===group&&tab.attributes.get('aria-pressed')==='true').map(tab=>tab.dataset.tabTarget);
 return {body,links,tabs,jump,toggle,menu,lockArea,focusable,sent,scrolled,events,draft,locked,current,pressed,
  fire,tabOf:(group,target)=>tabs.find(tab=>tab.dataset.tabGroup===group&&tab.dataset.tabTarget===target),
  documentListeners};
}
test('导航列出 9 个分区，每个页面元素只归属一个分区且原 id 唯一',()=>{
 assert.deepEqual([...html.matchAll(/data-nav-target="([a-z]+)"/g)].map(match=>match[1]),pages);
 for(const [id,page] of Object.entries(owners)){
  const tag=tagOf(id);
  assert.ok(tag,'缺少页面元素 '+id);
  assert.equal(pageOf(tag),page,id+' 的分区不对');
  assert.equal(tag.match(/data-nav-page="/g).length,1,id+' 只能有一个分区标记');
 }
 assert.equal(pageOf(html.match(/<div class="probe-page-heading[^"]*"[^>]*>/)[0]),'home');
 // 顶栏与解锁区在任何分区都可达，诊断区归属设置。
 assert.equal(pageOf(tagOf('management-panel')),'');
 assert.equal(pageOf(tagOf('runtime-panel')),'');
 assert.equal(pageOf(tagOf('house-dashboard')),'home');
 assert.equal(pageOf(tagOf('diagnostics-panel')),'settings');
 assert.equal(pageOf(html.match(/<header class="probe-topbar">/)[0]),'');
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
 assert.equal(page.scrolled.at(-1)[0].top,0,'切换后回到页面主区');
 assert.equal(page.draft.value,'未保存的资料文本');
 assert.equal(page.locked.hidden,true);
 assert.equal(page.draft.id,'persona-name');
 assert.equal(page.links[3].onclick,handler,'切换不重建按钮和监听');
 assert.equal(page.links.length,pages.length);
 assert.equal(page.events.at(-1).type,'qiban-nav-change','切换后通知会客室按原 close/reset 关闭');
 assert.equal(page.events.at(-1).detail.page,'model');
});
test('原 hidden 仍然优先：导航不写 hidden 或 disabled，CSS 只隐藏非当前分区',()=>{
 assert.doesNotMatch(navigation,/\bhidden\s*=/);
 assert.doesNotMatch(navigation,/\bdisabled\s*=/);
 assert.doesNotMatch(navigation,/createElement|cloneNode|innerHTML|appendChild|insertBefore|replaceChildren|removeChild/);
 const scoped=[...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].filter(match=>match[1].includes(':not([data-nav-page='));
 assert.ok(scoped.length>0);
 for(const [,selector,declaration] of scoped){
  assert.equal(declaration.trim(),'display:none',selector.trim()+' 只能隐藏非当前分区');
  if(selector.includes(':not('))for(const page of pages)
   assert.ok(selector.includes(`body[data-nav-page="${page}"] [data-nav-page]:not([data-nav-page="${page}"])`),page);
 }
 assert.equal(scoped.length,1,'分区可见性只应有一条合并规则');
 // 分组标签用同一形态、另一组属性，同样只写 display:none。
 const paneAttributes=tabGroups.map(group=>'data-'+group.name+'-pane');
 const panes=[...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
  .filter(match=>paneAttributes.some(name=>match[1].includes(':not(['+name+'~=')));
 assert.ok(panes.length>0,'缺少分组标签的显示规则');
 for(const [,selector,declaration] of panes)assert.equal(declaration.trim(),'display:none',selector.trim());
 for(const group of tabGroups)for(const target of group.targets)
  assert.ok(panes.some(([,selector])=>selector.includes(`body[data-tab-${group.name}="${target}"]`)),group.name+':'+target);
});
test('切换分区不发送任何桥接消息',()=>{
 const page=run();
 for(const link of page.links)link.onclick();
 for(const tab of page.tabs)tab.onclick();
 assert.equal(page.sent.length,0);
 assert.doesNotMatch(navigation,/postMessage|chrome\.webview|fetch\(|XMLHttpRequest/);
});
test('键盘可达：普通按钮、Tab 顺序与 Enter 激活，当前分区有 aria-current',()=>{
 for(const page of pages){
  const button=html.match(new RegExp('<button[^>]*data-nav-target="'+page+'"[^>]*>'))[0];
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
 // 会客室入口仍唯一，且固定在分区导航里；会客室 dialog 仍在同一外壳中。
 assert.equal(html.match(/id="room-open"/g).length,1);
 const nav=html.slice(html.indexOf('<nav class="app-nav"'),html.indexOf('</nav>'));
 assert.match(nav,/id="room-open"/);
 assert.equal(html.match(/id="room-dialog"/g).length,1);
 assert.match(html,/id="notice"/);
});
test('V3 默认监控台使用固定左导航、真实窗景、运行控制及同一会客室',()=>{
 assert.match(html,/<\/header>\s*<div class="probe-shell">\s*<nav class="app-nav"/);
 const shell=html.slice(html.indexOf('<div class="probe-shell">'),html.indexOf('</main>'));
 assert.match(shell,/^<div class="probe-shell">\s*<nav class="app-nav"/);
 const center=shell.slice(shell.indexOf('<div class="probe-center">'));
 assert.match(center,/id="room-dialog"/);assert.match(center,/id="monitor-window"/);
 assert.match(center,/id="runtime-platforms"/);assert.match(center,/id="house-events"/);
 assert.match(shell,/class="house-nav-foot"[\s\S]*?data-nav-target="settings"/);
 const bar=html.slice(html.indexOf('<header class="probe-topbar">'),html.indexOf('</header>'));
 assert.match(bar,/id="house-monitor"/);assert.match(shell,/src="\/art\/hoodie-avatar\.webp"/);
 assert.match(bar,/id="app-settings"[^>]*aria-expanded="false"[^>]*aria-controls="app-settings-menu"/);
 assert.match(bar,/id="app-settings-menu"/);
 for(const label of ['应用密码锁','收至托盘','应用使用说明','退出应用'])assert.ok(bar.includes(label),label);
 assert.doesNotMatch(bar,/最小化|最大化|关闭窗口/);
 // 后台启停移进菜单，诊断区只留低频内容。
 assert.match(bar,/id="start"/);
 assert.match(bar,/id="stop"/);
 assert.match(bar,/id="service"/);
 assert.match(html.slice(html.indexOf('id="diagnostics-panel"')),/id="pick"/);
});
test('分组标签：默认值来自 HTML，点击只改 body 属性与 aria-pressed',()=>{
 const page=run();
 for(const group of tabGroups){
  assert.ok(group.targets.length>=2,group.name+' 至少两个标签');
  assert.ok(group.targets.includes(defaults[group.name]),group.name+' 的默认标签必须存在');
  assert.deepEqual(page.pressed(group.name),[defaults[group.name]]);
 }
 const persona=tabGroups.find(group=>group.name==='persona');
 assert.deepEqual(persona.targets,['profile','memory','relations','sharing']);
 const platforms=tabGroups.find(group=>group.name==='platforms');
 assert.deepEqual(platforms.targets,['qq','wechat','bilibili']);
 const backup=tabGroups.find(group=>group.name==='backup');
 assert.deepEqual(backup.targets,['local','portable','preserved']);
 const media=tabGroups.find(group=>group.name==='media');
 assert.deepEqual(media.targets,['attachments','library','stickers']);
 const records=tabGroups.find(group=>group.name==='records');
 assert.deepEqual(records.targets,['tasks','trace']);
 for(const tab of page.tabs){
  tab.onclick();
  assert.deepEqual(page.pressed(tab.dataset.tabGroup),[tab.dataset.tabTarget]);
  assert.equal(page.body.dataset['tab'+tab.dataset.tabGroup[0].toUpperCase()+tab.dataset.tabGroup.slice(1)],tab.dataset.tabTarget);
  assert.equal(page.draft.value,'未保存的资料文本','切标签不碰未保存输入');
  assert.equal(page.locked.hidden,true);
  assert.equal(page.sent.length,0);
 }
});
test('应用设置菜单：默认收起、Esc 收起并回到按钮、外部点击收起、应用密码锁跳到设置分区',()=>{
 const page=run();
 assert.equal(page.toggle.getAttribute('aria-expanded'),'false','菜单默认收起');
 assert.equal(page.documentListeners.keydown.length,1);
 assert.equal(page.documentListeners.click.length,2);
 page.toggle.onclick();
 assert.equal(page.toggle.getAttribute('aria-expanded'),'true');
 page.toggle.onclick();
 assert.equal(page.toggle.getAttribute('aria-expanded'),'false');
 // 菜单收起时 Esc 不拦截，会客室仍能拿到这次按键。
 let stopped=false;
 page.fire('keydown',{key:'Escape',stopPropagation(){stopped=true;}});
 assert.equal(stopped,false);
 page.toggle.onclick();
 stopped=false;
 page.fire('keydown',{key:'Escape',stopPropagation(){stopped=true;}});
 assert.equal(page.toggle.getAttribute('aria-expanded'),'false');
 assert.equal(stopped,true,'菜单开着时只收起菜单');
 assert.equal(page.toggle.focused,1,'Esc 后焦点回到“应用设置”按钮');
 // 外部点击收起；菜单内部的点击交给菜单项自己处理。
 page.toggle.onclick();
 page.fire('click',{target:element()});
 assert.equal(page.toggle.getAttribute('aria-expanded'),'false');
 page.toggle.onclick();
 page.fire('click',{target:page.menu});
 assert.equal(page.toggle.getAttribute('aria-expanded'),'true');
 // 应用密码锁：切到设置分区、收起菜单、滚到锁定区并聚焦第一个可用控件。
 page.jump.onclick();
 assert.equal(page.body.dataset.navPage,'settings');
 assert.equal(page.toggle.getAttribute('aria-expanded'),'false');
 assert.equal(page.lockArea.scrolled,1);
 assert.equal(page.focusable.focused,1);
 assert.equal(page.sent.length,0,'只做显示层跳转');
});
