"use strict";
(() => {
  if (!document.querySelector("#explicit-task")) return;
  let previous = "", pending = null, pendingBody = null, active = false;
  const names = {web:ui("查网页资料"), learn:ui("专题学习"), video:ui("视频补读"), image:ui("生成图片")};
  const states = {queued:ui("等待下一阶段"), running:ui("正在处理"), completed:ui("已完成"), cancelled:ui("已取消"), failed:ui("暂未完成")};
  const phases = {start:ui("等待开始"), researched:ui("已收集材料"), questions_ready:ui("已出题"), answered:ui("已盲答"), research_again:ui("按缺口补查"), assessed:ui("已完成自检"), reading:ui("继续补读"), read:ui("区间读取完成"), done:ui("处理完成")};
  const element = (tag, text, className) => {const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(className)node.className=className;return node;};
  function fields(){const selected=$("#task-kind").value;$("#task-learn-fields").hidden=selected!=="learn";$("#task-video-fields").hidden=selected!=="video";$("#task-criteria").required=selected==="learn";$("#task-bvid").required=selected==="video";}
  $("#task-kind").addEventListener("change",fields);fields();
  function link(url, title){try{const value=new URL(url);if(!["http:","https:"].includes(value.protocol)||value.username||value.password)return element("span", title);const node=element("a", title);node.href=value.href;node.target="_blank";node.rel="noopener noreferrer";return node;}catch{return element("span", title);}}
  function render(rows){
    const list=$("#task-list");list.replaceChildren();
    if(!rows.length){list.append(element("p",ui("暂无可显示的任务。"),"task-hint"));return;}
    for(const task of rows){
      const card=element("article",undefined,"task-item"), heading=element("div",undefined,"section-heading");
      heading.append(element("h3",names[task.kind]||uiCode("kind",task.kind)),element("span",states[task.status]||uiCode("state",task.status),"badge"));card.append(heading);
      card.append(element("p",task.arguments.prompt||task.arguments.objective||task.arguments.reason));
      card.append(element("p",(phases[task.phase]||uiCode("stage",task.phase))+(task.error?" · "+uiError(task.error):""),"task-hint"));
      if(task.error)uiTechnical(card,{phase:task.phase,error:task.error});
      const result=task.result, data=result?.payload;
      if(result?.summary)card.append(element("p",result.summary,"task-summary"));
      if(data?.notes)for(const note of data.notes)card.append(element("p",note,"task-summary"));
      if(data?.intervals)card.append(element("p",ui("已覆盖区间（秒）：")+data.intervals.map(v=>v.join("–")).join("、")+ui("；已读窗口：")+data.windows,"task-hint"));
      if(data?.url)card.append(link(data.url,ui("打开原视频 ↗")));
      if(data?.sources)for(const source of data.sources){const item=element("div",undefined,"task-source");item.append(link(source.url,source.title||source.url),element("p",source.note));if(source.segments)item.append(element("blockquote",source.segments.map(s=>typeof s==="string"?s:s.text||"").join("\n")));card.append(item);}
      if(task.kind==="image"&&task.status==="completed"){const image=element("img");image.src="/task-image/"+encodeURIComponent(task.id);image.alt=ui("本次任务生成的图片");image.loading="lazy";image.addEventListener("error",()=>{image.removeAttribute("src");image.alt=ui("图片未能加载，请检查会话或资料权限");});card.append(image);}
      if(data){const detail=element("details"), summary=element("summary",ui("完整阶段结果与依据"));detail.append(summary,element("pre",JSON.stringify(data,null,2)));card.append(detail);}
      if(["queued","running","failed","cancelled"].includes(task.status)){const cancel=["queued","running"].includes(task.status),button=element("button",cancel?ui("取消任务"):ui("显式重试"),"secondary");button.disabled=!cancel&&!active;button.addEventListener("click",async()=>{button.disabled=true;try{await api("/task-control",{id:task.id,action:cancel?"cancel":"retry",revision:task.revision});previous="";await refresh();}catch(error){notice(error.message);previous="";await refresh().catch(()=>{});}});card.append(button);}
      list.append(card);
    }
  }
  window.refreshTasks=async state=>{
    const newActive=state.status==="active"&&!state.read_only;if(active!==newActive)previous="";active=newActive;$("#task-fields").disabled=!active;
    try{const rows=await api("/tasks"), serial=JSON.stringify(rows);if(serial!==previous){render(rows);previous=serial;}}
    catch(error){$("#task-list").replaceChildren();previous="";throw error;}
  };
  bind("#tasks-refresh",async()=>{previous="";await refresh();});
  bind("#explicit-task",async()=>{
    const selected=$("#task-kind").value, prompt=$("#task-prompt").value.trim();let args={prompt};
    if(selected==="learn"){const criteria=$("#task-criteria").value.split("\n").map(s=>s.trim()).filter(Boolean);if(!criteria.length||criteria.length>6||criteria.some(s=>s.length>500))throw new Error(ui("请填写 1 至 6 项验收要点，每项不超过 500 字。"));args={objective:prompt,criteria,round_limit:Number($("#task-rounds").value)};}
    if(selected==="video")args={bvid:$("#task-bvid").value.trim(),page_index:Number($("#task-page").value)-1,start_seconds:Number($("#task-start").value),end_seconds:Number($("#task-end").value),mode:$("#task-mode").value,reason:prompt};
    const serial=JSON.stringify({kind:selected,arguments:args});if(serial!==pendingBody){pending=nonce();pendingBody=serial;}
    await api("/tasks",{kind:selected,arguments:args,nonce:pending});pending=null;pendingBody=null;notice(ui("任务已安排，可查看阶段结果或随时取消。"));previous="";await refresh();
  },"submit");
})();
