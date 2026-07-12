#!/bin/bash
# build.sh - Build script for SchedX-Agent eBPF programs
#
# This script handles dependency installation and building for openEuler 24.03-LTS-SP3.
#
# Usage:
#   ./build.sh              # Build everything
#   ./build.sh --deps       # Install dependencies only
#   ./build.sh --check      # Check dependencies only
#   ./build.sh --clean      # Clean build artifacts
#   ./build.sh --install    # Install BPF programs

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Helper functions
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if running as root
check_root() {
    if [ "$EUID" -ne 0 ]; then
        log_error "This script must be run as root (use sudo)"
        exit 1
    fi
}

# Install dependencies for openEuler
install_deps_openeuler() {
    log_info "Installing dependencies for openEuler..."

    # Update package list
    dnf update -y

    # Install build tools
    dnf install -y \
        gcc \
        clang \
        llvm \
        llvm-tools \
        make \
        cmake \
        git \
        wget

    # Install kernel headers with BTF support
    dnf install -y \
        kernel-devel \
        kernel-headers \
        kernel-debuginfo

    # Install libbpf and dependencies
    dnf install -y \
        libbpf-devel \
        libbpf \
        elfutils-libelf-devel \
        zlib-devel

    # Install bpftool
    if command -v bpftool &>/dev/null; then
        log_info "bpftool already installed"
    else
        dnf install -y kernel-tools 2>/dev/null || {
            log_warn "bpftool not found in package manager, building from source..."
            build_bpftool_from_source
        }
    fi

    log_info "Dependencies installed successfully."
}

# Build bpftool from source
build_bpftool_from_source() {
    local build_dir="/tmp/bpftool-build"
    mkdir -p "$build_dir"
    cd "$build_dir"

    git clone --depth 1 https://github.com/libbpf/bpftool.git
    cd bpftool
    git submodule update --init

    make -j$(nproc)
    make install

    cd /
    rm -rf "$build_dir"
}

# Check dependencies
check_deps() {
    log_info "Checking dependencies..."

    local missing=()

    command -v clang &>/dev/null || missing+=("clang")
    command -v llc &>/dev/null || missing+=("llc (llvm)")
    command -v gcc &>/dev/null || missing+=("gcc")
    command -v bpftool &>/dev/null || missing+=("bpftool")
    command -v make &>/dev/null || missing+=("make")

    # Check kernel headers
    if [ ! -f "/usr/include/linux/bpf.h" ]; then
        missing+=("kernel-headers")
    fi

    # Check libbpf
    if [ ! -f "/usr/include/bpf/libbpf.h" ]; then
        missing+=("libbpf-devel")
    fi

    # Check for BTF support
    if [ ! -f "/sys/kernel/btf/vmlinux" ]; then
        log_warn "BTF (BPF Type Format) not available - some programs may not compile"
    fi

    if [ ${#missing[@]} -eq 0 ]; then
        log_info "All dependencies satisfied."
        return 0
    else
        log_error "Missing dependencies: ${missing[*]}"
        return 1
    fi
}

# Check kernel support for eBPF
check_ebpf_support() {
    log_info "Checking eBPF support..."

    local kernel_version=$(uname -r)
    log_info "Kernel version: $kernel_version"

    # Check for BPF filesystem
    if [ -d "/sys/fs/bpf" ]; then
        log_info "BPF filesystem mounted at /sys/fs/bpf"
    else
        log_warn "BPF filesystem not found at /sys/fs/bpf"
    fi

    # Check for sched_ext
    if [ -d "/sys/kernel/sched_ext" ]; then
        log_info "sched_ext is available at /sys/kernel/sched_ext"
    else
        log_warn "sched_ext not found at /sys/kernel/sched_ext"
    fi

    # Check kernel config
    if [ -f "/boot/config-$kernel_version" ]; then
        if grep -q "CONFIG_BPF=y" "/boot/config-$kernel_version"; then
            log_info "BPF is enabled in kernel config"
        else
            log_warn "BPF may not be enabled in kernel config"
        fi
    fi
}

# Build the project
build() {
    log_info "Building SchedX-Agent eBPF programs..."

    # Check dependencies first
    if ! check_deps; then
        log_error "Build failed: missing dependencies"
        log_info "Run: sudo ./build.sh --deps"
        exit 1
    fi

    # Run make
    make -j$(nproc)

    log_info "Build successful!"
    log_info "BPF programs are in: build/"
    log_info ""
    log_info "To install: sudo ./build.sh --install"
}

# Install BPF programs
install() {
    check_root
    log_info "Installing eBPF programs..."
    make install
    log_info "Installation complete."
}

# Clean build artifacts
clean() {
    log_info "Cleaning build artifacts..."
    make clean
    log_info "Clean complete."
}

# Main
main() {
    case "${1:-}" in
        --deps)
            check_root
            install_deps_openeuler
            ;;
        --check)
            check_deps
            check_ebpf_support
            ;;
        --clean)
            clean
            ;;
        --install)
            install
            ;;
        --help|-h)
            echo "Usage: $0 [option]"
            echo ""
            echo "Options:"
            echo "  (none)     Build everything"
            echo "  --deps     Install dependencies"
            echo "  --check    Check dependencies and kernel support"
            echo "  --clean    Clean build artifacts"
            echo "  --install  Install BPF programs"
            echo "  --help     Show this help"
            ;;
        *)
            build
            ;;
    esac
}

main "$@"
