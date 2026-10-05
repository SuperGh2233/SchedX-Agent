from __future__ import annotations
import datetime,hashlib,json,os,pathlib,subprocess,sys,time
ROOT=pathlib.Path('/tmp/schedx-acceptance-20261005-79905fb')
SOURCE=ROOT/'candidate-d1baee7'
REV='d1baee75b955c36452917af2836aa708c30c2ce3'
OUTPUT=ROOT/'evidence/final-validation-d1baee7'
OUTPUT.mkdir(exist_ok=False)
sys.path.insert(0,str(SOURCE))
from schedx.benchmark.versioning import version_identity
from schedx.state import atomic_json
identity=version_identity(ROOT/'repository.git',SOURCE,REV,ROOT/'native/candidate/build/bin/scx_agent',ROOT/'evidence/build-candidate-d1baee7.json')
env=dict(os.environ)
env['PATH']=str(ROOT/'native/candidate/build/bin')+os.pathsep+env['PATH']
env['PYTHONPATH']=str(SOURCE)
python=sys.executable
comparison=[python,str(SOURCE/'scripts/run_final_round_comparison.py'),'--repository',str(ROOT/'repository.git'),'--baseline-source',str(ROOT/'baseline'),'--baseline-ref','b0591a10785b74cf980c8f0b4ed703a9a95e1aa2','--baseline-binary',str(ROOT/'native/baseline/build/bin/scx_agent'),'--baseline-build-manifest',str(ROOT/'evidence/build-baseline.json'),'--candidate-source',str(SOURCE),'--candidate-ref',REV,'--candidate-binary',str(ROOT/'native/candidate/build/bin/scx_agent'),'--candidate-build-manifest',str(ROOT/'evidence/build-candidate-d1baee7.json'),'--output',str(OUTPUT/'shared-throughput-comparison'),'--repeats','5','--duration','10','--workers','3','--batch-threads','3','--profile','shared-throughput']
autonomous=[python,str(SOURCE/'scripts/verify_autonomous_flow.py'),'--repository',str(ROOT/'repository.git'),'--candidate-ref',REV,'--binary',str(ROOT/'native/candidate/build/bin/scx_agent'),'--build-manifest',str(ROOT/'evidence/build-candidate-d1baee7.json'),'--output',str(OUTPUT/'autonomous-flow'),'--rounds-per-stage','15','--duration','20','--workers','3']
stages=[('shared-throughput-comparison',comparison),('autonomous-flow',autonomous),('stability-30min',[python,str(SOURCE/'scripts/verify_optimization.py'),'--duration','1800','--output',str(OUTPUT/'stability-30min')]),('stability-2h',[python,str(SOURCE/'scripts/verify_optimization.py'),'--duration','7200','--fairness-only','--output',str(OUTPUT/'stability-2h')])]
def now():return datetime.datetime.now(datetime.timezone.utc).isoformat()
status={'status':'running','pid':os.getpid(),'candidate_commit':REV,'version_identity':identity,'guest_started_utc':now(),'stages':[],'harness_sha256':{name:hashlib.sha256(pathlib.Path(command[1]).read_bytes()).hexdigest() for name,command in stages}}
start=time.monotonic()
atomic_json(OUTPUT/'pipeline.json',status)
try:
 for name,command in stages:
  assert pathlib.Path('/sys/kernel/sched_ext/state').read_text().strip()=='disabled'
  row={'name':name,'status':'running','command':command,'guest_started_utc':now()}
  status['stages'].append(row);atomic_json(OUTPUT/'pipeline.json',status)
  with (OUTPUT/(name+'.log')).open('w') as log:
   process=subprocess.Popen(command,cwd=SOURCE,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT)
   row['pid']=process.pid;atomic_json(OUTPUT/'pipeline.json',status)
   returncode=process.wait()
  summary=json.loads((OUTPUT/name/'summary.json').read_text()) if (OUTPUT/name/'summary.json').exists() else {}
  cleanup=summary.get('final_scheduler_state',summary.get('final_sched_ext_state'))
  passed=returncode==0 and summary.get('status')=='passed' and cleanup=='disabled'
  row.update(status='passed' if passed else 'failed',returncode=returncode,summary_status=summary.get('status'),final_scheduler_state=cleanup,guest_finished_utc=now())
  atomic_json(OUTPUT/'pipeline.json',status)
  if not passed: raise RuntimeError(name+' failed; evidence preserved, later stages not started')
 assert version_identity(ROOT/'repository.git',SOURCE,REV,ROOT/'native/candidate/build/bin/scx_agent')['source_sha256']==identity['source_sha256']
 status['status']='passed'
except BaseException as exc:
 status.update(status='failed',error=f'{type(exc).__name__}: {exc}')
finally:
 status.update(guest_finished_utc=now(),elapsed_seconds=time.monotonic()-start,final_scheduler_state=pathlib.Path('/sys/kernel/sched_ext/state').read_text().strip())
 atomic_json(OUTPUT/'pipeline.json',status)
 print(json.dumps(status),flush=True)
sys.exit(0 if status['status']=='passed' else 1)
