import datetime as dt

import pytest

from schedule_logic import (
    calendar_bounds,
    day_is_live,
    day_last_pair_end,
    day_view,
    lesson_applies_on,
    next_lessons,
    parse_user_date,
    dashboard_presentation_fingerprint,
    resolve_dashboard_view,
    visible_week_days,
    week_view,
)


def test_dashboard_view_advances_after_saturdays_last_pair():
    before = resolve_dashboard_view(SCHEDULE, WEEKS, dt.datetime(2026, 9, 5, 12, 30))
    after = resolve_dashboard_view(SCHEDULE, WEEKS, dt.datetime(2026, 9, 5, 14, 0))
    assert before["target_date"] == dt.date(2026, 9, 5)
    assert after["target_date"] == dt.date(2026, 9, 7)
    assert after["week"]["week"] == 2


def test_dashboard_view_does_not_fall_back_to_first_week_after_calendar():
    view = resolve_dashboard_view(SCHEDULE, WEEKS, dt.datetime(2026, 12, 1, 12, 0))
    assert view == {"target_date": dt.date(2026, 12, 1), "week": None, "live_days": []}


def test_dashboard_presentation_changes_only_when_view_changes():
    view = resolve_dashboard_view(SCHEDULE, WEEKS, dt.datetime(2026, 9, 5, 12, 30))
    assert dashboard_presentation_fingerprint("fp", view) == dashboard_presentation_fingerprint("fp", dict(view))
    assert dashboard_presentation_fingerprint("fp", view) != dashboard_presentation_fingerprint("fp2", view)


def test_dashboard_rolls_top_bottom_top_with_unchanged_schedule():
    """Long-running dashboard alternates parity without a portal content change."""
    weeks = [
        {"week": 1, "half": "top", "start": "31.08.2026", "end": "05.09.2026"},
        {"week": 2, "half": "bottom", "start": "07.09.2026", "end": "12.09.2026"},
        {"week": 3, "half": "top", "start": "14.09.2026", "end": "19.09.2026"},
    ]
    schedule = {"days": {
        "Понедельник": [
            {"subject": "Только верхняя", "time": "09:00 10:00", "note": "по верхней неделе"},
            {"subject": "Только нижняя", "time": "11:00 12:00", "note": "по нижней неделе"},
        ],
        "Суббота": [
            {"subject": "Субботняя верхняя", "time": "09:00 10:00", "note": "по верхней неделе"},
            {"subject": "Субботняя нижняя", "time": "11:00 12:00", "note": "по нижней неделе"},
        ],
    }}
    moments = [
        (dt.datetime(2026, 9, 5, 10, 0), 1, "top", "Субботняя верхняя"),
        (dt.datetime(2026, 9, 5, 10, 46), 2, "bottom", "Только нижняя"),
        (dt.datetime(2026, 9, 6, 12, 0), 2, "bottom", "Только нижняя"),
        (dt.datetime(2026, 9, 12, 12, 46), 3, "top", "Только верхняя"),
        (dt.datetime(2026, 9, 13, 12, 0), 3, "top", "Только верхняя"),
    ]
    fingerprints = []
    for moment, number, half, expected_subject in moments:
        view = resolve_dashboard_view(schedule, weeks, moment)
        assert view["week"]["week"] == number
        assert view["week"]["half"] == half
        selected = week_view(schedule, weeks, view["week"], group="6381", source_url="https://example.test")
        subjects = {
            lesson["subject"]
            for day in selected["days"]
            for lesson in day["lessons"]
        }
        assert expected_subject in subjects
        assert ("Только нижняя" in subjects) is (half == "bottom")
        assert ("Только верхняя" in subjects) is (half == "top")
        fingerprints.append(dashboard_presentation_fingerprint("unchanged-content", view))
    assert fingerprints[0] != fingerprints[1]
    assert fingerprints[1] == fingerprints[2]
    assert fingerprints[2] != fingerprints[3]
    assert fingerprints[3] == fingerprints[4]


WEEKS = [
    {"week": 1, "half": "top", "start": "01.09.2026", "end": "05.09.2026"},
    {"week": 2, "half": "bottom", "start": "07.09.2026", "end": "12.09.2026"},
    {"week": 3, "half": "top", "start": "14.09.2026", "end": "19.09.2026"},
    {"week": 10, "half": "bottom", "start": "02.11.2026", "end": "07.11.2026"},
]


