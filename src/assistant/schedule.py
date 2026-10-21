"""The launchd LaunchAgent that starts `assistant run scheduled` at the brief time (FR-12, ARCHITECTURE §1).

`schedule install` renders `launchd/com.assistant.daily.plist.template` from
`morning_brief.time`, writes it to ~/Library/LaunchAgents and (re)loads it with
`launchctl`. `doctor` compares the installed plist with the preferences.

The job runs the project's own `.venv/bin/assistant`, so it needs neither `uv`
nor a shell, and gets a fixed PATH and no other environment (S-7: no billing
variables). Its stdout (the brief) is discarded: the brief is saved in
`briefs/` anyway. Its stderr goes to `data/logs/launchd.err`, which `install`
creates as 600 itself (launchd would create it as 644); `Umask` 077 covers
everything the job writes.
"""

import os
import plistlib
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from string import Template
from xml.sax.saxutils import escape

LABEL = "com.assistant.daily"
REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / "launchd" / f"{LABEL}.plist.template"
PROGRAM = REPO / ".venv" / "bin" / "assistant"
STDERR_FILE = Path("logs") / "launchd.err"  # under data/
TIMEOUT = 30  # seconds, for launchctl


def plist_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def domain() -> str:
    return f"gui/{os.getuid()}"


def render(brief_time: time, data_dir: Path) -> bytes:
    """The plist text for this brief time. Values are XML-escaped; the result must parse as a plist."""
    values = {
        "label": LABEL,
        "program": str(PROGRAM),
        "repo": str(REPO),
        "hour": str(brief_time.hour),
        "minute": str(brief_time.minute),
        "stderr": str(data_dir / STDERR_FILE),
    }
    text = Template(TEMPLATE.read_text()).substitute({k: escape(v) for k, v in values.items()})
    data = text.encode()
    plistlib.loads(data)  # refuse to install anything launchd couldn't read
    return data


@dataclass(frozen=True)
class Installed:
    hour: int
    minute: int
    program: str


def installed(home: Path | None = None) -> Installed | None:
    """The installed job's time and program, or None if there's no plist. Raises on an unreadable one."""
    path = plist_path(home)
    if not path.exists():
        return None
    with path.open("rb") as f:
        plist = plistlib.load(f)
    when = plist["StartCalendarInterval"]
    return Installed(int(when["Hour"]), int(when["Minute"]), plist["ProgramArguments"][0])


def mismatch(current: Installed, brief_time: time) -> list[str]:
    """What differs between the installed job and what `schedule install` would write now."""
    problems = []
    if (current.hour, current.minute) != (brief_time.hour, brief_time.minute):
        problems.append(f"it runs at {current.hour:02d}:{current.minute:02d} but morning_brief.time is {brief_time:%H:%M}")
    if current.program != str(PROGRAM):
        problems.append(f"it starts {current.program}, not this project's {PROGRAM}")
    return problems


# --- launchctl ------------------------------------------------------------------------------------------


def run_launchctl(args: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True, timeout=TIMEOUT, check=False)


class ScheduleError(Exception):
    pass


def install(brief_time: time, data_dir: Path, home: Path | None = None, launchctl: Callable | None = None) -> Path:
    """Write the plist (644, as launchd expects) and (re)load it. Returns the plist path."""
    launchctl = launchctl or (lambda args: run_launchctl(args))
    if not PROGRAM.exists():
        raise ScheduleError(f"{PROGRAM} doesn't exist; run `uv sync` in {REPO} first")
    data = render(brief_time, data_dir)
    # launchd opens StandardErrorPath before applying the job's Umask, so it would create it as 644.
    # Create it first as 600 (S-10); launchd then appends to it and keeps the mode.
    stderr = data_dir / STDERR_FILE
    stderr.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    stderr.touch(mode=0o600, exist_ok=True)
    stderr.chmod(0o600)
    path = plist_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".plist.tmp")
    tmp.write_bytes(data)
    tmp.chmod(0o644)
    os.replace(tmp, path)
    launchctl(["bootout", f"{domain()}/{LABEL}"])  # not loaded yet is fine
    out = launchctl(["bootstrap", domain(), str(path)])
    if out.returncode != 0:
        raise ScheduleError(f"launchctl bootstrap failed (exit {out.returncode}): {(out.stderr or '').strip()}")
    return path


def uninstall(home: Path | None = None, launchctl: Callable | None = None) -> bool:
    """Unload the job and remove its plist. False if nothing was installed."""
    launchctl = launchctl or (lambda args: run_launchctl(args))
    path = plist_path(home)
    launchctl(["bootout", f"{domain()}/{LABEL}"])
    if not path.exists():
        return False
    path.unlink()
    return True


def loaded(launchctl: Callable | None = None) -> bool:
    launchctl = launchctl or (lambda args: run_launchctl(args))
    return launchctl(["print", f"{domain()}/{LABEL}"]).returncode == 0
