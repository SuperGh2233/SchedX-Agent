# SPDX-License-Identifier: GPL-2.0
#
# Makefile for SchedX-Agent eBPF programs
#
# This Makefile builds the eBPF programs for scheduling, network,
# and resource control.
#
# Requirements:
#   - Linux kernel with eBPF support
#   - libbpf >= 1.0
#   - bpftool
#   - clang >= 15
#   - kernel headers with BTF (BPF Type Format)
#
# Usage:
#   make              # Build all BPF programs
#   make sched_trace  # Build sched_trace only
#   make net_policy   # Build net_policy only
#   make resource_ctrl # Build resource_ctrl only
#   make clean        # Clean build artifacts
#   make install      # Install to /usr/lib/schedx/ebpf
#   make check        # Check build dependencies

# Compiler and tools
CLANG ?= clang
LLC ?= llc
BPFTOOL ?= bpftool
CC ?= gcc

# Directories
BUILD_DIR := build
OUTPUT_DIR := output
INSTALL_DIR ?= /usr/lib/schedx/ebpf

# Kernel headers - adjust for your system
KERNEL_HEADERS ?= /usr/include
LIBBPF_INCLUDE ?= /usr/include
LIBBPF_LIB ?= /usr/lib64

# BPF compilation flags
BPF_CFLAGS := -g -O2 -target bpf \
              -I$(KERNEL_HEADERS) -I$(LIBBPF_INCLUDE) \
              -Wall -Wno-unused-value -Wno-pointer-sign \
              -Wno-compare-distinct-pointer-types \
              -D__TARGET_ARCH_x86

# Source files
SCHED_TRACE_SRC := sched_trace.bpf.c
NET_POLICY_SRC := net_policy.bpf.c
RESOURCE_CTRL_SRC := resource_ctrl.bpf.c

# Output files
SCHED_TRACE_OBJ := $(BUILD_DIR)/sched_trace.bpf.o
NET_POLICY_OBJ := $(BUILD_DIR)/net_policy.bpf.o
RESOURCE_CTRL_OBJ := $(BUILD_DIR)/resource_ctrl.bpf.o

# All BPF objects
BPF_OBJS := $(SCHED_TRACE_OBJ) $(NET_POLICY_OBJ) $(RESOURCE_CTRL_OBJ)

# Default target
.PHONY: all
all: $(BPF_OBJS)
	@echo "All BPF programs built successfully."

# Create build directories
$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

$(OUTPUT_DIR):
	mkdir -p $(OUTPUT_DIR)

# Build sched_trace BPF program
.PHONY: sched_trace
sched_trace: $(SCHED_TRACE_OBJ)

$(SCHED_TRACE_OBJ): $(SCHED_TRACE_SRC) | $(BUILD_DIR)
	$(CLANG) $(BPF_CFLAGS) -c $< -o $@
	@echo "Built: $@"

# Build net_policy BPF program
.PHONY: net_policy
net_policy: $(NET_POLICY_OBJ)

$(NET_POLICY_OBJ): $(NET_POLICY_SRC) | $(BUILD_DIR)
	$(CLANG) $(BPF_CFLAGS) -c $< -o $@
	@echo "Built: $@"

# Build resource_ctrl BPF program
.PHONY: resource_ctrl
resource_ctrl: $(RESOURCE_CTRL_OBJ)

$(RESOURCE_CTRL_OBJ): $(RESOURCE_CTRL_SRC) | $(BUILD_DIR)
	$(CLANG) $(BPF_CFLAGS) -c $< -o $@
	@echo "Built: $@"

# Generate skeletons for user-space loading
.PHONY: skeletons
skeletons: $(BPF_OBJS)
	@for obj in $(BPF_OBJS); do \
		skel=$${obj%.o}.skel.h; \
		$(BPFTOOL) gen skeleton $$obj > $$skel; \
		echo "Generated skeleton: $$skel"; \
	done

# Check build dependencies
.PHONY: check
check:
	@echo "Checking build dependencies..."
	@command -v $(CLANG) >/dev/null 2>&1 || { echo "ERROR: clang not found"; exit 1; }
	@command -v $(LLC) >/dev/null 2>&1 || { echo "ERROR: llc not found"; exit 1; }
	@command -v $(BPFTOOL) >/dev/null 2>&1 || { echo "ERROR: bpftool not found"; exit 1; }
	@command -v $(CC) >/dev/null 2>&1 || { echo "ERROR: gcc not found"; exit 1; }
	@test -f $(KERNEL_HEADERS)/linux/bpf.h || { echo "ERROR: kernel headers not found at $(KERNEL_HEADERS)"; exit 1; }
	@test -f $(LIBBPF_INCLUDE)/bpf/libbpf.h || { echo "ERROR: libbpf headers not found at $(LIBBPF_INCLUDE)"; exit 1; }
	@echo "All dependencies satisfied."

# Install BPF programs
.PHONY: install
install: $(BPF_OBJS)
	install -d $(INSTALL_DIR)
	install -m 644 $(BPF_OBJS) $(INSTALL_DIR)/
	@echo "Installed BPF programs to $(INSTALL_DIR)"

# Uninstall
.PHONY: uninstall
uninstall:
	rm -rf $(INSTALL_DIR)
	@echo "Uninstalled BPF programs from $(INSTALL_DIR)"

# Clean build artifacts
.PHONY: clean
clean:
	rm -rf $(BUILD_DIR) $(OUTPUT_DIR)
	@echo "Cleaned build artifacts"

# Verify BPF programs
.PHONY: verify
verify: $(BPF_OBJS)
	@for obj in $(BPF_OBJS); do \
		echo "Verifying $$obj..."; \
		$(BPFTOOL) prog dump xlated file $$obj > /dev/null; \
		if [ $$? -eq 0 ]; then \
			echo "  OK"; \
		else \
			echo "  FAILED"; \
			exit 1; \
		fi; \
	done
	@echo "All BPF programs verified."

# Help
.PHONY: help
help:
	@echo "SchedX-Agent eBPF Build System"
	@echo ""
	@echo "Targets:"
	@echo "  all           - Build all BPF programs (default)"
	@echo "  sched_trace   - Build sched_trace BPF program"
	@echo "  net_policy    - Build net_policy BPF program"
	@echo "  resource_ctrl - Build resource_ctrl BPF program"
	@echo "  skeletons     - Generate BPF skeletons for user-space"
	@echo "  check         - Check build dependencies"
	@echo "  install       - Install BPF programs to $(INSTALL_DIR)"
	@echo "  uninstall     - Uninstall BPF programs"
	@echo "  clean         - Clean build artifacts"
	@echo "  verify        - Verify BPF programs"
	@echo "  help          - Show this help message"
	@echo ""
	@echo "Variables:"
	@echo "  CLANG          - Clang compiler (default: clang)"
	@echo "  LLC            - LLVM compiler (default: llc)"
	@echo "  BPFTOOL        - BPF tool (default: bpftool)"
	@echo "  CC             - C compiler (default: gcc)"
	@echo "  KERNEL_HEADERS - Kernel headers path (default: /usr/include)"
	@echo "  LIBBPF_INCLUDE - libbpf headers path (default: /usr/include)"
	@echo "  LIBBPF_LIB     - libbpf library path (default: /usr/lib64)"
	@echo "  INSTALL_DIR    - Installation directory (default: /usr/lib/schedx/ebpf)"
