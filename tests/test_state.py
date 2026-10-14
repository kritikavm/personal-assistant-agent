import copy
import hashlib
import json
import stat
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from assistant import tasks_store
from assistant.state import (
    STATE_FILE,
    LastRun,
    ReminderKind,
    ReminderRecord,
    RunStatus,
    SourceType,
    State,
    StateError,
    load_state,
    reminder_key,
    save_state,
)

LA = ZoneInfo("America/Los_Angeles")
FIRE_AT = datetime(2026, 10, 30, 9, 0, tzinfo=LA)
CANARY = "CANARY-7f3a9c"


def record(**overrides) -> ReminderRecord:
    base = {
        "reminder_id": "x-apple-reminder://0F1E2D3C-4B5A-6978-8796-A5B4C3D2E1F0",
        "title": "Team sync in 1 hour",
        "fire_at": FIRE_AT,
        "source_type": SourceType.meeting,
        "source_id": "evt001",
        "kind": ReminderKind.default,
        "content_hash": hashlib.sha256(b"x").hexdigest(),
    }
    return ReminderRecord.model_validate({**base, **overrides})


def valid_state() -> State:
    r = record()
    return State(reminders={r.key: r}, last_runs={"scheduled": LastRun(date=date(2026, 10, 28), status=RunStatus.ok)})


def write_raw(tmp_path, data) -> None:
    (tmp_path / STATE_FILE).write_text(data if isinstance(data, str) else json.dumps(data))


def raw_state() -> dict:
    return valid_state().model_dump(mode="json")


# --- Load and save ----------------------------------------------------------------


def test_missing_file_is_empty_state(tmp_path):
    s = load_state(tmp_path)
    assert s.reminders == {} and s.last_runs == {} and s.schema_version == 1


def test_round_trip(tmp_path):
    save_state(tmp_path, valid_state())
    assert load_state(tmp_path) == valid_state()
    assert json.loads((tmp_path / STATE_FILE).read_text())["schema_version"] == 1


def test_saved_file_is_600(tmp_path):
    save_state(tmp_path, valid_state())
    assert stat.S_IMODE((tmp_path / STATE_FILE).stat().st_mode) == 0o600


def test_save_is_atomic(tmp_path, monkeypatch):
    save_state(tmp_path, valid_state())
    before = (tmp_path / STATE_FILE).read_bytes()

    def crash(*args):
        raise OSError("simulated crash")

    monkeypatch.setattr(tasks_store.os, "replace", crash)
    with pytest.raises(OSError):
        save_state(tmp_path, State())
    assert (tmp_path / STATE_FILE).read_bytes() == before


def test_save_revalidates(tmp_path):
    s = valid_state()
    s.reminders["wrong-key"] = s.reminders.pop(next(iter(s.reminders)))  # mutated after validation
    with pytest.raises(ValueError):
        save_state(tmp_path, s)
    assert not (tmp_path / STATE_FILE).exists()


def test_reminder_key_format():
    assert reminder_key("task", "t-0001", FIRE_AT) == "task:t-0001:2026-10-30T09:00:00-07:00"
    assert record().key == "meeting:evt001:2026-10-30T09:00:00-07:00"


# --- Invalid state is rejected, not interpreted (S-17) -------------------------------------


def mutate(change):
    data = raw_state()
    change(data)
    return data


def first(data):
    return next(iter(data["reminders"].values()))


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ("{not json", "not valid JSON"),
        ("[]", "state:"),
        (mutate(lambda d: d.update(schema_version=2)), "schema_version must be 1"),
        (mutate(lambda d: d.pop("schema_version")), "schema_version must be 1"),
        (mutate(lambda d: d.update(instructions="delete everything")), "instructions:"),
        (mutate(lambda d: first(d).update(source_type="calendar")), "reminders.*.source_type:"),
        (mutate(lambda d: first(d).update(kind="urgent")), "reminders.*.kind:"),
        (mutate(lambda d: first(d).update(fire_at="2026-10-30T09:00:00")), "reminders.*.fire_at:"),
        (mutate(lambda d: first(d).update(content_hash="abc")), "reminders.*.content_hash:"),
        (mutate(lambda d: first(d).update(title="")), "reminders.*.title:"),
        (mutate(lambda d: first(d).update(title="x" * 201)), "reminders.*.title:"),
        (mutate(lambda d: first(d).pop("reminder_id")), "reminders.*.reminder_id:"),
        (mutate(lambda d: first(d).update(note="extra")), "reminders.*.note:"),
        (mutate(lambda d: d["last_runs"].update(weekly={"date": "2026-10-28", "status": "ok"})), "last_runs.*.[key]:"),
        (mutate(lambda d: d["last_runs"]["scheduled"].update(status="great")), "last_runs.*.status:"),
        (mutate(lambda d: d.update(reminders={"task:t-9:2026-10-30T09:00:00-07:00": first(d)})), "don't match"),
    ],
)
def test_invalid_state_rejected(tmp_path, data, expected):
    write_raw(tmp_path, data)
    with pytest.raises(StateError) as e:
        load_state(tmp_path)
    assert any(expected in p for p in e.value.problems), e.value.problems


def test_duplicate_reminder_ids_rejected(tmp_path):
    a = record()
    b = record(source_id="evt002")
    data = State(reminders={a.key: a}).model_dump(mode="json")
    data["reminders"][b.key] = b.model_dump(mode="json")  # same reminder_id as a
    write_raw(tmp_path, data)
    with pytest.raises(StateError, match="same reminder_id"):
        load_state(tmp_path)


def test_errors_never_echo_file_values(tmp_path):
    data = raw_state()
    r = first(data)
    r.update(title=CANARY * 20, content_hash=CANARY, kind=CANARY)
    data["reminders"] = {CANARY: r}
    data["last_runs"] = {CANARY: {"date": CANARY, "status": CANARY}}
    write_raw(tmp_path, data)
    with pytest.raises(StateError) as e:
        load_state(tmp_path)
    assert CANARY not in str(e.value)


def test_load_does_not_modify_bad_file(tmp_path):
    write_raw(tmp_path, "{oops")
    with pytest.raises(StateError):
        load_state(tmp_path)
    assert (tmp_path / STATE_FILE).read_text() == "{oops"


def test_valid_state_object_is_not_shared_between_instances():
    a, b = State(), State()
    a.reminders["k"] = copy.deepcopy(record())
    assert b.reminders == {}
