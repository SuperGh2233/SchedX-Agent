# openEuler 24.03 LTS SP4 Setup

```bash
sudo dnf install -y python3 python3-pip git stress-ng nginx redis sysbench bpftool
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
schedx status
```

`wrk` may need to be built from source:

```bash
scripts/install_wrk.sh
```

The stock `6.6.0-159.4.3.154.oe2403sp4` binary kernel has BPF and BTF enabled
but does not enable `CONFIG_SCHED_CLASS_EXT`. SchedX remains fully usable in
cgroup-only fallback mode on that kernel.

For native sched_ext, follow the reproducible config-only rebuild in:

```text
kernel/openEuler-24.03-LTS-SP4/README.md
```

The validated custom kernel is
`6.6.0-159.4.3.154.oe2403sp4.schedx1`. Verify the running environment with:

```bash
kernel/openEuler-24.03-LTS-SP4/verify_sched_ext_kernel.sh
python3 -m pytest -q
sudo scx/build.sh
schedx status
```

Keep the stock kernel installed. The provided rollback script changes the GRUB
default only when `--apply` is explicitly supplied.
