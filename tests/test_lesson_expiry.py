"""Pure date-policy tests: no renderer, persistence or transport imports."""

import copy
import datetime as dt

import pytest

import schedule_logic as logic


BOUNDS = (dt.date(2026, 9, 1), dt.date(2027, 8, 31))
WEEK = {"week": 2, "half": "bottom", "start": "07.09.2026", "end": "12.09.2026"}


@pytest.mark.parametrize("field", ["note", "raw_comment", "subject", "subject_raw"])
@pytest.mark.parametrize("day,expired", [(2, False), (3, False), (4, True), (10, True)])
def test_exact_date_before_on_after_in_all_evaluator_fields(field, day, expired):
    lesson = {field: "Практикум только 03.09."}
    assert logic.lesson_has_expired(lesson, dt.date(2026, 9, day), WEEK, BOUNDS) is expired


@pytest.mark.parametrize("note", [
    "03.09, 17.09", "только 03.09; 17.09.", "03.09 и 17.09",
    "только 03.09; только 17.09", "03.09, 04.09; Антоново; 10.09, 17.09",
])
@pytest.mark.parametrize("day,expired", [(2, False), (3, False), (10, False), (17, False), (18, True)])
def test_all_explicit_lists_must_be_exhausted(note, day, expired):
    assert logic.lesson_has_expired({"note": note}, dt.date(2026, 9, day), WEEK, BOUNDS) is expired


@pytest.mark.parametrize("note", [
    "только 03.09, 31.09", "03.09, 04.09 и 99.99", "только 31.09",
    "только 03.09, 17/09", "только 03.09, 2026-09-17", "только 03.09, 17.??", "только 03.09, 17",
    "только 03.09.202", "только 03.09.20266", "только 003.09", "только 03.009",
    "только 03.09abc", "только 03.09.20xx", "только 03.09 и 17",
    "с 31.09 по 03.09", "с 01.09 по 31.09", "01.09-03.09; 04.09-31.09",
    "по 31.09", "только 03.09; по неизвестной дате", "до 0 недели",
    "только 03.09; до неизвестной недели", "только 03.09; с 99 недели",
])
def test_malformed_or_partial_conditions_keep_the_row(note):
    assert not logic.lesson_has_expired({"note": note}, dt.date(2026, 10, 1), WEEK, BOUNDS)


@pytest.mark.parametrize("note", [
    "", "по согласованию", "до конца семестра", "даты уточняются", "только по записи",
    "03.09", "Раздел 2.3", "только", "с 03.09", "с 17.09", "с октября",
    "с 9 недели", "после 9 недели", "по верхней неделе", "по нижней неделе",
    "только 03.09 или позже", "не только 03.09", "кроме 03.09, 04.09",
    "только 03.09 и далее", "только 03.09; даты уточняются", "ежегодно только 03.09",
])
@pytest.mark.parametrize("target", [dt.date(2026, 9, 10), dt.date(2027, 9, 10)])
def test_no_explicit_deadline_never_expires_even_after_calendar_end(note, target):
    assert not logic.lesson_has_expired({"note": note}, target, WEEK, BOUNDS)


@pytest.mark.parametrize("parity", ["верхней", "нижней"])
@pytest.mark.parametrize("half", ["top", "bottom"])
@pytest.mark.parametrize("day,expired", [(2, False), (3, False), (10, True)])
def test_date_expiry_is_independent_of_both_week_parities(parity, half, day, expired):
    lesson = {"note": f"по {parity} неделе, только 03.09"}
    week = {**WEEK, "half": half}
    assert logic.lesson_has_expired(lesson, dt.date(2026, 9, day), week, BOUNDS) is expired


@pytest.mark.parametrize("note", ["до 9 недели", "до 9-й недели", "до 9-ой недели"])
@pytest.mark.parametrize("number,expired", [(8, False), (9, True), (10, True)])
@pytest.mark.parametrize("half", ["top", "bottom"])
def test_until_week_expires_starting_at_exclusive_week_number(note, number, expired, half):
    week = {"week": number, "half": half}
    lesson = {"note": f"{note}, по верхней неделе"}
    assert logic.lesson_has_expired(lesson, dt.date(2026, 11, 1), week) is expired


