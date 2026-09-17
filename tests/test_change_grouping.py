import json
from copy import deepcopy

import pytest

import format as fmt


PARITY_NOTES = {
    "every": "",
    "upper": "\u043f\u043e \u0432\u0435\u0440\u0445\u043d\u0435\u0439 \u043d\u0435\u0434\u0435\u043b\u0435",
    "lower": "\u043f\u043e \u043d\u0438\u0436\u043d\u0435\u0439 \u043d\u0435\u0434\u0435\u043b\u0435",
}


def _lesson(time="09:00 10:00", parity="every", **overrides):
    return {
        "day": fmt.DAYS_ORDER[0],
        "time": time,
        "subject": "History",
        "subgroup": "1",
        "teacher": "Teacher A",
        "note": PARITY_NOTES[parity],
        "room": "101",
        "location": "Campus A",
        "delivery_mode": "in_person",
        **overrides,
    }


@pytest.mark.parametrize("reverse_added", [False, True])
def test_pair_moves_matches_parity_independently_of_added_order(reverse_added):
    removed = [_lesson(parity=parity) for parity in ("upper", "lower")]
    added = [
        _lesson("11:00 12:00", "upper"),
        _lesson("17:00 18:00", "lower"),
    ]
    expected = list(zip(removed, added))
    if reverse_added:
        added.reverse()
    original = deepcopy((removed, added))

    assert fmt._pair_moves(removed, added) == (expected, [], [])
    assert (removed, added) == original


def test_pair_moves_accepts_unambiguous_every_week_move():
    gone = _lesson()
    arrived = _lesson("11:00 12:00", room="202", location="Campus B")

    assert fmt._pair_moves([gone], [arrived]) == ([(gone, arrived)], [], [])


@pytest.mark.parametrize("old_time, new_time", [
    (None, "11:00 12:00"),
    ("09:00 10:00", None),
    ("", "11:00 12:00"),
    ("09:00 10:00", ""),
    ("25:99", "11:00 12:00"),
    ("09:00 10:00", "25:99"),
    (None, None),
    ("09:00 10:00", "09:00 10:00"),
    ("9:00 10:00", "10:00 09:00 09:00"),
])
def test_pair_moves_requires_real_distinct_academic_hours(old_time, new_time):
    removed = [_lesson(old_time)]
    added = [_lesson(new_time, room="202")]

    assert fmt._pair_moves(removed, added) == ([], removed, added)


@pytest.mark.parametrize("overrides", [
    {"day": fmt.DAYS_ORDER[1]},
    {"subject": "Geography"},
    {"subgroup": "2"},
    {"teacher": "Teacher B"},
    {"note": PARITY_NOTES["lower"]},
    {"note": PARITY_NOTES["every"]},
])
def test_pair_moves_preserves_unmatched_identity_day_or_parity(overrides):
    removed = [_lesson(parity="upper")]
    added = [_lesson("11:00 12:00", "upper", **overrides)]

    assert fmt._pair_moves(removed, added) == ([], removed, added)


@pytest.mark.parametrize("old_times, new_times", [
    (["09:00"], ["13:00", "15:00"]),
    (["09:00", "11:00"], ["13:00"]),
    (["09:00", "11:00"], ["13:00", "15:00"]),
    (["09:00", "09:00"], ["13:00", "13:00"]),
    (["09:00", "11:00"], ["09:00", "11:00"]),
])
@pytest.mark.parametrize("reverse_added", [False, True])
def test_pair_moves_does_not_guess_between_parallel_lessons(
    old_times, new_times, reverse_added,
):
    removed = [_lesson(time, "upper") for time in old_times]
    added = [_lesson(time, "upper") for time in new_times]
    if reverse_added:
        added.reverse()
    unique_old = _lesson(parity="lower")
    unique_new = _lesson("17:00 18:00", "lower")

    assert fmt._pair_moves(removed + [unique_old], added + [unique_new]) == (
        [(unique_old, unique_new)], removed, added,
    )


@pytest.mark.parametrize("old_time, new_time", [
    ("09:00 10:00", "17:00 18:00"),
    ("09:00 10:00", "11:00"),
    ("09:00", "11:00 12:00"),
    ("09:00 09:30 10:00", "11:00 12:00"),
    ("09:00 10:00", "11:00 11:30 12:00"),
])
def test_moved_variants_with_distinct_hour_sequences_stay_separate(old_time, new_time):
    upper = ("moved", _lesson(parity="upper"), _lesson("11:00 12:00", "upper"))
    lower = ("moved", _lesson(old_time, "lower"), _lesson(new_time, "lower"))

    assert fmt._variant_signature(*upper) != fmt._variant_signature(*lower)
    assert len(fmt._merge_week_variants([upper, lower])) == 2


@pytest.mark.parametrize("kind", ["added", "removed", "changed"])
def test_non_move_variants_with_different_durations_stay_separate(kind):
    upper = (kind, _lesson(parity="upper"), None)
    lower = (kind, _lesson("09:00", "lower"), None)

    assert fmt._variant_signature(*upper) != fmt._variant_signature(*lower)
    assert len(fmt._merge_week_variants([upper, lower])) == 2


