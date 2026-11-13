const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');

test('different UID shows both names, sends only on explicit confirmation, and lock hides late results',async()=>{
 const nodes={},requests=[];let inbound,state,sequence=0,poll;
 const el=id=>nodes['bili-'+id]??={textContent:'',hidden:false,replaceChildren(){},removeAttribute(name){delete this[name];}};
 const webview={postMessage:req=>requests.push(req),addEventListener:(_,fn)=>{inbound=fn;}};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'Assets/bili.js'),'utf8'),{
  document:{getElementById:id=>el(id.slice(5))},window:{chrome:{webview},addEventListener:(_,fn)=>{state=fn;}},
  crypto:{randomUUID:()=>String(++sequence).padStart(32,'0')},setTimeout(){},clearTimeout(){},setInterval(fn){poll=fn;}
 });
 const update=token=>state({detail:{ready:true,locked:!token,roomSession:token}});
 const reply=async(result)=>{
  inbound({data:{type:'management',request_id:requests.at(-1).request_id,response:{ok:true,result}}});
  await new Promise(r=>setImmediate(r));
 };
 const current={uid:'101',name:'原昵称'},candidate={uid:'202',name:'新昵称🙂'},ticket='ticket';
 update('owner');await reply({state:'idle',current:null});assert.equal(el('check').disabled,true);
 el('refresh').onclick();await reply({state:'idle',current});
 assert.equal(el('check').disabled,false);el('check').onclick();assert.equal(requests.at(-1).operation,'bili.check');
 await reply({state:'checking',current,ticket});assert.equal(el('check').disabled,true);poll();
 await reply({state:'checked',current,message:'已向 B 站核验，当前绑定账号登录有效。'});
 assert.match(el('status').textContent,/登录有效/);assert.equal(el('check').disabled,false);
 el('login').onclick();await reply({state:'waiting',current,ticket,qr:'png'});
 assert.equal(el('qr').hidden,false);poll();await reply({state:'confirm',current,candidate,ticket});
 assert.equal(el('qr').hidden,true);assert.equal(el('confirm').hidden,false);
 assert.match(el('current').textContent,/原昵称.*101/);assert.match(el('candidate').textContent,/新昵称🙂.*202/);
 assert.equal(requests.filter(r=>r.operation==='bili.confirm').length,0);
 el('confirm').onclick();assert.equal(requests.at(-1).arguments.ticket,ticket);assert.equal(requests.at(-1).operation,'bili.confirm');
 update(null);await reply({state:'saved',current:candidate,ticket});
 assert.equal(el('panel').hidden,true);assert.equal(el('candidate').textContent,'');assert.equal(el('current').textContent,'');
});