@pytest.mark.parametrize("field", ["note", "subject"])
@pytest.mark.parametrize("delivery", ["с использованием ДОТ", "С  использованием\tдот"])
@pytest.mark.parametrize("day,expired", [(2, False), (3, False), (10, True)])
def test_known_delivery_phrase_is_neutral_for_date_expiry(field, delivery, day, expired):
    lesson = {field: f"только 03.09 {delivery}"}
    assert logic.lesson_has_expired(lesson, dt.date(2026, 9, day), WEEK, BOUNDS) is expired


@pytest.mark.parametrize("half", ["top", "bottom"])
@pytest.mark.parametrize("number,expired", [(8, False), (9, True), (10, True)])
def test_known_delivery_phrase_is_neutral_for_week_expiry(half, number, expired):
    lesson = {"note": "до 9 недели, по верхней неделе, с использованием ДОТ"}
    week = {"week": number, "half": half}
    assert logic.lesson_has_expired(lesson, dt.date(2026, 11, 1), week) is expired


@pytest.mark.parametrize("note", [
    "с использованием ДОТ",
    "с 17.09 с использованием ДОТ",
    "только 03.09 с использованием ДОТ с 17.09",
    "до 2 недели с использованием ДОТ с 17.09",
    "только 03.09 с использованием неизвестного формата",
    "только 03.09 с использованием ДОТизация",
    "только 03.09 с использованием ДОТ с неизвестной даты",
    "только 03.09 с использованием ДОТ по согласованию",
])
def test_delivery_exception_preserves_future_and_unknown_conditions(note):
    assert not logic.lesson_has_expired({"note": note}, dt.date(2026, 9, 10), WEEK, BOUNDS)


@pytest.mark.parametrize("week", [None, {}, {"week": "bad"}, {"week": 0}, {"week": -1},
                                  {"week": 9.5}, {"week": True}])
def test_until_week_requires_a_valid_academic_week_number(week):
    assert not logic.lesson_has_expired({"note": "до 9 недели"}, dt.date(2026, 11, 1), week, BOUNDS)


@pytest.mark.parametrize("note", ["с 9 недели", "после 9 недели", "с 9-й недели"])
@pytest.mark.parametrize("number", [8, 9, 10, 99])
def test_lower_week_thresholds_are_never_deadlines(note, number):
    assert not logic.lesson_has_expired({"note": note}, dt.date(2026, 11, 1), {"week": number}, BOUNDS)


@pytest.mark.parametrize("note", ["по 03.09", "По 03.09.", "ул. Псковская д.3 по нижней неделе по 03.09"])
@pytest.mark.parametrize("day,expired", [(2, False), (3, False), (4, True)])
def test_standalone_end_is_inclusive(note, day, expired):
    assert logic.lesson_has_expired({"note": note}, dt.date(2026, 9, day), WEEK, BOUNDS) is expired


@pytest.mark.parametrize("note", ["с 03.09 по 17.09", "03.09-17.09", "03.09–17.09", "03.09—17.09"])
@pytest.mark.parametrize("day,expired", [(2, False), (3, False), (10, False), (17, False), (18, True)])
def test_ranges_keep_future_rows_and_include_both_endpoints(note, day, expired):
    assert logic.lesson_has_expired({"note": note}, dt.date(2026, 9, day), WEEK, BOUNDS) is expired


def test_all_ranges_must_end_not_just_the_first():
    lesson = {"note": "с 01.09 по 03.09; с 17.09 по 24.09"}
    assert not logic.lesson_has_expired(lesson, dt.date(2026, 9, 10), WEEK, BOUNDS)
    assert not logic.lesson_has_expired(lesson, dt.date(2026, 9, 24), WEEK, BOUNDS)
    assert logic.lesson_has_expired(lesson, dt.date(2026, 9, 25), WEEK, BOUNDS)


@pytest.mark.parametrize("note", ["с 15.12 по 10.01", "15.12-10.01", "15.12.26–10.01.27"])
@pytest.mark.parametrize("target,expired", [
    (dt.date(2026, 12, 14), False), (dt.date(2026, 12, 15), False),
    (dt.date(2027, 1, 1), False), (dt.date(2027, 1, 10), False), (dt.date(2027, 1, 11), True),
])
def test_december_january_ranges_use_the_full_calendar(note, target, expired):
    week = {"week": 19, "half": "top", "start": "04.01.2027", "end": "10.01.2027"}
    assert logic.lesson_has_expired({"note": note}, target, week, BOUNDS) is expired


