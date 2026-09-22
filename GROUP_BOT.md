# Групповой эфемерный бот команд (group_bot.py)

## Что это
Бот @novsutimetablebot отвечает в супергруппе «Чат расписание бот»
(NOVSU_GROUP_CHAT_ID) на слеш-команды расписания. Ответы — те же скрины,
что в закрепе канала (кеш `state/dashboard_screens.json`, рендера ноль).

Команды: `/timetable` и `/расписание` — вся неделя (альбом);
`/today` `/сегодня` — сегодня; `/tomorrow` `/завтра` — завтра;
аргументы: `сегодня|завтра|вся|неделя|пн..вс`.
Дата всегда по Москве (сервер в UTC).

## Как устроена «эфемерность»
Нативных эфемерных сообщений у Bot API нет (проверено 2026-09-22: методов
ephemeral/auto-delete нет, неизвестные поля молча игнорируются). Поэтому:
ответ шлётся, а через TTL (`NOVSU_GROUP_BOT_TTL`, дефолт 90 с) бот-админ
удаляет И свой ответ, И сообщение с командой (`deleteMessage`).
ВАЖНО ПОНИМАТЬ: команду видят все участники с момента отправки до удаления.
Спрятать мгновенно нельзя — это ограничение Telegram, не бага бота.

## Безопасность (только группа, только хозяин)
- Без `NOVSU_GROUP_CHAT_ID` процесс даже не стартует (fail-closed).
- Игнорируются: ЛС, чужие чаты, чужие пользователи.
- Разрешён `TG_DM_TARGET` и анонимный админ (from=None + sender_chat=чат).

## Где живёт и как управлять (tmux)
Запущен в tmux-сессии `group-bot`:
    tmux attach -t group-bot        # посмотреть лог
    tmux kill-session -t group-bot  # ВЫКЛЮЧИТЬ слеш-команды
    cd /root/novsu-timetable-api && tmux new-session -d -s group-bot \
        "/usr/bin/python3 group_bot.py"          # ВКЛЮЧИТЬ обратно
Если попросили «убрать слеш-функции бота» — kill-session достаточно.
Если «верни» — new-session как выше. Перед запуском проверить, что
другого поллера нет: `ps aux | grep group_bot` (getUpdates конфликтует!).

Systemd-вариант (сейчас НЕ используется): unit
`/etc/systemd/system/novsu-group-bot.service` — `systemctl enable --now`
/ `disable --now`. Не держать оба одновременно.

## Отладка одним батчем без tmux
    /usr/bin/python3 group_bot.py --once

## Краш-алерты
3 неудачных поллинга подряд → ЛС хозяину, восстановление → тоже ЛС.

## Связанные файлы
- `group_bot.py` — сам бот; `tests/test_group_bot.py` — тесты.
- `state/group_bot_offset.json` — оффсет getUpdates (не удалять без нужды).
- `state/last_parsed.json` — данные (пишет monitor).
- `state/dashboard_screens.json` — индекс готовых скринов закрепа.
