import os,sys,json,time,threading,subprocess,traceback,re,tarfile,collections
sys.path.insert(0,'/workspaces/grok-bot'); os.chdir('/workspaces/grok-bot')
from grokbot import cli,agent,providers
from grokbot.tools import ToolContext
from grokbot.approvals import SandboxPolicy
BASE='/workspaces/nim-pr57'; ROOT=BASE+'/taskforge'; LOG=BASE+'/events.jsonl'
os.makedirs(ROOT,exist_ok=True); lf=open(LOG,'a',buffering=1)
KEY=open('/workspaces/.secrets/nvidia').read().strip()
def clean(x):
    if isinstance(x,str):
        x=re.sub(r'nvapi-[A-Za-z0-9_-]+','<REDACTED>',x.replace(KEY,'<REDACTED>'))
        return x[:80]+'...[%d chars]'%len(x) if x.startswith('data:image') else x[:6000]
    if isinstance(x,dict): return {k:clean(v) for k,v in x.items()}
    if isinstance(x,(list,tuple)): return [clean(v) for v in x]
    return x
def rec(o): lf.write(json.dumps(clean({'time':round(time.time(),2),**o}),default=str)+'\n')
MODEL='nvidia/nemotron-3.5-super-vl-120b-a12b'
cfg=(KEY,'https://integrate.api.nvidia.com/v1/chat/completions',MODEL)
BUILD=('Build TaskForge, a stdlib-only Python SQLite task service, as a package named taskforge in this empty workspace. Public contract: '
 'python -m taskforge serve --db PATH --port N binds 127.0.0.1. GET /tasks returns a JSON array. POST /tasks {"title": string} returns 201 and the task object {id,title,done}. '
 'PATCH /tasks/ID {"done": boolean} returns the updated object. Unknown ID gives 404. Blank or non-string title and invalid JSON give 400. '
 'GET /tasks?done=true|false filters, any other value 400. An Idempotency-Key header on POST returns the same task ID on repeat. '
 'Writes are transactional and safe under concurrent requests, and data persists in the SQLite file. CLI: python -m taskforge add --db PATH --title TEXT and python -m taskforge list --db PATH (list prints each task title). '
 'GET / serves a usable HTML web UI for the tasks. The v1 SQLite schema is a table tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0). '
 'Write README.md and runnable unittest tests (test*.py, runnable with python -m unittest discover). Actually run your tests. No network or third-party dependencies. Do not stop with a plan.')
EVOLVE=('Evolve TaskForge without breaking v1. Add a nullable due RFC3339 timestamp (SQLite column due TEXT): normalize offsets to UTC formatted YYYY-MM-DDTHH:MM:SSZ; reject naive, invalid or non-string due with 400 (null allowed). '
 'GET /tasks?due_before=TIMESTAMP uses a STRICT earlier-than comparison and excludes tasks with null due; an invalid or timezone-less query value gives 400. '
 'Add archived boolean (SQLite column archived INTEGER NOT NULL DEFAULT 0), default false. PATCH supports archiving and unarchiving; a non-boolean gives 400. '
 'The default list EXCLUDES archived tasks. archived=true returns ONLY archived tasks, archived=false only active ones, any other value 400. Filters combine. '
 'Safely migrate an existing v1 tasks table in place, preserving data, for both HTTP and CLI and on repeated startup. Keep idempotency and concurrent writes working. '
 'Add docs/API.md documenting every endpoint, due_before, archived and idempotency; docs/MIGRATION.md with the schema change, backups and safe rollout; docs/RECOVERY.md covering recovery from an interrupted run and how to verify it. '
 'Add and actually run edge-case unit tests. Check the original request end to end; do not claim completion from your own tests alone.')