def test_variant_signature_normalizes_full_hour_sequences():
    upper = ("moved", _lesson("9:00 10:00", "upper"), _lesson("11:00 12:00", "upper"))
    lower = (
        "moved", _lesson("10:00 09:00 09:00", "lower"),
        _lesson("12:00 11:00 11:00", "lower"),
    )

    assert fmt._variant_signature(*upper) == fmt._variant_signature(*lower)
    assert len(fmt._merge_week_variants([upper, lower])) == 1


@pytest.mark.parametrize("reverse_entries", [False, True])
def test_merged_moves_preserve_old_and_new_locations_and_parities(reverse_entries):
    entries = [
        ("moved", _lesson(parity="upper"),
         _lesson("11:00 12:00", "upper", room="202", location="Campus B")),
        ("moved", _lesson(parity="lower", room="303", location="Campus C"),
         _lesson("11:00 12:00", "lower", room="", location="", delivery_mode="remote")),
    ]
    if reverse_entries:
        entries.reverse()
    original = deepcopy(entries)

    merged = fmt._merge_week_variants(entries)

    assert len(merged) == 1
    kind, before, after = merged[0]
    assert kind == "moved"
    expected_parities = ["lower", "upper"] if reverse_entries else ["upper", "lower"]
    assert before["_variants"] == [entry[1] for entry in entries]
    assert after["_variants"] == [entry[2] for entry in entries]
    assert before["_parities"] == after["_parities"] == expected_parities
    assert entries == original


def test_rename_still_merges_upper_and_lower_with_different_rooms_and_modes():
    fields = [["\u043f\u0440\u0435\u0434\u043c\u0435\u0442", "Old subject", "History"]]
    upper = _lesson(parity="upper", fields=fields)
    lower = _lesson(parity="lower", fields=fields, room="", location="", delivery_mode="remote")

    merged = fmt._merge_week_variants([("changed", upper, None), ("changed", lower, None)])

    assert len(merged) == 1
    kind, item, partner = merged[0]
    assert kind == "changed"
    assert item["_variants"] == [upper, lower]
    assert item["_parities"] == ["upper", "lower"]
    assert partner is None


def test_same_parity_entries_are_not_merged():
    entries = [("added", _lesson(parity="upper"), None)] * 2

    assert len(fmt._merge_week_variants(entries)) == 2


def test_group_by_day_keeps_cross_day_moves_as_removed_and_added():
    gone = _lesson()
    arrived = _lesson("11:00 12:00", day=fmt.DAYS_ORDER[1])

    grouped = fmt._group_by_day([arrived], [gone], [])

    assert [kind for kind, _, _ in grouped[gone["day"]]] == ["removed"]
    assert [kind for kind, _, _ in grouped[arrived["day"]]] == ["added"]


def test_time_less_lesson_does_not_break_day_grouping():
    """«Проектный день» стоит в сетке без времени («—»): дифф с такой строкой
    не должен ронять пост сравнением None с временем."""
    import datetime as dt
    import zoneinfo

    from format import build_changes_rich_message, changes_fallback_text
    from telegram_api import validate_rich_payload

    diff = {
        "added": [{"day": "Среда", "time": "14:00 15:00", "subject": "(пр.) Основы российской государственности",
                   "room": "303", "teacher": "Иванов Иван Иванович", "note": ""}],
        "removed": [{"day": "Среда", "time": "—", "subject": "Проектный день\n23.09, 21.10 и 16.12",
                     "room": "—", "teacher": "—", "note": "23.09, 21.10 и 16.12"},
                    {"day": "Среда", "time": "9:00 10:00", "subject": "(лек.) История России",
                     "room": "1201", "teacher": "Петров Пётр Петрович", "note": ""}],
        "changed": [],
        "transition": None,
    }
    now = dt.datetime(2026, 9, 14, 11, 51, tzinfo=zoneinfo.ZoneInfo("Europe/Moscow"))
    rich = build_changes_rich_message(diff, [], "https://example.test", now=now)
    validate_rich_payload(rich)
    blob = json.dumps(rich, ensure_ascii=False)
    assert "Проектный день" in blob and "История России" in blob
    text = changes_fallback_text(diff, "https://example.test", now=now)
    assert "Проектный день" in text


def test_day_table_is_capped_and_names_the_rest():
    """При переопубликовании всего расписания день не превращается в простыню:
    не больше _RICH_DIFF_MAX_ROWS_PER_DAY строк, остальное названо числом."""
    from format import _RICH_DIFF_MAX_ROWS_PER_DAY, build_changes_rich_message
    from telegram_api import validate_rich_payload

    cap = _RICH_DIFF_MAX_ROWS_PER_DAY
    added = [{"day": "Среда", "time": f"{8 + index // 6}:00 {9 + index // 6}:00", "subject": f"Предмет {index}",
              "room": str(100 + index), "teacher": f"Преподаватель {index}"} for index in range(cap + 5)]
    rich = build_changes_rich_message({"added": added, "removed": [], "changed": [], "transition": None},
                                      [], "https://example.test")
    validate_rich_payload(rich)
    day = next(b for b in rich["rich_message"]["blocks"] if b["type"] == "details")
    table, note, details = day["blocks"]
    assert len(table["cells"]) - 1 == cap
    assert json.dumps(note, ensure_ascii=False).count("Ещё 5 изменений этого дня не поместились") == 1
    assert len(details["blocks"][0]["cells"]) - 1 == cap
