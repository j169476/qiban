(() => {
 'use strict';
 const states={completed:'处理结束',failed:'失败',cancelled:'已取消',running:'处理中'};
 const results={cancelled:'未发送',uncertain:'发送结果待确认',failed:'操作失败',no_action:'无需发送',deferred:'已延后',duplicate:'已跳过重复处理'};
 const errors={
  wechat_prepare_timeout:'发送准备超时',wechat_prepare_access_denied:'无法访问发送所需资源',
  wechat_prepare_window_not_minimized:'请将电脑微信窗口最小化后再试',
  wechat_prepare_resource_missing:'发送组件缺失',wechat_prepare_version_unsupported:'当前微信版本暂不支持发送',
  wechat_prepare_client_unavailable:'微信连接不可用',wechat_native_closed:'微信连接不可用',wechat_stale_connection:'微信连接不可用',
  wechat_send_target_unverified:'无法确认接收会话',wechat_send_target_changed:'接收会话已变化',wechat_binding_changed:'绑定的微信账号已变化',
  wechat_send_text_invalid:'回复格式不支持',wechat_receipt_unavailable:'发送记录暂时无法核对',
  wechat_receipt_disconnected:'发送记录暂时无法核对',wechat_driver_prepare_failed:'微信发送准备失败'
 };
 function label(row){
  const result=row.outcome==='succeeded'?(row.platform==='wechat'?'发送端已记录':'操作成功'):
   Object.hasOwn(results,row.outcome)?results[row.outcome]:'';
  if(result&&row.status==='running')return result+' · 处理中';
  if(result&&row.status==='failed'&&row.outcome!=='failed')return result+' · 流程异常';
  return result||(Object.hasOwn(states,row.status)?states[row.status]:row.status?'未识别的状态（'+row.status+'）':'结果未记录');
 }
 function reason(row){
  const error=row.outcome_error||row.error;
  if(!error)return '';
  return Object.hasOwn(errors,error)?errors[error]:String(error);
 }
 window.QibanTraceResult={label,reason};
})();
