"""Tests for the agent-friendly CLI surface (--json, --no-interactive, payload shape)."""

import json
from unittest.mock import patch

from click.testing import CliRunner

from reverse_api.cli import (
    AGENT_JSON_SCHEMA_VERSION,
    _build_agent_payload,
    agent,
    list_runs,
    show_run,
)


class TestBuildAgentPayload:
    """Stable shape for the `agent --json` payload."""

    def test_success_shape(self, tmp_path):
        payload = _build_agent_payload(
            {
                "run_id": "abc123",
                "mode": "auto",
                "script_path": str(tmp_path / "scripts" / "api_client.py"),
                "usage": {"input_tokens": 1, "output_tokens": 2, "total_cost": 0.001},
            },
            prompt="capture the X api",
            url="https://example.com",
        )
        assert payload["schema_version"] == AGENT_JSON_SCHEMA_VERSION
        # No run produced a HAR yet on disk in this test, so har_path is None
        assert payload["status"] == "ok"
        assert payload["run_id"] == "abc123"
        assert payload["mode"] == "auto"
        assert payload["prompt"] == "capture the X api"
        assert payload["url"] == "https://example.com"
        assert payload["usage"]["total_cost"] == 0.001
        assert payload["error"] is None
        # Must be JSON-serializable (no Path objects sneaking through)
        json.dumps(payload)

    def test_no_run_id_is_error(self):
        payload = _build_agent_payload({}, prompt="x", url=None)
        assert payload["status"] == "error"
        assert payload["error"]
        assert payload["run_id"] is None

    def test_explicit_error_overrides(self):
        payload = _build_agent_payload(
            {"run_id": "abc", "mode": "auto"},
            prompt="x",
            url=None,
            error="boom",
        )
        assert payload["status"] == "error"
        assert payload["error"] == "boom"

    def test_inner_error_propagates(self):
        payload = _build_agent_payload(
            {"run_id": "abc", "mode": "auto", "error": "inner"},
            prompt="x",
            url=None,
        )
        assert payload["status"] == "error"
        assert payload["error"] == "inner"


class TestAgentCommandJson:
    """`agent` click command's --json / --no-interactive behavior."""

    def test_json_without_prompt_exits_2(self):
        runner = CliRunner()
        result = runner.invoke(agent, ["--json"])
        assert result.exit_code == 2
        payload = json.loads(result.stdout.strip())
        assert payload["status"] == "error"
        assert "prompt" in payload["error"].lower()

    def test_no_interactive_without_prompt_exits_2(self):
        runner = CliRunner()
        result = runner.invoke(agent, ["--no-interactive"])
        assert result.exit_code == 2
        # Plain text on stderr, not JSON, since --json wasn't requested
        assert "prompt" in (result.stderr or result.output).lower()

    def test_json_emits_payload_on_success(self):
        runner = CliRunner()
        fake_result = {
            "run_id": "deadbeef0001",
            "mode": "auto",
            "script_path": None,
            "usage": {"total_cost": 0.0},
        }
        with patch("reverse_api.cli.run_agent_capture", return_value=fake_result):
            result = runner.invoke(agent, ["--json", "-p", "capture the X api"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        assert payload["status"] == "ok"
        assert payload["run_id"] == "deadbeef0001"
        assert payload["prompt"] == "capture the X api"

    def test_json_emits_error_payload_on_exception(self):
        runner = CliRunner()
        with patch("reverse_api.cli.run_agent_capture", side_effect=RuntimeError("boom")):
            result = runner.invoke(agent, ["--json", "-p", "x"])
        assert result.exit_code == 1
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        assert payload["status"] == "error"
        assert payload["error"] == "boom"


class TestListJsonEmpty:
    """`list --json` should emit a JSON array even when there is no history."""

    def test_empty_history_emits_empty_array(self, tmp_path):
        runner = CliRunner()
        # Repoint session_manager.history to empty list for the test
        with patch("reverse_api.cli.session_manager") as sm:
            sm.history = []
            result = runner.invoke(list_runs, ["--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout.strip()) == []


class TestShowJsonNotFound:
    """`show <id> --json` should emit a structured error and exit non-zero."""

    def test_unknown_run_id_emits_error(self):
        runner = CliRunner()
        with patch("reverse_api.cli.session_manager") as sm:
            sm.get_run.return_value = None
            sm.history = []
            result = runner.invoke(show_run, ["doesnotexist", "--json"])
        assert result.exit_code == 1
        payload = json.loads(result.stdout.strip())
        assert "error" in payload
        assert payload["run_id"] == "doesnotexist"
