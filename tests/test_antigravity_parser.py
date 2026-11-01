import json
import sys
import time
from pathlib import Path

from subbench.agents.antigravity import (
    DEFAULT_SETTINGS,
    AntigravityAdapter,
    parse_stream_json,
)
from subbench.agents.prompts import RenderedPrompt

ROOT = Path(__file__).resolve().parent.parent


def test_parse_real_flash_low_ndjson():
    path = ROOT / "docs" / "phase0_samples" / "stream_json_flash_low.ndjson"
    lines = path.read_text().splitlines()
    summary = parse_stream_json(lines)

    assert summary.halt_reason == "completed"
    assert summary.finished_reason == "SUCCESS"
    assert summary.num_turns == 1
    assert summary.input_tokens == 13660
    assert summary.output_tokens == 33
    assert summary.thinking_tokens == 0
    assert summary.cache_read_tokens == 0
    assert summary.total_tokens == 13693
    assert summary.tool_calls == 1  # 1 write_file DONE
    assert len(summary.events) == 7
    assert summary.error_text is None


def test_parse_real_argon_low_ndjson():
    path = ROOT / "docs" / "phase0_samples" / "stream_json_argon_low.ndjson"
    lines = path.read_text().splitlines()
    summary = parse_stream_json(lines)

    assert summary.halt_reason == "completed"
    assert summary.input_tokens == 8984
    assert summary.output_tokens == 54
    assert summary.cache_read_tokens == 4757
    assert summary.total_tokens == 9038
    assert summary.tool_calls == 1
    assert len(summary.events) == 8


def test_parse_exit_code_zero_with_result_error():
    lines = [
        json.dumps({"event": "init", "init": {"model": "gemini-3.8-flash"}}),
        json.dumps(
            {
                "event": "result",
                "result": {
                    "status": "ERROR",
                    "error": "SyntaxError in agent code",
                    "finished_reason": "ERROR",
                },
            }
        ),
    ]
    summary = parse_stream_json(lines)
    assert summary.halt_reason == "agent_error"
    assert summary.finished_reason == "ERROR"
    assert summary.error_text == "SyntaxError in agent code"


def test_parse_quota_exhausted_classification():
    lines = [
        json.dumps(
            {
                "event": "result",
                "result": {
                    "status": "ERROR",
                    "error": "RESOURCE_EXHAUSTED: Quota exceeded for model gemini-4-argon",
                },
            }
        )
    ]
    summary = parse_stream_json(lines)
    assert summary.halt_reason == "quota_restricted"


def test_parse_infra_error_classification():
    lines = [
        json.dumps(
            {
                "event": "result",
                "result": {
                    "status": "ERROR",
                    "error": "Authentication failed: Keyring is locked",
                },
            }
        )
    ]
    summary = parse_stream_json(lines)
    assert summary.halt_reason == "infra_error"


def test_parse_truncated_timeout_stream():
    # No result line, ends with a half-written JSON fragment
    lines = [
        json.dumps({"event": "init", "init": {"model": "m"}}),
        json.dumps({"event": "step_update", "step_update": {"step_index": 0, "state": "DONE"}}),
        '{"event": "step_update", "step_update": {"step_index": 1, "state": "ACTI',  # truncated
    ]
    summary = parse_stream_json(lines)
    assert summary.halt_reason == "timeout"
    assert summary.result_event is None
    assert len(summary.events) == 2  # truncated line safely ignored


def test_antigravity_adapter_command_and_deny_list():
    adapter = AntigravityAdapter(model_id="gemini-3.8-flash")
    cmd = adapter.build_exec_command("solve problem")
    assert "--disable-slash-commands" in cmd
    assert "--output-format" in cmd
    assert "stream-json" in cmd

    deny_list = DEFAULT_SETTINGS["permissions"]["deny"]
    expected_denied = [
        "search_web",
        "search_web(*)",
        "read_url",
        "read_url(*)",
        "read_url_content",
        "read_url_content(*)",
        "open_browser_url",
        "open_browser_url(*)",
        "read_browser_page",
        "read_browser_page(*)",
        "capture_browser_screenshot",
        "capture_browser_screenshot(*)",
        "capture_browser_console_logs",
        "click_browser_pixel",
        "execute_browser_javascript",
        "list_browser_pages",
        "browser_subagent",
        "browser_*",
    ]
    for tool in expected_denied:
        assert tool in deny_list


def test_antigravity_adapter_timeout_offline(monkeypatch, tmp_path: Path):
    adapter = AntigravityAdapter(model_id="gemini-3.8-flash")
    raw_path = tmp_path / "stream_json.ndjson"
    prompt = RenderedPrompt(text="solve this", template_id="minimal-v1", sha256_hash="abc")

    # Command that outputs one init line immediately, then sleeps for 30s
    hanging_py = (
        'import time; print(\'{"event":"init","init":{"model":"gemini-3.8-flash"}}\', flush=True); '
        "time.sleep(30)"
    )
    hanging_cmd = [sys.executable, "-c", hanging_py]
    monkeypatch.setattr(adapter, "build_container_exec_args", lambda c, p: hanging_cmd)

    t0 = time.monotonic()
    outcome = adapter.run_attempt(
        instance_id="pytest-dev__pytest-5809",
        prompt=prompt,
        container_id="dummy-container",
        timeout_seconds=1,
        raw_output_path=raw_path,
    )
    elapsed = time.monotonic() - t0

    assert elapsed < 3.0, f"Process took {elapsed}s, expected ~1s timeout"
    assert outcome.halt_reason == "timeout"
    assert outcome.exit_code == -9
    assert raw_path.exists()
    content = raw_path.read_text()
    assert '{"event":"init"' in content
    assert len(outcome.events) >= 1
