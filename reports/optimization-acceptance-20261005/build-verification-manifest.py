import datetime
import hashlib
import json
from pathlib import Path

repo = Path('/Users/a123/Projects/SchedX-Agent')
output = repo / 'reports/optimization-acceptance-20261005'
evidence = output / 'remote-final-evidence/evidence'
latest = evidence / 'final-validation-47440e2'
def read(path):
    return json.loads(path.read_text())
def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
verification = read(output / 'local-final-verification.json')
pipeline = read(latest / 'pipeline.json')
comparison = read(latest / 'shared-adaptive-comparison/summary.json')
flow = read(latest / 'autonomous-flow/summary.json')
long30 = read(latest / 'stability-30min/summary.json')
long2h = read(latest / 'stability-2h/summary.json')
audit = read(evidence / 'final-audit/summary.json')
assert all(row['status'] == 'passed' for row in pipeline['stages'])
assert verification['status'] == audit['status'] == 'passed'
metrics = {}
for case, rows in comparison['assessment']['comparisons'].items():
    metrics[case] = {}
    for name, row in rows.items():
        assert row['valid_pair_count'] == 5
        assert not row['stable_regression_over_5_percent']
        assert all(pair['valid'] and min(pair['background_retention'].values()) >= 0.25 for pair in row['pairs'])
        metrics[case][name] = {key: row[key] for key in ('valid_pair_count', 'statistics', 'stable_regression_over_5_percent')}
batch = comparison['assessment']['comparisons']['batch']['events_per_second']['pairs']
retention = {version: {'min': min(pair['background_retention'][version] for pair in batch),
                       'max': max(pair['background_retention'][version] for pair in batch)} for version in ('baseline', 'candidate')}
manifest = {'recorded_client_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'candidate_commit': pipeline['candidate_commit'],
            'production_source_sha256': pipeline['version_identity']['source_sha256'],
            'candidate_binary_sha256': pipeline['version_identity']['binary_sha256'],
            'initial_commit': verification['baseline_identity']['commit'],
            'initial_is_candidate_ancestor': verification['initial_is_candidate_ancestor'],
            'local_tests': verification['tests']['local'], 'linux_tests': verification['tests']['linux'],
            'unit_evidence_covers_current_candidate': True,
            'implementation_and_required_acceptance_complete': True,
            'active_validation': dict(pipeline, pid_alive=False),
            'archive_verification': {key: verification[key] for key in ('status', 'archive_sha256', 'archive_bytes', 'inventory_sha256', 'ordinary_evidence_files_verified', 'harnesses_matched_git')},
            'local_source_matches_frozen_git_candidate': True,
            'shared_adaptive_comparison': {'status': comparison['status'], 'runs': len(comparison['runs']),
                'valid_pairs_per_metric': 5, 'metrics': metrics, 'batch_background_retention': retention,
                'scope': 'native baseline/candidate binaries with the same declared cgroup runtime adapter; not unmodified preliminary whole Agent',
                'settings_by_case': comparison['settings_by_case'], 'local_raw_evidence_archived': True,
                'claim': 'regression and background-progress gates passed; no statistically significant performance gain'},
            'real_autonomous_flow': dict(flow['assessment'], elapsed_seconds=flow['elapsed_seconds'],
                cleanup_passed=True, local_raw_evidence_archived=True,
                stability_tolerance_percent=flow['stability_tolerance_percent'], production_default_stability_tolerance_percent=1,
                final_scheduler_state=flow['final_scheduler_state']),
            'long_validation': {'30min': long30, '2h': long2h},
            'final_audit': audit,
            'current_kernel_limitations': {'cpu_max_native': 'not_enforced',
                'ordinary_cgroup_cpu_weight_native': 'default-scheduler weight effect not observed',
                'hard_quota_tool_contract': 'use verified default-scheduler path or reject before execution',
                'root_filesystem_full': True, 'client_guest_clock_offset_hours_approximately': 12},
            'historical_experiments': {'checkpoint_sha256': sha(output / 'remote-checkpoint.tar.gz'),
                'checkpoint_ordinary_files': 888,
                'static_compiled_defaults_comparison': 'not_accepted: batch background progress below 25%',
                'static_throughput40_comparison': 'not_accepted: batch background progress below 25%',
                'static_throughput20_comparison': 'not_accepted: batch background progress below 25%',
                'earlier_autonomous_failures_retained': True,
                '49_48_and_30_67_percent_p99_gains_scope': 'historical 59c1ca4 vs 58903fe, not official initial b0591a1'},
            'deferred_independent_research': ['unmodified initial whole-system performance comparison',
                'fixed-policy vs full-Agent performance comparison', 'Agent/cgroup group fairness at different thread counts',
                'learned expert routing from valid gain', 'native hard CPU quota implementation'],
            'automation_recreated': False, 'pushed': False, 'pull_request_created': False,
            'goal_complete': False, 'shutdown_sent': False,
            'shutdown': {'status': 'pending_after_local_commit', 'authorized_objective': '继续按计划推进  完成后关闭完的 远程windows 机器',
                'linux_vm': {'ip': '100.121.207.17', 'status': 'running'},
                'windows': {'ip': '100.105.181.122', 'status': 'not_yet_requested'}}}
paths = [output / 'report.md', output / 'local-final-verification.json', output / 'remote-final-evidence.tar.gz',
         output / 'remote-final-evidence/final-evidence-inventory.json', latest / 'pipeline.json',
         evidence / 'final-audit/summary.json', repo / 'docs/optimization-plan.md', repo / 'docs/final-round-improvements.md',
         repo / 'docs/optimization-routes-20261005.md']
manifest['final_file_hashes'] = {str(path.relative_to(repo)): sha(path) for path in paths}
(output / 'verification-manifest.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n')
print(json.dumps({'acceptance': 'passed', 'evidence_files_verified': verification['ordinary_evidence_files_verified'], 'batch_background_retention': retention}))
