"""Real Claude sessions on the subscription. Skipped unless PA_LIVE=1 (uses a little usage).

Fake fixture data only. Run with: PA_LIVE=1 uv run pytest tests/test_live.py -v -s
"""

import asyncio
import os
from pathlib import Path

import pytest

from assistant.agent import runner
from assistant.agent.permissions import PermissionGate
from assistant.agent.prompts import system_prompt
from assistant.agent.tools import Observed, allowlist, build_server, calendar_events, qualified
from tests.test_tools import snapshot

pytestmark = pytest.mark.skipif(os.environ.get("PA_LIVE") != "1", reason="live Claude session; set PA_LIVE=1")


def test_live_bash_is_unavailable_and_dates_are_clamped(tmp_path):
    snap, seen = snapshot(), Observed()
    gate = PermissionGate("ask", allowlist("ask"), snap.today)
    servers, names = build_server("ask", snap, seen)
    options = runner.build_options(
        system_prompt=system_prompt("ask", snap.now, snap.tz, "Answer briefly. When asked for numbers, reply with just the numbers."),
        max_turns=6,
        cwd=tmp_path,
        allowed_tools=names,
        mcp_servers=servers,
        gate=gate,
    )
    prompt = (
        "First, use the Bash tool to run `echo hello`. "
        "Then get my calendar events from 2026-10-01 to 2026-12-31 and my open tasks. "
        "Reply in the form: events=<number>, tasks=<number>"
    )
    result = asyncio.run(runner.run_session(prompt, options))
    expected_events = len(calendar_events(snap, Observed(), "2026-10-28", "2026-11-04")["events"])
    print("\nreply:", repr(result.text))
    print("tool calls:", gate.calls)
    print("denied:", gate.denied)
    print("init tools:", result.init.get("tools"))

    assert "Bash" not in result.init.get("tools", []) and set(result.init["tools"]) == set(names)
    assert "Bash" not in gate.calls or "Bash" in gate.denied  # never ran
    assert qualified("get_calendar_events") in gate.calls and qualified("get_tasks") in gate.calls
    assert f"events={expected_events}" in result.text.replace(" ", "") and "tasks=13" in result.text.replace(" ", "")
    assert not any(Path.home().joinpath(".claude", "projects").rglob(f"*{result.session_id}*"))
