const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync('agenticbot/static/app.js', 'utf8');
async function run(events) {
  const elements = {};
  const el = id => elements[id] ||= {value:'', textContent:'', children:[], disabled:false, isConnected:true,
    appendChild(c){this.children.push(c);}, replaceChildren(){this.children=[];}, focus(){}, addEventListener(){}};
  const ctx = {console, Set, Map, JSON, Promise, TextDecoder, setTimeout:()=>0, clearTimeout(){},
    fetch:async()=>({ok:true}), document:{getElementById:el, createElement:()=>({textContent:'', children:[], style:{}, appendChild(c){this.children.push(c);}})}};
  vm.createContext(ctx);
  vm.runInContext(source.replace('restoreTasks();','').replace('restoreHistory();','').replace('loadPolicy();','').replace('refreshKeyStatus();\n',''),ctx);
  ctx.events = events;
  vm.runInContext('consumeEventStream = async (res, fn) => { for (const ev of events) fn(ev); }',ctx);
  el('in').value='hello';
  await vm.runInContext('send()',ctx);
  return el('chat').children.map(c=>c.textContent);
}
(async()=>{
 const withCheck = await run([{type:'completion_check',status:'unverified',note:'limited checks'}, {type:'done',status:'unverified'}]);
 assert.equal(withCheck.filter(t=>t.startsWith('Verification:')).length,1);
 assert.equal(withCheck.filter(t=>t.startsWith('Unverified:')).length,0);
 const fallback = await run([{type:'done',status:'unverified'}]);
 assert.equal(fallback.filter(t=>t.startsWith('Unverified:')).length,1);
 console.log('PASS one verification note with legacy unverified fallback');
})().catch(e=>{console.error(e);process.exitCode=1});
