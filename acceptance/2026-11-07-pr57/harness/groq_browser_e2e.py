
import os, sys, json, time, threading, inspect, re, traceback
os.environ['GROK_AGENT_BROWSER']='on'
os.environ.setdefault('GROK_AGENT_BROWSER_IMAGES','on')
sys.path.insert(0,'/workspaces/grok-bot'); os.chdir('/workspaces/grok-bot')
MODEL=sys.argv[1]; TAG=sys.argv[2]
LOG='/workspaces/logs/groq-browser-%s.jsonl'%TAG
os.makedirs('/workspaces/logs',exist_ok=True); os.makedirs('/workspaces/e2e/'+TAG,exist_ok=True)
lf=open(LOG,'w')
def rec(o):
    def clean(x):
        if isinstance(x,dict): return {k:clean(v) for k,v in x.items()}
        if isinstance(x,list): return [clean(v) for v in x]
        if isinstance(x,str):
            x=re.sub(r'gsk_[A-Za-z0-9]+','gsk*',x)
            return x[:80]+'...[%d chars]'%len(x) if x.startswith('data:image') else x[:3000]
        return x
    o=clean(o); o['t']=round(time.time(),2); lf.write(json.dumps(o,default=str)+'\n'); lf.flush()
from grokbot import cli, agent as agent_core, providers
from grokbot.tools import ToolContext
from grokbot.approvals import SandboxPolicy
from grokbot.takeover import TakeoverBroker, validate_input
import grokbot.takeover as tk
rec({'type':'harness','model':MODEL,'commit':os.popen('git rev-parse HEAD').read().strip(),'validate_input_src':inspect.getsource(validate_input)[:2500]})
orig=providers.wire_messages
wire_stats={'calls':0,'private_key_leaks':0,'image_parts':0}
def wrapped(*a,**k):
    out=orig(*a,**k)
    wire_stats['calls']+=1
    for msg in out:
        if any(str(key).startswith('_') for key in msg): wire_stats['private_key_leaks']+=1
        c=msg.get('content')
        if isinstance(c,list): wire_stats['image_parts']+=sum(1 for p in c if isinstance(p,dict) and p.get('type')=='image_url')
    rec({'type':'wire','stats':dict(wire_stats),'n':len(out)})
    return out
providers.wire_messages=wrapped
key=open('/workspaces/.secrets/groq').read().strip()
cfg=(key,'https://api.groq.com/openai/v1/chat/completions',MODEL)
broker=TakeoverBroker(); SID='groq-e2e'
ctx=ToolContext(workspace=cli.build_workspace('/workspaces/e2e/'+TAG),session_id=SID,
    policy=SandboxPolicy(mode='workspace-write',approval='on-request',network=False),
    approvals=cli.TerminalApprovals(auto='allow'),stop=threading.Event(),
    extras={'run_source':'agent','takeovers':broker})
summary={'takeover_required':0,'frames':0,'inputs':[],'done_ok':None,'errors':[]}
def user_takes_over(tid):
    t0=time.time(); tko=None
    while time.time()-t0<60:
        tko=broker.get(tid,SID)
        if tko and tko.get_frame(): break
        time.sleep(0.5)
    fr=tko.get_frame() if tko else None
    summary['frames']=fr[1] if fr else 0
    rec({'type':'h_frame','seq':summary['frames'],'bytes':len(fr[0]) if fr else 0})
    vp=tko.viewport if tko else {}
    for ev in ({'type':'click','x':80,'y':200},{'kind':'click','x':80,'y':200}):
        try:
            r=broker.send_input(tid,SID,ev); summary['inputs'].append({'ev':ev,'queued':r}); rec({'type':'h_input','ev':ev,'queued':r}); break
        except Exception as e:
            summary['inputs'].append({'ev':ev,'err':str(e)}); rec({'type':'h_input_err','ev':ev,'err':str(e)})
    time.sleep(4)
    summary['frames_after']=(tko.get_frame() or (b'',0))[1] if tko else 0
    summary['done_ok']=broker.finish(tid,SID); rec({'type':'h_done','ok':summary['done_ok'],'frames_after':summary['frames_after']})
def emit(ev):
    rec(dict(ev))
    if ev.get('type')=='takeover_required':
        summary['takeover_required']+=1
        threading.Thread(target=user_takes_over,args=(ev['id'],),daemon=True).start()
    if ev.get('type')=='error': summary['errors'].append(ev.get('text'))
prompt=('Use the browser. 1) browse_open https://example.com and tell me the main heading you see. '
 '2) Then call request_takeover with the reason "please click the link on the page" so I can drive it. '
 '3) After I finish, call browse_see once and report the page heading and URL you see now. Keep it short.')
t0=time.time()
try:
    res=agent_core.run_turn([{'role':'user','content':prompt}],cli.default_registry(),ctx,cfg,emit,max_steps=15,model_label=MODEL)
    summary.update(status=res.status,text=res.text,steps=res.steps,tool_calls=res.tool_calls,usage=res.usage)
except Exception as e:
    summary.update(status='exception',exc=traceback.format_exc())
summary['seconds']=round(time.time()-t0,1); summary['wire']=wire_stats; summary['model']=MODEL
rec({'type':'summary',**summary})
json.dump(summary,open('/workspaces/logs/groq-browser-%s-summary.json'%TAG,'w'),indent=1,default=str)
print('HARNESS_DONE',TAG)

