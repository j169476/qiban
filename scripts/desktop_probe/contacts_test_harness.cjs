const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
module.exports=function page(script,{autoDetails=true}={}){
 const nodes=new Map(),events={},requests=[],listeners=[];let sequence=0,tick;
 const make=()=>({textContent:'',hidden:false,disabled:false,value:'',children:[],dataset:{},classList:{add(){}},
  replaceChildren(...children){this.children=children;this.value='';},append(...children){this.children.push(...children);},
  setAttribute(name,value){this[name]=value;},removeAttribute(name){delete this[name];}});
 const node=id=>{if(!nodes.has(id))nodes.set(id,make());return nodes.get(id);};
 const body={dataset:{navPage:'home',tabPlatforms:'qq'}};
 const emit=(name,detail)=>{for(const fn of events[name]||[])fn({detail});};
 const settle=()=>new Promise(resolve=>setImmediate(resolve));
 async function reply(request,result){for(const fn of listeners)fn({data:{type:'management',request_id:request.request_id,response:{ok:true,result}}});await settle();}
 const webview={addEventListener(_,fn){listeners.push(fn);},postMessage(request){requests.push(request);if(autoDetails&&request.operation==='wechat.account_details')queueMicrotask(()=>reply(request,{login:request.arguments.login,nickname:'测试昵称',wechat_id:''}));}};
 const window={chrome:{webview},addEventListener(name,fn){(events[name]||=[]).push(fn);}};
 const context=vm.createContext({document:{body,getElementById:node,createElement:make},window,queueMicrotask,
  crypto:{randomUUID:()=>String(++sequence)},setTimeout:()=>sequence,clearTimeout(){},setInterval(fn){tick=fn;}});
 for(const file of ['contacts.js',script])vm.runInContext(fs.readFileSync(path.join(__dirname,'Assets',file),'utf8'),context);
 const picker=id=>{const root=node(id);return {search:root.children[0].children[0],kind:root.children[0].children[1],sync:root.children[0].children[2],list:root.children[3],pager:root.children[4],status:root.children[2]};};
 return {node,requests,reply,settle,tick:()=>tick(),body,window,picker,
  update:allowed=>emit('qiban-management-state',{ready:true,locked:!allowed}),
  async enter(platform){body.dataset.navPage='platforms';body.dataset.tabPlatforms=platform;emit('qiban-nav-change',{page:'platforms'});emit('qiban-tab-change',{group:'platforms',target:platform});await settle();},
  async leave(){body.dataset.navPage='home';emit('qiban-nav-change',{page:'home'});await settle();}};
};
