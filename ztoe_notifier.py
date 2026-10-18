#!/usr/bin/env python3
"""Сповіщення про відключення світла за графіком Житомиробленерго (ztoe.com.ua).

Скрипт читає таблицю погодинних відключень на сторінці
https://www.ztoe.com.ua/unhooking-search.php для вашої підчерги (наприклад 3.2)
і надсилає в Telegram:
  * новий або змінений графік на сьогодні/завтра;
  * нагадування за N хвилин до відключення та до включення світла.

Запускайте його кожні 5 хвилин (Планувальник завдань Windows або cron).
Потрібен лише Python 3.9+, сторонні бібліотеки не потрібні.

Приклади:
  python ztoe_notifier.py                 # звичайний запуск (за config.json)
  python ztoe_notifier.py --dry-run       # нічого не надсилати, лише друкувати
  python ztoe_notifier.py --show          # показати поточний графік своєї підчерги
  python ztoe_notifier.py --html page.html --show   # розібрати збережену сторінку
  python ztoe_notifier.py --test-telegram # перевірити бота
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import logging.handlers
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

__version__ = "1.2.0"

BASE_URL = "https://www.ztoe.com.ua/unhooking-search.php"
# у зібраному .exe налаштування лежать поруч з .exe, а не у тимчасовій папці
HERE = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
# DATA_DIR (змінна оточення) дозволяє тримати налаштування окремо від коду
DATA_DIR = Path(os.environ["DATA_DIR"]) if os.environ.get("DATA_DIR") else HERE
CONFIG_PATH = DATA_DIR / "config.json"
STATE_PATH = DATA_DIR / "state.json"
LOG_PATH = DATA_DIR / "ztoe.log"

log = logging.getLogger("ztoe")


def setup_logging(debug: bool = False, to_file: bool = True) -> None:
    """Журнал у консоль і в DATA_DIR/ztoe.log (1 МБ × 3 файли); debug=True пише подробиці."""
    if not log.handlers:
        fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        log.addHandler(console)
        if to_file:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
            fh.setFormatter(fmt)
            log.addHandler(fh)
        log.propagate = False
    set_debug(debug)


def set_debug(debug: bool) -> None:
    log.setLevel(logging.DEBUG if debug else logging.INFO)

# Підчерга -> pidcherga_id у посиланнях на сайті (1.1 -> 1, 1.2 -> 2, ... 6.2 -> 12)
SUBQUEUE_IDS = {f"{q}.{s}": (q - 1) * 2 + s for q in range(1, 7) for s in (1, 2)}

SLOT_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*[-–]\s*(\d{1,2}):(\d{2})\s*$")
DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
SUBQUEUE_RE = re.compile(r"^\s*([1-6])\.([12])\s*$")


# ---------------------------------------------------------------- parsing ---

class _TableParser(HTMLParser):
    """Збирає всі таблиці сторінки як списки рядків з комірками (текст + атрибути)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[dict]]] = []
        self._stack: list[list[list[dict]]] = []
        self._row: list[dict] | None = None
        self._cell: dict | None = None
        self._inner_styles: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "table":
            self._stack.append([])
        elif tag == "tr" and self._stack:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = {"text": "", "attrs": a, "inner": []}
        elif self._cell is not None:
            # колір іноді задається на вкладеному <div>/<span>
            self._cell["inner"].append(a)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._cell["text"] = " ".join(self._cell["text"].split())
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None and self._stack:
            if self._row:
                self._stack[-1].append(self._row)
            self._row = None
        elif tag == "table" and self._stack:
            self.tables.append(self._stack.pop())

    def handle_data(self, data):
        if self._cell is not None:
            self._cell["text"] += data


_RED_WORDS = ("red", "off", "danger", "outage", "vidkl", "disable", "black", "grey", "gray")
_WHITE = {"#fff", "#ffffff", "white", "transparent", "rgb(255,255,255)", "none", "inherit", ""}