SCHEDULE = {
    "days": {
        "Понедельник": [
            {"number": 1, "subject": "Верхняя", "time": "09:00", "room": "101", "teacher": "А", "note": "по верхней неделе"},
            {"number": 2, "subject": "Нижняя", "time": "10:00", "room": "102", "teacher": "Б", "note": "по нижней неделе"},
            {"number": 3, "subject": "С 14.09", "time": "11:00", "room": "103", "teacher": "В", "note": "с 14.09. по верхней неделе"},
        ],
        "Суббота": [
            {"number": 1, "subject": "Только даты", "time": "12:00", "room": "201", "teacher": "Г", "note": "05.09, 12.09, 19.09."},
            {"number": 2, "subject": "После 9", "time": "13:00", "room": "202", "teacher": "Д", "note": "после 9 недели"},
        ],
    }
}


@pytest.mark.parametrize("date,number", [
    ("2026-08-31", 1), ("2026-09-01", 1), ("2026-09-05", 1),
    ("2026-09-06", 2), ("2026-09-07", 2), ("2026-09-12", 2),
    ("2026-09-13", 3), ("2026-09-14", 3), ("2026-10-01", 10),
    ("2026-12-27", 18), ("2026-12-28", 18), ("2026-12-31", 18),
    ("2027-01-01", None),
])
def test_auto_week_uses_published_bounds_including_gaps_and_partial_weeks(date, number):
    import schedule_logic

    weeks = [*WEEKS, {"week": 18, "half": "bottom", "start": "28.12.2026", "end": "31.12.2026"}]
    # The portal groups upper/lower weeks; source order need not be chronological.
    selected = schedule_logic.find_current_or_next_week(list(reversed(weeks)), dt.date.fromisoformat(date))
    assert (selected["week"] if selected else None) == number
    if selected:
        assert any(selected is week for week in weeks)


def test_auto_week_respects_sunday_when_the_portal_includes_it():
    import schedule_logic

    weeks = [{**WEEKS[0], "end": "06.09.2026"}, WEEKS[1]]
    assert schedule_logic.find_current_or_next_week(weeks, dt.date(2026, 9, 6)) == weeks[0]


def test_auto_week_does_not_invent_a_calendar_from_invalid_entries():
    import schedule_logic

    bad = [{"week": 1, "start": "invalid", "end": "05.09.2026"},
           {"week": 2, "start": "12.09.2026", "end": "07.09.2026"}]
    assert schedule_logic.find_current_or_next_week([], dt.date(2026, 9, 6)) is None
    assert schedule_logic.find_current_or_next_week(bad, dt.date(2026, 9, 6)) is None
    assert calendar_bounds(bad) is None
    assert schedule_logic.find_current_or_next_week([*bad, WEEKS[2]], dt.date(2026, 9, 6)) == WEEKS[2]


def test_parse_user_date_relative_and_yearless_dates():
    today = dt.date(2026, 9, 1)
    assert parse_user_date("сегодня", today=today) == today
    assert parse_user_date("завтра", today=today) == dt.date(2026, 9, 2)
    assert parse_user_date("08.09", today=today) == dt.date(2026, 9, 8)
    assert parse_user_date("08.09.", today=today) == dt.date(2026, 9, 8)
    assert parse_user_date("29.02", today=dt.date(2024, 1, 1)) == dt.date(2024, 2, 29)


def test_day_view_filters_week_parity_and_start_date():
    first = day_view(SCHEDULE, WEEKS, dt.date(2026, 9, 7), group="6381", source_url="https://example.test")
    assert [lesson["subject"] for lesson in first["lessons"]] == ["Нижняя"]

    second = day_view(SCHEDULE, WEEKS, dt.date(2026, 9, 14), group="6381", source_url="https://example.test")
    assert [lesson["subject"] for lesson in second["lessons"]] == ["Верхняя", "С 14.09"]


def test_day_view_filters_exact_dates_and_after_week():
    date_list = day_view(SCHEDULE, WEEKS, dt.date(2026, 9, 12), group="6381", source_url="https://example.test")
    assert [lesson["subject"] for lesson in date_list["lessons"]] == ["Только даты"]

    after = day_view(SCHEDULE, WEEKS, dt.date(2026, 11, 7), group="6381", source_url="https://example.test")
    assert [lesson["subject"] for lesson in after["lessons"]] == ["После 9"]