RESUME='Continue the interrupted evolution task. Audit every original requirement, repair failures, run the tests and finish the docs.'
open(BASE+'/requirements.txt','w').write('RECONSTRUCTED TASK (the 6 Nov harness was lost), not a bit-for-bit copy.\n\nBUILD:\n'+BUILD+'\n\nEVOLVE:\n'+EVOLVE+'\n\nRESUME:\n'+RESUME+'\n')
COMMIT=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
rec({'type':'start','model':MODEL,'commit':COMMIT,'reconstructed':True})
BUILD_NAMES=['server_starts','http_create','http_list','blank_title','nonstring_title','invalid_json','missing_404','patch_done','done_filter','invalid_done_filter','web_ui','idempotency','concurrent_writes','cli','readme','own_tests_present','own_tests_pass','persistence']
EVOLVE_NAMES=BUILD_NAMES+['due_utc_normalization','due_reject_naive','due_reject_invalid','due_reject_nonstring','due_strict_boundary','due_invalid_query','due_naive_query','archive_default_filter_unarchive','archive_invalid_query','archive_invalid_patch','docs_api','docs_migration','docs_recovery','legacy_migration']
def acceptance(phase):
    r=subprocess.run([sys.executable,'/workspaces/harness/nim_acceptance.py',ROOT,phase],capture_output=True,text=True,timeout=400)
    if r.returncode: raise RuntimeError(r.stderr[-3000:])
    return json.loads(r.stdout.strip().splitlines()[-1])
def summarize(msg): return providers.complete_once(cfg,msg,max_tokens=1200)
msgs=[]; previous=''; overall=[]; T0=time.time()
for phase,prompt,stop_after,resume in [('build',BUILD,0,False),('evolve',EVOLVE,8,False),('resume',RESUME,0,True)]:
    stop=threading.Event(); stats={'tools':0,'compactions':0,'checks':0,'types':collections.Counter()}
    ctx=ToolContext(workspace=cli.build_workspace(ROOT),session_id='nim-pr57',policy=SandboxPolicy(mode='workspace-write',approval='on-request',network=False),
        approvals=cli.TerminalApprovals(auto='allow'),stop=stop,extras={'run_source':'agent'})
    def emit(ev,phase=phase,stop=stop,stats=stats,stop_after=stop_after):
        rec({'phase':phase,**ev}); t=str(ev.get('type','')); stats['types'][t]+=1
        if 'tool' in t and any(k in t for k in ('result','end','done')):
            stats['tools']+=1
            if stop_after and stats['tools']==stop_after:
                stop.set(); rec({'type':'controlled_stop','phase':phase,'after_tools':stats['tools']})
        if 'compact' in t or ('compact' in str(ev.get('text','')).lower() and t=='status'): stats['compactions']+=1
        if t=='completion_check': stats['checks']+=1
    def verify(contract,ctx,phase=phase):
        out=acceptance('build' if phase=='build' else 'evolve'); rec({'type':'independent_acceptance','phase':phase,'checks':out})
        open(BASE+'/acceptance-'+phase+'.json','w').write(json.dumps(out,indent=2)); return out
    msgs.append({'role':'user','content':prompt}); t=time.time()
    try:
        res=agent.run_turn(msgs,cli.default_registry(),ctx,cfg,emit,max_steps=150,context_tokens=32768,model_label=MODEL,summarizer=summarize,
            previous_summary=previous,requirements=None if resume else (BUILD_NAMES if phase=='build' else EVOLVE_NAMES),completion_verifier=verify,completion_resume=resume)
        msgs=res.messages; previous=res.summary
        summary={'phase':phase,'status':res.status,'text':res.text,'steps':res.steps,'tool_calls':res.tool_calls,'usage':res.usage,'seconds':round(time.time()-t,1)}
        open(BASE+'/conversation-'+phase+'.json','w').write(json.dumps(clean(msgs),indent=1,default=str))
    except Exception:
        summary={'phase':phase,'status':'exception','traceback':traceback.format_exc(),'seconds':round(time.time()-t,1)}
    summary.update(tools_seen=stats['tools'],compactions=stats['compactions'],completion_checks=stats['checks'],event_types=dict(stats['types']))
    rec({'type':'phase_summary',**summary}); overall.append(clean(summary)); json.dump(overall,open(BASE+'/summary.json','w'),indent=2,default=str)
    with tarfile.open(BASE+'/snapshot-'+phase+'.tar.gz','w:gz') as tf: tf.add(ROOT,arcname='taskforge')
    if summary['status']=='exception': break
try:
    out=acceptance('evolve'); json.dump(out,open(BASE+'/acceptance-final.json','w'),indent=2)
    rec({'type':'final_acceptance','passed':sum(1 for v in out.values() if v['passed']),'total':len(out),'checks':out})
except Exception:
    rec({'type':'verifier_error','traceback':traceback.format_exc()})
rec({'type':'done','seconds':round(time.time()-T0,1)})
open(BASE+'/done.flag','w').write('finished')
