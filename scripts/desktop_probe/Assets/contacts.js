(()=>{
 'use strict';
 const filter=(rows,query,kind)=>{const q=query.trim().toLocaleLowerCase();return rows.filter(r=>(!kind||r.kind===kind)&&[r.name,r.nickname,r.alias,r.id].some(v=>String(v||'').toLocaleLowerCase().includes(q)));};
 class Picker{
  constructor(root,{selected,toggle,sync}){
   this.rows=[];this.page=1;this.busy=false;this.allowed=false;this.selected=selected;this.toggle=toggle;
   root.classList.add('contact-picker');
   const make=(tag,text,cls)=>{const n=document.createElement(tag);if(text)n.textContent=text;if(cls)n.className=cls;return n;};
   this.search=make('input');this.search.type='search';this.search.placeholder='搜索昵称、备注或账号';this.search.setAttribute('aria-label','搜索联系人');
   this.kind=make('select');this.kind.setAttribute('aria-label','联系人类型');
   for(const [value,title] of [['','全部'],['private','好友'],['group','群']]){const o=make('option',title);o.value=value;this.kind.append(o);}
   this.sync=make('button','同步名单','secondary');this.sync.onclick=sync;
   const bar=make('div',null,'contact-tools');bar.append(this.search,this.kind,this.sync);
   this.status=make('p',null,'inline-status');this.status.setAttribute('role','status');
   this.list=make('ul',null,'contact-list');this.previous=make('button','上一页','secondary');this.next=make('button','下一页','secondary');
   this.count=make('span');this.count.setAttribute('role','status');this.jump=make('input');this.jump.type='number';this.jump.min='1';this.jump.setAttribute('aria-label','跳至页码');
   this.go=make('button','跳转','secondary');const pager=make('div',null,'directory-pager');pager.append(this.previous,this.count,this.next,this.jump,this.go);
   this.selectedOnly=make('input');this.selectedOnly.type='checkbox';const label=make('label',null,'probe-check');label.append(this.selectedOnly,'只看已选');
   root.append(bar,label,this.status,this.list,pager);
   const first=()=>{this.page=1;this.render();};this.search.oninput=this.kind.onchange=this.selectedOnly.onchange=first;
   this.previous.onclick=()=>{this.page--;this.render();};this.next.onclick=()=>{this.page++;this.render();};
   this.go.onclick=()=>{const p=Number(this.jump.value);if(Number.isInteger(p)){this.page=p;this.render();}};
   this.render();
  }
  setRows(rows){this.rows=rows;this.render();}
  setState(allowed,busy){this.allowed=allowed;this.busy=busy;this.render();}
  render(){
   let matches=filter(this.rows,this.search.value||'',this.kind.value||'');if(this.selectedOnly.checked)matches=matches.filter(this.selected);
   const pages=Math.max(1,Math.ceil(matches.length/10));this.page=Math.max(1,Math.min(pages,this.page));
   this.list.replaceChildren();
   for(const row of matches.slice((this.page-1)*10,this.page*10)){
    const li=document.createElement('li'),label=document.createElement('label'),box=document.createElement('input'),text=document.createElement('span'),name=document.createElement('strong'),detail=document.createElement('small');
    box.type='checkbox';box.checked=this.selected(row);box.disabled=!this.allowed||this.busy;
    name.textContent=row.name||row.nickname||row.alias||row.id;
    detail.textContent=[row.alias&&'微信号：'+row.alias,row.nickname&&row.nickname!==row.name&&'昵称：'+row.nickname,row.id].filter(Boolean).join(' · ');
    text.append(name,detail);label.append(box,text);li.append(label);this.list.append(li);
    box.onchange=()=>{this.toggle(row,box.checked);this.render();};
   }
   this.count.textContent=`第 ${this.page} / ${pages} 页 · ${matches.length} 条`;this.jump.max=String(pages);this.jump.value=String(this.page);
   for(const n of [this.sync,this.search,this.kind,this.selectedOnly,this.previous,this.next,this.jump,this.go])n.disabled=!this.allowed||this.busy;
   this.previous.disabled ||= this.page<=1;this.next.disabled ||= this.page>=pages;
   window.QibanFeedback?.mark(this.sync,this.busy);
  }
 }
 // Entry is an edge, not a recurring state notification or a search event.
 function onEntry(platform,ready,job){
  let entered=false;
  const check=()=>{const visible=ready()&&document.body.dataset.navPage==='platforms'&&document.body.dataset.tabPlatforms===platform;
   if(visible&&!entered){entered=true;job();}else if(!visible)entered=false;};
  for(const name of ['qiban-nav-change','qiban-tab-change','qiban-management-state'])window.addEventListener(name,()=>queueMicrotask(check));
 }
 window.QibanContacts={Picker,filter,onEntry};
})();