def test_lesson_applies_on_handles_from_week_note():
    # Реальный пример с портала: "с 10 недели" / "с 9 недели, с использованием
    # ДОТ" — пара начинается не с первой учебной недели. До фикса
    # _FROM_WEEK_RE не существовал, и такие пары показывались с недели 1.
    lesson = {"note": "с 10 недели"}
    week_9 = {"week": 9, "half": "top"}
    week_10 = {"week": 10, "half": "bottom"}
    bounds_9 = (dt.date(2026, 10, 26), dt.date(2026, 11, 1))
    bounds_10 = (dt.date(2026, 11, 2), dt.date(2026, 11, 8))
    assert lesson_applies_on(lesson, dt.date(2026, 10, 26), week_9, bounds_9) is False
    assert lesson_applies_on(lesson, dt.date(2026, 11, 2), week_10, bounds_10) is True

    lesson_dot = {"note": "с 9 недели, с использованием ДОТ"}
    week_8 = {"week": 8, "half": "bottom"}
    bounds_8 = (dt.date(2026, 10, 19), dt.date(2026, 10, 25))
    assert lesson_applies_on(lesson_dot, dt.date(2026, 10, 19), week_8, bounds_8) is False
    assert lesson_applies_on(lesson_dot, dt.date(2026, 10, 26), week_9, bounds_9) is True


@pytest.mark.parametrize(("note", "week_number", "expected"), [
    ("с 9 недели", 8, "НЕ НА ЭТОЙ НЕДЕЛЕ · С 9‑Й НЕДЕЛИ"),
    ("с 9-й недели", 8, "НЕ НА ЭТОЙ НЕДЕЛЕ · С 9‑Й НЕДЕЛИ"),
    ("с 9 недели", 9, None),
    ("после 9 недели", 9, "НЕ НА ЭТОЙ НЕДЕЛЕ · ПОСЛЕ 9‑Й НЕДЕЛИ"),
    ("после 9-ой недели", 9, "НЕ НА ЭТОЙ НЕДЕЛЕ · ПОСЛЕ 9‑Й НЕДЕЛИ"),
    ("после 9 недели", 10, None),
    ("до 9 недели", 8, None),
    ("до 9-й недели", 9, "НЕ НА ЭТОЙ НЕДЕЛЕ · ДО 9‑Й НЕДЕЛИ"),
    ("до 9 недели", 9, "НЕ НА ЭТОЙ НЕДЕЛЕ · ДО 9‑Й НЕДЕЛИ"),
])
def test_non_applicability_reason_explains_academic_week_boundary(note, week_number, expected):
    from schedule_logic import lesson_non_applicability_reason

    target = dt.date(2026, 10, 19) + dt.timedelta(weeks=week_number - 8)
    week = {"week": week_number, "half": "top" if week_number % 2 else "bottom"}
    bounds = (target, target + dt.timedelta(days=6))
    assert lesson_non_applicability_reason({"note": note}, target, week, bounds) == expected


def test_non_applicability_reason_prioritizes_week_boundary_then_parity_then_date():
    from schedule_logic import lesson_non_applicability_reason

    week_8 = {"week": 8, "half": "bottom"}
    bounds = (dt.date(2026, 10, 19), dt.date(2026, 10, 25))
    assert lesson_non_applicability_reason(
        {"note": "с 9 недели по верхней неделе с 01.11"},
        bounds[0], week_8, bounds,
    ) == "НЕ НА ЭТОЙ НЕДЕЛЕ · С 9‑Й НЕДЕЛИ"
    assert lesson_non_applicability_reason(
        {"note": "по верхней неделе с 01.11"},
        bounds[0], week_8, bounds,
    ) == "НЕ НА ЭТОЙ НЕДЕЛЕ · ТОЛЬКО ВЕРХНЯЯ"
    assert lesson_non_applicability_reason(
        {"note": "с 01.11"},
        bounds[0], week_8, bounds,
    ) == "НЕ В ЭТУ ДАТУ · С 01.11"


