"""Minimal child launcher: enter the cgroup, then await policy registration."""

import os
import sys
from pathlib import Path


def main() -> None:
    ready, gate = int(sys.argv[1]), int(sys.argv[2])
    try:
        group = Path(sys.argv[3])
        (group / "cgroup.procs").write_text(str(os.getpid()), encoding="utf-8")
        os.write(ready, b"R")
        os.close(ready)
        if os.read(gate, 1) != b"G":
            raise RuntimeError("policy registration was cancelled")
        os.close(gate)
        os.execvp(sys.argv[4], sys.argv[4:])
    except Exception as exc:
        print(f"tool startup failed: {exc}", file=sys.stderr)
        sys.exit(125)


if __name__ == "__main__":
    main()
