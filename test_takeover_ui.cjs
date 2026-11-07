// Browser take-over live view (request_takeover): card, frame polling, input posts, Done, and the stream wiring.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const code=fs.readFileSync(__dirname+'/grokbot/static/app.js','utf8');
function setup(fetch){const elements={},timers=[];
 const mk=()=>({value:'',textContent:'',children:[],disabled:false,isConnected:true,style:{},className:'',
  classList:{_s:new Set(),add(...a){a.forEach(x=>this._s.add(x))},remove(...a){a.forEach(x=>this._s.delete(x))},toggle(x){this._s.has(x)?this._s.delete(x):this._s.add(x);return this._s.has(x)},contains(x){return this._s.has(x)}},
  appendChild(c){this.children.push(c)},append(...a){a.forEach(x=>this.children.push(x))},replaceChildren(){this.children=[]},focus(){},
  addEventListener(n,f){this[n]=f}});
 const el=id=>elements[id]||=mk();
 const ctx={console,Set,Map,JSON,Promise,TextDecoder,TextEncoder,fetch,Date,encodeURIComponent,setTimeout:f=>timers.push(f),clearTimeout:()=>{},document:{getElementById:el,createElement:mk}};
 vm.createContext(ctx);for(const n of ['keyin','urlin','modelin','inp','wrap','chat'])ctx[n]=el(n);vm.runInContext(code.replace('restoreTasks();','').replace('restoreHistory();','').replace('loadPolicy();','').replace('refreshKeyStatus();\n',''),ctx);return {ctx,el,timers,run:e=>vm.runInContext(e,ctx)}}
const resp=(obj,ok=true)=>({ok,json:async()=>obj,text:async()=>typeof obj==='string'?obj:JSON.stringify(obj)});
const flat=n=>[n.textContent||'',...(n.children||[]).map(flat)].join('|');
const find=(n,pred)=>{if(pred(n))return n;for(const c of n.children||[]){const f=find(c,pred);if(f)return f}return null};
(async()=>{
 { const posts=[]; const s=setup(async(u,o)=>{posts.push({u,body:o&&o.body?JSON.parse(o.body):null});return resp({ok:true})});
   const card=s.run("takeoverCard({id:'tk1',reason:'Log in to example.com',viewport:{width:1280,height:800}})");
   assert(card.className.includes('takeover'),card.className);
   assert(flat(card).includes('Log in to example.com'),flat(card));
   assert(flat(card).includes('does not see what you type'),flat(card));
   assert.strictEqual(s.run("takeoverCard({id:'tk1'})"),card,'one card per take-over');
   const img=find(card,c=>c.className==='liveview'); assert(img,'live view image');
   assert(img.src.startsWith('/agent/takeover/tk1/frame?t='),img.src);
   // a loaded frame schedules the next poll; a failed one (204 before the first frame) retries
   img.load(); assert.equal(s.timers.length,1); s.timers.shift()(); assert(img.src.startsWith('/agent/takeover/tk1/frame?t='));
   img.error(); assert.equal(s.timers.length,1); s.timers.length=0;
   // clicks are scaled from the displayed size to the page viewport
   img.clientWidth=640; img.clientHeight=400; await img.click({offsetX:100,offsetY:50});
   assert.deepEqual(posts.at(-1),{u:'/agent/takeover/tk1/input',body:{type:'click',x:200,y:100}});
   await img.wheel({deltaY:120,preventDefault(){}});
   assert.deepEqual(posts.at(-1).body,{type:'scroll',dy:120});
   // typed text goes to /input, is hidden by default, and the field is cleared
   const field=find(card,c=>c.className==='takeover-text'); assert.equal(field.type,'password');
   const btns=find(card,c=>c.className==='btns'); const btn=l=>btns.children.find(b=>b.textContent===l);
   field.value='hunter2'; await btn('Send').click(); assert.equal(field.value,'');
   assert.deepEqual(posts.at(-1).body,{type:'type',text:'hunter2'});
   field.value='x'.repeat(700); await btn('Send').click();
   assert.deepEqual(posts.slice(-2).map(p=>p.body.text.length),[500,200],'long text is split to the 500-char limit');
   field.value='me'; await field.keydown({key:'Enter',preventDefault(){}});
   assert.deepEqual(posts.slice(-2).map(p=>p.body),[{type:'type',text:'me'},{type:'key',key:'Enter'}]);
   await btn('Tab').click(); assert.deepEqual(posts.at(-1).body,{type:'key',key:'Tab'});
   const show=btn('Show'); show.click(); assert.equal(field.type,'text'); assert.equal(show.textContent,'Hide'); show.click(); assert.equal(field.type,'password');
   // Done posts /done and retires the card; polling stops
   await btn('Done').click(); assert.equal(posts.at(-1).u,'/agent/takeover/tk1/done');
   assert(card.classList.contains('resolved')); assert(flat(card).includes('Handed back to the agent.'));
   const before=img.src; img.load(); assert.equal(s.timers.length,1); s.timers.shift()(); assert.equal(img.src,before,'no polling after Done');
   s.run("finishTakeover({id:'tk1',status:'done'})"); assert.equal(flat(card).split('Handed back').length,2,'ended once');
 }
 // Done failing keeps the card usable
 { const s=setup(async()=>resp('No such take-over (it may have ended).',false));
   const card=s.run("takeoverCard({id:'tk2',reason:'Captcha'})"); const btns=find(card,c=>c.className==='btns');
   const done=btns.children.find(b=>b.textContent==='Done'); await done.click();
   assert.equal(done.disabled,false); assert(!card.classList.contains('resolved')); assert(flat(card).includes('Could not finish'),flat(card)); }
 // stream wiring: takeover_required renders the card, takeover ends it, the reply continues after
 { const enc=new TextEncoder(); const events=[{type:'takeover_required',id:'tk3',reason:'Solve the captcha',viewport:{width:1280,height:800}},{type:'takeover',id:'tk3',status:'timeout'},{type:'text',delta:'Back on the page.'},{type:'done',status:'completed'}];
   const body=events.map(e=>JSON.stringify(e)).join('\n')+'\n'; let sent=false;
   const s=setup(async(u)=>u==='/agent'?{ok:true,body:{getReader:()=>({read:async()=>sent?{done:true}:(sent=true,{done:false,value:enc.encode(body)})})}}:resp([]));
   s.el('in').value='go'; await s.run('send()');
   const card=s.el('chat').children.find(c=>typeof c.className==='string'&&c.className.includes('takeover'));
   assert(card,'take-over card from the stream'); assert(card.classList.contains('resolved'));
   assert(flat(card).includes('Take-over ended (timeout).'),flat(card));
   assert(flat(s.el('chat')).includes('Back on the page.')); }
 console.log('PASS takeover UI');
})().catch(e=>{console.error(e);process.exitCode=1});
