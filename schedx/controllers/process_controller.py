from __future__ import annotations

from pathlib import Path


# Protected control processes - must match planner.py
PROTECTED_CONTROL_PROCESSES = {
    "schedx",
    "python",
    "python3",
    "bash",
    "sh",
    "sshd",
    "systemd",
    "networkmanager",
    "firewalld",
    "tuned",
}


def is_protected_control_process(comm: str, cmdline: str) -> bool:
    """Check if a process is a protected control process."""
    base = comm.lower()
    text = f"{base} {cmdline.lower()}"
    if base in PROTECTED_CONTROL_PROCESSES:
        return True
    if "python -m schedx" in text or "python3 -m schedx" in text:
        return True
    if "schedx" in text and "stress-ng" not in text:
        return True
    return False


class ProcessController:
    def __init__(self, proc_root: Path = Path("/proc")) -> None:
        self.proc_root = proc_root

    def find_by_name(self, name: str) -> list[int]:
        pids: list[int] = []
        if not name.strip():
            return pids
        if not self.proc_root.exists():
            return pids
        for entry in self.proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            comm = entry / "comm"
            cmdline = entry / "cmdline"
            try:
                value = comm.read_text(encoding="utf-8", errors="replace").strip()
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
            try:
                raw_cmdline = cmdline.read_bytes()
                cmd_value = " ".join(
                    part.decode("utf-8", errors="replace") for part in raw_cmdline.split(b"\0") if part
                )
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                cmd_value = ""
            if not cmd_value:
                continue
            if is_protected_control_process(value.lower(), cmd_value.lower()):
                continue
            if value == name or name in cmd_value:
                pids.append(int(entry.name))
        return pids
