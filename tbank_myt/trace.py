"""Every tool call, recorded — so it can be seen HOW an agent uses this MCP.

Ни один сервис не расскажет, КАК им пользуются: тулы, до которых агент не дошёл,
аргументы, которые он угадал неверно, и отказы, которые он прочитал и повторил, не
оставляют следа нигде. Этот модуль — единственное место, где это видно.

WHAT IS RECORDED, per call: the tool name, its arguments (redacted, see below), how
long it took, whether the error path was taken, how big the answer was, and the FIRST
LINE of that answer. That last field is the important one: the tools return strings,
and a refusal — «MYT SESSION EXPIRED», «Назови ДЕНЬ вхождения», «Место NNN НЕ
забронировано» — is a perfectly ordinary return value. The first line is what the agent
actually read, and grouping by it later shows which messages agents run into.

WHAT IS **NOT** CLASSIFIED HERE. It would be easy to label each call ok / empty /
refused by matching the answer against a table of strings. That table would rot the
first time a message is reworded, and it would rot silently, turning a real failure
into a green count. So this module records faithfully and `report()` groups at read
time — the messages come out of the data, not out of a list somebody maintained.

SAFETY. Values go through redact._redact_value (by key name and by value
pattern), then are truncated. Arguments that are free text a person wrote or a
secret — a comment to a meeting organiser, a car plate, a password — are never
stored at all, only their length.

The argument tuple is also reduced to one short digest, so two identical calls in a
row can be spotted without keeping what was in them. That digest is keyed with a
random per-process value that is never written down. It has to be: an unkeyed hash
of a four-digit PIN is the PIN, and putting one next to the redacted field would
hand back exactly what the redaction removed.

ON BY DEFAULT, because a debugging aid that has to be switched on before the
interesting thing happens is not one. `MYT_TRACE=0` turns it off; the file is
capped and rotated so it cannot grow without bound.

    ~/.local/share/tbank-myt/calls.jsonl     (MYT_TRACE_FILE overrides)
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid

from .redact import _is_sensitive_key, _redact_value, redact_text

TRACE_FILE = os.environ.get(
    "MYT_TRACE_FILE",
    os.path.expanduser("~/.local/share/tbank-myt/calls.jsonl"),
)
MAX_BYTES = int(os.environ.get("MYT_TRACE_MAX_BYTES", 5 * 1024 * 1024))

# Arguments never stored, only measured. Free text a human wrote is theirs, and a
# credential is a credential — neither says anything about HOW the agent uses the
# tool, which is the whole point here.
#
# `comment` is the note that goes to the meeting organiser: a person wrote it for
# another person, and it is exactly the class `description` is opaque for.
# `car_number` matches no redaction rule by name or by shape — a plate has no run of
# four digits — and it identifies someone in a place where a log cannot help them: a
# parking barrier. See the note next to _RE_PLATE below for why it is nevertheless
# printed in the tool's ANSWER.
_OPAQUE_ARGS = {"text", "description", "password", "pin", "otp", "code", "body",
                "save_to", "comment", "car_number"}

# Tools whose ANSWER contains text a person wrote — the message just sent, the chat
# history, the preview of the last message in each conversation. Blanking the
# argument is not enough when the tool echoes it straight back: calendar_respond
# returns the comment it just sent to the organiser. For these the first line is
# replaced by its length — unless the call FAILED, in which case the first line is an
# error string from _err(), which is already redacted and is the thing worth keeping.
# calendar_respond echoes the comment the user wrote for the organiser, and the
# calendar tools print MEETING TITLES — written by colleagues, about work that is
# not this repository's to record.
_ECHOES_USER_TEXT = {
    # Названия встреч и повестку пишут ДРУГИЕ люди, участники — тоже люди, а
    # myt_status и myt_refresh_session печатают корпоративный логин. Ни одному из
    # этого не место в calls.jsonl дословно. На ОШИБКЕ голова сохраняется: там
    # первая строка — уже отредактированный текст из _err, и она единственное, ради
    # чего в этот файл потом заглядывают.
    "calendar_schedule", "calendar_event", "calendar_respond", "calendar_cancel",
    "myt_status", "myt_refresh_session",
}

# Тулов, называющих получателя денег, здесь нет: этот MCP денег не двигает.
_NAMES_A_COUNTERPARTY: set[str] = set()

_MAX_ARG = 64
_HEAD = 160

# Long digit runs — account, card, order and payment ids — are replaced in the stored
# first line. Two reasons, and they point the same way: this file is meant to be as
# shareable as events.jsonl, which promises never to carry account numbers; and the
# report GROUPS by that line, so a per-account id turns one recurring message into a
# crowd of singletons. Short numbers («229 всего, показано 50») survive.
_RE_LONG_ID = re.compile(r"\d{4,}")

# Госномер: буква, 3 цифры, 2 буквы, регион. Ни один прогон цифр в нём не длиннее
# трёх, поэтому _RE_LONG_ID его не видит, и redact_text тоже — там блобы, карты и
# JWT. А `car_number` уже объявлен непрозрачным аргументом (см. _OPAQUE_ARGS), и
# без этой строки та защита обходится сама собой: parking_book и office_bookings
# печатают номер, ПРОЧИТАННЫЙ С СЕРВЕРА, — то есть он попадает в calls.jsonl даже
# в тех вызовах, где агент номер вообще не передавал. Обе раскладки, потому что
# workplacer принимает кириллицу и возвращает транслит.
_RE_PLATE = re.compile(r"\b(?:[АВЕКМНОРСТУХ]\d{3}[АВЕКМНОРСТУХ]{2}"
                       r"|[ABEKMHOPCTYX]\d{3}[ABEKMHOPCTYX]{2})\d{2,3}\b")
# Заметь асимметрию: в ОТВЕТЕ тула номер печатается намеренно (пользователь обязан
# видеть, на какую машину бронь), а здесь маскируется. Это не противоречие, а разная
# цена ошибки: ответ читает владелец в своей сессии, а calls.jsonl лежит на диске,
# переживает сессию и задуман пригодным к тому, чтобы им поделиться.

# One id per server process. An MCP server is started per agent session, so this is
# the closest thing to "one agent's run" that exists without inventing a protocol.
RUN_ID = uuid.uuid4().hex[:8]
_seq = 0

# Set by server._err() on the way out, so "this call failed" is a FACT reported by
# the error path itself rather than a guess made by matching the returned string.
_last_error: dict = {}


def enabled() -> bool:
    return os.environ.get("MYT_TRACE", "1") not in ("0", "false", "no", "")


def note_error(exc: BaseException) -> None:
    """Called from the one place every tool funnels its failures through."""
    _last_error["cls"] = type(exc).__name__
    # The bank's result_code, when there is one. Not a secret — it is an error
    # taxonomy — and it is what turns «something failed» into a searchable fact.
    note_code(getattr(exc, "result_code", ""))


# The key for args_hash. Random per process and NEVER written anywhere — not into
# the record, not to disk, not to a log.
#
# The digest used to be a bare sha256 of the arguments, which undid the redaction
# standing next to it: `pin` was stored as "<4 chars>" while the hash beside it
# committed to the actual four digits. A PIN has 10 000 possible values and an SMS
# code a million, so anyone holding the file recovers both by counting — measured at
# 0.05 s and 2.4 s. A truncated hash of a low-entropy secret is that secret.
#
# Salting is enough because of what the digest is FOR: report() compares it between
# ADJACENT rows of ONE run, to notice an agent calling the same tool with the same
# arguments twice in a row. That never needs to be reproducible outside the process
# that wrote it, and never compares across runs. Hashing the sanitised dict instead
# would also stop the leak, but would make two DIFFERENT PINs collide and report a
# repeat that never happened.
_ARGS_HASH_KEY = secrets.token_bytes(32)


def _short_args(args: dict) -> tuple[dict, str]:
    """(what is safe to store, a per-run handle on what was really passed)."""
    canon = json.dumps({k: str(v) for k, v in sorted(args.items())},
                       ensure_ascii=False, sort_keys=True)
    digest = hmac.new(_ARGS_HASH_KEY, canon.encode("utf-8"),
                      hashlib.sha256).hexdigest()[:12]
    out: dict = {}
    for k, v in args.items():
        if k in _OPAQUE_ARGS:
            out[k] = f"<{len(str(v))} chars>" if v not in ("", None) else ""
            continue
        # The key decides, not just the value. _redact_value only consults
        # _is_sensitive_key when it is handed a DICT, and this loop unpacks the
        # arguments before calling it — so the name never reached the blocklist and
        # every phone, account and card id passed as an argument was stored verbatim,
        # in the one file this module promises is safe to share (see the header).
        # The answer's first line was scrubbed by _RE_LONG_ID all along; the
        # arguments were not.
        if _is_sensitive_key(k):
            out[k] = "<redacted>"
            continue
        red = _redact_value(v)
        s = red if isinstance(red, (int, float, bool)) or red is None else str(red)
        if isinstance(s, str) and len(s) > _MAX_ARG:
            s = s[:_MAX_ARG] + "…"
        out[k] = s
    return out, digest


def _rotate() -> None:
    try:
        if os.path.exists(TRACE_FILE) and os.path.getsize(TRACE_FILE) > MAX_BYTES:
            os.replace(TRACE_FILE, TRACE_FILE + ".1")
    except OSError:
        pass


def _append(rec: dict) -> None:
    _rotate()
    os.makedirs(os.path.dirname(TRACE_FILE), exist_ok=True)
    fd = os.open(TRACE_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    try:
        os.chmod(TRACE_FILE, 0o600)
    except OSError:
        pass


# Проставляется server._err() рядом с классом исключения. Класс говорит, что
# сломалось у НАС; код сервиса — что ответил он. Без кода отдельным полем причина
# остаётся только прозой внутри отредактированного `head`, а по прозе потом ничего
# не найти: «http_403» от kairos и «sms_required» от авторизации сольются в одну
# неразличимую строку отчёта.
def note_code(code: str) -> None:
    if code:
        _last_error["code"] = str(code)[:64]


def record(tool: str, args: dict, started: float, result, error: str | None) -> None:
    """Never raises: a tracer that can break the thing it traces is worse than none."""
    global _seq
    try:
        if not enabled():
            return
        _seq += 1
        text = result if isinstance(result, str) else ""
        head = ""
        if text.strip():
            if tool in _ECHOES_USER_TEXT and not error:
                head = f"<{len(text)} chars, содержимое не записывается>"
            elif tool in _NAMES_A_COUNTERPARTY and not error:
                head = "<успех, получатель не записывается>"
            else:
                first = redact_text(text.strip().splitlines()[0])
                head = _RE_LONG_ID.sub("#", _RE_PLATE.sub("<госномер>", first))[:_HEAD]
        safe, digest = _short_args(args)
        _append({
            "ts": round(started, 3), "run": RUN_ID, "seq": _seq, "tool": tool,
            "args": safe, "args_hash": digest,
            "ms": int((time.time() - started) * 1000),
            "err": error, "err_code": _last_error.pop("code", None),
            "chars": len(text), "head": head,
        })
    except Exception:                                        # noqa: BLE001
        pass


def wrap(fn):
    """Record one call of `fn`. functools.wraps keeps __wrapped__, which is what
    inspect.signature follows — so FastMCP builds the SAME schema and description it
    would have built for the undecorated function. Pinned by a test, because a
    silently changed schema would change what every agent sees."""
    name = fn.__name__

    def _bind(a, kw) -> dict:
        try:
            import inspect
            bound = inspect.signature(fn).bind_partial(*a, **kw)
            return dict(bound.arguments)
        except (TypeError, ValueError):
            return dict(kw)

    if asyncio.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def awrapper(*a, **kw):
            started = time.time()
            _last_error.pop("cls", None)
            try:
                out = await fn(*a, **kw)
            except BaseException as e:                       # noqa: BLE001
                record(name, _bind(a, kw), started, "", type(e).__name__)
                raise
            record(name, _bind(a, kw), started, out, _last_error.pop("cls", None))
            return out
        return awrapper

    @functools.wraps(fn)
    def swrapper(*a, **kw):
        started = time.time()
        _last_error.pop("cls", None)
        try:
            out = fn(*a, **kw)
        except BaseException as e:                           # noqa: BLE001
            record(name, _bind(a, kw), started, "", type(e).__name__)
            raise
        record(name, _bind(a, kw), started, out, _last_error.pop("cls", None))
        return out

    return swrapper


# ── reading the trace back ───────────────────────────────────────────────────

def load(path: str | None = None, runs: int = 0) -> list[dict]:
    """Every recorded call, oldest first. `runs=N` keeps only the last N runs —
    a run being one server process, i.e. roughly one agent session."""
    path = path or TRACE_FILE
    rows: list[dict] = []
    for p in (path + ".1", path):
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    if runs > 0:
        order, seen = [], set()
        for r in rows:                       # first-seen order, not set order
            if r.get("run") not in seen:
                seen.add(r.get("run"))
                order.append(r.get("run"))
        keep = set(order[-runs:])
        rows = [r for r in rows if r.get("run") in keep]
    return rows


def _pct(values: list[int], p: float) -> int:
    if not values:
        return 0
    s = sorted(values)
    return s[min(len(s) - 1, int(len(s) * p))]


def report(rows: list[dict], top: int = 6) -> dict:
    """Turn raw calls into the four questions worth asking of them.

    Deliberately NOT a health score. Every number here points at specific rows a
    person can go and read; a single «87% healthy» would hide exactly the rare call
    that broke a run."""
    per: dict = {}
    heads: dict = {}
    transitions: dict = {}
    repeats: list[dict] = []
    starts: dict = {}
    by_run: dict = {}

    for r in rows:
        by_run.setdefault(r.get("run"), []).append(r)

    for run, calls in by_run.items():
        calls.sort(key=lambda x: x.get("seq", 0))
        if calls:
            starts[calls[0].get("tool")] = starts.get(calls[0].get("tool"), 0) + 1
        run_len = 1
        for i, c in enumerate(calls):
            tool = c.get("tool", "?")
            st = per.setdefault(tool, {"n": 0, "err": 0, "ms": [], "chars": 0})
            st["n"] += 1
            st["ms"].append(int(c.get("ms") or 0))
            st["chars"] += int(c.get("chars") or 0)
            if c.get("err"):
                st["err"] += 1
            if c.get("head"):
                heads.setdefault(tool, {})
                heads[tool][c["head"]] = heads[tool].get(c["head"], 0) + 1
            if i + 1 < len(calls):
                key = (tool, calls[i + 1].get("tool", "?"))
                transitions[key] = transitions.get(key, 0) + 1
            # The same tool called with the same arguments, back to back, is an
            # agent that did not understand the answer it got.
            same = (i + 1 < len(calls)
                    and calls[i + 1].get("tool") == tool
                    and calls[i + 1].get("args_hash") == c.get("args_hash"))
            if same:
                run_len += 1
            else:
                if run_len > 1:
                    repeats.append({"run": run, "tool": tool, "times": run_len,
                                    "head": c.get("head", "")})
                run_len = 1

    tools = []
    for tool, st in sorted(per.items(), key=lambda kv: -kv[1]["n"]):
        tools.append({
            "tool": tool, "n": st["n"], "err": st["err"],
            "p50_ms": _pct(st["ms"], 0.5), "p95_ms": _pct(st["ms"], 0.95),
            "avg_chars": st["chars"] // max(1, st["n"]),
            "answers": sorted(heads.get(tool, {}).items(), key=lambda kv: -kv[1])[:top],
        })
    return {
        "runs": len(by_run), "calls": len(rows), "tools": tools,
        "repeats": sorted(repeats, key=lambda x: -x["times"])[:top * 2],
        "transitions": sorted(transitions.items(), key=lambda kv: -kv[1])[:top * 2],
        "starts": sorted(starts.items(), key=lambda kv: -kv[1]),
    }
