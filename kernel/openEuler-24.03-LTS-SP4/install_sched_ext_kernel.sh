#!/usr/bin/env bash
set -euo pipefail

RPM_PATH=${1:-}
APPLY=${2:-}

if [[ -z "$RPM_PATH" || ! -f "$RPM_PATH" ]]; then
    echo "Usage: $0 /path/to/kernel-*.schedx*.rpm [--apply]" >&2
    exit 2
fi

boot_image=$(rpm -qlp "$RPM_PATH" | grep -E '^/boot/vmlinuz-' | head -1 || true)
if [[ -z "$boot_image" || "$boot_image" != *schedx* ]]; then
    echo "Refusing to install an RPM without a schedx boot image." >&2
    exit 1
fi

echo "Current kernel: $(uname -r)"
echo "Candidate boot image: $boot_image"
echo "Commands: dnf install '$RPM_PATH'; grubby --set-default '$boot_image'"
if [[ "$APPLY" != "--apply" ]]; then
    echo "Dry run only. Re-run with --apply to install and select the kernel."
    exit 0
fi

[[ $EUID -eq 0 ]] || { echo "Root is required." >&2; exit 1; }
dnf install -y "$RPM_PATH"
test -f "$boot_image"
grubby --set-default "$boot_image"
grubby --default-kernel
echo "Reboot is required. The stock kernel remains installed for rollback."
