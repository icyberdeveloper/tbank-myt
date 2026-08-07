"""Вход в MyT — ВНЕ агента и вне LLM.

Пароль и SMS-код вводятся через getpass (не отображаются) или читаются из env
`MYT_PASSWORD`. В контекст модели они не попадают: это отдельный процесс, который
человек запускает сам, а тула, принимающего корпоративный пароль, в этом MCP нет и
не будет — рабочий пароль открывает не один сервис, а все рабочие системы разом.

    tbank-myt-login n.ivanov                      # после установки пакета
    .venv/bin/python login_cli.py n.ivanov        # из клонированного репозитория

После входа сессия лежит в ~/.local/share/tbank-myt/session.json (0600) и
продлевается сама; повторный вход нужен, только когда тул скажет MYT SESSION EXPIRED.
"""
import getpass
import os
import sys

from . import myt
from . import server as srv
from .errors import MytApiError


USAGE = """Usage:
  tbank-myt-login <корпоративный логин или телефон>
  .venv/bin/python login_cli.py <корпоративный логин или телефон>   (из клона)

  MYT_PASSWORD env — пароль (иначе спросит через getpass)"""


def main():
    # Ровно один позиционный аргумент и никаких флагов. Молча отбрасывать флаг
    # нельзя: опечатка вроде «--myt n.ivanov» иначе уходит в логин с «n.ivanov»
    # вместо имени и жжёт попытку SMS, ничего не сказав.
    args = sys.argv[1:]
    if len(args) != 1 or args[0].startswith("-"):
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
