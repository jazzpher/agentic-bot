"""Optional Chromium stress probe; requires playwright and two running Web UIs.

Usage: python acceptance/lightweight_ux_benchmark.py --baseline http://127.0.0.1:8792 --candidate http://127.0.0.1:8791 --output resources.json
Use isolated workspaces. This injects presentation events, not model calls; do not
interpret headless Linux measurements as laptop or whole-agent measurements.
"""
import argparse, asyncio, json, time
from pathlib import Path
from playwright.async_api import async_playwright
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--baseline',required=True)
parser.add_argument('--candidate',required=True)
parser.add_argument('--output',default='resources.json')
options=parser.parse_args()
async def main():
 rows=[]
 async with async_playwright() as p:
  for run in range(5):
   for label,url in ([('baseline',options.baseline),('changed',options.candidate)] if run%2==0 else [('changed',options.candidate),('baseline',options.baseline)]):
    browser=await p.chromium.launch(headless=True,args=['--no-sandbox'])
    page=await browser.new_page(viewport={'width':1365,'height':900})
    await page.goto(url);await page.wait_for_timeout(600)
    cdp=await page.context.new_cdp_session(page);await cdp.send('Performance.enable')
    await cdp.send('HeapProfiler.collectGarbage')
    async def metrics():return {x['name']:x['value'] for x in (await cdp.send('Performance.getMetrics'))['metrics']}
    before=await metrics();start=time.monotonic()
    await page.evaluate('''() => {for(let i=0;i<100;i++){toolCard({id:'bench'+i,name:'exec',arguments:{command:'printf benchmark'}});finishToolCard(JSON.parse(JSON.stringify({id:'bench'+i,ok:true,output:('benchmark '+i+' '+'.'.repeat(49970)),duration:0.1})));}if(typeof endToolGroup==='function')endToolGroup();}''')
    await page.wait_for_timeout(400);await cdp.send('HeapProfiler.collectGarbage');after=await metrics();
    procinfo=(await (await browser.new_browser_cdp_session()).send('SystemInfo.getProcessInfo'))['processInfo'];rss_kib=pss_kib=0
    for row in procinfo:
     try:
      for line in Path('/proc/'+str(row['id'])+'/smaps_rollup').read_text().splitlines():
       if line.startswith('Rss:'):rss_kib+=int(line.split()[1])
       elif line.startswith('Pss:'):pss_kib+=int(line.split()[1])
     except (OSError,ValueError):pass
    processing_wall=time.monotonic()-start
    idle_start=after;await page.wait_for_timeout(3000);activewait=await metrics()
    await page.evaluate("if(typeof endToolGroup==='function')endToolGroup()")
    completed_start=await metrics();await page.wait_for_timeout(3000);idle=await metrics()
    rows.append({'version':label,'run':run+1,'heap_before_bytes':before['JSHeapUsedSize'],'heap_after_bytes':after['JSHeapUsedSize'],'heap_delta_bytes':after['JSHeapUsedSize']-before['JSHeapUsedSize'],'cpu_task_seconds':after['TaskDuration']-before['TaskDuration'],'wall_seconds':processing_wall,'idle_cpu_task_seconds_first_3s':activewait['TaskDuration']-idle_start['TaskDuration'],'idle_cpu_task_seconds_next_3s':idle['TaskDuration']-completed_start['TaskDuration'],'chromium_process_rss_kib':rss_kib,'chromium_process_pss_kib':pss_kib,'dom_nodes':after['Nodes'],'rendered_tool_cards':await page.locator('.toolcard').count(),'animations_after_completion':await page.evaluate('document.getAnimations().length')})
    if label=='changed':
     assert rows[-1]['rendered_tool_cards']==24, 'tool-card cap regressed'
     assert rows[-1]['animations_after_completion']==0, 'completed tools still animate'
    await browser.close()
 Path(options.output).write_text(json.dumps(rows,indent=2))
 print(json.dumps(rows,indent=2))
asyncio.run(main())
