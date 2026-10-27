"use strict";
(() => {
  const valid = value => value === 'zh' || value === 'en';
  const cookie = document.cookie.split('; ').find(value => value.startsWith('qiban_language='))?.split('=')[1];
  let saved;
  try { saved = localStorage.getItem('qiban_language'); } catch {}
  const browser = (navigator.languages || [navigator.language]).map(value => value.toLowerCase().split('-')[0]).find(valid);
  const language = valid(cookie) ? cookie : valid(saved) ? saved : browser || 'en';
  window.uiLanguage = language;
  window.uiLocale = language === 'zh' ? 'zh-CN' : 'en-US';
  document.documentElement.lang = window.uiLocale;
  window.ui = (source, ...values) => {
    const template = language === 'en' ? window.QIBAN_EN[source] ?? source : source;
    return template.replace(/\{(\d+)\}/g, (match, index) => index < values.length ? String(values[index]) : match);
  };
  const pairs = {
    state: {
      active:['运行中','Active'], paused:['已暂停','Paused'], running:['进行中','Running'], queued:['等待处理','Queued'],
      completed:['处理完成','Completed'], success:['成功','Succeeded'], failed:['未完成','Failed'], cancelled:['已取消','Cancelled'],
      confirmed:['已确认发送','Delivery confirmed'], uncertain:['结果待核实','Result unconfirmed'], reserved:['处理中','In progress'],
      connected:['已连接','Connected'], connecting:['正在连接','Connecting'], launching:['正在启动连接器','Starting connector'],
      waiting_login:['等待登录','Waiting for login'], stopped:['已停止','Stopped'], closed:['已结束','Ended'], sent:['已发送','Sent'],
      retrying:['等待重连','Reconnecting'], blocked:['连接受阻','Connection blocked'], interrupted:['已中断','Interrupted']
    },
    stage: {
      unclassified:['未标注阶段','Unlabelled stage'], 'routing.recall.model.semantic_expansion':['回忆检索词扩展记录','Recall query expansion record'],
      'recall.expand':['生成回忆检索词','Generating recall queries'], 'reply.generate':['生成回复','Generating reply'],
      selection_context:['准备选择上下文','Preparing selection context'], select:['选择内容','Selecting content'],
      receive:['接收消息','Receiving message'], route:['分配消息','Routing message'], recall:['检索记忆','Retrieving memories'],
      model:['模型处理','Model processing'], send:['发送回复','Sending reply'], done:['处理完成','Completed'],
      start:['准备开始','Preparing'], research:['检索资料','Researching'], write:['整理内容','Writing'],
      python_create:['创建 Python 环境','Creating Python environment'], dependencies_check:['检查依赖','Checking dependencies'],
      dependencies_install:['安装依赖','Installing dependencies'], native_check:['检查原生桥接','Checking native bridge'],
      native_build:['构建原生桥接','Building native bridge'], native_verify:['校验原生桥接','Verifying native bridge']
    },
    kind: {persona:['人物卡','Persona'], memories:['记忆','Memories'], experiences:['经历','Experiences'], relationships:['关系','Relationships'],
      knowledge:['知识','Knowledge'],diaries:['日记','Diaries'],skills:['技能','Skills'],interest_signals:['兴趣线索','Interest signals'],identity_links:['身份关联','Identity links'],
      growth:['成长','Growth'], policy:['运行设置','Runtime settings'], companion:['陪伴设置','Companion settings'], private:['私聊','Direct message'], group:['群聊','Group'],
      web:['网页阅读','Web research'], learn:['专题学习','Learning'], video:['视频阅读','Video reading'], image:['图片生成','Image generation'],
      local_reply:['本机回复','Local reply'], reply:['回复','Reply'], comment:['评论','Comment'], dm_reply:['私信回复','Direct-message reply']},
    classification: {unclassified:['待审核','Pending review'], public:['公开','Public'], private:['私密','Private'], personal:['个人资料','Personal'], conversation:['会话范围','Conversation'], secret:['机密','Secret'], restricted:['限定范围','Restricted']},
    platform: {local:['本机','Local'], bilibili:['哔哩哔哩','Bilibili'], wechat:['微信','WeChat'], qq:['QQ','QQ']},
    effort: {none:['无','None'], minimal:['最低','Minimal'], low:['低','Low'], medium:['中','Medium'], high:['高','High'], xhigh:['很高','Extra high'], max:['最高','Maximum'], ultra:['极高','Ultra']}
  };
  const errors = {
    access_denied:['没有执行此操作的权限。','You do not have permission for this action.'],
    invalid_arguments:['请检查填写的内容。','Check the entered values.'], invalid_body:['请求内容无效。','The request is invalid.'],
    version_conflict:['设置已被修改，请重新读取后再保存。','Settings changed. Reload them before saving.'],
    edit_conflict:['内容或设置已变化，请重新读取后再保存。','Content or settings changed. Reload before saving.'],
    worker_busy:['请先暂停相应平台，并等待当前任务退出。','Pause the affected platform and wait for current work to finish.'],
    invalid_operation:['操作参数不完整或无效，请检查后重试。','The action parameters are incomplete or invalid. Check them and try again.'],
    state_conflict:['当前状态已变化，请刷新后重试。','The current state changed. Refresh and try again.'],
    authentication_required:['请重新登录。','Please sign in again.'], session_expired:['登录已过期，请重新登录。','Your session expired. Please sign in again.'],
    quota_exhausted:['当前额度已用尽。','The current quota is exhausted.'],
    reply_quota_migration_required:['历史额度账本无法自动核对，请在继续使用前检查旧账本。','Historical quota records could not be reconciled. Review the old ledger before continuing.'],
    public_growth_policy_missing:['公开成长尚未登记，请到系统设置检查并恢复。','Public growth is not registered. Review recovery in Settings.'],
    public_growth_source_changed:['公开成长的来源版本已变化，请到系统设置检查并恢复。','The public-growth source version changed. Review recovery in Settings.'],
    public_growth_source_denied:['公开成长的来源权限已失效，请到系统设置检查并恢复。','Public-growth source access is no longer valid. Review recovery in Settings.'],
    public_growth_invalid:['公开成长完整性检查未通过，请到系统设置检查并恢复。','Public-growth integrity validation failed. Review recovery in Settings.'],
    public_growth_validation_failed:['公开成长验证未完成，请到系统设置检查并恢复。','Public-growth validation could not complete. Review recovery in Settings.'],
    qq_login_required:['请先在 QQ 连接器中完成登录。','Complete login in the QQ connector.'],
    qq_identity_mismatch:['连接器账号与配置不一致，请检查登录账号。','The connector account does not match the configured account.'],
    qq_account_mismatch:['连接器账号与配置不一致，请检查登录账号。','The connector account does not match the configured account.'],
    qq_launch_not_configured:['请先配置 NapCat 启动程序，或手动启动连接器。','Configure the NapCat launcher or start the connector manually.'],
    qq_launch_configuration:['启动程序或工作目录无效，请检查接入设置。','The executable or working directory is invalid. Check connection settings.'],
    qq_launch_failed:['连接器启动失败，请检查启动程序与参数。','The connector failed to start. Check the executable and arguments.'],
    qq_shutdown_failed:['连接器退出状态未能确认，请检查进程与端口。','Connector shutdown could not be confirmed. Check its processes and ports.'],
    qq_launch_timeout:['等待连接器登录超时，请检查客户端后重试。','Connector login timed out. Check the client and try again.'],
    qq_port_conflict:['连接端口已被占用，且无法核验账号。','The connector port is in use and the account could not be verified.']
  };
  window.uiCode = (domain, value) => (pairs[domain]?.[value] || (domain === 'stage' ? ['未标注阶段','Unlabelled stage'] : ['未知','Unknown']))[language === 'en' ? 1 : 0];
  window.uiError = (code, message, fallback) => errors[code]?.[language === 'en' ? 1 : 0] ||
    (message && (language === 'zh' || Object.hasOwn(window.QIBAN_EN, message)) ? ui(message) : fallback || (language === 'zh' ? '操作未完成，请查看技术详情。' : 'The action could not complete. See technical details.'));
  window.uiTechnical = (element, value) => {
    if(!value)return;
    const details=document.createElement('details'),summary=document.createElement('summary'),pre=document.createElement('pre');
    details.className='technical-details';summary.textContent=language==='zh'?'技术详情':'Technical details';
    pre.textContent=typeof value==='string'?value:JSON.stringify(value,null,2);details.append(summary,pre);element.append(details);
  };
  // Translate only the initial UI. Never traverse dynamically inserted user or model content.
  const walker=document.createTreeWalker(document.documentElement,NodeFilter.SHOW_TEXT);
  const nodes=[];while(walker.nextNode())nodes.push(walker.currentNode);
  for(const node of nodes){
    if(node.parentElement.closest('script,style,pre,code,textarea,[data-content],[data-manual-language]'))continue;
    const source=node.textContent.trim();if(!source)continue;
    const translated=Object.hasOwn(window.QIBAN_EN,source)?ui(source):source;
    node.textContent=node.textContent.replace(source,translated);
  }
  for(const element of document.querySelectorAll('[placeholder],[title],[aria-label],[alt]'))for(const attr of ['placeholder','title','aria-label','alt']){
    const source=element.getAttribute(attr);if(source&&Object.hasOwn(window.QIBAN_EN,source))element.setAttribute(attr,ui(source));
  }
  for(const option of document.querySelectorAll('#kind option'))option.textContent=uiCode('kind',option.value);
  for(const option of document.querySelectorAll('#classification option'))option.textContent=uiCode('classification',option.value);
  document.querySelectorAll('[data-manual-language]').forEach(element=>element.hidden=element.dataset.manualLanguage!==language);
  document.querySelectorAll('[data-language]').forEach(select=>{
    select.value=language;
    select.addEventListener('change',()=>{
      if(!valid(select.value))return;
      document.cookie='qiban_language='+select.value+'; Path=/; Max-Age=31536000; SameSite=Strict';
      try{localStorage.setItem('qiban_language',select.value);}catch{}
      location.reload();
    });
  });
})();
