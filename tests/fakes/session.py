"""A scripted Claude for `run` sessions, and an in-process tool server it can call (Layer A, no AI)."""

import json
import re
from datetime import date, timedelta

import jsonschema
import pytest

from assistant.agent import tools as T
from assistant.agent.runner import SessionResult


@pytest.fixture(autouse=True)
def recording_server(monkeypatch):
    """Replace the in-process MCP server with one the fake session can call directly."""
    monkeypatch.setattr(T, "create_sdk_mcp_server", lambda name, tools: {"type": "sdk", "name": name, "tools": {t.name: t for t in tools}})


class Claude:
    """Plays Claude in a `run` session: the read calls, then each scripted submit_daily_plan.

    Every call goes through the permission hook, and arguments that break a tool's JSON schema are
    rejected before the handler, like the SDK. `fail` raises instead (usage, step limit, …).
    """

    def __init__(self, *submissions, reads=True, fail: Exception | None = None):
        self.submissions, self.reads, self.fail = submissions, reads, fail
        self.replies: list[dict] = []
        self.prompt = None

    async def __call__(self, prompt, options):
        self.prompt, self.options = prompt, options
        if self.fail:
            raise self.fail
        tools = options.mcp_servers["pa"]["tools"]
        hook = options.hooks["PreToolUse"][0].hooks[0]

        async def call(name, args):
            decision = (await hook({"tool_name": T.qualified(name), "tool_input": args}, "id", {}))["hookSpecificOutput"]
            assert decision["permissionDecision"] == "allow", name
            args = decision.get("updatedInput", args)
            try:
                jsonschema.validate(args, tools[name].input_schema)
            except jsonschema.ValidationError:
                return None
            out = json.loads((await tools[name].handler(args))["content"][0]["text"])
            self.replies.append(out)
            return out

        if self.reads:
            today = re.search(r"\d{4}-\d{2}-\d{2}", prompt)[0]
            end = (date.fromisoformat(today) + timedelta(days=7)).isoformat()
            for name, args in [
                ("get_preferences", {}),
                ("get_tasks", {}),
                ("get_calendar_events", {"start_date": today, "end_date": end}),
                ("get_default_reminders", {}),
                ("find_free_time", {"date": today}),
            ]:
                await call(name, args)
        for sub in self.submissions:
            await call("submit_daily_plan", sub)
        return SessionResult(text="Done.", init={}, num_turns=3)
