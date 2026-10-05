import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path('/tmp/schedx-acceptance-20261005-79905fb')
records = ROOT / 'evidence'
records.mkdir(exist_ok=True)
resume = (records / 'bootstrap.json').exists()
if resume:
    previous = json.loads((records / 'bootstrap.json').read_text())
    if previous['status'] != 'failed':
        raise RuntimeError('resume only after a confirmed terminal failed bootstrap')
    history = records / 'attempt-1'
    history.mkdir(exist_ok=False)
    for path in records.glob('*'):
        if path.is_file():
            shutil.copy2(path, history / path.name)
state = {'status': 'running', 'stage': 'inputs', 'guest_utc_started': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'commands': []}

def save():
    (records / 'bootstrap.json').write_text(json.dumps(state, indent=2) + '\n')

def run(command, label, cwd=None, stdin=None):
    state['stage'] = label
    state['commands'].append({'label': label, 'command': command, 'cwd': str(cwd) if cwd else None})
    save()
    with (records / (label + '.log')).open('w') as log:
        result = subprocess.run(command, cwd=cwd, stdin=stdin, stdout=log, stderr=subprocess.STDOUT, timeout=600)
    if result.returncode:
        raise RuntimeError(f'{label} failed with exit {result.returncode}')

try:
    for name, expected in {
        'schedx-source-proof-20261005.pack': '8125f0f1899bc3a9dfa7783a7dd6917a3c44ebf6503530c2830f3d7af073a07d',
        'schedx-candidate-core-79905fb.tar.gz': '2f39c7415b82c1d6c08e90675ad098bc13193c9cd123430243a59178b1e5e88f',
        'schedx-preliminary-core-b0591a1.tar.gz': 'e90f3a8b89ecb2357c6f5584208b186996f99d82b3dba3be7dc4cc7b7e60c6b3',
    }.items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError(f'input hash mismatch: {name}')
    if not resume:
        run(['git', 'init', '--bare', str(ROOT / 'repository.git')], 'git-init')
        with (ROOT / 'schedx-source-proof-20261005.pack').open('rb') as pack:
            run(['git', '-C', str(ROOT / 'repository.git'), 'index-pack', '--stdin'], 'git-production-objects', stdin=pack)
    state['git_proof_scope'] = 'original commit objects and production subtrees only; not a full historical clone'
    for version, archive in [('candidate', 'schedx-candidate-core-79905fb.tar.gz'), ('baseline', 'schedx-preliminary-core-b0591a1.tar.gz')]:
        source = ROOT / version
        if not resume:
            source.mkdir()
            run(['tar', '-xf', str(ROOT / archive), '-C', str(source)], 'extract-' + version)
    state['environment'] = {
        'kernel_release': os.uname().release, 'cpus': os.cpu_count(),
        'btf_sha256': hashlib.sha256(Path('/sys/kernel/btf/vmlinux').read_bytes()).hexdigest(),
        'global_binary_sha256': hashlib.sha256(Path('/usr/local/bin/scx_agent').read_bytes()).hexdigest(),
        'scheduler_state': Path('/sys/kernel/sched_ext/state').read_text().strip(),
        'guest_wall_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }
    for tool, args in [('gcc', ['--version']), ('clang', ['--version']), ('bpftool', ['version']),
                       ('wrk', ['--version']), ('redis-benchmark', ['--version']), ('sysbench', ['--version']),
                       ('nginx', ['-v'])]:
        result = subprocess.run([tool, *args], capture_output=True, text=True)
        state['environment'][tool] = (result.stdout + result.stderr).strip()
    save()
    template = Path('/root/kernel-build/kernel/tools/sched_ext-opt-20261003')
    layout = ROOT / 'toolchain/kernel/tools'
    layout.mkdir(parents=True, exist_ok=True)
    for name in ('build', 'scripts', 'lib', 'include', 'bpf'):
        if not (layout / name).exists():
            (layout / name).symlink_to(template.parent / name, target_is_directory=True)
    if not (layout.parent / 'include').exists():
        (layout.parent / 'include').symlink_to(template.parents[1] / 'include', target_is_directory=True)
    sys.path.insert(0, str(ROOT / 'candidate'))
    from schedx.benchmark.versioning import source_digest, version_identity
    for version, revision in [('baseline', 'b0591a10785b74cf980c8f0b4ed703a9a95e1aa2'),
                              ('candidate', '79905fbe79edaadeef9c24a6f16f4c37212e09e4')]:
        private_tool = layout / ('sched_ext-' + version)
        private_tool.mkdir(exist_ok=True)
        shutil.copy2(template / 'Makefile', private_tool / 'Makefile')
        shutil.copytree(template / 'include', private_tool / 'include', dirs_exist_ok=True)
        source = ROOT / version
        for original, target in [('scx_agent.bpf.c', 'scx_agent.bpf.c'), ('scx_agent_user.c', 'scx_agent.c'), ('scx_agent_common.h', 'scx_agent_common.h')]:
            shutil.copy2(source / 'scx' / original, private_tool / target)
        output = ROOT / 'native' / version
        output.mkdir(parents=True, exist_ok=True)
        for skeleton in (output / 'build/include').glob('scx_agent.bpf.*skel.h'):
            skeleton.unlink()
        matching_bpftool = template / 'build/sbin/bpftool'
        state['environment']['build_bpftool'] = subprocess.check_output([str(matching_bpftool), 'version'], text=True).strip()
        state['environment']['build_bpftool_sha256'] = hashlib.sha256(matching_bpftool.read_bytes()).hexdigest()
        command = ['make', '-C', str(private_tool), 'O=' + str(output), 'BPFTOOL=' + str(matching_bpftool),
                   'VMLINUX_BTF=/sys/kernel/btf/vmlinux', '-j2', 'scx_agent']
        run(command, 'build-native-' + version)
        binary = output / 'build/bin/scx_agent'
        identity = version_identity(ROOT / 'repository.git', source, revision, binary)
        identity.update(kernel_release=state['environment']['kernel_release'], btf_sha256=state['environment']['btf_sha256'],
                        compiler=state['environment']['clang'], build_command=command, build_log='build-native-' + version + '.log')
        (records / ('build-' + version + '.json')).write_text(json.dumps(identity, indent=2) + '\n')
    run(['make', 'clean'], 'clean-private-ebpf', ROOT / 'candidate/ebpf')
    run(['make', '-j2'], 'build-ebpf', ROOT / 'candidate/ebpf')
    state['ebpf_objects'] = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                            for path in (ROOT / 'candidate/ebpf/build').glob('*.bpf.o')}
    run([sys.executable, '-m', 'pytest', '-q', '--basetemp=' + str(ROOT / 'pytest-temp'),
         '--junitxml=' + str(records / 'linux-tests.xml')], 'linux-tests', ROOT / 'candidate')
    if Path('/sys/kernel/sched_ext/state').read_text().strip() != 'disabled':
        raise RuntimeError('unit checks left an active scheduler')
    if state['environment']['global_binary_sha256'] != hashlib.sha256(Path('/usr/local/bin/scx_agent').read_bytes()).hexdigest():
        raise RuntimeError('global installed binary unexpectedly changed')
    state.update(status='passed', stage='complete')
except BaseException as exc:
    state.update(status='failed', error=f'{type(exc).__name__}: {exc}')
finally:
    save()
    print(json.dumps(state, indent=2), flush=True)
if state['status'] != 'passed':
    raise SystemExit(1)
