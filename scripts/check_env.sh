#!/usr/bin/env bash
set -eu

echo "kernel: $(uname -r)"
echo "python: $(python3 --version)"
test -f /sys/fs/cgroup/cgroup.controllers && echo "cgroup v2: yes" || echo "cgroup v2: no"
test -d /sys/kernel/sched_ext && echo "sched_ext: yes" || echo "sched_ext: no"

