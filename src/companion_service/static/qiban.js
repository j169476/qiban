"use strict";
(() => {
  const authenticated = document.body.dataset.authenticated === "true";
  const admin = kind === "admin" && authenticated;
  const pages = {
    dashboard: [ui("我的栖伴"), ui("所有陪伴，在这里相遇。")],
    platforms: [ui("平台与会话"), ui("把每一段联系，照顾妥帖。")],
    memories: [ui("角色与记忆"), ui("珍藏经历，也尊重每一份隐私。")],
    tasks: [ui("任务手记"), ui("正在做的事，都有迹可循。")],
    usage: [ui("额度与用量"), ui("清楚每一份消耗，从容安排陪伴。")],
    settings: [ui("系统设置"), ui("为栖伴，布置一个安心的家。")]
  };
  const names = {local:ui("本机会客室"),bilibili:ui("哔哩哔哩"),wechat:ui("微信"),qq:"QQ"};
  const phases = {running:ui("进行中"),queued:ui("等待执行"),success:ui("完成"),completed:ui("已完成"),failed:ui("未完成"),cancelled:ui("已取消"),paused:ui("已暂停"),active:ui("运行中"),start:ui("准备开始"),research:ui("资料检索"),write:ui("整理内容")};
  const reasons = {explicit_start_required:ui("等待你开启陪伴"),service_restart:ui("服务已启动，等待明确开启"),host_stopped:ui("ChatGPT已停止运行，请重启服务"),platform_unavailable:ui("连接器尚未就绪"),chat_archived:ui("归属聊天已归档"),admin_web_stop:ui("已在管理页暂停"),explicit_stop:ui("已按指令停止"),duration_complete:ui("本次运行时间已结束"),explicit_service_resume:ui("服务已重启，请选择要启动的平台")};
  const format = value => typeof value === "number" && Number.isFinite(value) ? new Intl.NumberFormat(uiLocale).format(value) : "—";
  const time = value => typeof value === "number" ? new Date(value*1000).toLocaleString(uiLocale,{hour12:false}) : ui("未知");
  const text = (selector,value) => { const el=$(selector); if(el) el.textContent=value; };
  function element(tag, className, value) {const el=document.createElement(tag);if(className)el.className=className;if(value!==undefined)el.textContent=value;return el;}
  function icon(name) {
    const svg=document.createElementNS("http://www.w3.org/2000/svg","svg"),use=document.createElementNS("http://www.w3.org/2000/svg","use");
    svg.classList.add("icon");svg.setAttribute("aria-hidden","true");use.setAttribute("href","/static/icons.svg#"+name);svg.append(use);return svg;
  }
  function empty(container,title,message) {
    container.replaceChildren();const box=element("div","soft-empty");box.append(icon("moon"),element("h3","",title),element("p","",message));container.append(box);
  }
  function route(id) {
    if(!admin || !pages[id])return;
    document.querySelectorAll("[data-panel]").forEach(el=>{el.hidden=el.dataset.panel!==id;});
    document.querySelectorAll("[data-nav]").forEach(el=>{if(el.dataset.nav===id)el.setAttribute("aria-current","page");else el.removeAttribute("aria-current");});
    text("#page-title",pages[id][0]);text("#page-subtitle",pages[id][1]);document.title=pages[id][0]+ui(" · 栖伴 Qiban");
  }
  window.qibanNavigate=id=>{if(pages[id]){location.hash=id;route(id);}};
  if(admin){
    route(location.hash.slice(1) in pages ? location.hash.slice(1) : "dashboard");
    window.addEventListener("hashchange",()=>route(location.hash.slice(1)));
    document.querySelectorAll("[data-go]").forEach(button=>button.addEventListener("click",()=>window.qibanNavigate(button.dataset.go)));
  }
  let platformSignature="", reportData=null, loading=false;
  window.qibanSessionExpired=()=>{
    const banner=$("#host-banner");if(!banner)return;banner.hidden=false;banner.classList.remove("disconnected");
    text("#host-banner-title",ui("管理页登录已过期"));text("#host-banner-message",ui("请刷新页面并重新登录。登录过期不会停止后台平台，也不表示本机服务已断开。"));
    text("#status",ui("请重新登录"));$("#status")?.classList.remove("active");
    document.querySelectorAll("[data-runtime-start]").forEach(button=>button.disabled=true);platformSignature="";
  };
  window.qibanConnectionLost=()=>{
    const banner=$("#host-banner");if(!banner)return;banner.hidden=false;banner.classList.add("disconnected");
    text("#host-banner-title",ui("暂时无法连接本机服务"));text("#host-banner-message",ui("本机服务已断开，暂时无法确认宿主与任务状态，也无法保存配置。退出 Codex 可能同时结束后台；重新打开 Codex 后，请在管理聊天或命令行明确重启服务。"));
    text("#status",ui("连接中断"));$("#status")?.classList.remove("active");
    document.querySelectorAll("[data-runtime-start]").forEach(button=>button.disabled=true);
    const send=$("#message button[type=submit]");if(send)send.disabled=true;
    platformSignature="";
  };
  function hostBanner(state) {
    const host=state.host||{},banner=$("#host-banner");if(!banner)return;
    const stopped=host.available===false||host.restart_required;
    banner.hidden=!stopped;banner.classList.remove("disconnected");
    text("#host-banner-title",host.available===false?ui("ChatGPT已停止运行"):host.restart_required?ui("服务等待明确重启"):"");
    text("#host-banner-message",ui("平台运行保持禁用。请确认 Codex 已打开，再在管理聊天或命令行明确重启服务；恢复后仍需单独启动平台。"));
    text("#host-detail",stopped?ui("等待明确重启服务"):ui("Codex 宿主在线 · 本机服务可用"));
    if(stopped){text("#status",ui("等待重启"));$("#status")?.classList.remove("active");}
  }
  const disconnected=row=>row.status==="active"&&row.connection&&row.connection.state!=="connected";
  function connectionCopy(row){
    if(row.connection?.code==='client_restart_required')return [ui("需重启客户端"),ui("请正常退出并重开对应账号的客户端，然后重新启动平台。")];
    return ({launching:[uiCode("state","launching"),uiCode("state","launching")],waiting_login:[uiCode("state","waiting_login"),uiError("qq_login_required")],failed:[uiCode("state","failed"),uiError(row.connection?.code)],new:[ui("准备连接"),ui("平台已启动，正在准备首次连接。")],connecting:[ui("正在连接"),ui("正在建立平台连接，请稍候。")],retrying:[ui("等待重连"),ui("平台连接尚未恢复，请检查客户端登录与连接器。")],blocked:[ui("连接受阻"),ui("平台连接受阻，请检查登录、账号身份和连接器配置。")],uncertain:[ui("发送待核实"),ui("上次发送结果不确定，请先核实实际收件；系统不会自动补发。")]})[row.connection?.state]||[ui("连接待确认"),ui("尚未取得平台连接状态，请稍后查看。")];
  }
  function runtimeLabel(row){return !row.available?ui("未接入"):disconnected(row)?connectionCopy(row)[0]:row.status==="active"?(row.busy?ui("处理中"):ui("运行中")):ui("已暂停");}
  function runtimeDescription(row){return !row.available?ui("完成连接器配置后，即可在这里管理。"):disconnected(row)?connectionCopy(row)[1]:reasons[row.reason]||((row.reason?.startsWith("public_growth_")||row.reason?.startsWith("qq_"))?uiError(row.reason):null)||(row.status==="active"?ui("陪伴正在继续。"):ui("等待你的下一次启动。"));}
  function connectorLink(card,row){
    if(row.platform!=='qq')return;
    const link=card.querySelector('[data-connector-link]'),url=localConnectorURL(row.management_url);
    if(link){link.hidden=!url;if(url)link.href=url;else link.removeAttribute('href');}
  }
  function runtimeCards(state) {
    const list=[{platform:"local",available:true,...state},...(state.platforms||[])];
    const signature=JSON.stringify(list.map(p=>[p.platform,p.available,p.status,p.reason,p.owner,p.owner_title,p.generation,p.busy,p.connection,p.fault,p.management_url,state.host]));
    if(signature===platformSignature)return;platformSignature=signature;
    const grid=$("#platform-grid");if(!grid)return;
    // Keep edited options through background refreshes; never replace controls
    // while a user is focusing them.
    if(grid.contains(document.activeElement)){
      for(const row of list){const card=grid.querySelector(`[data-platform="${row.platform}"]`);if(!card)continue;const badge=card.querySelector('.badge');badge.textContent=runtimeLabel(row);badge.classList.toggle('active',row.status==='active'&&!disconnected(row));card.querySelector('.platform-description').textContent=runtimeDescription(row);card.querySelector('[data-runtime-start]').disabled=!row.available||row.status==='active'||state.host?.available===false||state.host?.restart_required===true;card.querySelector('[data-runtime-stop]').disabled=row.status!=='active';connectorLink(card,row);}
      if(state.host?.available===false||state.host?.restart_required)grid.querySelectorAll("[data-runtime-start]").forEach(button=>button.disabled=true);
      platformSignature="";return;
    }
    const saved=new Map([...grid.querySelectorAll(".platform-card")].map(card=>[card.dataset.platform,{duration:card.querySelector("input[type=number]")?.value,readonly:card.querySelector("input[type=checkbox]")?.checked}]));
    grid.replaceChildren();
    for(const row of list){
      const card=element("article","platform-card"),head=element("div","section-heading"),title=element("h3"),mark=element("span","platform-mark");card.dataset.platform=row.platform;
      mark.append(icon(row.platform==="local"?"cup":"platform"));title.append(mark,document.createTextNode(names[row.platform]));
      const badge=element("span","badge"+(row.status==="active"&&!disconnected(row)?" active":""),runtimeLabel(row));head.append(title,badge);
      card.append(head,element("p","platform-description",runtimeDescription(row)));if(row.connection?.code||row.reason?.startsWith("public_growth_"))uiTechnical(card,{code:row.connection?.code||row.reason,...(row.fault||{})});
      if(row.fault?.stage)card.append(element("p","task-hint",uiCode("stage",row.fault.stage)));
      if(row.owner){const owner=element('p','owner',ui("归属聊天 · ")+(row.owner_title||ui("聊天名称暂不可用")));owner.append(element('span','object-uid',' ID '+row.owner));card.append(owner);}
      if(row.reply_test&&row.status==='active')card.append(element('p','task-hint',ui("限定私信验收：{0} → {1}；仅新私信，最多一次外发尝试。",row.reply_test.account_uid,row.reply_test.target_uid)));
      if(row.reason==='reply_test_complete')card.append(element('p','task-hint',ui("本次限定私信验收已结束，请在任务记录核对发送结果。")));
      const options=element("div","platform-options"),durationLabel=element("label","",ui("本次时长（分钟，留空不限）")),duration=element("input"),readLabel=element("label"),readOnly=element("input");
      duration.type="number";duration.min="1";duration.max="1440";duration.placeholder=ui("不限");duration.value=saved.get(row.platform)?.duration||"";durationLabel.append(duration);
      readOnly.type="checkbox";readOnly.checked=saved.get(row.platform)?.readonly||false;readLabel.append(readOnly,document.createTextNode(ui("只读运行")));options.append(durationLabel,readLabel);
      const actions=element("div","actions"),start=element("button"),stop=element("button","secondary");
      start.type=stop.type="button";start.dataset.runtimeStart="true";stop.dataset.runtimeStop="true";start.append(icon("play"),document.createTextNode(ui("启动")));stop.append(icon("pause"),document.createTextNode(ui("暂停")));
      const unavailable=!row.available||state.host?.available===false||state.host?.restart_required;
      start.disabled=unavailable||row.status==="active";stop.disabled=row.status!=="active";duration.disabled=readOnly.disabled=row.status==="active";
      for(const [button,action] of [[start,"start"],[stop,"stop"]])button.addEventListener("click",async()=>{
        if(action==="start"&&!duration.checkValidity()){duration.reportValidity();return;}
        button.disabled=true;try{
          const args=action==="start"?{read_only:readOnly.checked,...(row.platform==="local"?{background:false}:{}),...(duration.value?{duration_minutes:Number(duration.value)}:{})}:{};
          await operate("platform_control",{platform:row.platform,action,expected_generation:row.generation??null,options:args});
          platformSignature="";notice(names[row.platform]+(action==="start"?ui("已启动。"):ui("已暂停。")));await refresh();
        }catch(e){notice(e.message);button.disabled=false;}
      });
      actions.append(start,stop);
      if(row.platform==='qq'){
        const link=element('a','button secondary connector-link',ui("打开 QQ 连接器 ↗"));link.dataset.connectorLink='true';link.target='_blank';link.rel='noopener noreferrer';link.setAttribute('aria-label',ui("打开 QQ 连接器管理页（新窗口）"));actions.append(link);
      }
      card.append(options,actions);connectorLink(card,row);grid.append(card);
    }
  }
  function activity(state){
    const list=[{platform:"local",...state},...(state.platforms||[])],active=list.filter(x=>x.status==="active"),offline=active.filter(disconnected),busy=active.filter(x=>x.busy&&!disconnected(x));
    text("#metric-platforms",active.length+" / 4");text("#metric-replies",format(state.quotas?.local?.remaining));
    text("#metric-replies-note",ui("已用 ")+format(state.quotas?.local?.used)+ui(" / 上限 ")+format(state.quotas?.local?.limit));
    const box=$("#activity-summary");if(!box)return;
    if(busy.length){box.replaceChildren(icon("spark"),element("h3","",busy.map(x=>names[x.platform]).join("、")+ui("正在处理任务")),element("p","",ui("执行状态会自动更新，关闭此页面不会中断任务。")));}
    else if(offline.length){const preparing=offline.every(x=>['new','connecting'].includes(x.connection.state));box.replaceChildren(icon("moon"),element("h3","",preparing?ui("正在准备平台连接"):ui("部分平台连接需要检查")),element("p","",offline.map(x=>names[x.platform]+"："+connectionCopy(x)[0]).join("；")+ui("。请查看平台卡片中的具体提示。")));}
    else if(active.length){box.replaceChildren(icon("cup"),element("h3","",ui("陪伴已就绪")),element("p","",active.map(x=>names[x.platform]).join("、")+ui("正在等待下一次交流。")));}
    else empty(box,ui("休息一下，也很好"),state.host?.restart_required?ui("请从管理聊天或命令行重启服务。"):ui("所有平台已暂停。准备好了，再开启陪伴。"));
  }
  window.renderQibanState=state=>{hostBanner(state);if(admin){runtimeCards(state);activity(state);}};
  function quotaTile(label,used,limit,remaining,note){
    const tile=element("article","quota-tile"),values=element("div","quota-value");tile.append(element("h3","",label));values.append(element("strong","",format(remaining)),element("span","",ui("剩余 / 上限 ")+format(limit)));tile.append(values);
    if(typeof used==="number"&&typeof limit==="number"){const progress=element("progress");progress.max=Math.max(1,limit);progress.value=Math.min(progress.max,used);progress.setAttribute("aria-label",label+ui("已用 ")+used+" / "+limit);tile.append(progress);}
    tile.append(element("p","",note||ui("已用 ")+format(used)+ui(" · 按设定时区每日计数")));return tile;
  }
  function renderReport(data){
    reportData=data;const quotas=$("#usage-quotas");quotas.replaceChildren(...data.quotas.map(q=>quotaTile(ui(q.label),q.used,q.limit,q.remaining)));
    text("#quota-timezone",data.timezone+ui(" · 每日计数"));
    const plan=data.coding_plan||{},windows=$("#plan-windows");windows.replaceChildren();
    text("#plan-freshness",plan.status!=="available"?ui("暂不可用")+(plan.updated_at?ui(" · 保留上次快照"):""):plan.stale?ui("数据已过期"):ui("同步于 ")+time(plan.updated_at));
    for(const bucket of plan.buckets||[]){for(const win of bucket.windows||[]){
      const hours=win.window_minutes===null?ui("未知周期"):win.window_minutes>=1440?format(win.window_minutes/1440)+ui(" 天"):win.window_minutes>=60?format(win.window_minutes/60)+ui(" 小时"):format(win.window_minutes)+ui(" 分钟");
      const tile=quotaTile(bucket.name+" · "+hours,win.used_percent,100,win.remaining_percent,ui("已用 ")+format(win.used_percent)+ui("% · 重置于 ")+time(win.resets_at));
      const strong=tile.querySelector("strong");strong.textContent=win.remaining_percent===null?ui("未知"):format(win.remaining_percent)+"%";windows.append(tile);
    }}
    if(!windows.children.length)empty(windows,ui("暂时没有额度数据"),ui("请确认 Codex 登录与宿主连接；无法读取时不将余额视为零。"));
    const bucket=plan.buckets?.find(b=>b.id==="codex")||plan.buckets?.[0],primary=bucket?.windows?.[0];
    text("#metric-plan",primary?.remaining_percent!=null?format(primary.remaining_percent)+"%":"—");
    text("#metric-plan-note",plan.status!=="available"||plan.stale?ui("额度快照暂不可用或已过期"):ui("账户共享 · 已用 ")+format(primary?.used_percent)+"%");
    const tokens=data.tokens||{},counters=$("#token-counters");counters.replaceChildren();
    for(const [key,label] of [["input_tokens",ui("输入词元")],["cached_input_tokens",ui("其中：缓存输入")],["output_tokens",ui("输出词元")],["reasoning_output_tokens",ui("其中：推理输出")]]){
      const metric=element("article","metric");metric.append(element("span","",label),element("strong","",tokens[key]===null?ui("未知"):format(tokens[key])),element("small","",format(data.token_known_calls[key])+ui(" 次调用已上报")));counters.append(metric);
    }
    text("#metric-tokens",typeof tokens.input_tokens==="number"&&typeof tokens.output_tokens==="number"?format(tokens.input_tokens+tokens.output_tokens):"—");
    text("#metric-tokens-note",ui("最近 ")+data.days+ui(" 天 · Qiban"));
    text("#token-coverage",ui("最近 ")+data.days+ui(" 天共记录 ")+data.model_calls+ui(" 次模型调用。")+(data.write_failures?ui("本次服务有 ")+data.write_failures+ui(" 次观测写入失败，统计可能不完整。"):""));
    const rows=$("#observation-rows");rows.replaceChildren();for(const item of data.recent){const tr=element("tr");for(const value of [new Date(item.started).toLocaleString(uiLocale,{hour12:false}),uiCode("stage",item.stage),item.status==="running"?ui("未记录结束"):phases[item.status]||uiCode("state",item.status),item.duration_ms===null?"—":format(item.duration_ms)+" ms",format(item.input_tokens)+" / "+format(item.output_tokens)])tr.append(element("td","",value));tr.children[1].title=item.stage;uiTechnical(tr.children[1],item.stage);rows.append(tr);}
    const tasks=$("#runtime-tasks");tasks.replaceChildren();
    if(!data.tasks.length)empty(tasks,ui("暂时没有任务手记"),ui("在会客室提交任务后，这里会显示它的执行进度。"));
    for(const task of data.tasks){const row=element("div","runtime-task"),description=element("div");description.append(element("strong","",(phases[task.status]||uiCode("state",task.status))+" · "+(phases[task.phase]||uiCode("stage",task.phase))),element("p","object-uid",task.id),element("p","",task.error?uiError(task.error):""));row.append(description);
      if(["queued","running"].includes(task.status)){const cancel=element("button","quiet",ui("取消任务"));cancel.type="button";cancel.addEventListener("click",async()=>{cancel.disabled=true;try{await operate("task_cancel",{id:task.id,expected_revision:task.revision});await refreshMonitor();notice(ui("任务已取消。"));}catch(e){notice(e.message);cancel.disabled=false;}});row.append(cancel);}tasks.append(row);
    }
  }
  async function refreshMonitor(){
    if(!admin||loading)return;loading=true;
    try{renderReport(await operate("runtime_monitor",{days:Number($("#monitor-days").value)}));}
    catch(error){notice(ui("运行监控暂未更新：")+error.message);if(!reportData){text("#plan-freshness",ui("暂不可用"));}}
    finally{loading=false;}
  }
  async function replyUsage(){
    try{const data=await operate("reply_policy_list"),container=$("#reply-usage");container.replaceChildren();
      for(const item of [...(data.contacts||[]),...(data.groups||[])]){
        const group=item.scope.kind==='group',used=group?item.used:item.usage.confirmed+item.usage.reserved+item.usage.uncertain;
        const limit=group?item.settings.group_session_replies:item.settings.private_daily_replies;
        const label=item.label&&item.label!==item.scope.target_id?item.label:(group?ui("未命名群聊"):ui("未命名联系人"));
        const note=group?(item.active?ui("本段参与"):ui("上一段参与 / 尚未唤起"))+ui(" · 已决策 ")+format(item.decisions):ui("成功 ")+item.usage.confirmed+ui(" · 处理中 ")+item.usage.reserved+ui(" · 结果不确定 ")+item.usage.uncertain;
        const tile=quotaTile(names[item.platform]+' · '+label,used,limit,group&&!item.active?null:Math.max(0,limit-used),note);
        tile.classList.add('object-quota');tile.dataset.scopeId=item.scope_id;
        const meta=element('div','object-meta');meta.append(element('span','object-uid','UID '+item.scope.target_id),element('span','',item.inherited?ui("使用账号默认值"):ui("独立额度")));
        const gear=element('button','quiet quota-gear');gear.type='button';gear.setAttribute('aria-label',ui("调整 ")+label+ui(" 的额度"));gear.append(icon('settings'));
        gear.addEventListener('click',()=>editObjectQuota(item,data.bounds,gear));tile.append(meta,gear);container.append(tile);
      }
    }catch(error){text("#reply-usage",ui("平台私聊用量暂不可用：")+error.message);}
  }
  window.qibanRefreshReplyUsage=replyUsage;
  function editObjectQuota(item,bounds,opener){
    const dialog=element('dialog','quota-dialog'),form=element('form'),heading=element('h2','',ui("调整对象额度"));
    heading.id='quota-dialog-title';dialog.setAttribute('aria-labelledby',heading.id);
    form.append(heading,element('p','',item.label&&item.label!==item.scope.target_id?item.label:ui("未命名对象")),element('p','object-uid','UID '+item.scope.target_id));
    const labels={private_daily_replies:ui("每日回复上限"),group_session_replies:ui("每段回复上限"),group_session_decisions:ui("每段决策上限"),group_session_minutes:ui("参与时长（分钟）")},inputs={};
    for(const [key,value] of Object.entries(item.settings)){const label=element('label','',labels[key]),input=element('input');input.type='number';input.required=true;input.step='1';[input.min,input.max]=bounds[key];input.value=value;inputs[key]=input;label.append(input);form.append(label);}
    const feedback=element('p','form-error');feedback.setAttribute('role','alert');
    const actions=element('div','actions'),save=element('button','',ui("保存")),reset=element('button','secondary',ui("恢复账号默认")),cancel=element('button','quiet',ui("取消"));save.type='submit';reset.type=cancel.type='button';actions.append(save,reset,cancel);form.append(feedback,actions);dialog.append(form);document.body.append(dialog);
    let saving=false;
    const commit=async restore=>{if(saving)return;saving=true;save.disabled=reset.disabled=true;try{
      await operate(restore?'reply_policy_object_reset':'reply_policy_object_set',{scope_id:item.scope_id,expected_version:item.version,...(restore?{}:{changes:Object.fromEntries(Object.entries(inputs).map(([key,input])=>[key,Number(input.value)]))})});
      dialog.close();await replyUsage();notice(ui("额度已保存并立即生效。"));
    }catch(error){feedback.textContent=error.message;}finally{saving=false;save.disabled=reset.disabled=false;}};
    form.addEventListener('submit',event=>{event.preventDefault();if(form.reportValidity())commit(false);});reset.addEventListener('click',()=>commit(true));cancel.addEventListener('click',()=>dialog.close());
    dialog.addEventListener('close',()=>{dialog.remove();if(opener.isConnected)opener.focus();});dialog.showModal();
  }
  if(admin){
    let testStarting=false;
    bind('#bili-test-form',async()=>{
      if(testStarting)return;
      const form=$('#bili-test-form');if(!form.reportValidity())return;
      testStarting=true;$('#bili-test-start').disabled=true;
      try{
        const state=await api('/status'),row=state.platforms?.find(p=>p.platform==='bilibili');
        if(!row?.available)throw new Error(ui("B站连接器尚未就绪"));
        if(row.status==='active')throw new Error(ui("请先暂停B站，再启动限定私信验收"));
        await operate('platform_control',{platform:'bilibili',action:'start',expected_generation:row.generation??null,
          options:{duration_minutes:Number($('#bili-test-duration').value),reply_test:{
            account_uid:$('#bili-test-account').value.trim(),target_uid:$('#bili-test-target').value.trim()}}});
        notice(ui("限定私信验收已启动：请由指定对象发送新的编号消息。"));await refresh();
      }finally{testStarting=false;$('#bili-test-start').disabled=false;}
    },'submit');
    bind("#open-codex",async()=>{await operate("open_codex",{});notice(ui("已请求打开 Codex。请在管理聊天选择 @qiban，明确发出所需指令。"));});
    bind("#copy-qiban",async()=>{await navigator.clipboard.writeText(ui("$qiban 打开栖伴管理页，查看运行状态和额度。"));notice(ui("管理指令已复制，请在 Codex 聊天中粘贴。"));});
    bind("#monitor-refresh",async()=>{await refreshMonitor();await replyUsage();});bind("#tasks-refresh",refreshMonitor);bind("#monitor-days",refreshMonitor,"change");
    refreshMonitor();replyUsage();let round=0;const poll=async()=>{if(!document.hidden){await refreshMonitor();if(++round%4===0)await replyUsage();}setTimeout(poll,15000);};setTimeout(poll,15000);
  }
})();
