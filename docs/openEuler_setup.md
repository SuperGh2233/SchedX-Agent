# openEuler 24.03-LTS-SP3 Setup

```bash
sudo dnf install -y python3 python3-pip git stress-ng nginx redis sysbench
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
schedx status
```

`wrk` may need to be built from source:

```bash
scripts/install_wrk.sh
```

For sched_ext experiments, use a kernel build with sched_ext enabled and install the scx userspace tools. Phase 1 still works in cgroup-only mode when `/sys/kernel/sched_ext` is absent.
