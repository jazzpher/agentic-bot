const fs=require('fs'),vm=require('vm'),assert=require('assert');
const nodes={}; let calls=[];
function node(tag='div'){return {tag,children:[],textContent:'',className:'',hidden:undefined,appendChild(c){this.children.push(c);c.parentNode=this},remove(){if(this.parentNode)this.parentNode.children=this.parentNode.children.filter(c=>c!==this)},addEventListener(n,f){this[n]=f},focus(){}}}
const ctx={console,Set,Map,JSON,Promise,TextDecoder,setTimeout,clearTimeout,
  fetch:async u=>{calls.push(u);return {ok:true,json:async()=>({text:'hello\nworld',truncated:false})}},
  document:{getElementById:id=>nodes[id]||=(node()),createElement:node}};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync('agenticbot/static/app.js','utf8').replace('restoreTasks();','').replace('restoreHistory();','').replace('loadPolicy();','').replace('refreshKeyStatus();\n',''),ctx);
(async()=>{
const pane=ctx.document.getElementById('artifacts'), list=ctx.document.getElementById('artifactlist'), count=ctx.document.getElementById('artifactcount');
assert.equal(pane.hidden,undefined,'pane starts closed');
// a successful write shows up in the pane and opens it
const card=ctx.toolCard({id:'w1',name:'write_file',arguments:{path:'notes/todo.md'}});
ctx.finishToolCard({id:'w1',ok:true,output:'wrote 12 bytes'});
assert.equal(pane.hidden,false,'pane opens on the first artifact');
assert.equal(count.textContent,'1');
assert.equal(list.children.length,1);
const row=list.children[0];
assert.equal(row.className,'artifact-row');
assert.equal(row.children[0].children[0].textContent,'notes/todo.md');
assert.equal(row.children[0].children[1].href,'/files/notes/todo.md');
assert.equal(calls.length,0,'no preview fetched until asked');
// preview is on demand and inert text
await row.children[0].children[0].click();
assert.equal(calls.length,1);
assert(calls[0].includes('/file-preview?name=notes%2Ftodo.md'));
const pre=row.children[row.children.length-1];
assert.equal(pre.tag,'pre'); assert(pre.textContent.startsWith('hello'));
await row.children[0].children[0].click();
assert.equal(row.children.length,2,'second click closes the preview');
// the same path written twice is one row, newest first
ctx.finishToolCard({id:'w2',ok:true,output:'ok'});
const c2=ctx.toolCard({id:'w2',name:'edit_file',arguments:{path:'notes/todo.md'}});
ctx.finishToolCard({id:'w2',ok:true,output:'ok'});
assert.equal(list.children.length,1,'same path stays one row');
assert.equal(count.textContent,'1');
// a failed write is not an artifact
const c3=ctx.toolCard({id:'w3',name:'write_file',arguments:{path:'broken.txt'}});
ctx.finishToolCard({id:'w3',ok:false,output:'permission denied'});
assert.equal(list.children.length,1,'failed write not listed');
// a closed pane stays closed for later artifacts
ctx.toggleArtifacts(false);
assert.equal(pane.hidden,true);
const c4=ctx.toolCard({id:'w4',name:'create_docx',arguments:{path:'report.docx'}});
ctx.finishToolCard({id:'w4',ok:true,output:'ok'});
assert.equal(pane.hidden,true,'closed pane is not forced open');
assert.equal(list.children.length,2);
// reopening lists both, newest first
ctx.toggleArtifacts(true);
assert.equal(pane.hidden,false);
assert.equal(list.children.length,2);
assert.equal(list.children[0].children[0].children[0].textContent,'report.docx');
// clearing (new chat / other conversation) empties it
ctx.clearArtifacts();
assert.equal(list.children.length,1);
assert.equal(list.children[0].className,'artifact-empty');
assert.equal(count.textContent,'');
console.log('PASS artifacts pane: live capture, dedupe, on-demand inert preview, failed writes excluded, close respected, cleared per chat');
})().catch(e=>{console.error(e);process.exitCode=1});