def test_non_applicability_reason_explains_calendar_context():
    from schedule_logic import lesson_non_applicability_reason

    target = dt.date(2026, 10, 19)
    week = {"week": 8, "half": "bottom"}
    bounds = (target, target + dt.timedelta(days=6))
    assert lesson_non_applicability_reason(
        {"note": "с 20.10 по 22.10"}, target, week, bounds,
    ) == "НЕ В ЭТУ ДАТУ · 20.10–22.10"
    assert lesson_non_applicability_reason(
        {"note": "19.10 занятий не будет"}, target, week, bounds,
    ) == "ЗАНЯТИЯ НЕ БУДЕТ · 19.10"
    assert lesson_non_applicability_reason(
        {"note": "только 20.10, 22.10"}, target, week, bounds,
    ) == "ТОЛЬКО ПО ДАТАМ · 20.10, 22.10"


def test_lesson_applies_on_handles_bare_end_date_note():
    # Реальные примеры: "По 30.09" и "ул. Псковская д.3 по нижней неделе по
    # 21.10" — одиночное «по DD.MM» без парного «с» задаёт последний день
    # действия пары. До фикса такой комментарий не давал верхней границы
    # вообще, и пара оставалась видимой бесконечно.
    lesson = {"note": "По 30.09"}
    week_in_range = {"week": 4, "half": "top"}
    bounds_in_range = (dt.date(2026, 9, 21), dt.date(2026, 9, 27))
    week_after = {"week": 6, "half": "top"}
    bounds_after = (dt.date(2026, 10, 5), dt.date(2026, 10, 11))
    assert lesson_applies_on(lesson, dt.date(2026, 9, 21), week_in_range, bounds_in_range) is True
    assert lesson_applies_on(lesson, dt.date(2026, 10, 5), week_after, bounds_after) is False

    lesson_addr = {"note": "ул. Псковская д.3 по нижней неделе  по 21.10"}
    week_ok = {"week": 4, "half": "bottom"}
    bounds_ok = (dt.date(2026, 9, 21), dt.date(2026, 9, 27))
    week_late = {"week": 10, "half": "bottom"}
    bounds_late = (dt.date(2026, 11, 2), dt.date(2026, 11, 8))
    assert lesson_applies_on(lesson_addr, dt.date(2026, 9, 22), week_ok, bounds_ok) is True
    assert lesson_applies_on(lesson_addr, dt.date(2026, 11, 3), week_late, bounds_late) is False

    # Диапазон "с X по Y" не должен путаться с одиночным "по Y".
    range_lesson = {"note": "с 05.10., по нижней неделе, ул. Б.С.-Петербургская, 41"}
    week_before = {"week": 5, "half": "bottom"}
    bounds_before = (dt.date(2026, 9, 28), dt.date(2026, 10, 4))
    week_during = {"week": 6, "half": "bottom"}
    bounds_during = (dt.date(2026, 10, 5), dt.date(2026, 10, 11))
    assert lesson_applies_on(range_lesson, dt.date(2026, 9, 28), week_before, bounds_before) is False
    assert lesson_applies_on(range_lesson, dt.date(2026, 10, 6), week_during, bounds_during) is True


def test_week_and_next_views_are_human_readable():
    week = week_view(SCHEDULE, WEEKS, WEEKS[1], group="6381", source_url="https://example.test")
    assert "неделя 2 (нижняя)" in week["summary"]
    assert "Нижняя" in week["summary"]

    nxt = next_lessons(SCHEDULE, WEEKS, dt.date(2026, 9, 7), start_time="09:30", group="6381", source_url="https://example.test", limit=1)
    assert "10:00" in nxt["summary"]


def test_explicit_date_ranges_are_inclusive_and_cross_new_year():
    bounds = (dt.date(2026, 9, 1), dt.date(2027, 1, 2))
    week = {"week": 1, "half": "top", "start": "01.09.2026", "end": "02.01.2027"}

    word_range = {"subject": "Курс", "note": "с 07.09 по 14.09"}
    assert lesson_applies_on(word_range, dt.date(2026, 9, 7), week, bounds)
    assert lesson_applies_on(word_range, dt.date(2026, 9, 14), week, bounds)
    assert not lesson_applies_on(word_range, dt.date(2026, 9, 15), week, bounds)

    dash_range = {"subject": "Курс", "note": "15.12–02.01"}
    assert lesson_applies_on(dash_range, dt.date(2026, 12, 15), week, bounds)
    assert lesson_applies_on(dash_range, dt.date(2027, 1, 2), week, bounds)
    assert not lesson_applies_on(dash_range, dt.date(2026, 12, 14), week, bounds)


