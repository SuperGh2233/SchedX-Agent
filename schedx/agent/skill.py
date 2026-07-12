from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from schedx.agent.context import AgentContext


@dataclass
class SkillResult:
    ok: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)


class Skill(Protocol):
    name: str
    description: str

    def run(self, context: AgentContext) -> SkillResult:
        ...