def _colour_values(attrs: dict) -> list[str]:
    vals = []
    if attrs.get("bgcolor"):
        vals.append(attrs["bgcolor"].strip().lower())
    style = attrs.get("style", "").lower()
    for m in re.finditer(r"background(?:-color)?\s*:\s*([^;]+)", style):
        vals.append(m.group(1).strip().replace(" ", ""))
    return vals


def _is_reddish(colour: str) -> bool:
    c = colour.replace("!important", "").strip()
    if c in _WHITE:
        return False
    if c in ("red", "darkred", "crimson", "tomato", "orangered", "firebrick"):
        return True
    m = re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6})", c)
    if m:
        h = m.group(1)
        if len(h) == 3:
            h = "".join(ch * 2 for ch in h)
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        return r > 150 and g < 140 and b < 140
    m = re.match(r"rgba?\((\d+),(\d+),(\d+)", c)
    if m:
        r, g, b = map(int, m.groups())
        return r > 150 and g < 140 and b < 140
    return False


def cell_is_off(cell: dict) -> bool:
    """True, якщо комірка позначена як відключення (червона)."""
    for attrs in [cell["attrs"], *cell["inner"]]:
        if any(_is_reddish(v) for v in _colour_values(attrs)):
            return True
        cls = attrs.get("class", "").lower()
        if any(w in cls for w in _RED_WORDS):
            return True
    return False


def parse_schedule(page_html: str) -> dict[str, dict[str, list[bool]]]:
    """Повертає {"дд.мм.рррр": {"3.2": [48 значень True=відключення, ...]}}."""
    p = _TableParser()
    p.feed(page_html)
    result: dict[str, dict[str, list[bool]]] = {}
    for table in p.tables:
        # рядки з часом: зазвичай один на всіх, але буває два — верхній для x.1, нижній для x.2
        headers: list[list[tuple[int, int]]] = []
        header_idx, date = None, None
        for i, row in enumerate(table):
            times = [_minutes(c["text"]) for c in row if SLOT_RE.match(c["text"])]
            if len(times) >= 24:
                headers.append(times)
                header_idx = i
                for c in row:
                    m = DATE_RE.search(c["text"])
                    if m:
                        date = date or m.group(0)
            elif headers and any(SUBQUEUE_RE.match(c["text"]) for c in row):
                break  # почалися рядки підчерг
        if header_idx is None:
            continue
        first_header = header_idx - len(headers) + 1
        if date is None:
            # дата може бути в окремому рядку над заголовком
            for row in table[:first_header]:
                for c in row:
                    m = DATE_RE.search(c["text"])
                    if m:
                        date = m.group(0)
        date = date or dt.date.today().strftime("%d.%m.%Y")
        for row in table[header_idx + 1:]:
            sub_pos = next((j for j, c in enumerate(row) if SUBQUEUE_RE.match(c["text"])), None)
            if sub_pos is None:
                continue
            sub = row[sub_pos]["text"].strip()
            times = headers[1] if sub.endswith(".2") and len(headers) > 1 else headers[0]
            # останні комірки рядка — це стовпці часу з відповідного заголовка
            cells = row[-len(times):] if len(row) >= len(times) else []
            if len(cells) != len(times):
                continue
            result.setdefault(date, {})[sub] = _to_slots(times, [cell_is_off(c) for c in cells])
    return result


def _minutes(text: str) -> tuple[int, int]:
    """"14:30-15:00" -> (870, 900); кінець 00:00/24:00 вважається кінцем доби."""
    h1, m1, h2, m2 = map(int, SLOT_RE.match(text).groups())
    start, end = h1 * 60 + m1, h2 * 60 + m2
    return start, end if end > start else 24 * 60


