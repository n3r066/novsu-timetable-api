"""Location inference for NovSU timetable rooms.

Resolves raw room numbers to normalized campus/building/address using
context from lesson location, comments, and known marker suffixes.

Official NovGU rule:
- 3-digit numbers: first digit = floor, remaining = cabinet number.
  Building/campus is determined by context, NOT by digit count alone.
- 4-digit numbers: first digit = building (korpus), second = floor,
  remaining = cabinet number.

Antonovo special rule (applies ONLY when context confirms Antonovo):
- 3-digit → new Antonovo building
- 4-digit starting with 1 → old Antonovo building

Without confirmed Antonovo context, 3-digit rooms have unknown campus.
"""
from __future__ import annotations

import re


# Marker suffixes that override digit-based building inference.
# These indicate the department that owns the room, not the student's institute.
_MARKER_PLACES: dict[str, str] = {
    "ИНПО": "ул. Чудинцева, 6",
    "ПИ": "ул. Псковская, 3",
}

# Markers that confirm Antonovo campus.
_ANTONOVO_MARKERS = {"ИГУМ", "ИГ"}

# Context strings that confirm Antonovo campus.
_ANTONOVO_CONTEXT_PATTERNS = [
    re.compile(r"антоново", re.I),
    re.compile(r"игум", re.I),
    re.compile(r"территория\s+антоново", re.I),
    re.compile(r"кампус\s+антоново", re.I),
]

_ROOM_RE = re.compile(
    r"^(\d{3,4})\s*"
    r"(ИГУМ|ИГ|ИЭ|ПИ|ПТИ|ХТИ|ИНПО|ИМО|МИ|ЮИ)?\s*"
    r"(?:\(\d{1,2}\)\s*)?"
    r"([а-яА-ЯёЁ])?\s*"
    r"(?:\d{3,4})?$"
)


def _has_antonovo_context(location: str, comment: str, hints: list[str]) -> bool:
    """Check if any context source confirms Antonovo campus."""
    combined = " ".join([location, comment, *hints])
    return any(p.search(combined) for p in _ANTONOVO_CONTEXT_PATTERNS)


