const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
function setup(){
 const events={},listeners=[],requests=[],nodes=new Map();let view=true,dirty=false,read=0,seq=0;
 const panel={hidden:false,getClientRects:()=>view?[{}]:[]};
 const button={dataset:{},disabled:false,parentElement:null,closest:()=>panel,removeAttribute(name){if(name==='data-auto-error')delete this.dataset.autoError},onclick(){read++;webview.postMessage({operation:'read',request_id:String(++seq)})}};nodes.set('read',button);
 const webview={postMessage:m=>requests.push(m),addEventListener(_,fn){listeners.push(fn)}};
 const window={chrome:{webview},addEventListener(name,fn){(events[name]||=[]).push(fn)}};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'Assets/auto-read.js'),'utf8'),{window,document:{getElementById:id=>nodes.get(id),addEventListener(){}},queueMicrotask,setTimeout:()=>0,clearTimeout(){}});
 window.QibanAutoRead.register('read',{dirty:()=>dirty});
 const emit=async(name,detail={})=>{for(const fn of events[name]||[])fn({detail});await new Promise(r=>setImmediate(r))};
 return {button,settled:window.QibanAutoRead.settled,onEntry(fn){window.addEventListener('qiban-management-state',()=>queueMicrotask(fn))},get count(){return read},set dirty(v){dirty=v},set visible(v){view=v},emit,reply(ok){listeners.forEach(f=>f({data:{type:'management',request_id:requests.at(-1).request_id,response:{ok}}}))}};
}
test('automatic reads run once per entry, preserve drafts and expose retry only on failure',async()=>{
 const p=setup();await p.emit('qiban-management-state',{ready:true,locked:false});assert.equal(p.count,1);p.reply(true);
 await p.emit('qiban-management-state',{ready:true,locked:false});assert.equal(p.count,1);
 p.visible=false;await p.emit('qiban-nav-change');p.dirty=true;p.visible=true;await p.emit('qiban-nav-change');assert.equal(p.count,1);
 p.dirty=false;await p.emit('qiban-management-state',{ready:true,locked:false});assert.equal(p.count,2);p.reply(false);assert.equal(p.button.dataset.autoError,'true');
 p.button.onclick();assert.equal(p.count,3);assert.equal(p.button.dataset.autoError,undefined);p.reply(true);
 await p.emit('qiban-management-state',{ready:true,locked:true});p.button.onclick();assert.equal(p.count,3);
});

test('contact entry synchronization finishes before the shared scope read',async()=>{
 const p=setup();p.onEntry(()=>{p.button.disabled=true});
 await p.emit('qiban-management-state',{ready:true,locked:false});assert.equal(p.count,0);
 p.button.disabled=false;p.settled();await new Promise(r=>setImmediate(r));assert.equal(p.count,1);
 p.settled();await new Promise(r=>setImmediate(r));assert.equal(p.count,1);
});
