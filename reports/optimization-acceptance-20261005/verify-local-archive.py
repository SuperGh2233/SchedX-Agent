import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import xml.etree.ElementTree as ET

repo = Path('/Users/a123/Projects/SchedX-Agent')
report_dir = repo / 'reports/optimization-acceptance-20261005'
archive = report_dir / 'remote-final-evidence.tar.gz'
def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
assert sha(archive) == '88ed357ea81fa1b2ee39800faeb3222e54484a66dc56d24e1bd1b207d4da33b5'
destination = report_dir / 'remote-final-evidence'
destination.mkdir(exist_ok=True)
with tarfile.open(archive) as bundle:
    bundle.extractall(destination, filter='data')
inventory = json.loads((destination / 'final-evidence-inventory.json').read_text())
for name, expected in inventory.items():
    path = destination / name
    assert path.is_file(), name
    assert sha(path) == expected['sha256'], name
    assert path.stat().st_size == expected['bytes'], name
evidence = destination / 'evidence'
pipeline = json.loads((evidence / 'final-validation-47440e2/pipeline.json').read_text())
assert pipeline['status'] == 'passed'
assert all(row['status'] == 'passed' and row['returncode'] == 0 for row in pipeline['stages'])
audit = json.loads((evidence / 'final-audit/summary.json').read_text())
assert audit['status'] == 'passed' and not audit['failures']
sys.path.insert(0, str(repo))
from schedx.benchmark.versioning import version_identity
candidate = pipeline['candidate_commit']
artifacts = evidence / 'final-artifacts'
source_dest = Path('/tmp/schedx-final-source-verification-47440e2')
source_dest.mkdir(exist_ok=True)
for name in ('baseline', 'candidate-47440e2'):
    with tarfile.open(artifacts / (name + '-source.tar.gz')) as bundle:
        bundle.extractall(source_dest, filter='data')
baseline_identity = version_identity(repo, source_dest / 'baseline', 'b0591a10785b74cf980c8f0b4ed703a9a95e1aa2', artifacts / 'scx_agent-baseline', evidence / 'build-baseline.json')
candidate_identity = version_identity(repo, source_dest / 'candidate-47440e2', candidate, artifacts / 'scx_agent-candidate', evidence / 'build-candidate-47440e2.json')
local_identity = version_identity(repo, repo, candidate, artifacts / 'scx_agent-candidate', evidence / 'build-candidate-47440e2.json')
assert local_identity['source_sha256'] == pipeline['version_identity']['source_sha256']
assert candidate_identity['source_sha256'] == local_identity['source_sha256']
harnesses = {}
for stage, filename in {'shared-adaptive-comparison': 'run_final_round_comparison.py', 'autonomous-flow': 'verify_autonomous_flow.py', 'stability-30min': 'verify_optimization.py', 'stability-2h': 'verify_optimization.py'}.items():
    data = subprocess.run(['git', '-C', str(repo), 'show', candidate + ':scripts/' + filename], check=True, capture_output=True).stdout
    actual = hashlib.sha256(data).hexdigest()
    assert actual == pipeline['harness_sha256'][stage]
    assert sha(source_dest / 'candidate-47440e2/scripts' / filename) == actual
    harnesses[stage] = actual
tests = {}
for name, path in {'local': report_dir / 'local-tests-adaptive.xml', 'linux': evidence / 'linux-tests-47440e2.xml'}.items():
    xml = ET.parse(path).getroot()
    suites = [xml] if xml.tag == 'testsuite' else list(xml.findall('testsuite'))
    counts = {key: sum(int(suite.attrib.get(key, 0)) for suite in suites) for key in ('tests', 'failures', 'errors', 'skipped')}
    assert counts == {'tests': 412, 'failures': 0, 'errors': 0, 'skipped': 0}
    tests[name] = dict(counts, sha256=sha(path), path=str(path.relative_to(report_dir)))
subprocess.run(['git', '-C', str(repo), 'merge-base', '--is-ancestor', baseline_identity['commit'], candidate], check=True)
verification = {'status': 'passed', 'recorded_client_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                'archive_sha256': sha(archive), 'archive_bytes': archive.stat().st_size,
                'inventory_sha256': sha(destination / 'final-evidence-inventory.json'),
                'ordinary_evidence_files_verified': len(inventory), 'baseline_identity': baseline_identity,
                'candidate_identity': candidate_identity, 'local_source_identity': local_identity,
                'harnesses_matched_git': harnesses, 'tests': tests, 'initial_is_candidate_ancestor': True,
                'audit_status': audit['status'], 'pipeline_status': pipeline['status']}
(report_dir / 'local-final-verification.json').write_text(json.dumps(verification, indent=2) + '\n')
print(json.dumps({'status': 'passed', 'archive_bytes': archive.stat().st_size, 'evidence_files_verified': len(inventory), 'tests': tests, 'source_sha256': local_identity['source_sha256']}))