def test_yearless_starts_use_academic_calendar_bounds_and_month_names():
    bounds = (dt.date(2026, 9, 1), dt.date(2027, 1, 2))
    january_week = {"week": 18, "half": "bottom", "start": "28.12.2026", "end": "02.01.2027"}
    september_week = {"week": 1, "half": "top", "start": "01.09.2026", "end": "05.09.2026"}

    assert lesson_applies_on(
        {"subject": "Курс", "note": "с 01.10"},
        dt.date(2027, 1, 2),
        january_week,
        bounds,
    )
    from_october = {"subject": "Курс", "note": "с октября"}
    assert not lesson_applies_on(from_october, dt.date(2026, 9, 5), september_week, bounds)
    assert lesson_applies_on(from_october, dt.date(2027, 1, 2), january_week, bounds)


def test_exact_dates_require_an_explicit_context_or_an_actual_list():
    bounds = (dt.date(2026, 9, 1), dt.date(2026, 9, 30))
    week = {"week": 2, "half": "bottom", "start": "07.09.2026", "end": "12.09.2026"}

    assert lesson_applies_on({"subject": "Раздел 2.3", "note": ""}, dt.date(2026, 9, 12), week, bounds)
    only = {"subject": "Консультация", "note": "только 12.09."}
    assert lesson_applies_on(only, dt.date(2026, 9, 12), week, bounds)
    assert not lesson_applies_on(only, dt.date(2026, 9, 11), week, bounds)
    date_list = {"subject": "Консультация", "note": "05.09, 12.09, 19.09."}
    assert lesson_applies_on(date_list, dt.date(2026, 9, 12), week, bounds)
    assert not lesson_applies_on(date_list, dt.date(2026, 9, 11), week, bounds)


def test_parity_comes_from_conditions_but_inline_start_date_is_preserved():
    weeks = [
        {"week": 2, "half": "bottom", "start": "07.09.2026", "end": "12.09.2026"},
        {"week": 4, "half": "bottom", "start": "21.09.2026", "end": "26.09.2026"},
    ]
    schedule = {
        "days": {
            "Понедельник": [
                {
                    "number": 1,
                    "subject": "Верхняя математика с 10.09",
                    "time": "09:00",
                    "note": "",
                }
            ]
        }
    }

    before = day_view(schedule, weeks, dt.date(2026, 9, 7), group="6381", source_url="test")
    assert before["lessons"] == []
    after = day_view(schedule, weeks, dt.date(2026, 9, 21), group="6381", source_url="test")
    assert [lesson["subject"] for lesson in after["lessons"]] == ["Верхняя математика с 10.09"]
    assert after["lessons"][0]["parity"] == "every"


def test_real_cancelled_lesson_dates_are_excluded():
    weeks = [
        {"week": 2, "half": "bottom", "start": "07.09.2026", "end": "12.09.2026"},
        {"week": 4, "half": "bottom", "start": "21.09.2026", "end": "26.09.2026"},
        {"week": 8, "half": "bottom", "start": "19.10.2026", "end": "24.10.2026"},
        {"week": 16, "half": "bottom", "start": "14.12.2026", "end": "19.12.2026"},
    ]
    schedule = {
        "days": {
            "Среда": [
                {
                    "number": 1,
                    "subject": "Основы профессиональной дискуссии",
                    "time": "09:00",
                    "note": "по нижней неделе, 23.09.; 21.10.; 16.12. занятий не будет",
                }
            ],
            "Четверг": [
                {
                    "number": 1,
                    "subject": "Физическая культура и спорт",
                    "time": "10:00",
                    "note": "ИГУМ, Антоново, 24.09.; 22.10.; 17.12. занятий не будет",
                }
            ],
        }
    }

    normal = day_view(schedule, weeks, dt.date(2026, 9, 9), group="5234", source_url="test")
    assert [lesson["subject"] for lesson in normal["lessons"]] == ["Основы профессиональной дискуссии"]
    for cancelled in (
        dt.date(2026, 9, 23),
        dt.date(2026, 10, 21),
        dt.date(2026, 12, 16),
        dt.date(2026, 9, 24),
        dt.date(2026, 10, 22),
        dt.date(2026, 12, 17),
    ):
        view = day_view(schedule, weeks, cancelled, group="5234", source_url="test")
        assert view["lessons"] == []


