"""Agent SDK sessions on the Claude subscription only (S-5–S-9, S-16, FR-24).

M7: one session with no tools. Every session:
- uses the bundled Claude Code CLI, never one found on PATH
- has every built-in tool off (`tools=[]`) and disallowed by name, no MCP
  servers but ours, no plugins (the CLI's built-in ones are turned off), no
  skills or slash commands, and `setting_sources=[]` (no user settings or
  CLAUDE.md files)
- isn't saved to disk (`--no-session-persistence`, S-16)
- runs with billing variables removed from this process's environment
- aborts if the init message reports an API key instead of a subscription
  login, or if the session isn't clean
- stops as soon as a usage limit is hit or overage (pay-as-you-go) would be used
"""

import json
import os
import subprocess
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import claude_agent_sdk
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    CLINotFoundError,
    RateLimitEvent,
    ResultError,
    ResultMessage,
    SystemMessage,
    TextBlock,
)

from assistant.guard import BILLING_VARS, check_billing

# Built-in tools, disallowed by name as well as switched off with tools=[] (S-5).
DISALLOWED_BUILTINS = (
    "Agent",
    "Bash",
    "BashOutput",
    "Edit",
    "ExitPlanMode",
    "Glob",
    "Grep",
    "KillShell",
    "ListMcpResourcesTool",
    "MultiEdit",
    "NotebookEdit",
    "Read",
    "ReadMcpResourceTool",
    "Skill",
    "SlashCommand",
    "Task",
    "TodoWrite",
    "WebFetch",
    "WebSearch",
    "Write",
)

# Plugins built into the Claude Code CLI (init path "builtin"). Turned off in every session;
# a new one in a later CLI version fails the init check until it's added here.
BUILTIN_PLUGINS = ("cc-plugin-agents-md", "cc-plugin-plugin-authoring", "cc-plugin-telemetry")

# Agent types built into the CLI. They can't be removed, but they're only reachable
# through the Agent/Task tool, which is off and disallowed. Any other agent fails.
BUILTIN_AGENTS = ("Explore", "Plan", "claude", "general-purpose", "statusline-setup")

# One model for every flow (user decision 2026-10-16: Sonnet uses less of the Pro limits).
# No fallback model exists (S-8). Change here if evals show a quality problem.
MODEL = "claude-sonnet-5-5"

SUBSCRIPTION_KEY_SOURCE = "none"  # init.apiKeySource when no API key is in use (claude.ai login)


def bundled_cli() -> Path:
    """The Claude Code CLI shipped inside claude-agent-sdk."""
    return Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"


# --- Failure kinds (FR-24, ARCHITECTURE §8) -----------------------------------------------


class FailureKind(StrEnum):
    usage_unavailable = "usage_unavailable"  # limit hit, or overage would be used (S-8)
    auth_unavailable = "auth_unavailable"  # not logged in / login expired
    api_key_auth = "api_key_auth"  # session would bill an API key (S-7)
    unsafe_session = "unsafe_session"  # built-in tools, plugins or settings showed up (S-5)
    step_limit = "step_limit"  # max_turns reached
    malformed_output = "malformed_output"  # proposal still invalid after one retry (FR-25)
    no_proposal = "no_proposal"  # the session ended without a submit_* call
    api_error = "api_error"  # other API failure (overloaded, server error, bad request)
    cli_missing = "cli_missing"
    sdk_error = "sdk_error"


class AgentFailure(Exception):
    """A session failed. `kind` is safe to log; `detail` holds codes and names only, never text."""

    def __init__(self, kind: FailureKind, detail: str = ""):
        self.kind = kind
        self.detail = detail
        super().__init__(f"{kind.value}{': ' + detail if detail else ''}")


_ASSISTANT_ERRORS = {
    "billing_error": FailureKind.usage_unavailable,
    "rate_limit": FailureKind.usage_unavailable,
    "authentication_failed": FailureKind.auth_unavailable,
}

_HTTP_STATUS = {
    401: FailureKind.auth_unavailable,
    402: FailureKind.usage_unavailable,
    403: FailureKind.auth_unavailable,
    429: FailureKind.usage_unavailable,
}


def classify_result(subtype: str | None, api_error_status: int | None, assistant_error: str | None = None) -> FailureKind:
    """Map an error result (or ResultError) to a failure kind."""
    if assistant_error in _ASSISTANT_ERRORS:
        return _ASSISTANT_ERRORS[assistant_error]
    if api_error_status in _HTTP_STATUS:
        return _HTTP_STATUS[api_error_status]
    if subtype == "error_max_turns":
        return FailureKind.step_limit
    if api_error_status is not None or assistant_error is not None:
        return FailureKind.api_error
    return FailureKind.sdk_error


