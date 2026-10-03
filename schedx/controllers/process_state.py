from __future__ import annotations

import json
import os
from pathlib import Path

from schedx.state import atomic_json, process_start_time, state_lock


class ProcessState:
    """Persist original task attributes before changing them."""

    def __init__(self, path: Path, owner: str | None = None, transaction: str | None = None) -> None:
        self.path = path
        self.owner = owner
        self.transaction = transaction

    def _load(self) -> list[dict]:
        return json.loads(self.path.read_text()) if self.path.exists() else []

    def record(self, pid: int, kind: str, previous: object) -> None:
        start = process_start_time(pid)
        if start is None:
            raise ProcessLookupError(f"cannot identify process {pid}")
        with state_lock(self.path):
            entries = self._load()
            for entry in entries:
                if (entry["pid"], entry["kind"], entry.get("pid_start")) == (pid, kind, start):
                    if entry.get("owner") != self.owner:
                        raise RuntimeError(f"process {pid} {kind} is owned by another operation")
                    if entry.get("transaction") == self.transaction:
                        return
            entries.append({"pid": pid, "pid_start": start, "kind": kind, "previous": previous, "owner": self.owner, "transaction": self.transaction})
            atomic_json(self.path, entries)

    def rollback(self, dry_run: bool = False) -> list[dict]:
        if dry_run:
            return [{**entry, "status": "dry_run"} for entry in self._load()]
        results = []
        with state_lock(self.path):
            pending = []
            blocked = set()
            for entry in reversed(self._load()):
                if (self.owner is not None and entry.get("owner") != self.owner) or (self.transaction is not None and entry.get("transaction") != self.transaction):
                    pending.append(entry)
                    continue
                pid = int(entry["pid"])
                key = (pid, entry["kind"], entry.get("pid_start"))
                if key in blocked:
                    pending.append(entry)
                    continue
                if process_start_time(pid) != entry.get("pid_start"):
                    results.append({**entry, "status": "skipped", "reason": "target_process_exited_or_reused"})
                    continue
                try:
                    value = entry["previous"]
                    if entry["kind"] == "nice":
                        os.setpriority(os.PRIO_PROCESS, pid, int(value))
                    elif entry["kind"] == "affinity":
                        os.sched_setaffinity(pid, set(value))
                    elif entry["kind"] == "sched_policy":
                        os.sched_setscheduler(pid, value["policy"], os.sched_param(value["priority"]))
                    else:
                        raise ValueError(f"unknown process setting: {entry['kind']}")
                    results.append({**entry, "status": "restored"})
                except (OSError, ValueError) as exc:
                    pending.append(entry)
                    blocked.add(key)
                    results.append({**entry, "status": "failed", "reason": f"restore_failed: {exc}"})
            if pending:
                atomic_json(self.path, list(reversed(pending)))
            else:
                self.path.unlink(missing_ok=True)
        return results
