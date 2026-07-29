#!/usr/bin/env bash
set -euo pipefail

REQUESTED=${1:-}
APPLY=${2:-}
if [[ "$REQUESTED" == "--apply" ]]; then
    APPLY=$REQUESTED
    REQUESTED=""
fi

stock_kernel=$REQUESTED
if [[ -z "$stock_kernel" ]]; then
    stock_kernel=$(find /boot -maxdepth 1 -type f -name 'vmlinuz-*oe2403sp4*' ! -name '*schedx*' | sort -V | tail -1)
fi
if [[ -z "$stock_kernel" || ! -f "$stock_kernel" ]]; then
    echo "No stock openEuler SP4 kernel was found in /boot." >&2
    exit 1
fi

echo "Stock rollback kernel: $stock_kernel"
if [[ "$APPLY" != "--apply" ]]; then
    echo "Dry run only. Re-run with --apply to change the next boot default."
    exit 0
fi

[[ $EUID -eq 0 ]] || { echo "Root is required." >&2; exit 1; }
grubby --set-default "$stock_kernel"
grubby --default-kernel
echo "Reboot to return to the stock kernel."
