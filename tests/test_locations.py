"""Regression tests for locations.py — Bug #2 Location Inference."""
from __future__ import annotations

import pytest

from locations import resolve_location


class TestAntonovoConfirmedContext:
    """При подтверждённом контексте Антоново трёхзначные номера — новый корпус."""

    def test_303_with_antonovo_hint_is_new_building(self):
        result = resolve_location("303", context_hints=["Антоново"])
        assert result["campus"] == "antonovo"
        assert result["building"] == "новый корпус — кампус Антоново"
        assert result["floor"] == "3"
        assert result["room_number"] == "03"
        assert result["confidence"] == "high"
        assert "подтверждённом контексте" in result["reason"]

    def test_303_with_igum_hint_is_new_building(self):
        result = resolve_location("303", context_hints=["ИГУМ"])
        assert result["campus"] == "antonovo"
        assert result["building"] == "новый корпус — кампус Антоново"
        assert result["confidence"] == "high"

    def test_303_with_location_antonovo(self):
        result = resolve_location("303", location="Антоново")
        assert result["campus"] == "antonovo"
        assert result["building"] == "новый корпус — кампус Антоново"
        assert result["confidence"] == "high"

    def test_1318_with_antonovo_hint_is_old_building(self):
        result = resolve_location("1318", context_hints=["Антоново"])
        assert result["campus"] == "antonovo"
        assert result["building"] == "старый корпус — кампус Антоново"
        assert result["floor"] == "3"
        assert result["room_number"] == "18"
        assert result["confidence"] == "high"

    def test_402igum_is_antonovo_new_building(self):
        result = resolve_location("402ИГУМ")
        assert result["campus"] == "antonovo"
        assert result["building"] == "новый корпус — кампус Антоново"
        assert result["floor"] == "4"
        assert result["room_number"] == "02"
        assert result["confidence"] == "high"

    def test_1313igum313_is_antonovo_old_building(self):
        result = resolve_location("1313ИГУМ313")
        assert result["campus"] == "antonovo"
        assert result["building"] == "старый корпус — кампус Антоново"
        assert result["floor"] == "3"
        assert result["room_number"] == "13"
        assert result["confidence"] == "high"


class TestNoAntonovoContext:
    """Без подтверждённого контекста Антоново трёхзначные номера — НЕ Антоново."""

    def test_512_bare_is_unknown_campus(self):
        result = resolve_location("512")
        assert result["campus"] is None
        assert result["building"] == ""
        assert result["floor"] == "5"
        assert result["room_number"] == "12"
        assert result["confidence"] == "low"
        assert "без контекста" in result["reason"]
        assert "Антоново" not in result["building"]

    def test_303_bare_is_unknown_campus(self):
        result = resolve_location("303")
        assert result["campus"] is None
        assert result["confidence"] == "low"
        assert "Антоново" not in result["building"]

    def test_2312_bare_is_korpus_2(self):
        result = resolve_location("2312")
        assert result["campus"] == "main"
        assert result["building"] == "корпус 2 (адрес смотри в примечаниях)"
        assert result["floor"] == "3"
        assert result["room_number"] == "12"
        assert result["address"] == "Б. Санкт-Петербургская, 41"
        assert result["confidence"] == "high"

    def test_1318_bare_is_korpus_1(self):
        result = resolve_location("1318")
        assert result["campus"] == "main"
        assert result["building"] == "корпус 1"
        assert result["floor"] == "3"
        assert result["room_number"] == "18"
        assert result["address"] == "Б. Санкт-Петербургская, 41"
        assert result["confidence"] == "medium"
        assert "без контекста Антоново" in result["reason"]