def classify_exception(exc: BaseException, assistant_error: str | None = None) -> FailureKind:
    if isinstance(exc, AgentFailure):
        return exc.kind
    if isinstance(exc, ResultError):
        return classify_result(exc.subtype, exc.api_error_status, assistant_error)
    if isinstance(exc, CLINotFoundError):
        return FailureKind.cli_missing
    if assistant_error is not None:
        return classify_result(None, None, assistant_error)
    return FailureKind.sdk_error


def rate_limit_failure(info: Any) -> AgentFailure | None:
    """Stop when a limit is hit or the overage window is in play (zero additional cost)."""
    if getattr(info, "status", None) == "rejected":
        return AgentFailure(FailureKind.usage_unavailable, f"rate_limit_rejected:{getattr(info, 'rate_limit_type', None) or 'unknown'}")
    if getattr(info, "rate_limit_type", None) == "overage":
        return AgentFailure(FailureKind.usage_unavailable, "overage")
    return None


# --- Session checks (S-5, S-7, safety test 9) ------------------------------------------------


def init_problems(init: Mapping[str, Any], allowed_tools: tuple[str, ...] = (), mcp_servers: tuple[str, ...] = ()) -> list[tuple[FailureKind, str]]:
    """What's wrong with a session's init message. Names and codes only."""
    problems: list[tuple[FailureKind, str]] = []
    key_source = init.get("apiKeySource")
    if key_source != SUBSCRIPTION_KEY_SOURCE:
        problems.append((FailureKind.api_key_auth, "api_key_source"))
    extra_tools = sorted(set(init.get("tools") or []) - set(allowed_tools))
    if extra_tools:
        problems.append((FailureKind.unsafe_session, "tools:" + ",".join(extra_tools)))
    servers = sorted({s.get("name", "?") for s in init.get("mcp_servers") or []} - set(mcp_servers))
    if servers:
        problems.append((FailureKind.unsafe_session, "mcp_servers:" + ",".join(servers)))
    if init.get("plugins"):
        problems.append((FailureKind.unsafe_session, "plugins"))
    unknown_agents = sorted(set(init.get("agents") or []) - set(BUILTIN_AGENTS))
    if unknown_agents:
        problems.append((FailureKind.unsafe_session, "agents:" + ",".join(unknown_agents)))
    if init.get("skills"):
        problems.append((FailureKind.unsafe_session, "skills"))
    if init.get("slash_commands"):
        problems.append((FailureKind.unsafe_session, "slash_commands"))
    return problems


# --- Options ----------------------------------------------------------------------------------


def build_options(
    *,
    system_prompt: str,
    max_turns: int,
    cwd: Path,
    allowed_tools: tuple[str, ...] = (),
    mcp_servers: dict[str, Any] | None = None,
    gate: Any = None,
) -> ClaudeAgentOptions:
    """Locked-down options. `gate` (a PermissionGate) adds the deny-by-default hook and callback."""
    return ClaudeAgentOptions(
        cli_path=bundled_cli(),
        system_prompt=system_prompt,
        tools=[],
        allowed_tools=list(allowed_tools),
        disallowed_tools=list(DISALLOWED_BUILTINS),
        mcp_servers=mcp_servers or {},
        strict_mcp_config=True,
        setting_sources=[],
        plugins=[],
        skills=[],  # hide every skill, including the CLI's bundled ones
        settings=json.dumps({"enabledPlugins": {f"{name}@builtin": False for name in BUILTIN_PLUGINS}}),
        model=MODEL,
        max_turns=max_turns,
        cwd=cwd,
        extra_args={"no-session-persistence": None, "disable-slash-commands": None},
        hooks=gate.hooks() if gate else None,
        can_use_tool=gate.can_use_tool if gate else None,
    )


def clean_process_env(env: dict[str, str] | None = None) -> None:
    """Remove billing variables from this process, which the SDK's CLI inherits.

    The SDK copies the parent environment and `options.env` can only add keys,
    so stripping has to happen here (backstop; the guard already refuses).
    """
    env = os.environ if env is None else env
    for name in BILLING_VARS:
        env.pop(name, None)


# --- Running a session ------------------------------------------------------------------------


@dataclass
class SessionResult:
    text: str
    init: dict[str, Any]
    num_turns: int = 0
    duration_ms: int = 0
    session_id: str = ""
    rate_limits: list[str] = field(default_factory=list)  # statuses seen, e.g. "allowed"


QueryFn = Callable[..., AsyncIterator[Any]]


