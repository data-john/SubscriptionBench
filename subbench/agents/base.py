"""Base interfaces and data structures for agent execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from subbench.agents.prompts import RenderedPrompt


@dataclass
class ParsedEvent:
    idx: int
    event: str
    step_type: str | None = None
    state: str | None = None
    tool_name: str | None = None
    duration_seconds: float | None = None
    total_tokens: int | None = None


@dataclass
class StreamSummary:
    events: list[ParsedEvent] = field(default_factory=list)
    result_event: dict[str, Any] | None = None
    halt_reason: str = "timeout"
    finished_reason: str | None = None
    num_turns: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    thinking_tokens: int | None = None
    cache_read_tokens: int | None = None
    total_tokens: int | None = None
    tool_calls: int = 0
    error_text: str | None = None


@dataclass
class AttemptOutcome:
    halt_reason: str
    exit_code: int | None
    finished_reason: str | None
    agy_version_pre: str | None
    agy_version_post: str | None
    wall_seconds: float
    num_turns: int | None
    input_tokens: int | None
    output_tokens: int | None
    thinking_tokens: int | None
    cache_read_tokens: int | None
    tool_calls: int
    events: list[ParsedEvent]
    error_text: str | None


class AgentAdapter(Protocol):
    """Protocol for driving an agent during a task attempt."""

    def run_attempt(
        self,
        *,
        instance_id: str,
        prompt: RenderedPrompt,
        container_id: str,
        timeout_seconds: int,
    ) -> AttemptOutcome:
        """Run an attempt inside the provisioned container until completion or timeout."""
        ...