def test_invalid_lesson_time_is_tolerated_but_invalid_start_time_is_rejected():
    weeks = [{"week": 2, "half": "bottom", "start": "07.09.2026", "end": "12.09.2026"}]
    schedule = {
        "days": {
            "Понедельник": [
                {"number": 1, "subject": "Курс", "time": "25:99", "note": ""},
            ]
        }
    }

    view = day_view(schedule, weeks, dt.date(2026, 9, 7), group="6381", source_url="test")
    assert [lesson["time"] for lesson in view["lessons"]] == ["25:99"]
    upcoming = next_lessons(
        schedule,
        weeks,
        dt.date(2026, 9, 7),
        start_time="09:00",
        group="6381",
        source_url="test",
    )
    assert [lesson["time"] for lesson in upcoming["lessons"]] == ["25:99"]
    with pytest.raises(ValueError, match="start_time"):
        next_lessons(
            schedule,
            weeks,
            dt.date(2026, 9, 7),
            start_time="25:00",
            group="6381",
            source_url="test",
        )


def test_exact_date_list_with_location_suffix_is_respected():
    weeks = [
        {"week": 1, "half": "top", "start": "01.09.2026", "end": "06.09.2026"},
        {"week": 2, "half": "bottom", "start": "07.09.2026", "end": "13.09.2026"},
    ]
    lesson = {"subject": "Практика", "note": "05.09, 12.09, 19.09., 26.09. Антоново"}
    bounds = calendar_bounds(weeks)
    assert lesson_applies_on(lesson, dt.date(2026, 9, 5), weeks[0], bounds)
    assert not lesson_applies_on(lesson, dt.date(2026, 9, 6), weeks[0], bounds)


def test_cancelled_dates_and_exact_lists_accept_and_separator():
    week = {"week": 4, "half": "bottom", "start": "21.09.2026", "end": "27.09.2026"}
    bounds = (dt.date(2026, 9, 1), dt.date(2026, 12, 31))
    cancelled = {"subject": "X", "note": "23.09, 21.10 и 16.12 занятий не будет"}
    assert not lesson_applies_on(cancelled, dt.date(2026, 9, 23), week, bounds)
    exact = {"subject": "X", "note": "23.09 и 21.10"}
    assert lesson_applies_on(exact, dt.date(2026, 9, 23), week, bounds)
    assert not lesson_applies_on(exact, dt.date(2026, 9, 24), week, bounds)


def test_explicit_date_can_move_lesson_to_actual_weekday():
    weeks = [{"week": 18, "half": "bottom", "start": "28.12.2026", "end": "31.12.2026"}]
    schedule = {"days": {"Вторник": [{
        "number": 1, "subject": "История", "time": "09:00",
        "room": "101", "teacher": "Иванов", "note": "только 28.12.",
    }]}}
    monday = day_view(schedule, weeks, dt.date(2026, 12, 28), group="1", source_url="x")
    tuesday = day_view(schedule, weeks, dt.date(2026, 12, 29), group="1", source_url="x")
    assert [item["subject"] for item in monday["lessons"]] == ["История"]
    assert tuesday["lessons"] == []


def test_next_lessons_keeps_the_second_pair_of_a_long_block():
    """Ячейка «14:00 15:00 16:00 17:00» — две пары; в 15:50 вторая не теряется."""
    schedule = {
        "days": {
            "Вторник": [
                {"number": 1, "subject": "Проект", "time": "14:00 15:00 16:00 17:00", "note": ""},
                {"number": 2, "subject": "Язык", "time": "19:00 20:00", "note": ""},
            ]
        }
    }
    weeks = [{"week": 2, "half": "bottom", "start": "07.09.2026", "end": "12.09.2026"}]
    upcoming = next_lessons(
        schedule, weeks, dt.date(2026, 9, 8), start_time="15:50",
        group="6381", source_url="https://example.test", limit=3,
    )
    assert [item["time_label"] for item in upcoming["lessons"]] == ["16:00–17:45", "19:00–20:45"]
    assert "16:00–17:45" in upcoming["summary"]


