"""Tests for the issue #62 follow-up items.

Covers:
1. TTY-detect at REPL entry (no subcommand + non-TTY stdin → exit 2 + help)
2. `engineer --json` / `--no-interactive` parity with `agent --json`
"""

import json
import subprocess
from unittest.mock import patch

from click.testing import CliRunner

from reverse_api.cli import (
    AGENT_JSON_SCHEMA_VERSION,
    _build_engineer_payload,
    engineer,
    main,
)

EXPECTED_ENGINEER_KEYS = {
    "schema_version",
    "status",
    "run_id",
    "prompt",
    "fresh",
    "script_path",
    "usage",
    "error",
}


# ---------------------------------------------------------------------------
# Item #1: TTY-detect at REPL entry
# ---------------------------------------------------------------------------


class TestTtyDetectionAtReplEntry:
    """Without a TTY and no subcommand, the REPL must NOT block on prompt_toolkit."""

    def test_no_tty_no_subcommand_exits_2(self):
        """End-to-end: invoke the installed binary with stdin redirected from /dev/null."""
        result = subprocess.run(
            ["uv", "run", "reverse-api-engineer"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 2
        assert "stdin is not a TTY" in result.stderr
        # Help is printed too so a wrapper can self-discover the subcommands
        assert "agent" in result.stderr
        assert "engineer" in result.stderr

    def test_no_tty_with_subcommand_does_not_trip(self):
        """Subcommands work fine without a TTY (this is the whole point of agent --json)."""
        result = subprocess.run(
            ["uv", "run", "reverse-api-engineer", "list", "--json"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
        )
        # list --json on empty history should succeed with []
        assert result.returncode == 0
        # Nothing about TTY in the output
        assert "TTY" not in result.stderr


# ---------------------------------------------------------------------------
# Item #2: engineer --json
# ---------------------------------------------------------------------------


class TestBuildEngineerPayload:
    """Stable shape for the `engineer --json` payload."""

    def test_success_shape(self):
        payload = _build_engineer_payload(
            {"script_path": "/abs/api_client.py", "usage": {"total_cost": 0.001}},
            run_id="abc123",
            prompt="add pagination",
            fresh=False,
        )
        assert payload["schema_version"] == AGENT_JSON_SCHEMA_VERSION
        assert payload["status"] == "ok"
        assert payload["run_id"] == "abc123"
        assert payload["prompt"] == "add pagination"
        assert payload["fresh"] is False
        assert payload["script_path"] == "/abs/api_client.py"
        assert payload["usage"]["total_cost"] == 0.001
        assert payload["error"] is None
        assert set(payload.keys()) == EXPECTED_ENGINEER_KEYS

    def test_none_result_is_error(self):
        payload = _build_engineer_payload(None, run_id="abc", prompt=None, fresh=False)
        assert payload["status"] == "error"
        assert payload["error"]
        assert set(payload.keys()) == EXPECTED_ENGINEER_KEYS

    def test_explicit_error_overrides(self):
        payload = _build_engineer_payload(
            {"script_path": "/x.py"}, run_id="abc", prompt=None, fresh=False, error="boom"
        )
        assert payload["status"] == "error"
        assert payload["error"] == "boom"

    def test_inner_error_propagates(self):
        payload = _build_engineer_payload(
            {"error": "engine crashed"}, run_id="abc", prompt=None, fresh=False
        )
        assert payload["status"] == "error"
        assert payload["error"] == "engine crashed"


class TestEngineerCommandJson:
    """`engineer` click command --json wiring."""

    def test_json_emits_payload_on_success(self):
        runner = CliRunner()
        fake_result = {"script_path": "/abs/api_client.py", "usage": {"total_cost": 0.0}}
        with patch("reverse_api.cli.run_engineer", return_value=fake_result):
            result = runner.invoke(engineer, ["abc123", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        assert payload["status"] == "ok"
        assert payload["run_id"] == "abc123"
        assert payload["script_path"] == "/abs/api_client.py"
        assert set(payload.keys()) == EXPECTED_ENGINEER_KEYS

    def test_json_emits_error_on_not_found(self):
        runner = CliRunner()
        # run_engineer returns None when the run can't be located
        with patch("reverse_api.cli.run_engineer", return_value=None):
            result = runner.invoke(engineer, ["doesnotexist", "--json"])
        assert result.exit_code == 1
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        assert payload["status"] == "error"
        assert payload["run_id"] == "doesnotexist"
        assert payload["error"]
        assert set(payload.keys()) == EXPECTED_ENGINEER_KEYS

    def test_json_emits_error_on_keyboard_interrupt(self):
        runner = CliRunner()
        with patch("reverse_api.cli.run_engineer", side_effect=KeyboardInterrupt):
            result = runner.invoke(engineer, ["abc123", "--json"])
        assert result.exit_code == 1
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        assert payload["status"] == "error"
        assert payload["error"] == "interrupted"

    def test_json_emits_error_on_exception(self):
        runner = CliRunner()
        with patch("reverse_api.cli.run_engineer", side_effect=RuntimeError("boom")):
            result = runner.invoke(engineer, ["abc123", "--json"])
        assert result.exit_code == 1
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        assert payload["status"] == "error"
        assert payload["error"] == "boom"

    def test_prompt_without_fresh_threaded_as_additional(self):
        """--prompt without --fresh must reach run_engineer as additional_instructions
        even on the JSON path (regression for the cubic-dev-ai catch on PR #63)."""
        runner = CliRunner()
        with patch("reverse_api.cli.run_engineer", return_value={"script_path": "/x.py"}) as mock_run:
            result = runner.invoke(engineer, ["abc123", "--json", "-p", "add pagination"])
        assert result.exit_code == 0, result.output
        kwargs = mock_run.call_args.kwargs
        assert kwargs["prompt"] is None
        assert kwargs["additional_instructions"] == "add pagination"
        assert kwargs["is_fresh"] is False

    def test_fresh_with_prompt_threaded_as_main(self):
        runner = CliRunner()
        with patch("reverse_api.cli.run_engineer", return_value={"script_path": "/x.py"}) as mock_run:
            result = runner.invoke(
                engineer, ["abc123", "--json", "--fresh", "-p", "extract auth"]
            )
        assert result.exit_code == 0, result.output
        kwargs = mock_run.call_args.kwargs
        assert kwargs["prompt"] == "extract auth"
        assert kwargs["additional_instructions"] is None
        assert kwargs["is_fresh"] is True

    def test_no_interactive_flag_accepted(self):
        """--no-interactive is reserved for symmetry; should not crash."""
        runner = CliRunner()
        with patch("reverse_api.cli.run_engineer", return_value={"script_path": "/x.py"}):
            result = runner.invoke(engineer, ["abc123", "--no-interactive"])
        assert result.exit_code == 0, result.output


class TestRootHelpMentionsScripted:
    """Item #6 partial: root --help should advertise scripted features."""

    def test_root_help_mentions_json_and_no_interactive(self):
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "--json" in result.output
        assert "--no-interactive" in result.output
