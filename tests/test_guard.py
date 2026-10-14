import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from assistant import config
from assistant.cli import app
from assistant.guard import LOCK_FILE, RunLockedError, run_lock

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

# Tries to take the lock in a separate process; exit code 0 = got it, 3 = locked.
TRY_LOCK = """
import sys
from pathlib import Path
from assistant.guard import RunLockedError, run_lock
try:
    with run_lock(Path(sys.argv[1])):
        pass
except RunLockedError:
    sys.exit(3)
"""


def other_process_tries(data_dir: Path) -> int:
    return subprocess.run([sys.executable, "-c", TRY_LOCK, str(data_dir)], timeout=30).returncode


def test_second_process_cant_take_the_lock(tmp_path):
    with run_lock(tmp_path):
        assert other_process_tries(tmp_path) == 3
    assert other_process_tries(tmp_path) == 0  # released


def test_second_lock_in_same_process_fails(tmp_path):
    with run_lock(tmp_path):
        with pytest.raises(RunLockedError, match="another run is in progress"):
            with run_lock(tmp_path):
                pass


def test_lock_released_on_exception(tmp_path):
    with pytest.raises(ZeroDivisionError):
        with run_lock(tmp_path):
            1 / 0
    with run_lock(tmp_path):
        pass


def test_lock_released_when_holder_dies(tmp_path):
    holder = subprocess.Popen(
        [sys.executable, "-c", "import sys, time; from pathlib import Path; from assistant.guard import run_lock\n"
         "with run_lock(Path(sys.argv[1])):\n    print('locked', flush=True); time.sleep(60)", str(tmp_path)],
        stdout=subprocess.PIPE, text=True,
    )  # fmt: skip
    try:
        assert holder.stdout.readline().strip() == "locked"
        with pytest.raises(RunLockedError):
            with run_lock(tmp_path):
                pass
    finally:
        holder.kill()
        holder.wait()
    with run_lock(tmp_path):  # no stale lock after a crash
        pass


def test_lock_file_is_600(tmp_path):
    with run_lock(tmp_path):
        pass
    assert stat.S_IMODE((tmp_path / LOCK_FILE).stat().st_mode) == 0o600


def test_task_write_refused_while_locked(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    config.ensure_data_dir(data_dir)
    shutil.copy(EXAMPLES / "preferences.yaml", data_dir / "preferences.yaml")
    shutil.copy(EXAMPLES / "tasks.json", data_dir / "tasks.json")
    monkeypatch.setattr(config, "default_data_dir", lambda: data_dir)
    before = (data_dir / "tasks.json").read_bytes()
    with run_lock(data_dir):
        result = CliRunner().invoke(app, ["tasks", "done", "t-0001"])
        assert CliRunner().invoke(app, ["tasks", "list"]).exit_code == 0  # reads don't need the lock
    assert result.exit_code == 1 and "another run is in progress" in result.output
    assert (data_dir / "tasks.json").read_bytes() == before
    assert '"error_code": "RunLockedError"' in "".join(p.read_text() for p in (data_dir / "logs").glob("*.jsonl"))
