/* Viewport over one immutable learning snapshot, in backend world coordinates. */
(() => {
 'use strict';
 const ui = window.QibanWorkspace;
 ui.learningMap = function(host, snapshot, {load, select, onError, signal}) {
  const [left, top, right, bottom] = snapshot.bounds;
  const tileSize = snapshot.tile_size;
  host.innerHTML = '<div class="ws-actions"><button data-zoom="in" aria-label="放大">＋</button><button data-zoom="out" aria-label="缩小">−</button><button data-zoom="fit">全图</button><output></output></div><div class="ws-map-frame"><svg class="ws-canvas" tabindex="0" role="group" aria-label="学习树画布，方向键移动，加减号缩放"></svg><svg class="ws-mini" tabindex="0" aria-label="缩略图，点击或拖动定位"></svg></div><button class="secondary" data-next hidden>继续读取当前图块</button>';
  const canvas = host.querySelector('.ws-canvas'), mini = host.querySelector('.ws-mini');
  const cache = new Map(), failed = new Set();
  const local=new AbortController();
  let cx = (left+right)/2, cy = (top+bottom)/2, scale, timer, frame, reading = false;
  let visible = [], selected = '', pointer, moreKey = '', closed = false;
  const rect = () => canvas.getBoundingClientRect();
  const fit = () => Math.min(1, (rect().width-12)/Math.max(1,right-left), (rect().height-12)/Math.max(1,bottom-top));
  const clamp = (v,lo,hi) => Math.max(lo,Math.min(hi,v));
  function refresh(wait=100) {
   if (closed) return;
   cancelAnimationFrame(frame); frame = requestAnimationFrame(paint);
   clearTimeout(timer); timer = setTimeout(() => {paint(); void readOne();},wait);
  }
  function zoom(value,x=cx,y=cy) {
   const before=scale;
   scale=clamp(value,Math.max(1e-9,fit()/2),2.5);
   cx=x+(cx-x)*before/scale; cy=y+(cy-y)*before/scale;
   refresh();
  }
  function paint() {
   if(closed)return;
   const r=rect(); if(!r.width||!r.height)return;
   scale ??= fit();
   cx=clamp(cx,left,right); cy=clamp(cy,top,bottom);
   const w=r.width/scale,h=r.height/scale,x0=cx-w/2,y0=cy-h/2;
   let level=clamp(Math.ceil(Math.log2(Math.max(260,w/3,h/3)/(tileSize))),0,12);
   const size=tileSize*2**level, tiles=[];
   for(let y=Math.max(0,Math.floor(y0/size)); y<=Math.min(Math.ceil(bottom/size)-1,Math.floor((y0+h)/size))&&tiles.length<16;y++){
    for(let x=Math.max(0,Math.floor(x0/size)); x<=Math.min(Math.ceil(right/size)-1,Math.floor((x0+w)/size))&&tiles.length<16;x++){
     tiles.push({x,y,level,key:[level,x,y].join('-')});
    }
   }
   visible=tiles;
   const nodes=new Map(),groups=[];
   let missing=0, more=false;
   for(const tile of tiles){
    const data=cache.get(tile.key);
    if(!data){missing++;continue;}
    if(level>0 && data.total>0){
     const tx=tile.x*size,ty=tile.y*size;
     // Keep a cluster in view for narrow vertical or horizontal trees.
     const x=(Math.max(tx,x0,left)+Math.min(tx+size,x0+w,right))/2;
     const y=(Math.max(ty,y0,top)+Math.min(ty+size,y0+h,bottom))/2;
     groups.push('<g class="ws-cluster" tabindex="0" role="button" aria-label="'+data.total+' 个节点，放大" data-cluster="'+tile.key+'" transform="translate('+x+' '+y+') scale('+1/scale+')"><rect x="-32" y="-15" width="64" height="30" rx="13"/><text text-anchor="middle" y="5">'+data.total+'</text></g>');
    }else if(level===0){for(const node of data.items)nodes.set(node.id,node);more ||= data.next_offset!=null;}
   }
   const edges=[],marks=[];
   for(const n of nodes.values()){
    for(const id of [n.parent_id,...(n.prerequisites||[])]){
     const p=nodes.get(id); if(p)edges.push('<line x1="'+(p.x+130)+'" y1="'+(p.y+22)+'" x2="'+n.x+'" y2="'+(n.y+22)+'" '+(id!==n.parent_id?'stroke-dasharray="6 4"':'')+'/>');
    }
    const action=select?'role="button" tabindex="0" data-node="'+ui.escape(n.id)+'"':'';
    marks.push('<g '+action+' aria-label="'+ui.escape(n.title)+'" class="ws-node'+(selected===n.id?' chosen':'')+(n.archived?' archived':'')+'" transform="translate('+n.x+' '+n.y+')"><title>'+ui.escape(n.title)+'</title><rect width="184" height="45" rx="12"/><text x="10" y="28">'+ui.escape(Array.from(n.title).slice(0,11).join(''))+'</text></g>');
   }
   canvas.setAttribute('viewBox',[x0,y0,w,h].join(' '));
   canvas.innerHTML=edges.join('')+groups.join('')+marks.join('');
   // A square overview preserves its coordinate transform at all aspect ratios.
   const box=Math.max(right-left,bottom-top,1);
   mini.setAttribute('viewBox',[(left+right-box)/2,(top+bottom-box)/2,box,box].join(' '));
   const overview=[];
   for(const [key,data] of cache){
    if(!data.total)continue;
    const [z,x,y]=key.split('-').map(Number),unit=tileSize*2**z;
    overview.push('<rect class="ws-occupied" x="'+x*unit+'" y="'+y*unit+'" width="'+Math.min(unit,right-x*unit)+'" height="'+Math.min(unit,bottom-y*unit)+'"/>');
   }
   mini.innerHTML='<rect class="ws-world" x="'+left+'" y="'+top+'" width="'+(right-left)+'" height="'+(bottom-top)+'"/>'+overview.join('')+'<rect class="ws-sight" x="'+clamp(x0,left,right)+'" y="'+clamp(y0,top,bottom)+'" width="'+Math.max(box*.015,Math.min(right,x0+w)-Math.max(left,x0))+'" height="'+Math.max(box*.015,Math.min(bottom,y0+h)-Math.max(top,y0))+'"/>';
   host.querySelector('output').textContent=(level?'分块概览':'节点视图')+' · '+Number((scale*100).toPrecision(3))+'%'+(missing?' · 图块读取中':'');
   host.querySelector('[data-next]').hidden=!more;
  }
  async function readOne(){
   if(reading||closed||document.hidden)return;
   const t=visible.find(t=>!cache.has(t.key)&&!failed.has(t.key)) || visible.find(t=>t.key===moreKey);
   if(!t)return;
   const prior=cache.get(t.key),offset=prior?.next_offset??0;
   moreKey='';reading=true;
   try{
    const data=await load({x:t.x,y:t.y,level:t.level,offset});
    if(closed)return;
    if(data.snapshot!==snapshot.snapshot)throw Object.assign(Error('学习快照已变化。'),{code:'edit_conflict'});
    cache.set(t.key,{...data,items:t.level?[]:[...(prior?.items||[]),...data.items]});
    if(cache.size>72){const old=[...cache.keys()].find(key=>!visible.some(v=>v.key===key));if(old)cache.delete(old);}
   }catch(error){if(!closed){failed.add(t.key);onError(error);}}
   finally{reading=false;if(!closed)refresh(40);}
  }
  function activate(target){
   if(target.dataset.node){select(target.dataset.node);return;}
   const t=visible.find(t=>t.key===target.dataset.cluster);if(!t)return;
   const s=tileSize*2**t.level;
   cx=(t.x*s+Math.min((t.x+1)*s,right))/2;cy=(t.y*s+Math.min((t.y+1)*s,bottom))/2;
   zoom(Math.max(scale*2.2,Math.min(rect().width,rect().height)*1.15/s));
  }
  function miniMove(event){
   const p=new DOMPoint(event.clientX,event.clientY).matrixTransform(mini.getScreenCTM().inverse());
   cx=p.x;cy=p.y;refresh();
  }
  for(const el of [mini,canvas]){
   el.onpointerdown=event=>{
    if(event.button!==0)return;
    const target=event.target.closest('[data-node],[data-cluster]');
    pointer={id:event.pointerId,x:event.clientX,y:event.clientY,cx,cy,el,target,moved:false};
    el.setPointerCapture(event.pointerId);if(el===mini)miniMove(event);
   };
   el.onpointermove=event=>{
    if(!pointer||pointer.el!==el||event.pointerId!==pointer.id)return;
    if(el===mini){miniMove(event);return;}
    const dx=event.clientX-pointer.x,dy=event.clientY-pointer.y;
    if(Math.abs(dx)+Math.abs(dy)>4)pointer.moved=true;
    if(pointer.moved){cx=pointer.cx-dx/scale;cy=pointer.cy-dy/scale;refresh();}
   };
   el.onpointerup=event=>{if(pointer?.id!==event.pointerId)return;const p=pointer;pointer=null;if(!p.moved&&p.target)activate(p.target);};
   el.onpointercancel=()=>{pointer=null;};
   el.onkeydown=event=>{
    if(event.key==='Enter'&&event.target.matches('[data-node],[data-cluster]'))activate(event.target);
    else if(event.key==='+'||event.key==='=')zoom(scale*1.6);
    else if(event.key==='-')zoom(scale/1.6);
    else if(event.key.startsWith('Arrow')){const step=80/scale;cx+=({'ArrowLeft':-step,'ArrowRight':step}[event.key]||0);cy+=({'ArrowUp':-step,'ArrowDown':step}[event.key]||0);refresh();}
    else return;
    event.preventDefault();
   };
  }
  canvas.addEventListener('wheel',event=>{
   event.preventDefault();const r=rect();zoom(scale*Math.exp(-clamp(event.deltaY,-250,250)/420),cx+(event.clientX-r.left-r.width/2)/scale,cy+(event.clientY-r.top-r.height/2)/scale);
  },{passive:false,signal:local.signal});
  host.onclick=event=>{
   const b=event.target.closest('button');if(!b)return;
   if(b.dataset.zoom==='fit'){scale=fit();cx=(left+right)/2;cy=(top+bottom)/2;refresh();}
   else if(b.dataset.zoom)zoom(scale*(b.dataset.zoom==='in'?1.65:1/1.65));
   else if(b.hasAttribute('data-next')){moreKey=visible.find(t=>cache.get(t.key)?.next_offset!=null)?.key||'';void readOne();}
  };
  const resize=new ResizeObserver(()=>refresh());resize.observe(canvas);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();},{signal:local.signal});
  function close(){closed=true;local.abort();signal.removeEventListener('abort',close);resize.disconnect();clearTimeout(timer);cancelAnimationFrame(frame);cache.clear();}
  signal.addEventListener('abort',close,{once:true});refresh(0);
  return {close,focus(n){selected=n.id;scale=1;cx=n.x+92;cy=n.y+22;refresh(0);}};
 };
})();
