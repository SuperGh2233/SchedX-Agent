import pathlib,sys,os,time,json,subprocess
root=pathlib.Path('/tmp/schedx-acceptance-20261005-79905fb')
sys.path.insert(0,str(root/'candidate-d1baee7'))
from scripts.run_final_round_comparison import NativeSession
from schedx.benchmark.scoped_workloads import OwnedWorkloads,process_ticks
from schedx.state import atomic_json
output=root/'evidence/fairness-diagnostic-1';output.mkdir(exist_ok=False)
allowed=sorted(os.sched_getaffinity(0));os.sched_setaffinity(0,{allowed[-1]})
work=OwnedWorkloads(output/'workloads',workers=3,cpus=set(allowed[:-1]))
result={'status':'running','purpose':'diagnose runtime effect and actual kernel map values; not a repeated performance claim','rows':[],'candidate_commit':'d1baee75b955c36452917af2836aa708c30c2ce3'}
session=None
try:
 work.__enter__();work.noise(True)
 batch_command=['sysbench','cpu','--threads=3','--time=15','run']
 work.spawn('batch',batch_command)
 time.sleep(1)
 before=process_ticks(work.pids('noise'));start=time.monotonic();time.sleep(10)
 result['cfs_background_cpu_seconds_per_second']=(process_ticks(work.pids('noise'))-before)/os.sysconf('SC_CLK_TCK')/(time.monotonic()-start)
 work.stop('batch');work.stop('noise')
 native_log=output/'native';native_log.mkdir()
 session=NativeSession(root/'native/candidate/build/bin/scx_agent',work.groups,native_log,profile='shared-throughput',case='batch')
 session.__enter__();work.noise(True);work.spawn('batch',['sysbench','cpu','--threads=3','--time=100','run'])
 for interval in (64,40,20,5,1):
  assert session.controller.set_fairness(interval,32)
  time.sleep(1)
  before_cpu=process_ticks(work.pids('noise'));before_metrics=session.controller.get_cgroup_metrics();before_class=session.controller.get_class_metrics()
  start=time.monotonic();time.sleep(10);elapsed=time.monotonic()-start
  cpu=(process_ticks(work.pids('noise'))-before_cpu)/os.sysconf('SC_CLK_TCK')/elapsed
  maps=[]
  for m in json.loads(subprocess.check_output(['bpftool','-j','map','show'])):
   if any(part in m.get('name','') for part in ('schedul','dispatch_ctx','cgroup','class_metric')):
    try:maps.append({'metadata':m,'values':json.loads(subprocess.check_output(['bpftool','-j','map','dump','id',str(m['id'])]))})
    except subprocess.CalledProcessError: pass
  row={'interval':interval,'background_cpu_seconds_per_second':cpu,'retention':cpu/result['cfs_background_cpu_seconds_per_second'],'before_metrics':before_metrics,'after_metrics':session.controller.get_cgroup_metrics(),'before_class':before_class,'after_class':session.controller.get_class_metrics(),'kernel_maps':maps,'live_tasks':{str(p):pathlib.Path(f'/proc/{p}/cgroup').read_text().strip() for p in work.pids()}}
  result['rows'].append(row);atomic_json(output/'summary.json',result)
  print(json.dumps({'interval':interval,'background_retention':row['retention']}),flush=True)
 result['status']='passed'
except BaseException as exc:result.update(status='failed',error=f'{type(exc).__name__}: {exc}')
finally:
 if session:session.close();result['native_cleanup']=session.evidence
 work.close();os.sched_setaffinity(0,set(allowed));result['cleanup']=work.cleanup
 result['final_state']=pathlib.Path('/sys/kernel/sched_ext/state').read_text().strip()
 atomic_json(output/'summary.json',result)
 print(json.dumps({'status':result['status'],'final_state':result['final_state'],'cleanup':result['cleanup']}),flush=True)
