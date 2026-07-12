#!/bin/bash
# build.sh - Build script for scx_agent BPF scheduler
#
# This script handles dependency installation and building for openEuler 24.03-LTS-SP3.
#
# Usage:
#   ./build.sh              # Build everything
#   ./build.sh --deps       # Install dependencies only
#   ./build.sh --check      # Check dependencies only
#   ./build.sh --clean      # Clean build artifacts

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
        make \
        cmake \
        git \
        wget

    # Install kernel headers
    dnf install -y \
        kernel-devel \
        kernel-headers

    # Install libbpf and dependencies
    dnf install -y \
        libbpf-devel \
        libbpf \
        elfutils-libelf-devel \
        zlib-devel

    # Install bpftool (may be in different package)
    if command -v bpftool &>/dev/null; then
        log_info "bpftool already installed"
    else
        # Try to install from kernel-tools
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

    if [ ${#missing[@]} -eq 0 ]; then
        log_info "All dependencies satisfied."
        return 0
    else
        log_error "Missing dependencies: ${missing[*]}"
        return 1
    fi
}

# Check kernel support for sched_ext
check_sched_ext() {
    log_info "Checking sched_ext support..."

    # Check if running kernel supports sched_ext
    local kernel_version=$(uname -r)
    log_info "Kernel version: $kernel_version"

    # Check for sched_ext in kernel config
    if [ -f "/boot/config-$kernel_version" ]; then
        if grep -q "CONFIG_SCHED_CLASS_EXT=y" "/boot/config-$kernel_version"; then
            log_info "sched_ext is enabled in kernel config"
        else
            log_warn "sched_ext may not be enabled in kernel config"
        fi
    fi

    # Check if sched_ext sysfs exists
    if [ -d "/sys/kernel/sched_ext" ]; then
        log_info "sched_ext is available at /sys/kernel/sched_ext"
        cat /sys/kernel/sched_ext/state 2>/dev/null || true
    else
        log_warn "sched_ext not found at /sys/kernel/sched_ext"
    fi
}

# Build the project
build() {
    log_info "Building scx_agent..."

    # Check dependencies first
    if ! check_deps; then
        log_error "Build failed: missing dependencies"
        log_info "Run: sudo ./build.sh --deps"
        exit 1
    fi

    # Run make
    make -j$(nproc)

    log_info "Build successful!"
    log_info "Binary: output/scx_agent"
    log_info ""
    log_info "To install: sudo make install"
    log_info "To run: sudo ./output/scx_agent"
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
            check_sched_ext
            ;;
        --clean)
            clean
            ;;
        --help|-h)
            echo "Usage: $0 [option]"
            echo ""
            echo "Options:"
            echo "  (none)     Build everything"
            echo "  --deps     Install dependencies"
            echo "  --check    Check dependencies and kernel support"
            echo "  --clean    Clean build artifacts"
            echo "  --help     Show this help"
            ;;
        *)
            build
            ;;
    esac
}

main "$@"
