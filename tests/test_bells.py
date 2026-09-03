"""Модель учебного времени НовГУ: академические часы → интервалы пар."""
import datetime as dt

import pytest

import bells


@pytest.mark.parametrize("cell,hours,start,end", [
    ("9:00 10:00", 2, dt.time(9, 0), dt.time(10, 45)),
    ("11:00 12:00", 2, dt.time(11, 0), dt.time(12, 45)),
    ("14:00 15:00", 2, dt.time(14, 0), dt.time(15, 45)),
    ("20:00 21:00", 2, dt.time(20, 0), dt.time(21, 45)),
    ("9:00", 1, dt.time(9, 0), dt.time(9, 45)),
])
def test_pair_span_from_academic_hours(cell, hours, start, end):
    slot = bells.slot(cell)
    assert slot.hours == hours
    assert slot.start == start
    assert slot.end == end


def test_pair_end_without_inner_break_is_15_minutes_earlier():
    slot = bells.slot("9:00 10:00")
    assert slot.end == dt.time(10, 45)
    assert slot.end_min == dt.time(10, 30)
    assert slot.label(both=True) == "09:00–10:30/10:45"


def test_four_hours_are_two_pairs_not_one_lesson():
    slot = bells.slot("14:00 15:00 16:00 17:00")
    assert slot.pairs == 2
    assert [pair.label for pair in slot.pair_blocks()] == ["14:00–15:45", "16:00–17:45"]
    assert slot.label() == "14:00–15:45 + 16:00–17:45"
    assert slot.label_cell() == "14:00–15:45\n16:00–17:45"


def test_three_hours_keep_the_trailing_single_hour_visible():
    slot = bells.slot("16:00 17:00 18:00")
    assert slot.pairs == 2
    assert slot.label() == "16:00–17:45 + 18:00–18:45"
    assert "1 ак. ч." in slot.label_full()


def test_gap_between_hours_splits_blocks():
    slot = bells.slot("9:00 11:00")
    assert len(slot.blocks) == 2
    assert slot.label() == "09:00–09:45 · 11:00–11:45"


def test_portal_separators_and_padding_do_not_change_the_result():
    assert bells.slot("09:00  10:00").label() == bells.slot("9:00 10:00").label()
    assert bells.slot("9:00\n10:00").label() == "09:00–10:45"
    assert bells.slot("9:00-10:00").label() == "09:00–10:45"


@pytest.mark.parametrize("cell", ["—", "", None, "25:99", "нет времени"])
def test_invalid_time_is_preserved_and_sorts_last(cell):
    slot = bells.slot(cell)
    assert slot.blocks == ()
    assert not slot.ok
    assert slot.start is None
    assert slot.label() == ("—" if not str(cell or "").strip() or str(cell).strip() == "—" else str(cell).strip())
    assert slot.sort_key()[0] == 24 * 60


def test_structure_legend_text_is_explicit():
    slot = bells.slot("9:00 10:00")
    assert slot.label_full() == "09:00–10:45 (45 + 15 перерыв + 45)"
    assert "45" in bells.LEGEND and "перерыв" in bells.LEGEND


def test_block_never_ends_after_the_regulation_limit():
    """Регламент: занятия до 22.00; последний час 21:00 кончается в 21:45."""
    slot = bells.slot("21:00")
    assert slot.end == dt.time(21, 45)
    assert slot.end <= bells.LATEST


def test_end_datetime_for_reminders():
    block = bells.slot("14:00 15:00").blocks[0]
    date = dt.date(2026, 9, 3)
    assert block.end_datetime(date) == dt.datetime(2026, 9, 3, 15, 45)
    assert block.end_datetime(date, strict=True) == dt.datetime(2026, 9, 3, 15, 30)


def test_hour_tokens_are_strict_but_keep_portal_spelling():
    assert bells.hour_tokens("9:00 10:00") == ["9:00", "10:00"]
    assert bells.hour_tokens("09:00  10:00") == ["09:00", "10:00"]
    assert bells.hour_tokens("9:00-10:30") == ["9:00", "10:30"]
    # Хронология чинится, дубли схлопываются.
    assert bells.hour_tokens("17:00 16:00 15:00 14:00") == ["14:00", "15:00", "16:00", "17:00"]
    assert bells.hour_tokens("9:00 09:00 9:00") == ["9:00"]
    # Мусор не «чинится» молча: «119:00» — это не «19:00».
    assert bells.hour_tokens("119:00") == []
    assert bells.hour_tokens("25:99") == []
    assert bells.hour_tokens("—") == []
    assert bells.hour_tokens(None) == []


def test_unordered_portal_tokens_still_form_one_block():
    slot = bells.slot("17:00 16:00 15:00 14:00")
    assert slot.hours == 4
    assert slot.label() == "14:00–15:45 + 16:00–17:45"
