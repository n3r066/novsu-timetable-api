import datetime as dt
import json

import pytest

import monitor
import telegram_api


@pytest.mark.parametrize(('timestamp', 'silent'), [
    ('2026-09-15T21:59:59+03:00', False),
    ('2026-09-15T22:00:00+03:00', True),
    ('2026-09-16T00:00:00+03:00', True),
    ('2026-09-16T06:59:59+03:00', True),
    ('2026-09-16T07:00:00+03:00', False),
    ('2026-09-16T12:00:00+03:00', False),
    ('2026-09-15T19:00:00+00:00', True),
    ('2026-09-16T04:00:00+00:00', False),
    ('2026-09-16T06:59:59', True),
])
def test_moscow_quiet_hours_include_22_and_exclude_07(timestamp, silent):
    assert monitor._change_notifications_silent(dt.datetime.fromisoformat(timestamp)) is silent


def change():
    return {'added': [], 'removed': [], 'transition': None, 'changed': [{
        'day': 'Четверг', 'subject': 'География туризма', 'time': '09:00 10:00',
        'fields': [['ауд.', '418', '303']], 'room': '303',
    }]}


@pytest.mark.parametrize('silent', [True, False])
def test_change_post_uses_send_clock_even_with_old_observation(monkeypatch, silent):
    sent = []
    monkeypatch.setattr(monitor, '_change_notifications_silent', lambda: silent)
    monkeypatch.setattr(monitor, 'send_rich_message',
        lambda payload, **kwargs: sent.append(kwargs) or {'ok': True, 'result': {'message_id': 1}})
    result = monitor._post_changes_to_channel(change(), [],
        change_time=dt.datetime(2026, 9, 14, 12, tzinfo=dt.timezone.utc))
    assert result['ok'] and result['_rich']
    assert sent[0]['disable_notification'] is silent


def test_html_fallback_rechecks_night_boundary(monkeypatch):
    clocks = iter([False, True])
    rich, fallback = [], []
    monkeypatch.setattr(monitor, '_change_notifications_silent', lambda: next(clocks))
    monkeypatch.setattr(monitor, 'send_rich_message',
        lambda payload, **kwargs: rich.append(kwargs) or {'ok': False})
    monkeypatch.setattr(monitor, '_tg_api',
        lambda method, **kwargs: fallback.append((method, kwargs)) or {'ok': True})
    assert monitor._post_changes_to_channel(change(), [])['ok']
    assert rich[0]['disable_notification'] is False
    assert fallback[0][0] == 'sendMessage'
    assert fallback[0][1]['disable_notification'] is True


@pytest.mark.parametrize('silent', [True, False, None])
@pytest.mark.parametrize('multipart', [False, True])
def test_telegram_request_preserves_silent_flag(monkeypatch, tmp_path, silent, multipart):
    requests = []
    monkeypatch.setattr(telegram_api, '_call_with_retries',
        lambda request, **kwargs: requests.append(request) or {'ok': True})
    path = tmp_path / 'photo.png'
    path.write_bytes(b'fixture')
    payload = {'rich_message': {'blocks': [{'type': 'paragraph', 'text': ['Изменения']}]}}
    result = telegram_api.send_rich_message(payload, token='test', chat_id=1,
        files={'photo': path} if multipart else None, disable_notification=silent)
    assert result['ok'] and len(requests) == 1
    request = requests[0]
    if multipart:
        body = request.data.decode()
        if silent is None:
            assert 'name="disable_notification"' not in body
        else:
            assert f'name="disable_notification"\r\n\r\n{json.dumps(silent)}\r\n' in body
    else:
        body = json.loads(request.data)
        if silent is None:
            assert 'disable_notification' not in body
        else:
            assert body['disable_notification'] is silent
