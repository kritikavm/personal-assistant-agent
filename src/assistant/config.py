"""Data folder and preferences loading (PRD §8, S-10)."""

import os
import stat
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from assistant.schemas import Preferences

DIR_MODE = 0o700
FILE_MODE = 0o600
SUBDIRS = ("secrets", "briefs", "logs", "backups")
PREFERENCES_FILE = "preferences.yaml"


def default_data_dir() -> Path:
    """`data/` at the repo root (src/assistant/config.py → repo)."""
    return Path(__file__).resolve().parents[2] / "data"


# --- Data folder -------------------------------------------------------------


def ensure_data_dir(data_dir: Path) -> list[Path]:
    """Create `data/` and its subfolders with mode 700 if missing.

    Existing folders are never changed; `permission_issues` reports them instead.
    Returns the folders that were created.
    """
    created = []
    for path in (data_dir, *(data_dir / name for name in SUBDIRS)):
        if not path.exists():
            path.mkdir(mode=DIR_MODE)
            os.chmod(path, DIR_MODE)  # mkdir's mode is reduced by the umask
            created.append(path)
    return created


@dataclass(frozen=True)
class PermissionIssue:
    path: Path
    mode: int
    expected: int

    def __str__(self) -> str:
        return f"{self.path} is {self.mode:o}, expected {self.expected:o} (run: chmod {self.expected:o} '{self.path}')"


def permission_issues(data_dir: Path) -> list[PermissionIssue]:
    """Folders and files under `data/` that group or others can access."""
    if not data_dir.exists():
        return []
    issues = []
    for path in (data_dir, *sorted(data_dir.rglob("*"))):
        if path.is_symlink():
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        expected = DIR_MODE if path.is_dir() else FILE_MODE
        if mode & 0o077:
            issues.append(PermissionIssue(path, mode, expected))
    return issues


def icloud_synced_folder(path: Path, home: Path | None = None) -> Path | None:
    """The iCloud-synced folder that contains `path`, or None.

    Covers iCloud Drive (~/Library/Mobile Documents) and Desktop & Documents
    sync, which shows up as Desktop/Documents links inside iCloud Drive.
    If the check itself can't see iCloud Drive, assume synced so `doctor` warns.
    """
    home = (home or Path.home()).resolve()
    path = path.resolve()
    icloud = home / "Library" / "Mobile Documents"
    if path.is_relative_to(icloud):
        return icloud
    for name in ("Desktop", "Documents"):
        folder = home / name
        if path.is_relative_to(folder):
            try:
                synced = os.path.lexists(icloud / "com~apple~CloudDocs" / name)
            except OSError:
                synced = True
            return folder if synced else None
    return None


# --- Preferences --------------------------------------------------------------


class PreferencesError(Exception):
    """Preferences are missing or invalid. Each message names the field."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def format_validation_error(err: ValidationError, root: str = "(file)") -> list[str]:
    """Turn pydantic errors into "field.path: message (got value)" lines.

    `root` names errors that aren't about one field (whole-object rules)."""
    lines = []
    for e in err.errors():
        field = ".".join(str(part) for part in e["loc"]) or root
        message = e["msg"].removeprefix("Value error, ")
        got = e.get("input")
        shown = "" if isinstance(got, (dict, list)) or e["type"] == "missing" else f" (got {got!r})"
        lines.append(f"{field}: {message}{shown}")
    return lines


def load_preferences(path: Path) -> Preferences:
    if not path.exists():
        raise PreferencesError([f"{path} not found; copy examples/{PREFERENCES_FILE} to data/ and edit it"])
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise PreferencesError([f"{path.name}: not valid YAML ({e.__class__.__name__})"]) from None
    if not isinstance(raw, dict):
        raise PreferencesError([f"{path.name}: expected a mapping of settings at the top level"])
    try:
        return Preferences.model_validate(raw)
    except ValidationError as e:
        raise PreferencesError(format_validation_error(e)) from None
