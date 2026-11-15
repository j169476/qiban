const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
function page(){
 const events={},listeners=[],timers=new Map();let seq=0;
 const button={dataset:{},attrs:{},loading:false,matches:s=>s==='button',classList:{toggle(_,value){button.loading=value;}},setAttribute(k,v){this.attrs[k]=v;}};
 const bridge={postMessage(){},addEventListener(_,fn){listeners.push(fn);}};
 const window={chrome:{webview:bridge}};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'Assets/feedback.js'),'utf8'),{window,
  document:{addEventListener(n,f){events[n]=f;}},setTimeout(fn){timers.set(++seq,fn);return seq;},clearTimeout(id){timers.delete(id);}});
 return {button,bridge,window,click:()=>events.click({target:{closest:()=>button}}),reply:data=>listeners.forEach(f=>f({data})),timers};
}
test('initiating button loads until its matching response; background polling is quiet',()=>{
 const p=page();p.bridge.postMessage({action:'management',request_id:'poll'});assert.equal(p.button.loading,false);
 p.click();p.bridge.postMessage({action:'management',request_id:'one'});assert.equal(p.button.attrs['aria-busy'],'true');
 p.reply({type:'management',request_id:'other'});assert.equal(p.button.loading,true);
 p.reply({type:'management',request_id:'one'});assert.equal(p.button.loading,false);
});
test('overlapping work and native backup keep feedback until all work ends',()=>{
 const p=page(),finish=p.window.QibanFeedback.start(p.button);
 p.click();p.bridge.postMessage({action:'uninstall-backup'});finish();
 p.reply({type:'uninstall',state:{backup:'running'}});assert.equal(p.button.loading,true);
 p.reply({type:'uninstall',state:{backup:'succeeded'}});assert.equal(p.button.loading,false);
});
test('asynchronous login state survives the short request acknowledgement',()=>{
 const p=page();p.click();p.bridge.postMessage({action:'management',request_id:'one'});
 p.window.QibanFeedback.mark(p.button,true);p.reply({type:'management',request_id:'one'});assert.equal(p.button.loading,true);
 p.window.QibanFeedback.mark(p.button,false);assert.equal(p.button.loading,false);
});
