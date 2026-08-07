"""Редактирование секретов перед логом и перед контекстом модели.

Файл — то, что осталось от банковского observability.py: там вторая половина писала
журнал попыток оплаты, а здесь платежей нет, и она была мертва целиком — ни одного
вызова emit() во всём репозитории. Оставлены три функции, которыми пользуются
server._err и trace.

Правило про номера карт тоже убрано, и не только потому, что карт тут нет: оно
съедало наши собственные идентификаторы. `00000000-0000-4000-8000-000000000012` —
обычный id встречи — превращался в «<card><card>», то есть трассировка теряла ровно
то, ради чего её читают.

Защита двойная и это намеренно: по ИМЕНИ ключа (_REDACT_KEY) и по ВИДУ значения
(_RE_*). Имя ловит то, что не опознать по форме, вид — то, что положили под
безобидным именем.
"""
import json
import re

_MAX_VAL = 300  # truncate any string value longer than this

# key fragments (case-insensitive substring) that mark a secret / PII field → redacted
_REDACT_KEY = (
    "token", "cookie", "authoriz", "password", "passwd", "pin", "otp", "sms",
    "address", "phone", "tel", "email", "e-mail", "account", "cardnum", "card",
    "pan", "cvv", "cvc", "cipher", "sessionid", "session_id", "sso", "secret",
    "bearer", "apikey", "api_key", "fingerprint", "deviceid", "device_id",
    "passport", "inn", "login", "credential",
)

# value patterns that look like a secret regardless of the key name
_RE_JWT = re.compile(r"eyJ[A-Za-z0-9_\-]{8,}(?:\.[A-Za-z0-9_\-]+){0,2}")
# any 40+ char base64/hex run — refresh_token (86), access_token (88), cipher_key (86),
# fingerprint blob (1333) — standalone OR embedded in a string. Short values
# (cart_hash=16, uuid=32, order_id=12 digits) are NOT matched.
_RE_BLOB = re.compile(r"[A-Za-z0-9+/=_\-]{40,}")

# Токен здесь ездит в заголовке, а не в query — но URL целиком попадает в текст
# ConnectionError и MaxRetryError, и однажды в него положат что-нибудь ещё. Чистка по
# ИМЕНИ параметра дешева и переживает смену транспорта, поэтому список оставлен
# шире, чем нужно сегодня.
_RE_QS_SECRET = re.compile(
    r"(?i)\b(sessionid|session_id|wuid|deviceid|olddeviceid|access_token|refresh_token|"
    r"id_token|client_assertion|fingerprint|code|pointer|phone)=[^&\s\"'<>]+")


def redact_text(s: str) -> str:
    """Scrub a free-text string (an exception message, a URL) before it reaches a
    log or the model's context. Safe on any input."""
    return _RE_BLOB.sub("<redacted-blob>",
        _RE_JWT.sub("<jwt>",
            _RE_QS_SECRET.sub(r"\1=<redacted>", str(s))))


def _is_sensitive_key(k: str) -> bool:
    kl = str(k).lower()
    return any(frag in kl for frag in _REDACT_KEY)


def _redact_value(v):
    """Recursively redact secrets/PII in a value and truncate long strings."""
    if isinstance(v, str):
        # If the string is itself JSON (e.g. a raw response dumped into an `err`
        # field), parse + redact its STRUCTURE so a secret under a sensitive key
        # inside the dump (cookie:"SSO=zzz", access_token:"eyJ…") is scrubbed —
        # the value patterns below can't see short values or key names in a string.
        try:
            parsed = json.loads(v)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            s = json.dumps(_redact_value(parsed), ensure_ascii=False)
            # Mark the cut like the plain-string branch below does — a JSON dump
            # severed at 300 chars is otherwise indistinguishable from a whole one.
            return s if len(s) <= _MAX_VAL else s[:_MAX_VAL] + "…<trunc>"
        v = redact_text(v)
        if len(v) > _MAX_VAL:
            v = v[:_MAX_VAL] + "…<trunc>"
        return v
    if isinstance(v, dict):
        return {k: ("<redacted>" if _is_sensitive_key(k) else _redact_value(val))
                for k, val in v.items()}
    if isinstance(v, list):
        return [_redact_value(x) for x in v][:50]
    return v
