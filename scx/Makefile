# Build the native scheduler through the sched_ext toolchain shipped with the
# running experiment kernel. This keeps scx compatibility headers aligned with
# the kernel ABI being tested.

KERNEL_SCX_DIR ?= /root/kernel-build/kernel/tools/sched_ext
OUTPUT_DIR := output
KERNEL_BIN := $(KERNEL_SCX_DIR)/build/bin/scx_agent

.PHONY: all check sync build install test clean

all: build

check:
	test -d "$(KERNEL_SCX_DIR)"
	test -f "$(KERNEL_SCX_DIR)/include/scx/common.bpf.h"
	test -r /sys/kernel/btf/vmlinux

sync: check
	install -m 0644 scx_agent.bpf.c "$(KERNEL_SCX_DIR)/scx_agent.bpf.c"
	install -m 0644 scx_agent_user.c "$(KERNEL_SCX_DIR)/scx_agent.c"
	install -m 0644 scx_agent_common.h "$(KERNEL_SCX_DIR)/scx_agent_common.h"
	grep -q 'c-sched-targets.*scx_agent' "$(KERNEL_SCX_DIR)/Makefile" || \
		sed -i 's/^c-sched-targets = /c-sched-targets = scx_agent /' "$(KERNEL_SCX_DIR)/Makefile"
	rm -f "$(KERNEL_SCX_DIR)/build/obj/sched_ext/scx_agent.bpf.o" \
		"$(KERNEL_SCX_DIR)/build/include/scx_agent.bpf.skel.h"

build: sync
	$(MAKE) -C "$(KERNEL_SCX_DIR)" scx_agent
	mkdir -p "$(OUTPUT_DIR)"
	cp "$(KERNEL_BIN)" "$(OUTPUT_DIR)/scx_agent"

install: build
	install -m 0755 "$(OUTPUT_DIR)/scx_agent" /usr/local/bin/scx_agent

test: build
	"$(OUTPUT_DIR)/scx_agent" -1

clean:
	rm -rf "$(OUTPUT_DIR)"
