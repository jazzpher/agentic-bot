import json, os, sys, traceback
from pathlib import Path
from grokbot import cli, agent
from grokbot.tools import ToolContext
from grokbot.approvals import SandboxPolicy
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
BASE=Path(os.environ['SKILLS_ACCEPTANCE_DIR']); BASE.mkdir(parents=True, exist_ok=False)
ROOT=BASE/'workspace'; ROOT.mkdir(exist_ok=True)
KEY=Path(os.environ['NVIDIA_API_KEY_FILE']).read_text().strip()
cfg=(KEY,'https://integrate.api.nvidia.com/v1/chat/completions','nvidia/nemotron-3.5-super-vl-120b-a12b')
log=(BASE/'events.jsonl').open('a',buffering=1)
def rec(e):
 s=json.dumps(e,default=str).replace(KEY,'<REDACTED>'); log.write(s+'\n')
summary=[]
try:
 for phase,data,want in [('learn','team,amount\nA,2.50\nB,4.25\nA,1.25\n',{'A':'3.75','B':'4.25'}),('reuse','team,amount\nZ,10.10\nY,-2.20\nZ,0.05\n',{'Y':'-2.20','Z':'10.15'})]:
  (ROOT/'input.csv').write_text(data); output=ROOT/'totals.json'; output.unlink(missing_ok=True)
  ws=cli.build_workspace(str(ROOT)); ctx=ToolContext(workspace=ws,session_id='skills-'+phase,policy=SandboxPolicy(mode='workspace-write',approval='on-request',network=False),approvals=cli.TerminalApprovals(auto='allow'),extras={'run_source':'agent'})
  events=[]
  def emit(e): events.append(e); rec({'phase':phase,**e})
  def verify(contract,context):
   try: got=json.loads(output.read_text()); passed=got=={'totals':want,'method':'decimal-group-v1'}
   except Exception: passed=False
   rec({'type':'independent_acceptance','phase':phase,'passed':passed,'skills_before_commit':ws.skills.list()})
   return {'artifact':{'passed':passed,'evidence':'Independent exact JSON comparison including negative amounts and Decimal precision'}}
  prompt='Read input.csv in this workspace and write totals.json as JSON with totals mapping each team to its sum as a decimal string with exactly two places. Use Decimal, not floats. '
  if phase=='learn': prompt+='Include method=decimal-group-v1 in the JSON. Run your own checks. Then use save_skill to stage a reusable workflow named decimal-csv-totals describing the method marker, exact decimal arithmetic, grouping, output schema and verification. Do not embed source data or credentials. The host independently verifies the artifact before persisting the skill.'
  else: prompt+='Discover and read the relevant existing skill, and follow its output convention. Run your checks. This is a new conversation, use procedural memory rather than previous messages.'
  res=agent.run_turn([{'role':'user','content':prompt}],cli.default_registry(),ctx,cfg,emit,max_steps=30,context_tokens=32768,requirements=['artifact'],completion_verifier=verify)
  names=[c['function']['name'] for m in res.messages for c in m.get('tool_calls',[])]
  checks={'completed':res.status=='completed','artifact':verify(None,ctx)['artifact']['passed'],'skill_persisted':any(s['name']=='decimal-csv-totals' for s in ws.skills.list()),'retrieved':phase!='reuse' or 'read_skill' in names}
  summary.append({'phase':phase,'status':res.status,'tools':names,'checks':checks}); (BASE/'summary.json').write_text(json.dumps(summary,indent=2))
  rec({'type':'phase_summary',**summary[-1]})
  assert all(checks.values()), checks
except Exception:
 rec({'type':'failure','traceback':traceback.format_exc()}); raise
finally:
 (BASE/'done.flag').write_text('done')
