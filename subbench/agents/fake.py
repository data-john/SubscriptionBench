"""Offline fake agent adapter for unit and integration testing."""

from __future__ import annotations

from pathlib import Path

from subbench.agents.base import AttemptOutcome, ParsedEvent
from subbench.agents.prompts import RenderedPrompt


class FakeAgentAdapter:
    """Configurable fake agent adapter producing predictable outcomes offline."""

    def __init__(
        self,
        halt_reason: str = "completed",
        tool_calls: int = 2,
        events: list[ParsedEvent] | None = None,
        error_text: str | None = None,
    ) -> None:
        self.halt_reason = halt_reason
        self.tool_calls = tool_calls
        self.events = events or [
            ParsedEvent(idx=1, event="init"),
            ParsedEvent(
                idx=2,
                event="step_update",
                step_type="tool",
                state="DONE",
                tool_name="edit_file",
            ),
            ParsedEvent(idx=3, event="result"),
        ]
        self.error_text = error_text

    def run_attempt(
        self,
        *,
        instance_id: str,
        prompt: RenderedPrompt,
        container_id: str,
        timeout_seconds: int,
        raw_output_path: Path | None = None,
        stderr_output_path: Path | None = None,
    ) -> AttemptOutcome:
        if raw_output_path:
            raw_output_path.parent.mkdir(parents=True, exist_ok=True)
            res_status = "SUCCESS" if self.halt_reason == "completed" else "ERROR"
            raw_output_path.write_text(
                '{"event":"init"}\n'
                '{"event":"step_update","step_update":{"step_index":0,"state":"DONE","tool_name":"edit_file"}}\n'
                f'{{"event":"result","result":{{"status":"{res_status}"}}}}\n'
            )
        if stderr_output_path:
            stderr_output_path.parent.mkdir(parents=True, exist_ok=True)
            stderr_output_path.write_text("")
        return AttemptOutcome(
            halt_reason=self.halt_reason,
            exit_code=0 if self.halt_reason == "completed" else 1,
            finished_reason="SUCCESS" if self.halt_reason == "completed" else "ERROR",
            agy_version_pre="v1.4.12",
            agy_version_post="v1.4.12",
            wall_seconds=0.05,
            num_turns=1,
            input_tokens=1000,
            output_tokens=50,
            thinking_tokens=0,
            cache_read_tokens=0,
            tool_calls=self.tool_calls,
            events=self.events,
            error_text=self.error_text,
        )