def _to_slots(times: list[tuple[int, int]], offs: list[bool]) -> list[bool]:
    """Стовпці з довільним часом -> 48 півгодинних слотів (слот вимкнено, якщо його зачіпає вимкнений стовпець)."""
    slots = [False] * 48
    for (start, end), off in zip(times, offs):
        if off:
            for i in range(start // 30, min(48, -(-end // 30))):
                slots[i] = True
    return slots


# ----------------------------------------------------------- formatting ---

def slot_time(i: int) -> str:
    m = i * 30
    return f"{m // 60:02d}:{m % 60:02d}"


def intervals(slots: list[bool]) -> list[tuple[int, int]]:
    """Перетворює 48 слотів на інтервали відключень [(start_slot, end_slot_exclusive)]."""
    out, start = [], None
    for i, off in enumerate(slots + [False]):
        if off and start is None:
            start = i
        elif not off and start is not None:
            out.append((start, i))
            start = None
    return out


ABOUT = ("«Відключення світла» стежить за графіком погодинних відключень Житомиробленерго "
         "(ztoe.com.ua) для вашої адреси та підчерги. Показує графік на сьогодні й завтра, "
         "попереджає про вимкнення і включення в Telegram і сповіщеннями Windows, "
         "може показати вікно зі звуком і вимкнути ПК перед відключенням.")


def slot_end(b: int) -> str:
    return slot_time(b) if b < 48 else "24:00"


def fmt_left(delta: dt.timedelta) -> str:
    """Скільки лишилося: «2 год 05 хв», «25 хв», «менше хвилини»."""
    mins = int(delta.total_seconds() // 60)
    if mins < 1:
        return "менше хвилини"
    d, rest = divmod(mins, 24 * 60)
    h, m = divmod(rest, 60)
    if d:
        return f"{d} д {h} год"
    return f"{h} год {m:02d} хв" if h else f"{m} хв"


def address_text(cfg: dict) -> str:
    """«місто Житомир, вул. Небесної Сотні, 28» зі збереженої адреси (порожньо, якщо не обрано)."""
    a = cfg.get("address") or {}
    house = (a.get("house") or "").split(" — ")[0].strip()
    return ", ".join(p for p in (a.get("naspunkt"), a.get("vulica"), house) if p)


def next_change(schedule: dict, sub: str, now: dt.datetime):
    """(відключення зараз?, ("off"|"on", час) найближчої зміни або None, чи є графік на сьогодні)."""
    today = schedule.get(now.strftime("%d.%m.%Y"), {}).get(sub)
    off_now = bool(today and today[(now.hour * 60 + now.minute) // 30])
    nxt = next(((k, t) for k, t in transitions(schedule, sub) if t > now), None)
    return off_now, nxt, today is not None


def format_day(date: str, sub: str, slots: list[bool]) -> str:
    iv = intervals(slots)
    if not iv:
        return f"📅 {date}, підчерга {sub}: відключень не заплановано ✅"
    lines = [f"📅 {date}, підчерга {sub}: можливі відключення"]
    for a, b in iv:
        lines.append(f"  🔴 {slot_time(a)} – {slot_time(b) if b < 48 else '24:00'}")
    return "\n".join(lines)


# -------------------------------------------------------------- network ---

def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ztoe-notifier/1.0",
        "Accept-Language": "uk-UA,uk;q=0.9",
    })
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        charset = r.headers.get_content_charset()
    log.debug("GET %s -> %d байт за %.1f с", url, len(raw), time.monotonic() - t0)
    for enc in filter(None, (charset, "utf-8", "cp1251")):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


SILENT_MODES = {"none": "усе зі звуком", "schedule": "графік беззвучно", "all": "усе беззвучно"}


QUIET_DEFAULT = ("22:00", "07:00")   # нічні години, які пропонуються для беззвучного режиму
_HHMM = re.compile(r"^\s*([01]?\d|2[0-3])[:.]([0-5]\d)\s*$")


def parse_hhmm(text: str) -> str | None:
    m = _HHMM.match(str(text or ""))
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


def parse_quiet_hours(text: str) -> tuple[str, str] | None:
    """«23:00-06:30» / «23.00 – 6.30» -> ("23:00", "06:30")."""
    parts = re.split(r"\s*[-–—]\s*|\s+", str(text or "").strip())
    if len(parts) != 2:
        return None
    a, b = parse_hhmm(parts[0]), parse_hhmm(parts[1])
    return (a, b) if a and b and a != b else None


def in_quiet_hours(now: dt.datetime, start: str | None, end: str | None) -> bool:
    """Чи зараз у проміжку [start, end); проміжок може переходити через північ (22:00–07:00)."""
    a, b = parse_hhmm(start), parse_hhmm(end)
    if not a or not b:
        return True
    t = now.strftime("%H:%M")
    return a <= t < b if a < b else t >= a or t < b


def quiet_text(when: str, start: str | None, end: str | None) -> str:
    return "завжди" if when == "always" else f"з {start or QUIET_DEFAULT[0]} до {end or QUIET_DEFAULT[1]}"


def silent_now(mode: str, when: str, start, end, kind: str, now: dt.datetime | None = None) -> bool:
    """Беззвучно? mode: none | schedule | all; when: always | hours (з start до end); kind: schedule | alert."""
    if mode == "none" or mode == "schedule" and kind != "schedule":
        return False
    return when == "always" or in_quiet_hours(now or dt.datetime.now(), start, end)


def is_silent(cfg: dict, kind: str, now: dt.datetime | None = None) -> bool:
    """Беззвучно у ваш чат? kind: "schedule" — новий / змінений графік, "alert" — попередження."""
    return silent_now(cfg.get("telegram_silent", "none"), cfg.get("telegram_silent_when", "hours"),
                      cfg.get("telegram_silent_from", QUIET_DEFAULT[0]), cfg.get("telegram_silent_to", QUIET_DEFAULT[1]),
                      kind, now)


def send_telegram(cfg: dict, text: str, dry_run: bool, kind: str = "alert") -> None:
    if dry_run or not cfg.get("telegram_token"):
        print("---- повідомлення ----\n" + text + "\n----------------------")
        return
    url = f"https://api.telegram.org/bot{cfg['telegram_token']}/sendMessage"
    params = {"chat_id": cfg["telegram_chat_id"], "text": text}
    thread = thread_id(cfg)
    if thread:
        params["message_thread_id"] = thread
    if is_silent(cfg, kind):
        params["disable_notification"] = "true"
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30) as r:
        r.read()


def thread_id(cfg: dict) -> int | None:
    """ID гілки (теми) у групі з темами, якщо її увімкнено в налаштуваннях."""
    raw = str(cfg.get("telegram_thread_id") or "").strip()
    if cfg.get("telegram_thread_enabled") and raw.lstrip("-").isdigit():
        return int(raw)
    return None


def detect_chat(token: str) -> dict | None:
    """Чат і гілка з останнього повідомлення, яке отримав бот (getUpdates).

    Повертає {"chat_id", "thread_id" (або None), "title"} або None, якщо повідомлень немає.
    Не працює, якщо той самий бот уже запущено десь іще (вебхук або інший getUpdates).
    """
    url = f"https://api.telegram.org/bot{token}/getUpdates?" + urllib.parse.urlencode({"limit": 100, "timeout": 0})
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            desc = json.loads(e.read().decode("utf-8")).get("description", "")
        except Exception:  # noqa: BLE001
            desc = ""
        if e.code == 409:
            raise RuntimeError("цей бот уже працює деінде (вебхук або інша програма читає його повідомлення). "
                               "Зупиніть її або впишіть ID вручну.") from e
        if e.code in (401, 404):
            raise RuntimeError("невірний токен бота") from e
        raise RuntimeError(desc or str(e)) from e
    for upd in reversed(data.get("result", [])):
        msg = next((upd[k] for k in ("message", "edited_message", "channel_post", "edited_channel_post",
                                     "my_chat_member") if k in upd), None)
        if not msg or "chat" not in msg:
            continue
        chat = msg["chat"]
        title = chat.get("title") or " ".join(filter(None, (chat.get("first_name"), chat.get("last_name"))))             or chat.get("username") or str(chat["id"])
        thread = msg.get("message_thread_id") if msg.get("is_topic_message") else None
        return {"chat_id": chat["id"], "thread_id": thread, "title": title, "type": chat.get("type", "")}
    return None


# ----------------------------------------------------------------- main ---

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def lead_times(cfg: dict) -> tuple[int, int]:
    """За скільки хвилин попереджати про вимкнення і про включення (0 — не попереджати)."""
    base = int(cfg.get("remind_minutes_before", 15))
    off = int(cfg.get("remind_off_minutes", base)) if cfg.get("remind_off_enabled", True) else 0
    on = int(cfg.get("remind_on_minutes", base)) if cfg.get("remind_on_enabled", True) else 0
    return off, on


def transitions(schedule: dict, sub: str) -> list[tuple[str, dt.datetime]]:
    """Моменти зміни стану за графіком: [("off"|"on", час)], відключення через північ не розриваються."""
    out: list[tuple[str, dt.datetime]] = []
    state, prev_day = False, None
    for date in sorted(schedule, key=lambda d: dt.datetime.strptime(d, "%d.%m.%Y")):
        slots = schedule[date].get(sub)
        d = dt.datetime.strptime(date, "%d.%m.%Y")
        if slots is None:
            continue
        if prev_day is not None and d - prev_day != dt.timedelta(days=1):
            if state:
                out.append(("on", prev_day + dt.timedelta(days=1)))
            state = False
        for i, off in enumerate(slots):
            if off != state:
                out.append(("off" if off else "on", d + dt.timedelta(minutes=30 * i)))
                state = off
        prev_day = d
    if prev_day is not None and state:
        out.append(("on", prev_day + dt.timedelta(days=1)))
    return out


def event_text(kind: str, t: dt.datetime, sub: str) -> str:
    if kind == "off":
        return f"⚠️ О {t:%H:%M} можливе відключення світла (підчерга {sub})"
    return f"💡 О {t:%H:%M} очікується включення світла (підчерга {sub})"


def reminders(schedule: dict, sub: str, now: dt.datetime, lead_off: int, lead_on: int) -> list[tuple[str, str]]:
    """Події «вимкнення/увімкнення», до яких лишилося не більше lead_* хвилин: [(ключ, текст)]."""
    events = []
    for kind, t in transitions(schedule, sub):
        lead = lead_off if kind == "off" else lead_on
        mins = (t - now).total_seconds() / 60
        if lead > 0 and -5 <= mins <= lead:
            events.append((f"{kind}:{t.isoformat()}", event_text(kind, t, sub)))
    return events


DEFAULT_CHECK_MINUTES = 5       # як часто перечитувати графік на сайті
DEFAULT_ADDRESS_CACHE_HOURS = 24  # скільки зберігати списки населених пунктів / вулиць / будинків


def migrate_config(cfg: dict) -> dict:
    """Переводить налаштування попередніх версій на нові ключі (без втрати значень)."""
    if "remind_minutes_before" in cfg:  # до 1.2: одне значення для вимкнення й включення
        base = int(cfg.pop("remind_minutes_before"))
        cfg.setdefault("remind_off_minutes", base)
        cfg.setdefault("remind_on_minutes", base)
    if "check_interval_minutes" not in cfg and os.environ.get("CHECK_INTERVAL", "").isdigit():
        cfg["check_interval_minutes"] = max(1, int(os.environ["CHECK_INTERVAL"]) // 60)  # старе значення в секундах
    return cfg


NO_ADDRESS_TEXT = "адресу не вибрано — спершу додайте її на вкладці «Графік»"


def load_config(path: Path = None) -> dict:
    """config.json + змінні оточення (TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, SUBQUEUE) як запасні значення."""
    cfg = migrate_config(load_json(path or CONFIG_PATH, {}))
    for key, env in (("telegram_token", "TELEGRAM_TOKEN"), ("telegram_chat_id", "TELEGRAM_CHAT_ID"),
                     ("subqueue", "SUBQUEUE")):
        if not cfg.get(key) and os.environ.get(env):
            cfg[key] = os.environ[env]
    return cfg


def check_seconds(cfg: dict) -> int:
    """Період опитування сайту з налаштувань, секунд (1–240 хв)."""
    return max(1, min(240, int(cfg.get("check_interval_minutes") or DEFAULT_CHECK_MINUTES))) * 60


def address_cache_seconds(cfg: dict) -> int:
    """Скільки жити кешу адрес, секунд (0 — не кешувати)."""
    return max(0, min(24 * 30, int(cfg.get("address_cache_hours", DEFAULT_ADDRESS_CACHE_HOURS)))) * 3600


def load_schedule(sub: str) -> dict[str, dict[str, list[bool]]]:
    """Завантажує й розбирає графік з сайту (спершу сторінку підчерги, потім повну; sub="" — одразу повну)."""
    schedule = parse_schedule(fetch(f"{BASE_URL}?pidcherga_id={SUBQUEUE_IDS[sub]}")) if sub else {}
    if not any(sub in s for s in schedule.values()):
        schedule = parse_schedule(fetch(BASE_URL))
    return schedule



def load_schedule_retry(sub: str) -> dict | None:
    """load_schedule з кількома спробами (для запуску з Планувальника); None — сайт не відповідає."""
    for attempt in range(SITE_DOWN_AFTER):
        try:
            schedule = load_schedule(sub)
            if not schedule:
                raise RuntimeError("таблицю графіка на сторінці не знайдено")
            site_result(True)
            return schedule
        except Exception as e:  # noqa: BLE001
            site_result(False, str(e))
            if attempt + 1 < SITE_DOWN_AFTER:
                time.sleep(SITE_RETRY_SECONDS)
    return None


_shared = {"t": 0.0, "data": {}, "checked": 0.0}  # checked — коли сайт востаннє читали (вдало чи ні)
_shared_lock = threading.Lock()


def shared_schedule(max_age: float, force: bool = False) -> dict:
    """Графік усіх підчерг, спільний для веб-версії і бота: сайт читається не частіше, ніж раз на max_age с.

    Якщо сайт недоступний, повертає останній відомий графік (або кидає помилку, якщо його ще немає).
    """
    with _shared_lock:
        if not force and _shared["data"] and time.time() - _shared["t"] < max_age:
            return _shared["data"]
        _shared["checked"] = time.time()
        try:
            sched = parse_schedule(fetch(BASE_URL))
            if not sched:
                raise RuntimeError("таблицю графіка на сторінці не знайдено")
        except Exception as e:
            site_result(False, str(e))
            _shared["t"] = time.time() - max_age + 60  # повторити через хвилину, а не одразу
            if _shared["data"]:
                return _shared["data"]
            raise
        site_result(True)
        _shared.update(t=time.time(), data=sched)
        return sched


def shared_check_times(max_age: float) -> tuple[dt.datetime | None, dt.datetime | None]:
    """Коли графік на сайті перевіряли востаннє і коли перевірять наступного разу (None — ще не перевіряли)."""
    with _shared_lock:
        if not _shared["checked"]:
            return None, None
        nxt = max(_shared["t"] + max_age, time.time())
        return dt.datetime.fromtimestamp(_shared["checked"]), dt.datetime.fromtimestamp(nxt)


def fmt_check_time(t: dt.datetime | None, now: dt.datetime | None = None) -> str:
    """«16:58», для іншого дня — «16.10 16:58»."""
    if t is None:
        return "—"
    now = now or dt.datetime.now()
    return f"{t:%H:%M}" if t.date() == now.date() else f"{t:%d.%m %H:%M}"


# -------------------------------------------------------- стан сайту ---

SITE_DOWN_AFTER = 3   # стільки невдалих спроб поспіль — і сайт вважається непрацюючим
SITE_DOWN_TEXT = ("⚠️ Сайт ztoe.com.ua зараз не працює: графік не перевіряється, сповіщення призупинено.\n"
                  "Щойно доступ до сайту відновиться — я повідомлю.")
SITE_UP_TEXT = "✅ Роботу сайту ztoe.com.ua відновлено — графік і сповіщення знову працюють."
SITE_WAIT_TEXT = "Очікую відновлення роботи сайта"
SITE_RETRY_SECONDS = 20   # пауза між повторними спробами в межах одного запуску з Планувальника

_site = {"fails": 0, "error": ""}

NET_PROBES = (("1.1.1.1", 443), ("8.8.8.8", 53), ("api.telegram.org", 443))


def internet_ok(timeout: float = 3.0) -> bool:
    """True, якщо є з'єднання з інтернетом (вдалося під'єднатися хоча б до одного з NET_PROBES)."""
    for host, port in NET_PROBES:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def site_result(ok: bool, error: str = "") -> None:
    """Запам'ятовує результат чергового звернення до сайту."""
    if ok:
        if _site["fails"] >= SITE_DOWN_AFTER:
            log.info("сайт знову працює")
        _site.update(fails=0, error="")
    else:
        _site["fails"] += 1
        _site["error"] = error
        if _site["fails"] == SITE_DOWN_AFTER:
            log.warning("сайт не працює (%d спроби поспіль): %s", SITE_DOWN_AFTER, error)


def site_down() -> bool:
    """True, якщо сайт не відповідає кілька спроб поспіль."""
    return _site["fails"] >= SITE_DOWN_AFTER


def site_notice(state: dict, send) -> None:
    """Повідомляє (send(text, kind)) про падіння або відновлення сайту — один раз на кожну зміну.

    Позначка site_down зберігається в state, тож повідомлення не дублюються між запусками.
    Якщо надіслати не вдалося (кидає виняток), позначка не змінюється і спроба повториться.
    """
    down = site_down()
    if down == bool(state.get("site_down")):
        return
    send(SITE_DOWN_TEXT if down else SITE_UP_TEXT, "alert")
    state["site_down"] = down


def process(cfg: dict, schedule: dict, state: dict, now: dt.datetime, send) -> dict:
    """Надсилає новий/змінений графік і нагадування; повертає оновлений стан."""
    sub = str(cfg.get("subqueue") or "")
    lead_off, lead_on = lead_times(cfg)
    state.setdefault("days", {})
    # 1) новий або змінений графік
    for date in sorted(schedule, key=lambda d: dt.datetime.strptime(d, "%d.%m.%Y")):
        slots = schedule[date].get(sub)
        if slots is None:
            continue
        old = state["days"].get(date)
        if old != slots:
            if cfg.get("notify_schedule", True):
                prefix = "🆕 Графік оновлено" if old is not None else "📢 Графік"
                send(f"{prefix}\n{format_day(date, sub, slots)}", "schedule")
            state["days"][date] = slots
    # 2) нагадування перед вимкненням / увімкненням
    if lead_off > 0 or lead_on > 0:
        sent = set(state.get("sent", []))
        for key, txt in reminders(schedule, sub, now, lead_off, lead_on):
            if key not in sent:
                send(txt, "alert")
                sent.add(key)
        state["sent"] = sorted(sent)[-200:]
    # прибрати старі дні
    cutoff = (now - dt.timedelta(days=3)).date()
    state["days"] = {d: v for d, v in state["days"].items()
                     if dt.datetime.strptime(d, "%d.%m.%Y").date() >= cutoff}
    return state


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=CONFIG_PATH)
    ap.add_argument("--html", type=Path, help="розібрати локальний HTML замість завантаження")
    ap.add_argument("--dry-run", action="store_true", help="друкувати замість надсилання")
    ap.add_argument("--show", action="store_true", help="показати графік і вийти")
    ap.add_argument("--test-telegram", action="store_true", help="надіслати тестове повідомлення")
    ap.add_argument("--save-html", type=Path, help="зберегти завантажену сторінку у файл")
    args = ap.parse_args()

    cfg = load_config(args.config)
    sub = str(cfg.get("subqueue") or "")  # "" — адресу видалено: графік читаємо, сповіщень немає
    if sub and sub not in SUBQUEUE_IDS:
        print(f"Невідома підчерга {sub!r}; має бути від 1.1 до 6.2", file=sys.stderr)
        return 2

    if args.test_telegram and not sub:
        print(NO_ADDRESS_TEXT, file=sys.stderr)
        return 2
    if args.test_telegram:
        send_telegram(cfg, f"✅ Бот працює. Стежу за підчергою {sub}.", args.dry_run)
        return 0

    send = lambda text, kind: send_telegram(cfg, text, args.dry_run, kind)  # noqa: E731
    keep_state = not args.dry_run and cfg.get("telegram_token")  # без токена нічого не надіслано
    if args.html:
        schedule = parse_schedule(args.html.read_text(encoding="utf-8", errors="replace"))
    elif args.save_html:
        page = fetch(f"{BASE_URL}?pidcherga_id={SUBQUEUE_IDS[sub]}" if sub else BASE_URL)
        args.save_html.write_text(page, encoding="utf-8")
        schedule = parse_schedule(page)
        if not any(sub in s for s in schedule.values()):
            schedule = parse_schedule(fetch(BASE_URL))  # запасний варіант: повна таблиця
    else:
        if not internet_ok():  # без інтернету нічого не перевіряємо й не надсилаємо — чекаємо зв'язку
            log.info("немає інтернету — перевірку пропущено")
            print("Немає інтернету", file=sys.stderr)
            return 1
        schedule = load_schedule_retry(sub)
        if not args.show:
            state = load_json(STATE_PATH, {"days": {}, "sent": []})
            try:  # про падіння й відновлення сайту — у Telegram, якщо це увімкнено
                site_notice(state, send if cfg.get("telegram_site_down", True) else lambda _t, _k: None)
            except Exception as e:  # noqa: BLE001
                print(f"Telegram: {e}", file=sys.stderr)
            if schedule is None:  # сайт не працює: жодних нагадувань, доки не відновиться
                if keep_state:
                    save_json(STATE_PATH, state)
                print(f"Сайт не відповідає: {_site['error']}", file=sys.stderr)
                return 1
            if keep_state:
                save_json(STATE_PATH, state)
        elif schedule is None:
            print(f"Сайт не відповідає: {_site['error']}", file=sys.stderr)
            return 1
    if sub and not any(sub in s for s in schedule.values()):
        print(f"Не знайшов рядок підчерги {sub} у таблиці. Можливо, сайт змінив розмітку; "
              f"збережіть сторінку через --save-html і перевірте.", file=sys.stderr)
        return 1

    if args.show:
        for date in sorted(schedule, key=lambda d: dt.datetime.strptime(d, "%d.%m.%Y")):
            for s in [sub] if sub else sorted(schedule[date]):
                if s in schedule[date]:
                    print(format_day(date, s, schedule[date][s]))
        return 0
    if not sub:  # адресу не вибрано — лише графік, без сповіщень
        return 0

    state = process(cfg, schedule, load_json(STATE_PATH, {"days": {}, "sent": []}), dt.datetime.now(), send)
    if keep_state:
        save_json(STATE_PATH, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
