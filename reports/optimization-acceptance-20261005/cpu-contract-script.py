import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path('/tmp/schedx-acceptance-20261005-79905fb')
sys.path.insert(0, str(ROOT / 'candidate'))
from schedx.controllers.scx_controller import ScxController
from schedx.cpu_backend import CpuBackendBusy
from schedx.tool_runner import CpuQuotaUnavailable, ToolCallRunner
from schedx.state import atomic_json

output = ROOT / 'evidence/cpu-contract'
output.mkdir(exist_ok=False)
os.environ['PATH'] = str(ROOT / 'native/candidate/build/bin') + os.pathsep + os.environ['PATH']
controller = ScxController(dry_run=False)
runner = ToolCallRunner(state_dir=output / 'tools')
result = {'status': 'running', 'cases': {}, 'kernel': os.uname().release}
busy = "import time; a=time.process_time(); end=time.monotonic()+2\nwhile time.monotonic()<end: pass\nprint(time.process_time()-a)"
quota = {'cpu_max': '10000 100000'}
try:
    assert controller.state() == 'disabled'
    hard = runner.run([sys.executable, '-c', busy], profile_overrides=quota)
    cpu = float(hard['stdout'].strip())
    assert hard['returncode'] == 0 and cpu < 0.6 and hard['scx_mode'] == 'cgroup', hard
    assert hard['cleanup']['cgroup_removed'] and hard['cleanup']['scheduler_stopped']
    result['cases']['hard_quota'] = {'cpu_seconds': cpu, 'result': hard}
    marker = output / 'hard-running'
    overlap_code = f"from pathlib import Path; Path({str(marker)!r}).touch(); " + busy.replace('\n', '\n')
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runner.run, [sys.executable, '-c', overlap_code], profile_overrides=quota)
        deadline = time.monotonic() + 5
        while not marker.exists() and not future.done() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists(), 'hard quota command did not start'
        blocked = False
        try:
            controller.start_scheduler()
        except CpuBackendBusy:
            blocked = True
        assert blocked and controller.state() == 'disabled'
        overlap = future.result(timeout=10)
        assert overlap['returncode'] == 0 and overlap['cleanup']['cgroup_removed']
        result['cases']['native_blocked_by_live_quota'] = {'blocked_before_attach': blocked, 'hard_result': overlap}
    assert controller.start_scheduler()
    rejected_marker = output / 'must-not-execute'
    try:
        runner.run([sys.executable, '-c', f"from pathlib import Path; Path({str(rejected_marker)!r}).touch()"], profile_overrides=quota)
        raise AssertionError('hard request unexpectedly executed under native')
    except CpuQuotaUnavailable:
        pass
    assert not rejected_marker.exists()
    result['cases']['hard_rejected_under_native'] = {'command_started': False}
    soft_runner = ToolCallRunner(state_dir=output / 'soft', native_scx=False)
    soft = soft_runner.run([sys.executable, '-c', busy], profile_overrides=quota, cpu_limit_mode='soft')
    assert soft['returncode'] == 0 and not soft['hard_cpu_quota_requested']
    assert soft['cpu_control_support_at_launch']['cpu_max'] == 'not_enforced'
    assert soft['cleanup']['cgroup_removed']
    result['cases']['explicit_soft_under_native'] = {'cpu_seconds': float(soft['stdout'].strip()), 'result': soft}
    result['status'] = 'passed'
except BaseException as exc:
    result.update(status='failed', error=f'{type(exc).__name__}: {exc}')
finally:
    result['scheduler_stopped'] = controller.stop_scheduler()
    result['final_state'] = controller.state()
    if not result['scheduler_stopped'] or result['final_state'] != 'disabled':
        result['status'] = 'failed'
    atomic_json(output / 'summary.json', result)
    print(json.dumps(result, indent=2), flush=True)
if result['status'] != 'passed':
    raise SystemExit(1)
