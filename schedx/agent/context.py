from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class DecisionRecord:
    """Record of an agent decision."""
    timestamp: str
    phase: str
    action: str
    reason: str
    success: bool
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionState:
    """Persistent session state across loop iterations."""
    session_id: str
    start_time: str
    iteration_count: int = 0
    decisions: list[DecisionRecord] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentContext:
    dry_run: bool = False
    state_dir: Path = Path(".schedx")
    data: dict[str, Any] = field(default_factory=dict)
    session: SessionState | None = None
    history: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.session is None:
            self.session = SessionState(
                session_id=f"session-{int(time.time())}",
                start_time=datetime.now().isoformat(),
            )

    @property
    def rollback_file(self) -> Path:
        return self.state_dir / "rollback.json"

    @property
    def scx_rollback_file(self) -> Path:
        return self.state_dir / "scx_rollback.json"

    @property
    def session_file(self) -> Path:
        return self.state_dir / "session.json"

    def record_decision(self, phase: str, action: str, reason: str, success: bool, **kwargs: Any) -> None:
        """Record a decision in the session history."""
        record = DecisionRecord(
            timestamp=datetime.now().isoformat(),
            phase=phase,
            action=action,
            reason=reason,
            success=success,
            metadata=kwargs,
        )
        if self.session:
            self.session.decisions.append(record)
            self.session.iteration_count += 1

        self.history.append({
            "timestamp": record.timestamp,
            "phase": phase,
            "action": action,
            "reason": reason,
            "success": success,
            **kwargs,
        })

    def save_session(self) -> None:
        """Save session state to disk."""
        if not self.session:
            return
        self.state_dir.mkdir(parents=True, exist_ok=True)
        session_data = {
            "session_id": self.session.session_id,
            "start_time": self.session.start_time,
            "iteration_count": self.session.iteration_count,
            "decisions": [
                {
                    "timestamp": d.timestamp,
                    "phase": d.phase,
                    "action": d.action,
                    "reason": d.reason,
                    "success": d.success,
                    "metadata": d.metadata,
                }
                for d in self.session.decisions
            ],
            "metrics": self.session.metrics,
        }
        self.session_file.write_text(
            json.dumps(session_data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def load_session(self) -> bool:
        """Load session state from disk."""
        if not self.session_file.exists():
            return False
        try:
            data = json.loads(self.session_file.read_text(encoding="utf-8"))
            self.session = SessionState(
                session_id=data["session_id"],
                start_time=data["start_time"],
                iteration_count=data.get("iteration_count", 0),
                decisions=[
                    DecisionRecord(**d) for d in data.get("decisions", [])
                ],
                metrics=data.get("metrics", {}),
            )
            return True
        except (json.JSONDecodeError, KeyError):
            return False

    def get_decision_summary(self) -> dict[str, Any]:
        """Get summary of all decisions made in this session."""
        if not self.session:
            return {"total": 0, "successful": 0, "failed": 0}

        total = len(self.session.decisions)
        successful = sum(1 for d in self.session.decisions if d.success)
        return {
            "total": total,
            "successful": successful,
            "failed": total - successful,
            "success_rate": successful / total if total > 0 else 0.0,
            "phases_covered": list(set(d.phase for d in self.session.decisions)),
        }
