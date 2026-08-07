#!/usr/bin/env python3
"""Вход в MyT из клонированного репозитория.

Вся логика — в tbank_myt/login.py, чтобы она ставилась вместе с пакетом: на неё
ссылается каждый ответ «MYT SESSION EXPIRED», а файл, лежащий только в исходниках,
установленному MCP недоступен. Здесь остаётся подсказка про интерпретатор — её
первым делом и получают, запустив системным python3.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from tbank_myt.login import main
except ModuleNotFoundError as _e:
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

if __name__ == "__main__":
    sys.exit(main())