class TestSpecialMarks:
    """Суффиксы-маркеры с фиксированными адресами."""

    def test_310_inpo_is_chudintseva(self):
        result = resolve_location("310 ИНПО")
        assert result["campus"] == "other"
        assert result["address"] == "ул. Чудинцева, 6"
        assert result["confidence"] == "high"
        assert "ИНПО" in result["reason"]
        assert "Антоново" not in result["address"]

    def test_103_pi_is_pskovskaya(self):
        result = resolve_location("103ПИ")
        assert result["campus"] == "other"
        assert result["address"] == "ул. Псковская, 3"
        assert result["confidence"] == "high"

    def test_310_igum_is_antonovo(self):
        result = resolve_location("310 ИГУМ")
        assert result["campus"] == "antonovo"
        assert result["building"] == "новый корпус — кампус Антоново"
        assert result["confidence"] == "high"


class TestEdgeCases:
    """Граничные случаи: поточные, литеры, склейки."""

    def test_3_potochnaya(self):
        result = resolve_location("3 поточная")
        assert result["campus"] == "main"
        assert result["building"] == "корпус 3 — Б. Санкт-Петербургская, 41"
        assert result["floor"] == "—"
        assert result["room_number"] == "3 поточная"
        assert result["confidence"] == "high"

    def test_potochnaya_without_number(self):
        result = resolve_location("поточная")
        assert result["campus"] is None
        assert result["building"] == "поточная аудитория"
        assert result["confidence"] == "low"

    def test_323a_litera(self):
        result = resolve_location("323а", context_hints=["Антоново"])
        assert result["campus"] == "antonovo"
        assert result["floor"] == "3"
        assert result["room_number"] == "23а"
        assert result["confidence"] == "high"

    def test_1100_1_variant(self):
        result = resolve_location("1100(1)", context_hints=["Антоново"])
        assert result["campus"] == "antonovo"
        assert result["building"] == "старый корпус — кампус Антоново"
        assert result["floor"] == "1"
        assert result["room_number"] == "00"
        assert result["confidence"] == "high"

    def test_empty_room(self):
        result = resolve_location("")
        assert result["campus"] is None
        assert result["confidence"] == "low"
        assert "пустой номер" in result["reason"]

    def test_sportzal_not_a_room(self):
        result = resolve_location("Спортзал")
        assert result["campus"] is None
        assert result["confidence"] == "low"
        assert "нестандартный формат" in result["reason"]

    def test_209_226_slash_not_a_room(self):
        result = resolve_location("209/226")
        assert result["campus"] is None
        assert result["confidence"] == "low"


class TestGuideCompatibility:
    """Проверка совместимости с _guide_decode_room из format.py."""

    def test_guide_decode_room_402igum(self):
        from format import _guide_decode_room
        result = _guide_decode_room("402ИГУМ")
        assert result is not None
        assert result["floor"] == "4"
        assert result["num"] == "02"
        assert "ИГУМ" in result["place"]
        assert "новый корпус" in result["place"]

    def test_guide_decode_room_1318(self):
        from format import _guide_decode_room
        result = _guide_decode_room("1318")
        assert result is not None
        assert result["place"] == "старый корпус — кампус Антоново"

    def test_guide_decode_room_214(self):
        from format import _guide_decode_room
        result = _guide_decode_room("214")
        assert result is not None
        assert result["place"] == "новый корпус — кампус Антоново"

    def test_guide_decode_room_310_inpo(self):
        from format import _guide_decode_room
        result = _guide_decode_room("310 ИНПО")
        assert result is not None
        assert "Чудинцева" in result["place"]
        assert "не Антоново" in result["place"]

    def test_guide_decode_room_323a(self):
        from format import _guide_decode_room
        result = _guide_decode_room("323а")
        assert result is not None
        assert result["floor"] == "3"
        assert result["num"] == "23а"
        assert "новый корпус" in result["place"]

    def test_guide_decode_room_3potochnaya(self):
        from format import _guide_decode_room
        result = _guide_decode_room("3поточная")
        assert result is not None
        assert result["floor"] == "—"
        assert result["num"] == "3 поточная"
        assert "корпус 3" in result["place"]

    def test_guide_decode_room_sportzal(self):
        from format import _guide_decode_room
        result = _guide_decode_room("Спортзал")
        assert result is None

    def test_guide_decode_room_209_226(self):
        from format import _guide_decode_room
        result = _guide_decode_room("209/226")
        assert result is None
