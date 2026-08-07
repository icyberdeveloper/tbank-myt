"""Пакет установлен и работает ИЗ ЧУЖОГО КАТАЛОГА, а не только из репозитория.

Этот тест написан по следу настоящего отказа. После переименования пакета из
`src` в `tbank_myt` venv не переустановили, и путь редактируемой установки
остался вести на каталог, которого больше нет:

    MAPPING = {'ca.roots': '.../tbank-myt/ca/roots', 'src': '.../tbank-myt/src'}

При этом всё выглядело исправным. Из репозитория `python -m tbank_myt.server`
поднимался — каталог `tbank_myt/` лежал рядом и подхватывался через cwd. Весь
остальной набор тестов тоже проходил: он первой же строкой кладёт корень
репозитория в sys.path, то есть проверяет ИСХОДНИКИ и по построению не может
заметить, что установка сломана.

А MCP-клиент запускает сервер из домашнего каталога пользователя. Там импортировать
нечего, и единственное, что видит человек, — `Failed to connect: Connection closed`,
без единого слова о причине.

Поэтому здесь всё наоборот: sys.path не трогается, сервер поднимается настоящим
подпроцессом из временного каталога и с ним говорят по протоколу. Ровно то, что
делает клиент. Тест требует установленного пакета (`pip install -e .`) — это не
неудобство, а суть проверки: репозиторий без установки для MCP нерабочий.
"""
import json
import os
import subprocess
import sys
import tempfile

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  FAIL: {msg}")


def _speak_mcp(cwd, env):
    """Поднять сервер подпроцессом и провести рукопожатие MCP. Вернуть ответы."""
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "packaging-test", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    p = subprocess.run([sys.executable, "-m", "tbank_myt.server"],
                       input="\n".join(json.dumps(r) for r in requests) + "\n",
                       capture_output=True, text=True, cwd=cwd, env=env, timeout=60)
    out = {}
    for line in p.stdout.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("id") is not None:
            out[d["id"]] = d
    return p, out


def check_server_starts_from_a_foreign_directory():
    """Запуск из каталога, не имеющего отношения к репозиторию, — как у клиента."""
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ)
        # Ни один настоящий файл сессии или трассировки не должен быть затронут.
        env.update({"MYT_SESSION": os.path.join(tmp, "session.json"),
                    "MYT_TRACE_FILE": os.path.join(tmp, "calls.jsonl"),
                    "MYT_EVENTS": os.path.join(tmp, "events.jsonl")})
        # PYTHONPATH мог бы протащить корень репозитория и спрятать поломку.
        env.pop("PYTHONPATH", None)

        p, answers = _speak_mcp(tmp, env)

        if 1 not in answers:
            hint = (p.stderr or "").strip().splitlines()
            tail = hint[-1] if hint else "(stderr пуст)"
            check(False, "сервер не поднялся из чужого каталога — MCP-клиент увидит "
                         f"«Failed to connect». Причина: {tail}. "
                         "Обычно лечится: .venv/bin/pip install -e .")
            return

        info = answers[1].get("result", {}).get("serverInfo", {})
        check(info.get("name") == "myt",
              f"сервер должен представляться как myt, а не {info.get('name')!r}")
        check(2 in answers, "сервер не ответил на tools/list")
        tools = answers.get(2, {}).get("result", {}).get("tools", [])
        names = {t["name"] for t in tools}
        expected = {"myt_status", "myt_refresh_session", "calendar_schedule",
                    "calendar_event", "calendar_respond", "calendar_cancel",
                    "parking_places", "parking_book", "office_bookings"}
        missing = expected - names
        check(not missing, f"тулы не доехали до клиента: {sorted(missing)}")
        # Лишний тул — тоже дефект: значит, в установке чужой пакет.
        check(not (names - expected), f"неожиданные тулы: {sorted(names - expected)}")
        print(f"  сервер поднялся из {tmp}: myt, {len(names)} тулов")


def check_pinned_root_travels_with_the_package():
    """Корень Минцифры должен лежать ВНУТРИ пакета, иначе вход умрёт после установки.

    Без него `magentbep.tcsbank.ru` не проходит проверку сертификата, то есть не
    работает ни вход, ни продление сессии. Каталог `ca/` рядом с исходниками при
    установке никуда не едет — проверяем именно установленное расположение.
    """
    with tempfile.TemporaryDirectory() as tmp:
        code = ("import os, json\n"
                "from tbank_myt import tls\n"
                "roots = tls.load_roots()\n"
                "print(json.dumps({'dir': tls.ROOTS_DIR, 'exists': os.path.isdir(tls.ROOTS_DIR),"
                " 'roots': len(roots)}))\n")
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, cwd=tmp, env=env, timeout=60)
        if p.returncode != 0:
            check(False, f"пакет не импортируется из чужого каталога: {p.stderr.strip()[-300:]}")
            return
        d = json.loads(p.stdout.strip().splitlines()[-1])
        check(d["exists"], f"каталог корней не доехал до установки: {d['dir']}")
        check(d["roots"] >= 1,
              "приколотых корней в установленном пакете нет — вход и продление "
              f"сессии сломаются на TLS ({d['dir']})")
        print(f"  приколотых корней в установленном пакете: {d['roots']}")


def main():
    for fn in (check_server_starts_from_a_foreign_directory,
               check_pinned_root_travels_with_the_package):
        print(f"{fn.__name__}:")
        fn()
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED")
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
