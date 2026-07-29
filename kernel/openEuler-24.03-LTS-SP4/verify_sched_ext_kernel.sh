#!/usr/bin/env bash
set -euo pipefail

kernel=$(uname -r)
config="/boot/config-$kernel"
failed=0

check() {
    local description=$1
    shift
    if "$@"; then
        printf 'PASS  %s\n' "$description"
    else
        printf 'FAIL  %s\n' "$description"
        failed=1
    fi
}

echo "Kernel: $kernel"
check "kernel config exists" test -f "$config"
check "CONFIG_SCHED_CLASS_EXT=y" grep -qx 'CONFIG_SCHED_CLASS_EXT=y' "$config"
check "CONFIG_BPF_SYSCALL=y" grep -qx 'CONFIG_BPF_SYSCALL=y' "$config"
check "CONFIG_DEBUG_INFO_BTF=y" grep -qx 'CONFIG_DEBUG_INFO_BTF=y' "$config"
check "sched_ext sysfs exists" test -d /sys/kernel/sched_ext
check "kernel BTF exists" test -f /sys/kernel/btf/vmlinux

if [[ -f /sys/kernel/sched_ext/state ]]; then
    echo "sched_ext state: $(cat /sys/kernel/sched_ext/state)"
fi
exit "$failed"
