/* Local date and time select the window and room. Activity selects a still pose. */
(() => {
 'use strict';
 const base=new URL('.',document.currentScript.src);
 const idlePoses=['sleeping','reading','window','tail'];
 const working=new Set(['replying','learning','watching']);
 const fadeMs=1100,blackMs=300;
 let roomNumber=0;

 class RoomScene extends HTMLElement {
  static get observedAttributes(){return ['action','quiet','preview-hour','outfit'];}
  connectedCallback(){
   this.media=window.qibanRoomScenes;
   if(!this.media)return;
   const doc=this.ownerDocument;
   if(!doc.querySelector('link[data-room-styles]')){
    const css=doc.createElement('link');css.rel='stylesheet';css.href=new URL('room-scene.css',base);css.dataset.roomStyles='';doc.head.append(css);
   }
   this.innerHTML='<div class="room-stage"><img class="room-base" alt=""><div class="room-season"></div><div class="room-actor"></div><div class="room-shade" aria-hidden="true"></div></div>';
   this.stage=this.firstElementChild;
   [this.base,this.landscape,this.actor,this.shade]=this.stage.children;
   this.stage.style.aspectRatio=this.media.size.join(' / ');this.dataset.rendering=this.media.rendering;
   const svg=doc.createElementNS('http://www.w3.org/2000/svg','svg'),id='room-light-'+(++roomNumber);
   svg.setAttribute('class','room-light-map');svg.setAttribute('aria-hidden','true');
   svg.innerHTML='<defs><filter id="'+id+'" color-interpolation-filters="sRGB"><feColorMatrix type="matrix"/></filter></defs>';
   this.matrix=svg.querySelector('feColorMatrix');this.stage.append(svg);this.actor.style.filter='url(#'+id+')';
   this.images=new Map();this.reduced=matchMedia('(prefers-reduced-motion: reduce)');
   this.visible=false;this.target=null;this.shown=null;this.readyFrame=null;this.animation=null;this.dataset.phase='still';
   this.generation=0;this.idlePose=null;this.idleDeadline=0;this.idleRemaining=0;
   this.live=new AbortController();const options={signal:this.live.signal};
   const refresh=()=>this.update();
   doc.addEventListener('visibilitychange',refresh,options);window.addEventListener('pageshow',refresh,options);
   this.reduced.addEventListener('change',refresh,options);
   this.observer=new IntersectionObserver(([entry])=>{this.visible=entry.isIntersecting;this.update();});this.observer.observe(this);
   this.update();
  }
  disconnectedCallback(){
   this.live?.abort();this.observer?.disconnect();clearTimeout(this.wake);
   this.target=null;this.images?.clear();if(this.shade)this.resetShade();
  }
  attributeChangedCallback(){if(this.live&&!this.live.signal.aborted&&this.isConnected)this.update();}
  setActivity(activity,quiet=false){
   this.activity=activity;
   if(this.hasAttribute('quiet')!==quiet)this.toggleAttribute('quiet',quiet);
   else if(this.live&&this.isConnected)this.update();
  }
  resolvePose(activity,observing,now){
   if(activity==='browsing')activity='watching';
   if(working.has(activity)||(this.hasAttribute('data-preview')&&idlePoses.includes(activity))){
    this.idlePose=null;this.idleDeadline=0;return activity;
   }
   if(!this.idlePose||(observing&&this.idleDeadline&&now>=this.idleDeadline)){
    this.idlePose=idlePoses[Math.floor(Math.random()*idlePoses.length)];
    this.idleRemaining=180000+Math.floor(Math.random()*120001);this.idleDeadline=0;
   }
   if(observing&&!this.idleDeadline)this.idleDeadline=now+this.idleRemaining;
   if(!observing&&this.idleDeadline){this.idleRemaining=Math.max(0,this.idleDeadline-now);this.idleDeadline=0;}
   return this.idlePose;
  }
  update(){
   if(!this.isConnected||!this.live||this.live.signal.aborted)return;
   const now=new Date(),visible=this.visible&&!this.ownerDocument.hidden;
   const observing=visible&&!this.hasAttribute('quiet');this.canFade=observing&&!this.reduced.matches;
   const override=this.hasAttribute('data-preview')?this.getAttribute('preview-hour'):null;
   const minute=override!==null&&Number(override)>=0&&Number(override)<24?Math.floor(Number(override)*60):now.getHours()*60+now.getMinutes();
   const slot=this.media.periods.findLastIndex(item=>item.start<=minute);
   const period=this.media.periods[slot],month=now.getMonth()+1;
   const season=this.media.seasons?.find(item=>item.months.includes(month));
   const pose=this.resolvePose(this.getAttribute('action')||this.activity||'idle',observing,now.getTime());
   const outfit=this.getAttribute('outfit')||'hoodie';
   const layer=this.media.characters[outfit]?.[pose]||this.media.characters.hoodie[pose];
   clearTimeout(this.wake);
   if(visible){
    const untilMinute=60000-now.getSeconds()*1000-now.getMilliseconds();
    const untilIdle=this.idleDeadline?this.idleDeadline-now.getTime():Infinity;
    this.wake=setTimeout(()=>this.update(),Math.max(1,Math.min(untilMinute,untilIdle)));
   }
   const key=(season?.id||'')+'/'+period.id+'/'+layer.still;
   if(this.target?.key!==key){
    const target={key,pose,period:period.id,season:season?.id,slot,light:layer.lighting[period.id]};
    this.generation++;this.target=target;this.dataset.target=pose;
    this.readyFrame=this.shown?.key===key?this.shown:null;
    if(!this.readyFrame){
     const windowImage=season?this.image(season.src).catch(()=>null):null;
     Promise.all([this.image(period.src),this.image(layer.still),windowImage]).then(([bg,portrait,scenery])=>{
      if(this.target!==target||!this.isConnected)return;
      this.readyFrame={...target,bg,portrait,scenery};this.paint();
     }).catch(()=>{
      if(this.target!==target)return;
      this.target=null;this.readyFrame=null;this.resetShade();
     });
    }
   }
   this.paint();
  }
  image(path){
   if(!this.images.has(path)){
    const cache=this.images,img=new Image();img.alt='';img.src=new URL(path,base).href;
    cache.set(path,img.decode().then(()=>img).catch(error=>{cache.delete(path);throw error;}));
   }
   return this.images.get(path);
  }
  show(frame){
   if(!frame||this.shown===frame)return;
   if(this.base!==frame.bg){frame.bg.className='room-base';this.base.replaceWith(frame.bg);this.base=frame.bg;}
   if(frame.scenery){
    if(this.landscape.firstChild!==frame.scenery)this.landscape.replaceChildren(frame.scenery);
    frame.scenery.style.width=(this.media.periods.length*100)+'%';
    frame.scenery.style.left=(-frame.slot*100)+'%';
   }else this.landscape.replaceChildren();
   if(this.actor.firstChild!==frame.portrait)this.actor.replaceChildren(frame.portrait);
   const [r,g,b]=frame.light;
   this.matrix.setAttribute('values',[r,0,0,0,0,0,g,0,0,0,0,0,b,0,0,0,0,0,1,0].join(' '));
   this.shown=frame;this.dataset.pose=frame.pose;this.dataset.period=frame.period;this.dataset.season=frame.scenery?frame.season:'';
  }
  resetShade(){
   clearTimeout(this.hold);this.animation?.cancel();this.animation=null;
   this.shade.style.opacity='0';this.dataset.phase='still';
  }
  fade(to,phase,finished){
   const from=Number(getComputedStyle(this.shade).opacity);
   this.animation?.cancel();this.shade.style.opacity=String(from);this.dataset.phase=phase;
   const animation=this.shade.animate([{opacity:from},{opacity:to}],{duration:Math.abs(to-from)*fadeMs,easing:'ease-in-out',fill:'forwards'});
   this.animation=animation;
   animation.onfinish=()=>{
    if(this.animation!==animation)return;
    this.shade.style.opacity=String(to);animation.cancel();this.animation=null;finished();
   };
  }
  reveal(){
   this.fade(0,'fade-in',()=>{this.dataset.phase='still';this.paint();});
  }
  paint(){
   if(!this.shown||!this.canFade){this.resetShade();this.show(this.readyFrame);return;}
   const phase=this.dataset.phase;
   if(!this.readyFrame||phase==='black')return;
   if(this.readyFrame.key===this.shown.key){
    if(phase==='fade-out')this.reveal();
    return;
   }
   if(phase==='fade-out')return;
   this.fade(1,'fade-out',()=>{
    this.dataset.phase='black';
    this.hold=setTimeout(()=>{
     // Coalesce all requests made during the fade. Keep the previous image if the latest one is still decoding.
     this.show(this.readyFrame);this.reveal();
    },blackMs);
   });
  }
 }
 customElements.define('qiban-room',RoomScene);
})();
