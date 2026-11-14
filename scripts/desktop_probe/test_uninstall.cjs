const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
function page(){
 const nodes=new Map(),sent=[];let listener;
 const get=id=>{if(!nodes.has(id))nodes.set(id,{textContent:'',disabled:false,hidden:false,value:'',open:false,
   setAttribute(){},querySelector:()=>({setAttribute(){}}),showModal(){this.open=true;},close(){this.open=false;}});return nodes.get(id);};
 const webview={postMessage:value=>sent.push(value),addEventListener:(_,handler)=>listener=handler};
 let sequence=0;
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'Assets/uninstall.js'),'utf8'),{
   document:{getElementById:get,body:{dataset:{}}},window:{chrome:{webview}},
   crypto:{randomUUID:()=>String(++sequence)},setTimeout:()=>sequence,clearTimeout(){}});
 const state=override=>listener({data:{type:'uninstall',state:{registered:true,backup_available:true,
   backup:'none',continue_refusal:'uninstall_backup_incomplete',...override}}});
 return {get,sent,state,event:data=>listener({data})};
}
test('opening preparation only requests status; backup starts on one click without a separate start action',()=>{
 const p=page();assert.deepEqual(p.sent.map(x=>x.action),['uninstall-status']);
 p.state();assert.equal(p.get('backup-create').disabled,false);
 assert.equal(p.get('uninstall-continue').disabled,true);
 p.get('backup-create').onclick();assert.equal(p.sent.at(-1).action,'uninstall-backup');
 assert.equal(p.get('uninstall-skip').disabled,true);
 p.state({backup:'running',continue_refusal:'uninstall_backup_running'});
 assert.equal(p.get('portable-export').disabled,true);
});
test('failed backup keeps uninstall blocked but permits an explicit skip and retry',()=>{
 const p=page();p.state({backup:'failed',backup_detail:'备份未完成'});
 assert.equal(p.get('uninstall-continue').disabled,true);
 assert.equal(p.get('backup-create').disabled,false);
 assert.equal(p.get('uninstall-skip').disabled,false);
 p.get('uninstall-skip').onclick();assert.equal(p.sent.at(-1).action,'uninstall-skip');
});
test('canceling final confirmation sends no uninstall request',()=>{
 const p=page();p.state({backup:'succeeded',backup_name:'test.ccbackup',continue_refusal:''});
 assert.equal(p.get('backup-notice').textContent,'备份已保存：test.ccbackup');
 p.get('uninstall-continue').onclick();assert.equal(p.get('uninstall-confirm').open,true);
 p.get('uninstall-confirm-no').onclick();assert.equal(p.get('uninstall-confirm').open,false);
 assert.equal(p.sent.some(x=>x.action==='uninstall-continue'),false);
 p.get('uninstall-continue').onclick();p.get('uninstall-confirm-yes').onclick();
 assert.equal(p.sent.at(-1).action,'uninstall-continue');
 assert.equal(p.get('uninstall-continue').disabled,true);
});
test('portable directories can back up but cannot silently delete program files',()=>{
 const p=page();p.state({registered:false,continue_refusal:'uninstaller_missing'});
 assert.equal(p.get('backup-create').disabled,false);
 assert.equal(p.get('uninstall-continue').disabled,true);
 assert.match(p.get('uninstall-result').textContent,/手动删除/);
});
test('a never-initialized profile offers no backup but allows explicit skip',()=>{
 const p=page();p.state({backup_available:false});
 assert.equal(p.get('backup-create').disabled,true);
 assert.equal(p.get('portable-export').disabled,true);
 assert.equal(p.get('uninstall-skip').disabled,false);
});
