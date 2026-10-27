"use strict";
(() => {
  if(!document.querySelector('#growth-preview'))return;
  let token=null;
  bind('#growth-preview',async()=>{
    token=null;$('#growth-rebuild').hidden=true;$('#growth-preview-content').hidden=true;
    const preview=await operate('public_growth_preview');
    token=preview.token;$('#growth-status').textContent=preview.reason==='readable'?ui('当前成长状态可读取，无需恢复。'):uiError(preview.reason);
    $('#growth-data').textContent=JSON.stringify(preview.data,null,2);
    $('#growth-preview-content').hidden=preview.reason==='readable';$('#growth-rebuild').hidden=preview.reason==='readable';
  });
  bind('#growth-rebuild',async()=>{
    if(!token)return;
    if(!confirm(ui('确认以预览中的中性内容重建公开成长？旧状态将归档，B站不会自动启动。')))return;
    const current=token;token=null;$('#growth-rebuild').hidden=true;
    const result=await operate('public_growth_rebuild',{token:current});
    $('#growth-preview-content').hidden=true;$('#growth-status').textContent=ui('公开成长已重建，旧状态已归档。请核对后明确启动 B站。');
    uiTechnical($('#growth-status'),{archive:result.archive});await refresh();
  });
})();
