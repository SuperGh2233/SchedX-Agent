# SchedX-Agent Code Origin Statistics

This submission snapshot contains original SchedX-Agent implementation code.
External operating systems, libraries, benchmark programs, and hosted model
services are dependencies rather than redistributed source.

## Counting Method

- Scope: Git-tracked source files in `schedx/`, `scx/`, `ebpf/`, `scripts/`,
  `kernel/`, and `tests/`.
- Source extensions: `.py`, `.c`, `.h`, `.sh`, and `.rs`.
- Line metric: non-empty physical source lines.
- Excluded: generated results, reports, Office documents, media, build output,
  vendored dependencies, and third-party binaries.
- Snapshot date: 2026-07-30.

## Statistics

| Code category | Files | Non-empty source lines | Description |
|---|---:|---:|---|
| Python Agent | 61 | 8,521 | Agent, Skills, probes, policies, controllers, CLI, benchmarks, and reports |
| Native scx | 3 | 584 | Project BPF scheduler, user-space loader, and shared protocol |
| eBPF hooks | 6 | 1,457 | Scheduler tracing and policy hook prototypes |
| Automation and kernel integration | 30 | 3,018 | Experiment, demonstration, report, SP4 build, verification, and rollback scripts |
| Product code subtotal | 100 | 13,580 | Excludes tests |
| Test code | 26 | 2,193 | Automated regression and integration tests |
| Original source total | 126 | 15,773 | Submission snapshot |

No third-party source is directly copied and redistributed inside the counted
directories. This 100% figure applies only to the source distribution scope
defined above; it does not claim ownership of Linux, openEuler, libbpf,
sched-ext/scx, benchmark tools, Python packages, or external model services.
Their roles and licenses are documented in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

