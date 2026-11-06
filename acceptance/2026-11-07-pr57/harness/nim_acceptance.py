import os,sys,json,tempfile,subprocess,socket,time,urllib.request,urllib.error,sqlite3,concurrent.futures
ROOT=sys.argv[1]; PHASE=sys.argv[2]; results={}
def check(name,fn):
    try:
        fn(); results[name]={'passed':True,'evidence':name+' passed'}
    except Exception as e:
        results[name]={'passed':False,'evidence':type(e).__name__+': '+str(e)[:700]}
def must(v,msg='assertion failed'):
    if not v: raise AssertionError(msg)
tmp=tempfile.TemporaryDirectory(); db=tmp.name+'/tasks.db'
s=socket.socket(); s.bind(('127.0.0.1',0)); port=s.getsockname()[1]; s.close(); base='http://127.0.0.1:%d'%port
env=dict(os.environ); env['PYTHONPATH']=ROOT
p=subprocess.Popen([sys.executable,'-m','taskforge','serve','--db',db,'--port',str(port)],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
def raw(path,method='GET',data=None,headers=None):
    r=urllib.request.Request(base+path,data=data,method=method,headers={'Content-Type':'application/json',**(headers or {})})
    try:
        with urllib.request.urlopen(r,timeout=5) as f: st=f.status; b=f.read()
    except urllib.error.HTTPError as e:
        st=e.code; b=e.read()
    try: o=json.loads(b)
    except Exception: o=b.decode(errors='replace')
    return st,o
def req(path,method='GET',body=None,headers=None):
    return raw(path,method,json.dumps(body).encode() if body is not None else None,headers)
up=False
for _ in range(100):
    try:
        raw('/tasks'); up=True; break
    except Exception: time.sleep(0.1)
check('server_starts',lambda:must(up,'server did not answer within 10s'))
ids=[]
def create(title='alpha',**kw):
    st,o=req('/tasks','POST',dict(title=title,**kw)); must(st==201,str((st,o))); must(isinstance(o,dict) and 'id' in o,str(o)); return o
check('http_create',lambda:ids.append(create()['id']))
def lst():
    st,o=req('/tasks'); must(st==200 and isinstance(o,list),str((st,o)))
check('http_list',lst)
check('blank_title',lambda:must(req('/tasks','POST',{'title':'  '})[0]==400))
check('nonstring_title',lambda:must(req('/tasks','POST',{'title':22})[0]==400))
check('invalid_json',lambda:must(raw('/tasks','POST',b'{bad')[0]==400))
check('missing_404',lambda:must(req('/tasks/999999','PATCH',{'done':True})[0]==404))
def patch():
    st,o=req('/tasks/%s'%ids[0],'PATCH',{'done':True}); must(st==200 and o.get('done') is True,str((st,o)))
check('patch_done',patch)
def donef():
    st,o=req('/tasks?done=true'); must(st==200 and o and all(t['done'] is True for t in o),str((st,o)))
check('done_filter',donef)
check('invalid_done_filter',lambda:must(req('/tasks?done=wat')[0]==400))
def ui():
    st,o=raw('/'); must(st==200 and isinstance(o,str) and '<html' in o.lower(),str(st))
check('web_ui',ui)
def idem():
    h={'Idempotency-Key':'acceptance-one'}; a=req('/tasks','POST',{'title':'idem'},h); b=req('/tasks','POST',{'title':'idem'},h)
    must(a[0] in (200,201) and b[0] in (200,201) and a[1]['id']==b[1]['id'],str((a,b)))
check('idempotency',idem)
def conc():
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex: rows=list(ex.map(lambda i:create('parallel%d'%i),range(12)))
    must(len({r['id'] for r in rows})==12)
check('concurrent_writes',conc)
def run(*a,t=30):
    return subprocess.run([sys.executable,'-m',*a],cwd=ROOT,env=env,capture_output=True,text=True,timeout=t)
def clic():
    r=run('taskforge','add','--db',db,'--title','cli-task'); must(r.returncode==0,r.stderr[-500:])
    r=run('taskforge','list','--db',db); must(r.returncode==0 and 'cli-task' in r.stdout,(r.stderr+r.stdout)[-500:])
check('cli',clic)
check('readme',lambda:must(os.path.isfile(ROOT+'/README.md') and len(open(ROOT+'/README.md').read())>300))
check('own_tests_present',lambda:must(any(f.startswith('test') and f.endswith('.py') for _,_,fs in os.walk(ROOT) for f in fs)))
def owntests():
    r=run('unittest','discover','-s','.','-p','test*.py',t=150); must(r.returncode==0 and 'Ran ' in r.stderr,r.stderr[-700:])
check('own_tests_pass',owntests)
if PHASE=='evolve':
    def due_norm():
        o=create('tz',due='2030-01-01T10:00:00+02:00'); must(o.get('due')=='2030-01-01T08:00:00Z',str(o))
    check('due_utc_normalization',due_norm)
    check('due_reject_naive',lambda:must(req('/tasks','POST',{'title':'bad','due':'2030-01-01T08:00:00'})[0]==400))
    check('due_reject_invalid',lambda:must(req('/tasks','POST',{'title':'bad','due':'not-date'})[0]==400))
    check('due_reject_nonstring',lambda:must(req('/tasks','POST',{'title':'bad','due':12})[0]==400))
    def boundary():
        a=create('before',due='2030-01-01T07:59:59Z'); b=create('equal',due='2030-01-01T08:00:00Z'); c=create('after',due='2030-01-01T08:00:01Z')
        st,o=req('/tasks?due_before=2030-01-01T08%3A00%3A00Z'); must(st==200,str((st,o))); got={t['id'] for t in o}
        must(a['id'] in got and b['id'] not in got and c['id'] not in got and ids[0] not in got,str(o))
    check('due_strict_boundary',boundary)
    check('due_invalid_query',lambda:must(req('/tasks?due_before=garbage')[0]==400))
    check('due_naive_query',lambda:must(req('/tasks?due_before=2030-01-01T08%3A00%3A00')[0]==400))
    def archive():
        a=create('archived-one'); st,o=req('/tasks/%s'%a['id'],'PATCH',{'archived':True}); must(st==200 and o.get('archived') is True,str((st,o)))
        must(a['id'] not in {t['id'] for t in req('/tasks')[1]},'archived task in default list')
        st,o=req('/tasks?archived=true'); must(st==200 and a['id'] in {t['id'] for t in o} and all(t['archived'] is True for t in o),str(o))
        st,o=req('/tasks?archived=false'); must(st==200 and all(t['archived'] is False for t in o),str(o))
        st,o=req('/tasks/%s'%a['id'],'PATCH',{'archived':False}); must(st==200 and o.get('archived') is False,str((st,o)))
    check('archive_default_filter_unarchive',archive)
    check('archive_invalid_query',lambda:must(req('/tasks?archived=wat')[0]==400))
    check('archive_invalid_patch',lambda:must(req('/tasks/%s'%ids[0],'PATCH',{'archived':'yes'})[0]==400))
    def doc(n,words,size=150):
        f=ROOT+'/docs/'+n; must(os.path.isfile(f),'missing '+n); t=open(f).read().lower(); must(len(t)>size and all(w in t for w in words),n+' lacks '+str(words))
    check('docs_api',lambda:doc('API.md',['due_before','archived','idempotency']))
    check('docs_migration',lambda:doc('MIGRATION.md',['backup']))
    check('docs_recovery',lambda:doc('RECOVERY.md',['verif']))
    def migration():
        old=tmp.name+'/old.db'; c=sqlite3.connect(old)
        c.execute('CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT NOT NULL,done INTEGER NOT NULL DEFAULT 0)')
        c.execute('INSERT INTO tasks(title,done) VALUES (?,?)',('legacy',0)); c.commit(); c.close()
        for _ in range(2):
            r=run('taskforge','list','--db',old); must(r.returncode==0 and 'legacy' in r.stdout,(r.stderr+r.stdout)[-500:])
        c=sqlite3.connect(old); cols={x[1] for x in c.execute('PRAGMA table_info(tasks)')}; must({'due','archived'}<=cols,str(cols))
        row=c.execute('SELECT title,done,due,archived FROM tasks').fetchone(); c.close(); must(row==('legacy',0,None,0),str(row))
    check('legacy_migration',migration)
p.terminate()
try: p.wait(timeout=5)
except Exception: p.kill()
def persist():
    r=run('taskforge','list','--db',db); must(r.returncode==0 and 'alpha' in r.stdout,(r.stderr+r.stdout)[-500:])
check('persistence',persist)
print(json.dumps(results,sort_keys=True))
