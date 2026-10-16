"""Deny-by-default tool permissions (ARCHITECTURE §7, SECURITY §4, S-5, safety test 4).

Two layers, same rule: a `PreToolUse` hook sees every tool call, and
`can_use_tool` answers any permission prompt the CLI would otherwise show.
A tool that isn't on this flow's allowlist is denied and logged. Arguments are
checked too: date ranges are clamped to today … today + 7, and malformed or
out-of-window dates are denied with a reason Claude can act on.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from claude_agent_sdk import HookMatcher, PermissionResultAllow, PermissionResultDeny

from assistant.integrations.google_calendar import WINDOW_DAYS, clamp_range
from assistant.logging_setup import RunLog


@dataclass
class Decision:
    allow: bool
    reason: str = ""
    updated_input: dict[str, Any] | None = None


@dataclass
class PermissionGate:
    flow: str
    allowed: frozenset[str]
    today: date
    log: RunLog | None = None
    denied: list[str] = field(default_factory=list)  # tool names, for tests and the run summary
    calls: list[str] = field(default_factory=list)  # every tool name seen

    # --- the rule ----------------------------------------------------------------------------------

    def decide(self, tool_name: str, tool_input: dict[str, Any] | None) -> Decision:
        self.calls.append(tool_name)
        if tool_name not in self.allowed:
            return self._deny(tool_name, "not_allowed", f"{tool_name} is not available in this flow. Use only the pa tools you were given.")
        args = dict(tool_input or {})
        short = tool_name.rsplit("__", 1)[-1]
        if short == "get_calendar_events":
            return self._check_range(tool_name, args)
        if short == "find_free_time":
            return self._check_day(tool_name, args)
        if short == "get_tasks" and "completed_since" in args and not _is_date(args["completed_since"]):
            return self._deny(tool_name, "bad_arguments", "completed_since must be a date like 2026-10-28.")
        self._log("tool_allowed", tool_name)
        return Decision(True)

    def _check_range(self, tool_name: str, args: dict[str, Any]) -> Decision:
        start, end = _as_date(args.get("start_date")), _as_date(args.get("end_date"))
        if start is None or end is None or end < start:
            return self._deny(tool_name, "bad_arguments", "start_date and end_date must be dates like 2026-10-28, with end_date not before start_date.")
        window = clamp_range(start, end, self.today)
        last = self.today + timedelta(days=WINDOW_DAYS)
        if window is None:
            return self._deny(tool_name, "out_of_window", f"Only {self.today} to {last} is available.")
        if window != (start, end):
            self._log("tool_clamped", tool_name)
            return Decision(True, updated_input={**args, "start_date": window[0].isoformat(), "end_date": window[1].isoformat()})
        self._log("tool_allowed", tool_name)
        return Decision(True)

    def _check_day(self, tool_name: str, args: dict[str, Any]) -> Decision:
        day = _as_date(args.get("date"))
        last = self.today + timedelta(days=WINDOW_DAYS)
        if day is None:
            return self._deny(tool_name, "bad_arguments", "date must be a date like 2026-10-28.")
        if not self.today <= day <= last:
            return self._deny(tool_name, "out_of_window", f"date must be between {self.today} and {last}.")
        self._log("tool_allowed", tool_name)
        return Decision(True)

    def _deny(self, tool_name: str, code: str, reason: str) -> Decision:
        self.denied.append(tool_name)
        self._log("tool_denied", tool_name, code)
        return Decision(False, reason)

    def _log(self, event: str, tool_name: str, code: str | None = None) -> None:
        if self.log:
            fields: dict[str, Any] = {"tool": tool_name, "flow": self.flow}
            if code:
                fields["error_code"] = code
            self.log.event(event, **fields)

    # --- SDK adapters ----------------------------------------------------------------------------------

    async def pre_tool_use(self, hook_input: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        d = self.decide(hook_input.get("tool_name", ""), hook_input.get("tool_input"))
        out: dict[str, Any] = {"hookEventName": "PreToolUse", "permissionDecision": "allow" if d.allow else "deny"}
        if d.reason:
            out["permissionDecisionReason"] = d.reason
        if d.updated_input is not None:
            out["updatedInput"] = d.updated_input
        return {"hookSpecificOutput": out}

    async def can_use_tool(self, tool_name: str, tool_input: dict[str, Any], context: Any):
        d = self.decide(tool_name, tool_input)
        if not d.allow:
            return PermissionResultDeny(message=d.reason)
        return PermissionResultAllow(updated_input=d.updated_input)

    def hooks(self) -> dict[str, list[HookMatcher]]:
        return {"PreToolUse": [HookMatcher(matcher=None, hooks=[self.pre_tool_use])]}


def _as_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _is_date(value: Any) -> bool:
    return _as_date(value) is not None