@pytest.mark.parametrize("note", ["только 03.09", "по 03.09", "01.09-03.09"])
def test_past_autumn_dates_remain_expired_in_january_only_with_full_bounds(note):
    target = dt.date(2027, 1, 10)
    week = {"week": 19, "half": "top", "start": "04.01.2027", "end": "10.01.2027"}
    lesson = {"note": note}
    assert logic.lesson_has_expired(lesson, target, week, BOUNDS)
    assert not logic.lesson_has_expired(lesson, target, week)
    assert not logic.lesson_has_expired(lesson, target, week, (dt.date(2027, 1, 4), target))


@pytest.mark.parametrize("bounds", [None, (dt.date(2026, 9, 1), dt.date(2027, 9, 30)),
                                    (BOUNDS[1], BOUNDS[0]), ("invalid", "invalid"), ()])
def test_missing_ambiguous_or_malformed_calendar_cannot_assign_a_year(bounds):
    assert not logic.lesson_has_expired({"note": "только 03.09"}, dt.date(2027, 9, 10), WEEK, bounds)


@pytest.mark.parametrize("note", ["только 03.09.2026", "по 03.09.26", "01.09.2026-03.09.2026"])
def test_explicit_years_need_neither_calendar_nor_week(note):
    assert logic.lesson_has_expired({"note": note}, dt.date(2027, 1, 1), None)


@pytest.mark.parametrize("note", ["17.09-03.09", "10.01-15.12", "15.12.2026-10.01.2026"])
def test_reversed_ranges_do_not_invent_another_year(note):
    assert not logic.lesson_has_expired({"note": note}, dt.date(2027, 8, 1), WEEK, BOUNDS)


@pytest.mark.parametrize("note", [
    "03.09 занятий не будет", "03.09, 04.09 и 05.09 занятий не будет",
    "занятий не будет 03.09, 04.09", "03.09 занятие отменено", "отмена 03.09, 04.09",
    "03.09 занятия не состоятся", "с 01.09 по 03.09 занятий не будет",
    "01.09-03.09 занятий не будет", "по 03.09 занятий не будет", "до 2 недели занятий не будет",
    "только 03.09 занятий не будет", "без занятий 03.09, 04.09",
])
@pytest.mark.parametrize("target", [dt.date(2026, 9, 3), dt.date(2026, 9, 10)])
def test_cancellations_are_not_positive_occurrences_or_bounds(note, target):
    assert not logic.lesson_has_expired({"note": note}, target, WEEK, BOUNDS)


@pytest.mark.parametrize("deadline", ["только 03.09", "по 03.09", "01.09-03.09"])
def test_cancelled_future_dates_do_not_extend_a_separate_positive_deadline(deadline):
    lesson = {"note": f"{deadline} (17.09, 24.09 занятий не будет)"}
    assert logic.lesson_has_expired(lesson, dt.date(2026, 9, 10), WEEK, BOUNDS)


@pytest.mark.parametrize("deadline", ["только 03.09", "по 03.09", "01.09-03.09"])
def test_ambiguous_semicolon_list_keeps_the_evaluators_cancellation_scope(deadline):
    lesson = {"note": f"{deadline}; 17.09, 24.09 занятий не будет"}
    assert not logic.lesson_has_expired(lesson, dt.date(2026, 9, 10), WEEK, BOUNDS)


def test_future_dates_in_another_field_keep_the_row():
    lesson = {"note": "только 03.09", "subject_raw": "Практикум только 17.09"}
    assert not logic.lesson_has_expired(lesson, dt.date(2026, 9, 10), WEEK, BOUNDS)


@pytest.mark.parametrize("field", ["subject", "subject_raw"])
@pytest.mark.parametrize("condition,number,expired", [
    ("с 9 недели", 2, False), ("с 9 недели", 9, True),
    ("с 9-й недели", 2, False), ("после 9 недели", 2, False),
    ("после 9 недели", 9, False), ("после 9 недели", 10, True),
])
def test_inline_title_week_start_cannot_be_ignored_by_expiry(field, condition, number, expired):
    lesson = {field: f"География туризма {condition}", "note": "только 03.09"}
    week = {**WEEK, "week": number}
    assert logic.lesson_has_expired(lesson, dt.date(2026, 9, 10), week, BOUNDS) is expired


