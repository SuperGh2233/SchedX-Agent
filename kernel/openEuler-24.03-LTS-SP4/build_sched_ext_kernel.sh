#!/usr/bin/env bash
set -euo pipefail

EXPECTED_SOURCE_SHA256="493e4edab60f2807b7a5bfb4dd616f8386a5432c01b15d70e99d6dce897ae3dc"
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
FRAGMENT="$SCRIPT_DIR/config.sched_ext.fragment"
SOURCE_RPM=${1:-}
MODE=${2:-build}
WORKDIR=${SCHEDX_KERNEL_WORKDIR:-"$PWD/schedx-kernel-build"}
JOBS=${SCHEDX_KERNEL_JOBS:-$(nproc)}
SUFFIX=${SCHEDX_KERNEL_SUFFIX:-schedx1}

usage() {
    echo "Usage: $0 /path/to/kernel-6.6.0-159.4.3.154.oe2403sp4.src.rpm [prepare|build]"
    echo "Environment: SCHEDX_KERNEL_WORKDIR, SCHEDX_KERNEL_JOBS, SCHEDX_KERNEL_SUFFIX"
}

if [[ -z "$SOURCE_RPM" || ! -f "$SOURCE_RPM" ]]; then
    usage >&2
    exit 2
fi
SOURCE_RPM=$(realpath "$SOURCE_RPM")
WORKDIR=$(realpath -m "$WORKDIR")
if [[ "$WORKDIR" == "/" || "$WORKDIR" == "/root" || "$WORKDIR" == "/home" ]]; then
    echo "Refusing unsafe kernel work directory: $WORKDIR" >&2
    exit 1
fi
if [[ "$MODE" != "prepare" && "$MODE" != "build" ]]; then
    usage >&2
    exit 2
fi

for command in rpm rpm2cpio cpio tar sha256sum make gcc bc bison flex openssl pahole rpmbuild; do
    command -v "$command" >/dev/null || {
        echo "Missing build command: $command" >&2
        echo "Install the openEuler kernel build dependencies before retrying." >&2
        exit 1
    }
done

actual_sha=$(sha256sum "$SOURCE_RPM" | awk '{print $1}')
if [[ "$actual_sha" != "$EXPECTED_SOURCE_SHA256" && "${SCHEDX_ALLOW_SOURCE_MISMATCH:-0}" != "1" ]]; then
    echo "Source RPM checksum mismatch: $actual_sha" >&2
    echo "Expected: $EXPECTED_SOURCE_SHA256" >&2
    echo "Set SCHEDX_ALLOW_SOURCE_MISMATCH=1 only after reviewing a newer SP4 source package." >&2
    exit 1
fi

rm -rf "$WORKDIR/extracted"
mkdir -p "$WORKDIR/extracted"
(
    cd "$WORKDIR/extracted"
    rpm2cpio "$SOURCE_RPM" | cpio -idm --quiet
    tar -xzf kernel.tar.gz
)

SOURCE_DIR="$WORKDIR/extracted/kernel"
if [[ ! -f "$SOURCE_DIR/kernel/sched/ext.c" || ! -d "$SOURCE_DIR/tools/sched_ext" ]]; then
    echo "The SP4 source package does not contain sched_ext sources." >&2
    exit 1
fi

(
    cd "$SOURCE_DIR"
    make ARCH=x86 openeuler_defconfig
    scripts/kconfig/merge_config.sh -m .config "$FRAGMENT"
    make ARCH=x86 olddefconfig
    grep -qx 'CONFIG_SCHED_CLASS_EXT=y' .config
    grep -qx 'CONFIG_EXT_GROUP_SCHED=y' .config
)

package_version=$(rpm -qp --qf '%{VERSION}-%{RELEASE}' "$SOURCE_RPM")
kernel_release="${package_version}.${SUFFIX}"
cat >"$WORKDIR/build-manifest.txt" <<EOF
source_rpm=$(basename "$SOURCE_RPM")
source_sha256=$actual_sha
kernel_release=$kernel_release
config_sched_class_ext=y
config_ext_group_sched=y
jobs=$JOBS
EOF

echo "Prepared sched_ext kernel source at $SOURCE_DIR"
echo "Kernel release: $kernel_release"
if [[ "$MODE" == "prepare" ]]; then
    exit 0
fi

(
    cd "$SOURCE_DIR"
    make -j"$JOBS" ARCH=x86 KERNELRELEASE="$kernel_release" binrpm-pkg
)

echo "Build complete. RPMs:"
find "$SOURCE_DIR/rpmbuild/RPMS" -type f -name '*.rpm' -print