def resolve_location(
    raw_room: str,
    *,
    location: str = "",
    comment: str = "",
    context_hints: list[str] | None = None,
) -> dict:
    """Resolve a raw room string to normalized location info.

    Returns a dict with:
        raw_room, room, campus, building, floor, room_number,
        address, confidence, reason
    """
    hints = context_hints or []
    result = {
        "raw_room": raw_room,
        "room": "",
        "campus": None,
        "building": "",
        "floor": "—",
        "room_number": "—",
        "address": "",
        "confidence": "low",
        "reason": "",
    }

    room = (raw_room or "").strip()
    if not room:
        result["reason"] = "пустой номер аудитории"
        return result

    # Handle "N поточн" pattern
    stream = re.match(r"^(\d{1,4})\s*поточн", room, re.I)
    if stream:
        digits = stream.group(1)
        antonovo = _has_antonovo_context(location, comment, hints)
        if len(digits) == 1:
            korpus = digits
            result.update({
                "room": f"{digits} поточная",
                "campus": "main",
                "building": f"корпус {korpus}" + (" — Б. Санкт-Петербургская, 41" if korpus == "3" else ""),
                "floor": "—",
                "room_number": f"{digits} поточная",
                "address": "Б. Санкт-Петербургская, 41" if korpus == "3" else f"корпус {korpus}",
                "confidence": "high",
                "reason": "поточная аудитория, корпус по первой цифре",
            })
        elif antonovo and digits.startswith("1"):
            result.update({
                "room": f"{digits} поточная",
                "campus": "antonovo",
                "building": "старый корпус — кампус Антоново",
                "address": "кампус Антоново",
                "confidence": "high",
                "reason": "поточная в Антоново (контекст подтверждён)",
            })
        else:
            result.update({
                "room": f"{digits} поточная",
                "building": f"корпус {digits[0]}" if len(digits) >= 4 else "адрес в примечаниях",
                "address": "адрес уточните в примечаниях",
                "confidence": "low",
                "reason": "поточная аудитория без контекста",
            })
        return result

    if "поточн" in room.lower():
        result.update({
            "room": "—",
            "building": "поточная аудитория",
            "address": "поточная аудитория — адрес в колонке «комм.»",
            "confidence": "low",
            "reason": "поточная аудитория, адрес в примечаниях",
        })
        return result

    m = _ROOM_RE.match(room)
    if not m:
        result.update({
            "room": room,
            "reason": "нестандартный формат номера",
        })
        return result

    digits = m.group(1)
    mark = (m.group(2) or "").upper()
    letter = m.group(3) or ""

    # Extract floor and cabinet number
    if len(digits) == 3:
        floor = digits[0]
        cabinet = digits[1:] + letter
    else:  # 4 digits
        floor = digits[1]
        cabinet = digits[2:] + letter

    result["floor"] = floor
    result["room_number"] = cabinet
    result["room"] = digits + letter

    # Check marker suffix first — it overrides digit-based inference
    if mark in _MARKER_PLACES:
        result.update({
            "campus": "other",
            "building": f"пометка {mark}: {_MARKER_PLACES[mark]}",
            "address": _MARKER_PLACES[mark],
            "confidence": "high",
            "reason": f"маркер {mark} указывает на конкретный адрес",
        })
        return result

    # Check if context confirms Antonovo
    antonovo = _has_antonovo_context(location, comment, hints)
    if mark in _ANTONOVO_MARKERS:
        antonovo = True

    if antonovo:
        # Antonovo special rules apply
        if len(digits) == 3:
            result.update({
                "campus": "antonovo",
                "building": "новый корпус — кампус Антоново",
                "address": "кампус Антоново",
                "confidence": "high",
                "reason": "трёхзначный номер при подтверждённом контексте Антоново",
            })
        elif digits[0] == "1":
            result.update({
                "campus": "antonovo",
                "building": "старый корпус — кампус Антоново",
                "address": "кампус Антоново",
                "confidence": "high",
                "reason": "четырёхзначный 1xxx + контекст Антоново → старый корпус",
            })
        else:
            # 4-значный не-1xxx: первая цифра = корпус главного кампуса.
            # Даже при контексте Антоново это не Антоново (там только 1xxx и 3-значные).
            korpus = digits[0]
            building = "корпус 3 — Б. Санкт-Петербургская, 41" if korpus == "3" else f"корпус {korpus} (адрес смотри в примечаниях)"
            result.update({
                "campus": "main",
                "building": building,
                "address": "Б. Санкт-Петербургская, 41",
                "confidence": "high",
                "reason": f"четырёхзначный номер {korpus}xxx — корпус {korpus}, не Антоново",
            })
        if mark and mark not in _ANTONOVO_MARKERS:
            result["reason"] += f"; маркер {mark}"
        return result

    # No Antonovo context — use general rules
    if len(digits) == 3:
        # 3-digit WITHOUT context: cannot assume Antonovo
        result.update({
            "campus": None,
            "building": "",
            "address": "адрес уточните в примечаниях",
            "confidence": "low",
            "reason": "трёхзначный номер без контекста — адрес уточните в примечаниях",
        })
    elif digits[0] == "1":
        # 4-digit starting with 1 without context
        result.update({
            "campus": "main",
            "building": "корпус 1",
            "address": "Б. Санкт-Петербургская, 41",
            "confidence": "medium",
            "reason": "четырёхзначный номер 1xxx без контекста Антоново — корпус 1",
        })
    else:
        # 4-digit non-1xxx: main campus building
        korpus = digits[0]
        addr = "Б. Санкт-Петербургская, 41"
        building = f"корпус {korpus} — Б. Санкт-Петербургская, 41" if korpus == "3" else f"корпус {korpus} (адрес смотри в примечаниях)"
        result.update({
            "campus": "main",
            "building": building,
            "address": addr,
            "confidence": "high",
            "reason": f"четырёхзначный номер {korpus}xxx — корпус {korpus}",
        })

    if mark:
        result["reason"] += f"; маркер {mark} — сверьтесь с примечаниями"

    return result
