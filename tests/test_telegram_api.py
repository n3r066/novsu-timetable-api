import pytest

from telegram_api import validate_rich_message


def _photo(media: str) -> dict:
    return {"type": "photo", "photo": {"type": "photo", "media": media}}


def test_slideshow_validates_nested_photos():
    validate_rich_message({"blocks": [{
        "type": "details",
        "summary": "Было / Стало",
        "blocks": [{"type": "slideshow", "blocks": [_photo("a"), _photo("b")]}],
    }]})


def test_slideshow_rejects_invalid_nested_media():
    with pytest.raises(ValueError, match="photo block requires"):
        validate_rich_message({"blocks": [{
            "type": "slideshow",
            "blocks": [{"type": "photo", "photo": {"type": "photo"}}],
        }]})


def test_table_accepts_rowspan_for_stacked_cells():
    validate_rich_message({"blocks": [{
        "type": "table",
        "cells": [
            [
                {"text": ["№"], "align": "left", "valign": "middle", "is_header": True},
                {"text": ["занятие"], "align": "left", "valign": "middle", "is_header": True},
                {"text": ["место / время"], "align": "left", "valign": "middle", "is_header": True},
            ],
            [
                {"text": ["1"], "align": "left", "valign": "middle", "rowspan": 2},
                {"text": ["Проект"], "align": "left", "valign": "middle", "rowspan": 2},
                {"text": ["1331\nАнтоново"], "align": "left", "valign": "middle"},
            ],
            [{"text": [{"type": "code", "text": "14:00–15:45"}], "align": "left", "valign": "middle"}],
        ],
        "is_bordered": True,
        "is_striped": True,
    }]})
