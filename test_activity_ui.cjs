const fs=require('fs'),vm=require('vm'),assert=require('assert');
const nodes={}; let calls=[];
function node(tag='div'){return {tag,children:[],textContent:'',className:'',appendChild(c){this.children.push(c);c.parentNode=this},remove(){if(this.parentNode)this.parentNode.children=this.parentNode.children.filter(c=>c!==this)},addEventListener(n,f){this[n]=f},focus(){}}}
const ctx={console,Set,Map,JSON,Promise,TextDecoder,setTimeout,clearTimeout,fetch:async u=>{calls.push(u);return {ok:true,json:async()=>({text:'<script>bad()</script>',truncated:true})}},document:{getElementById:id=>nodes[id]||=(node()),createElement:node}};
vm.createContext(ctx);vm.runInContext(fs.readFileSync('agenticbot/static/app.js','utf8').replace('restoreTasks();','').replace('restoreHistory();','').replace('loadPolicy();','').replace('refreshKeyStatus();\n',''),ctx);
(async()=>{
for(let i=0;i<100;i++) {ctx.toolCard({id:'t'+i,name:'exec',arguments:{command:'echo '+i}});ctx.finishToolCard({id:'t'+i,ok:i%2===0,output:'x'.repeat(50000),duration:0.1});}
const group=nodes.chat.children[0];assert.equal(group._body.children.length,24);assert(group._summary.textContent.includes('100'));assert(group._summary.textContent.includes('latest 24'));
const card=group._cards[0];assert.equal(card.children.length,1);card.open=true;card.toggle();assert(card._out.textContent.length<12200);assert(card._out.textContent.includes('display clipped'));assert.equal(card.children[0].children[2].textContent,'succeeded · 0.1s');
const failed=ctx.toolCard({id:'nonzero',name:'run_command',arguments:{}});ctx.finishToolCard({id:'nonzero',ok:true,output:'AssertionError\n[exit code 1 · 1.8s · docker:python:3.12-slim]'});assert.equal(failed.className,'toolcard err');assert.equal(failed._meta.textContent,'failed');
const passed=ctx.toolCard({id:'zero',name:'run_command',arguments:{}});ctx.finishToolCard({id:'zero',ok:true,output:'OK\n[exit code 0 · 1.8s · docker:python:3.12-slim]'});assert.equal(passed.className,'toolcard ok');
let cancelled=0;group.getAnimations=()=>[{cancel(){cancelled++}}];
const pending=ctx.toolCard({id:'pending',name:'exec',arguments:{}});ctx.endToolGroup();assert.equal(cancelled,1);assert.equal(pending._meta.textContent,'outcome not received');assert.equal(group.open,false);
const artifact=ctx.artifactCard('folder/a #.html');assert.equal(calls.length,0);artifact.open=true;await artifact.toggle();assert.equal(calls.length,1);assert(calls[0].includes('folder%2Fa%20%23.html'));assert(artifact.children[2].textContent.startsWith('<script>'));assert.equal(artifact.children[2].tag,'pre');await artifact.toggle();assert.equal(calls.length,1);
const css=fs.readFileSync('agenticbot/static/app.css','utf8');assert(css.includes('prefers-reduced-motion'));console.log('PASS bounded logs, explicit tool outcomes, on-demand inert artifact preview, reduced motion');
})().catch(e=>{console.error(e);process.exitCode=1});
