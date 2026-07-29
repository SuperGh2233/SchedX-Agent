# Third-Party Notices

SchedX-Agent is original project code built on standard open-source operating
system interfaces and development tools. No third-party scheduler source is
copied into the project source directories.

The following software is used as a platform, build dependency, test tool, or
external service. Each component remains governed by its own license.

| Component | Role in SchedX-Agent | License / terms | Distributed in this repository |
|---|---|---|---|
| Linux kernel and sched_ext | Kernel scheduler interface and BPF struct_ops execution | GPL-2.0-only | No kernel source; reproducible build scripts only |
| openEuler | Target operating system and source RPM provider | Component-specific open-source licenses | No distribution image or RPM |
| sched-ext/scx | Design reference and sched_ext toolchain reference | GPL-2.0 | No copied scheduler source |
| libbpf | BPF loading and CO-RE support | LGPL-2.1-only OR BSD-2-Clause | Linked as a system dependency |
| Python | Agent runtime | Python Software Foundation License | No |
| setuptools | Python package build tool | MIT | No |
| pytest | Test framework | MIT | No |
| nginx | Latency-sensitive benchmark workload | 2-clause BSD-like license | No |
| wrk | HTTP benchmark client | Apache-2.0 | No |
| stress-ng | CPU interference workload | GPL-2.0-or-later | No |
| sysbench | Batch workload benchmark | GPL-2.0-or-later | No |
| DeepSeek API | Optional structured policy proposal service | Provider service terms | No model weights or SDK source |

Project source statistics and the counting method are recorded in
[`code_origin_stats.md`](code_origin_stats.md).

References:

- Linux sched_ext documentation: <https://docs.kernel.org/scheduler/sched-ext.html>
- sched-ext/scx: <https://github.com/sched-ext/scx>
- openEuler: <https://www.openeuler.org/>
- libbpf: <https://github.com/libbpf/libbpf>
- nginx: <https://nginx.org/>
- wrk: <https://github.com/wg/wrk>
- stress-ng: <https://github.com/ColinIanKing/stress-ng>
- sysbench: <https://github.com/akopytov/sysbench>

