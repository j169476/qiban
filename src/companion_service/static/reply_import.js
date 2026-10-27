"use strict";
(() => {
  if(kind!=='admin'||document.body.dataset.authenticated!=='true')return;
  let preview=null,request=0;
  function clear(){request++;preview=null;$('#reply-import-confirm').hidden=true;$('#reply-import-preview').replaceChildren();$('#reply-import-summary').textContent=ui("名单或平台已改变，请重新预览。");}
  $('#reply-search-platform')?.addEventListener('change',clear);
  $('#reply-import-identifiers')?.addEventListener('input',clear);
  bind('#reply-import-form',async()=>{
    clear();const current=request;
    const identifiers=$('#reply-import-identifiers').value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean);
    if(!identifiers.length||identifiers.length>200)throw new Error(ui("每批请输入 1–200 个 ID，可分批追加。"));
    const value=await operate('reply_import_preview',{platform:$('#reply-search-platform').value,identifiers});
    if(current!==request)return;preview=value;
    const list=$('#reply-import-preview');list.replaceChildren();
    const statuses={ready:ui("待授权"),already_authorized:ui("已授权，保持原状"),unverified:ui("尚未核验，请先配置连接器"),ambiguous:ui("匹配多个账号或会话，请在管理聊天指定账号后重试")};
    for(const item of value.items){const row=document.createElement('p');row.textContent=`${item.identifier} · ${statuses[item.status]}${item.account_label?' · '+item.account_label:''}`;list.append(row);}
    $('#reply-import-summary').textContent=ui("共 {0} 个对象，去除 {1} 个重复项。{2} 当前权限未改变。",value.items.length,value.duplicates_removed,value.confirmable?ui("预览十分钟内有效，确认后追加授权。"):ui("请修正未核验或有歧义的对象，再重新预览。"));
    $('#reply-import-confirm').hidden=!value.confirmable;
  },'submit');
  bind('#reply-import-confirm',async()=>{
    if(!preview?.confirmable)return;const current=request;
    const result=await operate('reply_import_confirm',{token:preview.token});
    if(current!==request)return;clear();
    $('#reply-import-summary').textContent=ui("已追加 {0} 项，保留 {1} 项已有授权。{2}",result.added,result.already_authorized,result.platform_paused?ui("对应平台已暂停，需明确重新启动。"):ui("没有启动角色。"));
  });
})();
