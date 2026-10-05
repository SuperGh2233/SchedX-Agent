import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

root = Path('/tmp/schedx-acceptance-20261005-79905fb')
evidence = root / 'evidence'
audit_dir = evidence / 'final-audit'
audit_dir.mkdir(exist_ok=True)
def digest_stream(stream):
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
        digest.update(chunk)
    return digest.hexdigest()
def digest(path):
    with path.open('rb') as stream:
        return digest_stream(stream)
def save(name, obj):
    (audit_dir / name).write_text(json.dumps(obj, indent=2) + '\n')

failures = []
original = Path('/root/SchedX-Agent')
rows = []
with tarfile.open('/root/schedx-optimization-backup-20261003/workspace.tar.gz') as backup:
    for member in backup:
        if not member.isfile():
            continue
        name = member.name.removeprefix('./').removeprefix('SchedX-Agent/')
        path = original / name
        expected = digest_stream(backup.extractfile(member))
        actual = digest(path) if path.is_file() and not path.is_symlink() else None
        rows.append({'path': name, 'backup_sha256': expected, 'current_sha256': actual})
        if expected != actual:
            failures.append('original_file_mismatch:' + name)
save('original-files.json', rows)
binary_sha = digest(Path('/usr/local/bin/scx_agent'))
if binary_sha != '6e2148d333d5833dc889f8436b7746661edd61d9c78e89b57b4a54bd8f373f39':
    failures.append('original_installed_binary_mismatch')
before = json.loads((evidence / 'original-preservation-before-d1baee7.json').read_text())
nginx = {}
for pid, expected in before['original_nginx'].items():
    proc = Path('/proc') / pid
    if not proc.exists():
        failures.append('original_nginx_missing:' + pid)
        continue
    actual = {'cgroup': (proc / 'cgroup').read_text().strip(), 'nice': os.getpriority(os.PRIO_PROCESS, int(pid)),
              'affinity': sorted(os.sched_getaffinity(int(pid))), 'policy': os.sched_getscheduler(int(pid))}
    nginx[pid] = actual
    if actual != expected:
        failures.append('original_nginx_state_mismatch:' + pid)

owned_processes = []
for proc in Path('/proc').iterdir():
    if not proc.name.isdigit() or int(proc.name) == os.getpid():
        continue
    try:
        argv = (proc / 'cmdline').read_bytes().split(b'\0')
        exe = (proc / 'exe').resolve()
        matches = str(exe).startswith(str(root) + '/') or any(
            arg.decode(errors='replace').startswith(str(root) + '/') for arg in argv)
        comm = (proc / 'comm').read_text().strip()
        if matches or comm == 'scx_agent':
            owned_processes.append({'pid': int(proc.name), 'comm': comm, 'args': [arg.decode(errors='replace') for arg in argv if arg]})
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        continue
groups = [str(path) for path in Path('/sys/fs/cgroup').rglob('*') if path.is_dir() and 'schedx' in path.name]
pins = [str(path) for path in Path('/sys/fs/bpf').rglob('*')]
state = Path('/sys/kernel/sched_ext/state').read_text().strip()
socket_remaining = Path('/run/schedx/scx-daemon.sock').exists()
journals = [str(path) for path in evidence.rglob('*') if path.is_file() and path.name in {
    'rollback.json', 'scx_rollback.json', 'process_rollback.json', 'recovery.json', 'network-recovery.json'}]
bpf = {}
for kind in ('prog', 'link', 'map'):
    result = subprocess.run(['bpftool', '-j', kind, 'show'], capture_output=True, text=True, timeout=20)
    if result.returncode:
        failures.append('bpf_enumeration_failed:' + kind)
        bpf[kind] = {'returncode': result.returncode, 'stderr': result.stderr}
    else:
        bpf[kind] = json.loads(result.stdout)
        if any(any(token in row.get('name', '') for token in ('scx_agent', 'schedx', 'net_policy', 'sched_policy', 'security_policy')) for row in bpf[kind]):
            failures.append('owned_bpf_object_remaining:' + kind)
save('bpf-inventory.json', bpf)
for key, value in {'owned_processes': owned_processes, 'owned_cgroups': groups,
                   'pinned_hooks': pins, 'journals': journals, 'daemon_socket': socket_remaining}.items():
    if value:
        failures.append('remaining:' + key)
if state != 'disabled':
    failures.append('native_scheduler_not_disabled')
pipeline = json.loads((evidence / 'final-validation-47440e2/pipeline.json').read_text())
if pipeline['status'] != 'passed' or any(row['status'] != 'passed' for row in pipeline['stages']):
    failures.append('pipeline_not_passed')
summary = {'status': 'failed' if failures else 'passed',
           'guest_verified_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
           'failures': failures, 'regular_files_checked': len(rows),
           'regular_files_matched': sum(row['backup_sha256'] == row['current_sha256'] for row in rows),
           'installed_binary_sha256': binary_sha, 'original_nginx': nginx,
           'native_state': state, 'owned_processes': owned_processes, 'owned_cgroups': groups,
           'pinned_hooks': pins, 'remaining_journals': journals, 'daemon_socket_remaining': socket_remaining,
           'bpf_inventory_note': 'systemd cgroup programs are unrelated; bpftool may expose transient enumeration maps'}
save('summary.json', summary)
print(json.dumps(summary))
if failures:
    raise SystemExit(1)

artifacts = evidence / 'final-artifacts'
artifacts.mkdir(exist_ok=True)
for version in ('baseline', 'candidate'):
    shutil.copy2(root / 'native' / version / 'build/bin/scx_agent', artifacts / ('scx_agent-' + version))
for name in ('baseline', 'candidate-47440e2'):
    with tarfile.open(artifacts / (name + '-source.tar.gz'), 'w:gz', dereference=True) as archive:
        archive.add(root / name, arcname=name, filter=lambda member: None if any(part in ('__pycache__', '.pytest_cache', '.git') for part in Path(member.name).parts) else member)
inventory = {}
for path in sorted(evidence.rglob('*')):
    if path.is_file():
        inventory[str(path.relative_to(root))] = {'sha256': digest(path), 'bytes': path.stat().st_size}
(root / 'final-evidence-inventory.json').write_text(json.dumps(inventory, indent=2) + '\n')
print(json.dumps({'inventory_files': len(inventory), 'inventory_sha256': digest(root / 'final-evidence-inventory.json')}))
