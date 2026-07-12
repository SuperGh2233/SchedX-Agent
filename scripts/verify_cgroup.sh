#!/usr/bin/env bash
# verify_cgroup.sh - Verify cgroup isolation is working
#
# Run this AFTER schedx optimize to check that cgroup values are set correctly.
#
# Usage: sudo bash scripts/verify_cgroup.sh

set -euo pipefail

echo "============================================"
echo "  Cgroup Verification"
echo "============================================"
echo ""

# Check if schedx cgroup exists
if [ ! -d "/sys/fs/cgroup/schedx" ]; then
    echo "ERROR: /sys/fs/cgroup/schedx does not exist"
    echo "Run: sudo python3 -m schedx optimize"
    exit 1
fi

echo "Schedx cgroup groups:"
for dir in /sys/fs/cgroup/schedx/pid-*; do
    [ -d "$dir" ] || continue
    pid=$(basename "$dir" | sed 's/pid-//')
    weight=$(cat "$dir/cpu.weight" 2>/dev/null || echo "N/A")
    max=$(cat "$dir/cpu.max" 2>/dev/null || echo "N/A")
    cpus=$(cat "$dir/cpuset.cpus" 2>/dev/null || echo "N/A")
    procs=$(cat "$dir/cgroup.procs" 2>/dev/null | head -1 || echo "N/A")
    comm=$(cat "/proc/$pid/comm" 2>/dev/null | tr -d '\n' || echo "dead")
    echo "  PID=$pid ($comm) cpu.weight=$weight cpu.max=$max cpuset.cpus=$cpus"
done

echo ""
echo "Root cgroup cpu.weight:"
cat /sys/fs/cgroup/cpu.weight 2>/dev/null || echo "N/A"

echo ""
echo "Top processes by CPU:"
ps aux --sort=-%cpu | head -6
