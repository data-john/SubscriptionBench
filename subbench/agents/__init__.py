"""Agent adapters, prompt rendering, and stream parsers."""

from subbench.agents.antigravity import AntigravityAdapter
from subbench.agents.base import AgentAdapter, AttemptOutcome, ParsedEvent, StreamSummary
from subbench.agents.codex import CodexAdapter
from subbench.agents.fake import FakeAgentAdapter
from subbench.agents.prompts import RenderedPrompt, render_prompt

__all__ = [
    "AgentAdapter",
    "AntigravityAdapter",
    "AttemptOutcome",
    "CodexAdapter",
    "FakeAgentAdapter",
    "ParsedEvent",
    "RenderedPrompt",
    "StreamSummary",
    "render_prompt",
]
