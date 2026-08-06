#!/usr/bin/env python3
"""MyT MCP — локальный скрипт входа (ВНЕ агента/LLM).

Пароль и SMS-код вводятся через getpass (не отображаются в терминале) или читаются
из env `MYT_PASSWORD`. В контекст модели они не попадают: скрипт запускается прямо
из шелла, а тула, принимающего корпоративный пароль, в этом MCP нет и не будет.
Рабочий пароль открывает не один сервис, а все рабочие системы разом, и цена его
попадания в транскрипт несопоставима с удобством.

    .venv/bin/python login_cli.py n.ivanov
    MYT_PASSWORD="пароль" .venv/bin/python login_cli.py n.ivanov

Интерпретатор — из .venv этого репозитория: там стоят зависимости, и оттуда же
MCP берёт тот же файл сессии.

После входа сессия лежит в ~/.local/share/tbank-myt/session.json (права 0600) и
продлевается сама; повторный вход нужен, только когда тулы скажут
MYT SESSION EXPIRED.
"""
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from src import myt
    from src import server as srv
    from src.errors import MytApiError
except ModuleNotFoundError as _e:
    # Первым делом скрипт запускают системным python3 — так короче набирать. Голый
    # ModuleNotFoundError не подсказывает ничего, и человек идёт ставить пакет
    # глобально вместо того, чтобы взять готовое окружение.
    _VENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv", "bin", "python")
    print(f"Не хватает зависимости: {_e.name}")
    if os.path.exists(_VENV):
        print("Похоже, запущено системным python. Повтори ту же команду "
              "интерпретатором из окружения репозитория:")
        print(f"  {_VENV} login_cli.py …")
    else:
        print("Окружения нет. Создай его:")
        print("  python3 -m venv .venv && .venv/bin/pip install -e .")
    sys.exit(1)


USAGE = """Usage:
  .venv/bin/python login_cli.py <корпоративный логин или телефон>

  MYT_PASSWORD env — пароль (иначе спросит через getpass)"""


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if len(args) != 1:
        print(USAGE)
        return 1
    username = args[0]
    s = myt.MytSession()

    if os.environ.get("MYT_PASSWORD"):
        password = os.environ["MYT_PASSWORD"]
        print("[1/2] Пароль из MYT_PASSWORD env.")
    else:
        password = getpass.getpass("[1/2] Пароль (не отображается): ")

    # Первый вызов без кода — он и ЗАКАЗЫВАЕТ SMS. Сервер отвечает ошибкой
    # sms_required: это штатный шаг протокола, а не сбой.
    try:
        s.login(username, password)
        print("    SMS не потребовалась.")
    except MytApiError as e:
        if e.result_code != "sms_required":
            print(f"    ОШИБКА: {e}")
            return 1
        print(f"    SMS отправлена: {e.message}")
        code = getpass.getpass("[2/2] SMS-код: ")
        try:
            s.login(username, password, code)
        except MytApiError as e2:
            print(f"    ОШИБКА: {e2}")
            return 1

    srv._myt_session = s
    s._on_persist = lambda: srv._save_myt(s)
    try:
        srv._save_myt(s)
    except OSError as e:
        # Вход без записи на диск бесполезен: SMS уже потрачена, а MCP поднимет
        # пустоту. Это провал, а не «почти получилось».
        print(f"\n✗ Вход прошёл, но сессию НЕ удалось сохранить: {e}")
        print(f"  Проверь права на {os.path.dirname(srv._MYT_FILE)} и повтори.")
        return 1

    print(f"\n✓ ГОТОВО! Сессия сохранена: {srv._MYT_FILE} (права 0600).")
    print(f"  Сотрудник: {s.username}, токен живёт {s.expires_in} с и продлевается сам.")
    print("  Тулы: calendar_schedule, calendar_event, calendar_respond, calendar_cancel,")
    print("        parking_places, parking_book, office_bookings, myt_status,")
    print("        myt_refresh_session.")
    print("  Пароль НЕ передан агенту.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
