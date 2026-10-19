"""M7 Layer A: runner options, init checks, usage-error classification, login status.

No real Claude calls: the SDK stream is faked with the SDK's own message types.
"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    CLINotFoundError,
    ProcessError,
    RateLimitEvent,
    RateLimitInfo,
    ResultError,
    ResultMessage,
    SystemMessage,
    TextBlock,
)
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
from typer.testing import CliRunner

from assistant import cli, config
from assistant.agent import runner
from assistant.agent.runner import (
    DISALLOWED_BUILTINS,
    AgentFailure,
    FailureKind,
    build_options,
    classify_exception,
    classify_result,
    init_problems,
    run_session,
)
from assistant.guard import BILLING_VARS, BillingCredentialError

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

CLEAN_INIT = {
    "type": "system",
    "subtype": "init",
    "session_id": "s-1",
    "apiKeySource": "none",
    "tools": [],
    "mcp_servers": [],
    "plugins": [],
    "skills": [],
    "slash_commands": [],
    "agents": ["claude", "Explore", "general-purpose", "Plan", "statusline-setup"],
    "model": "claude-fake",
    "permissionMode": "default",
}


def init(**changes) -> SystemMessage:
    return SystemMessage(subtype="init", data={**CLEAN_INIT, **changes})


def reply(text="PA-SMOKE-OK", error=None) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="claude-fake", error=error)


def result(text="PA-SMOKE-OK", is_error=False, subtype="success", status=None) -> ResultMessage:
    return ResultMessage(
        subtype=subtype, duration_ms=1200, duration_api_ms=900, is_error=is_error, num_turns=1, session_id="s-1", result=text, api_error_status=status
    )


def limit(status="allowed", kind="five_hour") -> RateLimitEvent:
    return RateLimitEvent(rate_limit_info=RateLimitInfo(status=status, rate_limit_type=kind), uuid="u", session_id="s-1")


class FakeStream:
    """Yields messages; records whether it was closed (the CLI process stopped)."""

    def __init__(self, messages, raise_after: Exception | None = None):
        self.messages, self.raise_after = list(messages), raise_after
        self.yielded = 0
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.yielded < len(self.messages):
            self.yielded += 1
            return self.messages[self.yielded - 1]
        if self.raise_after:
            raise self.raise_after
        raise StopAsyncIteration

    async def aclose(self):
        self.closed = True


def run(messages, raise_after=None, options=None):
    stream = FakeStream(messages, raise_after)
    opts = options or build_options(system_prompt="x", max_turns=1, cwd=Path("/tmp"))
    try:
        return asyncio.run(run_session("hi", opts, query_fn=lambda **kw: stream)), stream
    finally:
        assert stream.closed


def failure(messages, raise_after=None) -> tuple[AgentFailure, FakeStream]:
    stream = FakeStream(messages, raise_after)
    opts = build_options(system_prompt="x", max_turns=1, cwd=Path("/tmp"))
    with pytest.raises(AgentFailure) as e:
        asyncio.run(run_session("hi", opts, query_fn=lambda **kw: stream))
    assert stream.closed
    return e.value, stream


# --- Options and the CLI command (S-5, S-16, safety test 9 Layer A) ----------------------------


def test_options_are_locked_down():
    o = build_options(system_prompt="x", max_turns=6, cwd=Path("/tmp"))
    assert o.tools == [] and o.allowed_tools == [] and o.setting_sources == []
    assert o.mcp_servers == {} and o.strict_mcp_config and o.plugins == []
    assert o.max_turns == 6 and o.fallback_model is None and o.model == runner.MODEL == "claude-sonnet-5-5" and o.skills == []
    assert json.loads(o.settings) == {"enabledPlugins": {f"{n}@builtin": False for n in runner.BUILTIN_PLUGINS}}
    assert Path(o.cli_path) == runner.bundled_cli() and runner.bundled_cli().is_file()
    assert {"Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch", "Task", "NotebookEdit"} <= set(o.disallowed_tools)


def test_cli_command_has_no_builtin_tools_settings_or_persistence():
    o = build_options(system_prompt="x", max_turns=1, cwd=Path("/tmp"))
    cmd = SubprocessCLITransport(prompt="hi", options=o)._build_command()
    assert cmd[0] == str(runner.bundled_cli())
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--setting-sources=" in cmd and "--strict-mcp-config" in cmd and "--no-session-persistence" in cmd
    assert "--disable-slash-commands" in cmd and "--settings" in cmd
    assert cmd[cmd.index("--disallowedTools") + 1].split(",") == list(DISALLOWED_BUILTINS)
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-5-5"
    assert not any(flag in cmd for flag in ("--fallback-model", "--plugin-dir", "--resume", "--continue"))


def test_billing_variables_are_removed_from_the_process_env():
    env = {"PATH": "/usr/bin", **{name: "x" for name in BILLING_VARS}}
    runner.clean_process_env(env)
    assert env == {"PATH": "/usr/bin"}


def test_run_session_refuses_with_billing_variable(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    stream = FakeStream([init(), reply(), result()])
    with pytest.raises(BillingCredentialError):
        asyncio.run(run_session("hi", build_options(system_prompt="x", max_turns=1, cwd=Path("/tmp")), query_fn=lambda **kw: stream))
    assert stream.yielded == 0  # no session started


# --- Happy path -------------------------------------------------------------------------------


def test_clean_session_returns_text():
    res, _ = run([init(), limit("allowed"), reply(), result()])
    assert res.text == "PA-SMOKE-OK" and res.num_turns == 1 and res.session_id == "s-1"
    assert res.rate_limits == ["allowed"] and res.init["apiKeySource"] == "none"


def test_warning_level_rate_limit_is_fine():
    res, _ = run([init(), limit("allowed_warning"), reply(), result()])
    assert res.text == "PA-SMOKE-OK"


# --- Auth source and clean session (S-7, safety test 9) ----------------------------------------


@pytest.mark.parametrize("source", ["ANTHROPIC_API_KEY", "apiKeyHelper", "/login managed key", None])
def test_api_key_auth_aborts_before_any_reply(source):
    e, stream = failure([init(apiKeySource=source), reply(), result()])
    assert e.kind is FailureKind.api_key_auth and stream.yielded == 1


@pytest.mark.parametrize(
    "changes,detail",
    [
        ({"tools": ["Bash"]}, "tools:Bash"),
        ({"tools": ["Read", "mcp__pa__get_tasks"]}, "tools:Read,mcp__pa__get_tasks"),
        ({"mcp_servers": [{"name": "my-personal-server", "status": "connected"}]}, "mcp_servers:my-personal-server"),
        ({"plugins": [{"name": "p", "path": "/x"}]}, "plugins"),
        ({"skills": ["my-skill"]}, "skills"),
        ({"agents": ["claude", "my-custom-agent"]}, "agents:my-custom-agent"),
        ({"slash_commands": ["compact"]}, "slash_commands"),
    ],
)
def test_unclean_session_aborts(changes, detail):
    e, stream = failure([init(**changes), reply(), result()])
    assert e.kind is FailureKind.unsafe_session and detail in e.detail and stream.yielded == 1


def test_allowed_tools_and_servers_pass():
    assert init_problems({**CLEAN_INIT, "tools": ["mcp__pa__get_tasks"], "mcp_servers": [{"name": "pa"}]}, ("mcp__pa__get_tasks",), ("pa",)) == []


def test_reply_without_init_aborts():
    e, _ = failure([reply(), result()])
    assert e.kind is FailureKind.unsafe_session


# --- Usage and error classification (FR-24) -------------------------------------------------------


@pytest.mark.parametrize(
    "event,kind",
    [(limit("rejected", "five_hour"), FailureKind.usage_unavailable), (limit("rejected", "seven_day"), FailureKind.usage_unavailable), (limit("allowed", "overage"), FailureKind.usage_unavailable)],
)
def test_limit_hit_or_overage_stops_at_once(event, kind):
    e, stream = failure([init(), event, reply(), result()])
    assert e.kind is kind and stream.yielded == 2


@pytest.mark.parametrize(
    "error,kind",
    [
        ("rate_limit", FailureKind.usage_unavailable),
        ("billing_error", FailureKind.usage_unavailable),
        ("authentication_failed", FailureKind.auth_unavailable),
        ("server_error", FailureKind.api_error),
        ("invalid_request", FailureKind.api_error),
        ("unknown", FailureKind.api_error),
    ],
)
def test_assistant_error_then_error_result(error, kind):
    e, _ = failure([init(), reply("API Error", error=error), result("API Error", is_error=True)])
    assert e.kind is kind


@pytest.mark.parametrize(
    "status,subtype,kind",
    [
        (429, "success", FailureKind.usage_unavailable),
        (402, "success", FailureKind.usage_unavailable),
        (401, "success", FailureKind.auth_unavailable),
        (403, "success", FailureKind.auth_unavailable),
        (529, "success", FailureKind.api_error),
        (500, "success", FailureKind.api_error),
        (None, "error_max_turns", FailureKind.step_limit),
        (None, "error_during_execution", FailureKind.sdk_error),
    ],
)
def test_error_result_message(status, subtype, kind):
    e, _ = failure([init(), result("x", is_error=True, subtype=subtype, status=status)])
    assert e.kind is kind


@pytest.mark.parametrize(
    "exc,kind",
    [
        (ResultError("failed", {"subtype": "success", "api_error_status": 429}), FailureKind.usage_unavailable),
        (ResultError("failed", {"subtype": "error_max_turns"}), FailureKind.step_limit),
        (ResultError("failed", {"subtype": "success", "api_error_status": 401}), FailureKind.auth_unavailable),
        (CLINotFoundError("no cli"), FailureKind.cli_missing),
        (ProcessError("boom", exit_code=1), FailureKind.sdk_error),
    ],
)
def test_sdk_exceptions_are_classified(exc, kind):
    e, _ = failure([init()], raise_after=exc)
    assert e.kind is kind
    assert classify_exception(exc) is kind


def test_process_error_after_usage_error_reply_is_usage():
    e, _ = failure([init(), reply("limit", error="rate_limit")], raise_after=ProcessError("exit", exit_code=1))
    assert e.kind is FailureKind.usage_unavailable


def test_stream_ending_without_result_fails():
    e, _ = failure([init(), reply()])
    assert e.kind is FailureKind.sdk_error


def test_failure_message_has_codes_not_text():
    e, _ = failure([init(), reply("secret-ish model text", error="rate_limit"), result("secret-ish model text", is_error=True)])
    assert "secret-ish" not in str(e)


def test_classify_result_prefers_assistant_error():
    assert classify_result("success", 500, "rate_limit") is FailureKind.usage_unavailable


# --- Login status for doctor (no model call) -----------------------------------------------------


def fake_run(payload, returncode=0):
    def _run(cmd, **kwargs):
        assert cmd[1:] == ["auth", "status", "--json"] and Path(cmd[0]) == runner.bundled_cli()
        assert not set(BILLING_VARS) & set(kwargs["env"])
        return subprocess.CompletedProcess(cmd, returncode, stdout=payload if isinstance(payload, str) else json.dumps(payload), stderr="")

    return _run


SUBSCRIPTION = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "pro", "email": "fake@example.com", "orgName": "Fake Org"}


@pytest.mark.parametrize(
    "payload,ok,text",
    [
        (SUBSCRIPTION, True, "subscription login (pro)"),
        ({**SUBSCRIPTION, "loggedIn": False}, False, "not logged in"),
        ({**SUBSCRIPTION, "authMethod": "api_key"}, False, "not a Claude subscription"),
        ({**SUBSCRIPTION, "apiProvider": "bedrock"}, False, "not a Claude subscription"),
        ("not json", False, "couldn't read"),
        ([], False, "not logged in"),
    ],
)
def test_login_status(payload, ok, text, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")  # never passed to the CLI (fake_run checks)
    status = runner.check_login(run=fake_run(payload))
    assert status.ok is ok and text in status.summary
    assert "fake@example.com" not in status.summary and "Fake Org" not in status.summary


def test_login_status_cli_missing():
    def broken(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    assert not runner.check_login(run=broken).ok


# --- doctor and smoke CLI --------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    config.ensure_data_dir(d)
    (d / config.PREFERENCES_FILE).write_text((EXAMPLES / "preferences.yaml").read_text())
    monkeypatch.setattr(config, "default_data_dir", lambda: d)
    return d


def test_doctor_shows_subscription_login(data_dir):
    out = CliRunner().invoke(cli.app, ["doctor"])
    assert "OK    Claude: subscription login (pro)" in out.output


def test_doctor_fails_without_subscription_login(data_dir, monkeypatch):
    monkeypatch.setattr(runner, "login_status", lambda: runner.LoginStatus(False, "not logged in"))
    out = CliRunner().invoke(cli.app, ["doctor"])
    assert out.exit_code == 1 and "FAIL  Claude: not logged in" in out.output


def fake_session(messages):
    async def _run(prompt, options, query_fn=None):
        return await run_session(prompt, options, query_fn=lambda **kw: FakeStream(messages))  # the real one, fake stream

    return _run


def test_smoke_ok(data_dir, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.setattr(runner, "run_session", fake_session([init(), reply(), result()]))
    out = CliRunner().invoke(cli.app, ["smoke"])
    assert out.exit_code == 0, out.output
    assert "'PA-SMOKE-OK' (as expected)" in out.output and "apiKeySource='none'" in out.output
    assert "Saved transcript: none found" in out.output
    log = "\n".join(f.read_text() for f in (data_dir / "logs").iterdir())
    assert '"event": "session_ok"' in log and "PA-SMOKE-OK" not in log


def test_smoke_flags_a_saved_transcript(data_dir, monkeypatch, tmp_path):
    projects = tmp_path / "claude-config" / "projects" / "-fake-cwd"
    projects.mkdir(parents=True)
    (projects / "s-1.jsonl").write_text("{}")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.setattr(runner, "run_session", fake_session([init(), reply(), result()]))
    out = CliRunner().invoke(cli.app, ["smoke"])
    assert out.exit_code == 1 and "Saved transcript: YES" in out.output


def test_smoke_usage_failure(data_dir, monkeypatch):
    monkeypatch.setattr(runner, "run_session", fake_session([init(), limit("rejected")]))
    out = CliRunner().invoke(cli.app, ["smoke"])
    assert out.exit_code == 1 and "usage_unavailable" in out.output
    log = "\n".join(f.read_text() for f in (data_dir / "logs").iterdir())
    assert '"status": "usage_unavailable"' in log


def test_smoke_refuses_with_billing_variable(data_dir, monkeypatch):
    called = []
    monkeypatch.setattr(runner, "run_session", lambda *a, **k: called.append(1))
    out = CliRunner().invoke(cli.app, ["smoke"], env={"ANTHROPIC_API_KEY": "x"})
    assert out.exit_code == 1 and "ANTHROPIC_API_KEY" in out.output and not called


def test_backstop_strips_billing_variables_even_if_the_check_were_skipped(monkeypatch):
    import os

    monkeypatch.setattr(runner, "check_billing", lambda env: None)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://fake.example.com")
    seen = {}

    def query(**kw):
        seen["env"] = dict(os.environ)  # what the CLI subprocess would inherit
        return FakeStream([init(), reply(), result()])

    asyncio.run(run_session("hi", build_options(system_prompt="x", max_turns=1, cwd=Path("/tmp")), query_fn=query))
    assert not set(BILLING_VARS) & set(seen["env"])




def test_only_the_shadowed_can_use_tool_warning_is_hidden():
    # A fresh interpreter: pytest resets warning filters for each test.
    code = (
        "import warnings, claude_agent_sdk, assistant.agent.runner\n"
        "warnings.warn('shadowed-msg', claude_agent_sdk.CanUseToolShadowedWarning)\n"
        "warnings.warn('other-msg', UserWarning)\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert "shadowed-msg" not in out.stderr and "other-msg" in out.stderr
