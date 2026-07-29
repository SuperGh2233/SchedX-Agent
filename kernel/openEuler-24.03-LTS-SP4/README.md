# openEuler 24.03 LTS SP4 sched_ext Kernel

The SP4 `kernel-6.6.0-159.4.3.154.oe2403sp4` source RPM already contains
`kernel/sched/ext.c` and `tools/sched_ext`. The stock binary kernel leaves
`CONFIG_SCHED_CLASS_EXT` disabled, so SchedX uses a config-only rebuild rather
than carrying an out-of-tree scheduler patch.

Validated source RPM:

- NEVRA: `kernel-6.6.0-159.4.3.154.oe2403sp4.x86_64`
- SHA256: `493e4edab60f2807b7a5bfb4dd616f8386a5432c01b15d70e99d6dce897ae3dc`
- Validated custom kernel: `6.6.0-159.4.3.154.oe2403sp4.schedx1`

## Build

Install the standard openEuler kernel build dependencies, then run:

```bash
kernel/openEuler-24.03-LTS-SP4/build_sched_ext_kernel.sh \
  /path/to/kernel-6.6.0-159.4.3.154.oe2403sp4.src.rpm prepare

SCHEDX_KERNEL_JOBS=4 \
kernel/openEuler-24.03-LTS-SP4/build_sched_ext_kernel.sh \
  /path/to/kernel-6.6.0-159.4.3.154.oe2403sp4.src.rpm build
```

`prepare` verifies the source checksum, extracts the source and merges the
minimal config fragment without compiling. `build` additionally creates a
versioned kernel RPM. A full build needs substantial disk space and time.
The `prepare` path and both boot-selection dry runs were validated on the SP4
VM against the source RPM checksum above.

## Install And Roll Back

Both boot-selection scripts default to a dry run:

```bash
kernel/openEuler-24.03-LTS-SP4/install_sched_ext_kernel.sh /path/to/kernel.rpm
sudo kernel/openEuler-24.03-LTS-SP4/install_sched_ext_kernel.sh /path/to/kernel.rpm --apply

kernel/openEuler-24.03-LTS-SP4/select_stock_kernel.sh
sudo kernel/openEuler-24.03-LTS-SP4/select_stock_kernel.sh --apply
```

After rebooting into the custom kernel, verify it with:

```bash
kernel/openEuler-24.03-LTS-SP4/verify_sched_ext_kernel.sh
```

The stock kernel is deliberately retained. SchedX automatically uses the
cgroup-only fallback when booted into a kernel without sched_ext.