class _Turn:
    """Checks one turn's messages. `init` carries over between turns of a conversation."""

    def __init__(self, options: ClaudeAgentOptions, init: dict[str, Any] | None = None):
        self.allowed = tuple(options.allowed_tools)
        self.servers = tuple(options.mcp_servers) if isinstance(options.mcp_servers, dict) else ()
        self.init = init
        self.texts: list[str] = []
        self.assistant_error: str | None = None
        self.rate_limits: list[str] = []

    def handle(self, message: Any) -> SessionResult | None:
        """Raises AgentFailure on anything unsafe or failed; returns the result at the end of the turn."""
        if isinstance(message, SystemMessage) and message.subtype == "init":
            self.init = dict(message.data)
            problems = init_problems(self.init, self.allowed, self.servers)
            if problems:
                raise AgentFailure(problems[0][0], ";".join(p[1] for p in problems))
        elif isinstance(message, RateLimitEvent):
            self.rate_limits.append(str(getattr(message.rate_limit_info, "status", "")))
            failure = rate_limit_failure(message.rate_limit_info)
            if failure:
                raise failure
        elif isinstance(message, AssistantMessage):
            if self.init is None:
                raise AgentFailure(FailureKind.unsafe_session, "no_init_before_reply")
            if message.error:
                self.assistant_error = message.error
            self.texts.extend(block.text for block in message.content if isinstance(block, TextBlock))
        elif isinstance(message, ResultMessage):
            if message.is_error:
                raise AgentFailure(classify_result(message.subtype, message.api_error_status, self.assistant_error), message.subtype)
            if self.init is None:
                raise AgentFailure(FailureKind.unsafe_session, "no_init")
            return SessionResult(
                text=(message.result if message.result is not None else "".join(self.texts)).strip(),
                init=self.init,
                num_turns=message.num_turns,
                duration_ms=message.duration_ms,
                session_id=message.session_id,
                rate_limits=self.rate_limits,
            )
        return None

    async def consume(self, stream: AsyncIterator[Any]) -> SessionResult:
        try:
            async for message in stream:
                result = self.handle(message)
                if result is not None:
                    return result
        except AgentFailure:
            raise
        except ClaudeSDKError as e:
            raise AgentFailure(classify_exception(e, self.assistant_error), e.__class__.__name__) from None
        raise AgentFailure(classify_result(None, None, self.assistant_error), "no_result")


def _prepare() -> None:
    check_billing(os.environ)
    clean_process_env()


async def run_session(prompt: str, options: ClaudeAgentOptions, query_fn: QueryFn | None = None) -> SessionResult:
    """One single-turn session. Raises AgentFailure; never retries, never falls back (S-8)."""
    _prepare()
    query_fn = query_fn or claude_agent_sdk.query
    stream = query_fn(prompt=prompt, options=options)
    try:
        return await _Turn(options).consume(stream)
    finally:
        aclose = getattr(stream, "aclose", None)
        if aclose:
            await aclose()


class Conversation:
    """A multi-turn session (e.g. a clarifying question and its answer, ARCHITECTURE §6.2).

    Same checks as run_session on every turn. Use as `async with Conversation(options) as c:`.
    """

    def __init__(self, options: ClaudeAgentOptions, client_factory: Callable[..., Any] | None = None):
        self.options = options
        self.client_factory = client_factory or claude_agent_sdk.ClaudeSDKClient
        self.client: Any = None
        self.init: dict[str, Any] | None = None

    async def __aenter__(self) -> "Conversation":
        _prepare()
        self.client = self.client_factory(options=self.options)
        try:
            await self.client.connect()
        except ClaudeSDKError as e:
            raise AgentFailure(classify_exception(e), e.__class__.__name__) from None
        return self

    async def send(self, prompt: str) -> SessionResult:
        turn = _Turn(self.options, self.init)
        try:
            await self.client.query(prompt)
        except ClaudeSDKError as e:
            raise AgentFailure(classify_exception(e), e.__class__.__name__) from None
        result = await turn.consume(self.client.receive_response())
        self.init = turn.init
        return result

    async def __aexit__(self, *exc: Any) -> None:
        if self.client is not None:
            await self.client.disconnect()


# --- Login check for doctor (no model call) --------------------------------------------------------


@dataclass
class LoginStatus:
    ok: bool
    summary: str  # safe to print: method and plan, never email or org


def login_status() -> LoginStatus:
    """The Claude login as `doctor` shows it."""
    return check_login()


def check_login(run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> LoginStatus:
    """`claude auth status --json` from the bundled CLI. Local only; uses no usage."""
    env = dict(os.environ)
    clean_process_env(env)
    try:
        proc = run([str(bundled_cli()), "auth", "status", "--json"], capture_output=True, text=True, timeout=30, env=env)
        data = json.loads(proc.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return LoginStatus(False, "couldn't read the Claude login status")
    if not isinstance(data, dict) or not data.get("loggedIn"):
        return LoginStatus(False, "not logged in; run `claude` and log in with your subscription")
    method, provider, plan = data.get("authMethod"), data.get("apiProvider"), data.get("subscriptionType")
    if method != "claude.ai" or provider != "firstParty":
        return LoginStatus(False, f"logged in with {method or 'unknown'} via {provider or 'unknown'}, not a Claude subscription")
    return LoginStatus(True, f"subscription login ({plan or 'plan unknown'})")