def test_lesson_carries_machine_readable_times():
    schedule = {
        "days": {
            "Вторник": [
                {"number": 1, "subject": "Проект", "time": "14:00 15:00 16:00 17:00", "note": ""},
            ]
        }
    }
    weeks = [{"week": 2, "half": "bottom", "start": "07.09.2026", "end": "12.09.2026"}]
    view = day_view(schedule, weeks, dt.date(2026, 9, 8), group="6381", source_url="https://example.test")
    lesson = view["lessons"][0]
    # Идентификатор пары не меняется, а рядом лежат готовые подписи и границы.
    assert lesson["time"] == "14:00 15:00 16:00 17:00"
    assert lesson["time_start"] == "14:00"
    assert lesson["time_end"] == "17:45"
    assert lesson["time_label"] == "14:00–15:45 + 16:00–17:45"
    assert lesson["time_cell"] == "14:00–15:45\n16:00–17:45"


def test_visible_week_days_drops_past_days_at_midnight_msk():
    """Граница — 00:00 Мск: в пятницу остаются только пт и сб."""
    assert visible_week_days(WEEKS, dt.date(2026, 9, 4), dt.date(2026, 9, 4)) == ["Пятница", "Суббота"]
    assert visible_week_days(WEEKS, dt.date(2026, 9, 2), dt.date(2026, 9, 2)) == [
        "Среда", "Четверг", "Пятница", "Суббота",
    ]
    # Первая неделя 2026 года начинается во вторник (01.09) — имя дня берём
    # из реальной даты, иначе фильтр сдвинулся бы на понедельник.
    assert visible_week_days(WEEKS, dt.date(2026, 9, 1), dt.date(2026, 9, 1)) == [
        "Вторник", "Среда", "Четверг", "Пятница", "Суббота",
    ]


def test_visible_week_days_shows_whole_next_week_from_sunday():
    """В ночь на воскресенье фокус уезжает на понедельник — видна вся неделя."""
    monday = dt.date(2026, 9, 7)
    assert visible_week_days(WEEKS, monday, monday) == [
        "Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота",
    ]
    # Смотрим на следующую неделю ещё из воскресенья: фильтр ей не мешает.
    assert visible_week_days(WEEKS, monday, dt.date(2026, 9, 6)) == [
        "Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота",
    ]


def test_visible_week_days_keeps_everything_outside_calendar():
    """Дата между неделями (воскресенье) — не фильтруем, чтобы не получить пустой пост."""
    sunday = dt.date(2026, 9, 6)
    assert visible_week_days(WEEKS, sunday, sunday) == [
        "Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье",
    ]


def test_day_last_pair_end_and_day_is_live():
    day = {
        "date": "2026-09-04",
        "lessons": [
            {"number": 1, "subject": "Пара", "time": "11:00 12:00", "note": ""},
            {"number": 2, "subject": "Двойная", "time": "14:00 15:00 16:00 17:00", "note": ""},
        ],
    }
    assert day_last_pair_end(day["lessons"]) == dt.time(17, 45)
    assert day_is_live(day, dt.datetime(2026, 9, 4, 17, 44))
    assert not day_is_live(day, dt.datetime(2026, 9, 4, 17, 45))
    assert not day_is_live(day, dt.datetime(2026, 9, 5, 0, 0))
    assert day_is_live(day, dt.datetime(2026, 9, 3, 12, 0))


def test_visible_week_days_hides_today_after_last_pair():
    """Вариант 2: сегодня держится до конца последней пары, потом исчезает."""
    schedule = {
        "days": {
            "Пятница": [
                {"number": 3, "subject": "История", "time": "14:00 15:00", "note": ""},
            ],
            "Суббота": [
                {"number": 1, "subject": "Английский", "time": "09:00", "note": ""},
            ],
        }
    }
    weeks = [{"week": 1, "half": "top", "start": "01.09.2026", "end": "05.09.2026"}]
    friday = dt.date(2026, 9, 4)
    assert visible_week_days(
        weeks, friday, friday,
        schedule=schedule, now=dt.datetime(2026, 9, 4, 14, 30),
    ) == ["Пятница", "Суббота"]
    assert visible_week_days(
        weeks, friday, friday,
        schedule=schedule, now=dt.datetime(2026, 9, 4, 15, 46),
    ) == ["Суббота"]