@pytest.mark.parametrize("field", ["subject", "subject_raw"])
@pytest.mark.parametrize("note", ["", "только 03.09"])
@pytest.mark.parametrize("number,expired", [(2, False), (9, True), (10, True)])
def test_inline_title_until_week_is_an_explicit_deadline(field, note, number, expired):
    lesson = {field: "География туризма до 9 недели", "note": note}
    week = {**WEEK, "week": number}
    assert logic.lesson_has_expired(lesson, dt.date(2026, 9, 10), week, BOUNDS) is expired


@pytest.mark.parametrize("field", ["subject", "subject_raw"])
@pytest.mark.parametrize("condition", [
    "с неизвестной недели", "до N недели", "после",
    "с 0 недели", "до 0 недели", "после 0 недели", "с 31.09",
])
def test_inline_title_unknown_or_malformed_condition_keeps_the_row(field, condition):
    lesson = {field: f"География туризма {condition}", "note": "только 03.09"}
    assert not logic.lesson_has_expired(lesson, dt.date(2026, 9, 10), WEEK, BOUNDS)


@pytest.mark.parametrize("condition", [
    "с 17.09", "с октября", "с 9 недели", "после 9 недели", "по 17.09",
    "до 9 недели", "17.09-24.09", "только 17.09",
])
def test_conflicting_future_conditions_are_not_hidden_by_an_old_deadline(condition):
    lesson = {"note": "только 03.09", "raw_comment": condition}
    assert not logic.lesson_has_expired(lesson, dt.date(2026, 9, 10), WEEK, BOUNDS)


def test_yearless_january_occurrences_are_future_in_december_and_expire_after_january():
    lesson = {"note": "только 03.09, 10.01"}
    assert not logic.lesson_has_expired(lesson, dt.date(2026, 12, 31), WEEK, BOUNDS)
    assert not logic.lesson_has_expired(lesson, dt.date(2027, 1, 10), WEEK, BOUNDS)
    assert logic.lesson_has_expired(lesson, dt.date(2027, 1, 11), WEEK, BOUNDS)


def test_leap_day_uses_the_calendar_year_and_invalid_leap_day_keeps_the_row():
    bounds = (dt.date(2023, 9, 1), dt.date(2024, 8, 31))
    lesson = {"note": "только 29.02"}
    assert not logic.lesson_has_expired(lesson, dt.date(2024, 2, 29), None, bounds)
    assert logic.lesson_has_expired(lesson, dt.date(2024, 3, 1), None, bounds)
    assert not logic.lesson_has_expired(lesson, dt.date(2027, 3, 1), None, BOUNDS)


def test_subject_continuation_and_canonical_inputs_are_unchanged():
    lesson = {"subject": "Практикум\nтолько 03.09", "source_row": 12,
              "raw_comment": "только 03.09", "metadata": {"untouched": [1, 2]}}
    original = copy.deepcopy((lesson, WEEK, BOUNDS))
    assert logic.lesson_has_expired(lesson, dt.date(2026, 9, 10), WEEK, BOUNDS)
    assert (lesson, WEEK, BOUNDS) == original


def test_expiry_does_not_use_applicability_or_human_reason_text(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("expiry must use explicit deadlines, not applicability scans/reasons")

    monkeypatch.setattr(logic, "lesson_non_applicability_reason", forbidden)
    monkeypatch.setattr(logic, "lesson_applies_on", forbidden)
    monkeypatch.setattr(logic, "lessons_for_date", forbidden)
    assert logic.lesson_has_expired({"note": "только 03.09"}, dt.date(2026, 9, 10), WEEK, BOUNDS)
    assert not logic.lesson_has_expired({"note": "после 99 недели"}, dt.date(2026, 9, 10), WEEK, BOUNDS)


def test_existing_permissive_date_parser_and_applicability_are_unchanged():
    lesson = {"note": "только 03.09, 31.09"}
    assert logic._parse_note_date("03.09", 2026) == dt.date(2026, 9, 3)
    assert logic._parse_note_date("03.09", 2026, strict=True) is None
    assert logic.lesson_applies_on(lesson, dt.date(2026, 9, 3), WEEK, BOUNDS)
    assert not logic.lesson_applies_on(lesson, dt.date(2026, 9, 10), WEEK, BOUNDS)
    assert not logic.lesson_has_expired(lesson, dt.date(2026, 9, 10), WEEK, BOUNDS)
