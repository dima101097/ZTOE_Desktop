"""Вибір адреси (РЕМ → населений пункт → вулиця → будинок) для вікна налаштувань.

Сайт ztoe.com.ua не має API: кожен вибір — це запит тієї ж сторінки з параметрами
rem_id / naspunkt_id / vulica_id, а відповідь містить наступний список і таблицю
«Будинки | Черга | Підчерга». Вибір запам'ятовується в config.json → "address".
"""
from __future__ import annotations

import html
import json
import re
import sqlite3
import threading
import time
import urllib.parse

from tkinter import ttk

import ztoe_notifier as zn

REMS = {
    "Андрушівський": 1, "Баранівський": 2, "Бердичівський": 3, "Брусилівський": 4,
    "Житомирський": 7, "Звягельський": 16, "Коростенський": 9, "Коростишівський": 11,
    "Любарський": 13, "Малинський": 14, "Овруцький": 17, "Олевський": 18,
    "Попільнянський": 19, "Пулинський": 23, "Радомишльський": 20, "Романівський": 21,
    "Хорошівський": 5, "Черняхівський": 24, "Чуднівський": 25,
}

_ROW_RE = re.compile(r"<tr>\s*((?:<td[^>]*>.*?</td>\s*){7})</tr>", re.S | re.I)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)


def _clean(s: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s)).split())


def _house_key(h: str):
    """Природне сортування номерів будинків: 2 < 10 < 10А."""
    m = re.match(r"(\d+)", h)
    return (0, int(m.group(1)), h) if m else (1, 0, h)


def query(**params) -> str:
    params = {k: v for k, v in params.items() if v}
    params["all"] = 1
    return zn.fetch(f"{zn.BASE_URL}?{urllib.parse.urlencode(params)}")


# ---------------------------------------------------------------- кеш ---
# Списки населених пунктів, вулиць і будинків змінюються рідко, тож зберігаються в базі (таблиця
# address_cache у address_cache.db) на CACHE_TTL секунд. Без бази кеш лише в пам'яті.
CACHE_DB = None                                  # CacheDB або None
CACHE_TTL = zn.DEFAULT_ADDRESS_CACHE_HOURS * 3600
_memory: dict[str, tuple[float, object]] = {}


class CacheDB:
    """Маленька SQLite-база лише для кешу адрес (у десктопній версії немає бази бота)."""

    def __init__(self, path) -> None:
        self.con = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.con.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.run("CREATE TABLE IF NOT EXISTS address_cache(key TEXT PRIMARY KEY, ts REAL NOT NULL, data TEXT NOT NULL)")

    def one(self, sql: str, args=()):
        with self.lock:
            return self.con.execute(sql, args).fetchone()

    def run(self, sql: str, args=()) -> sqlite3.Cursor:
        with self.lock:
            return self.con.execute(sql, args)


def configure_cache(db, ttl_seconds: int) -> None:
    global CACHE_DB, CACHE_TTL
    CACHE_DB, CACHE_TTL = db, int(ttl_seconds)


def _cached(key: str, fn):
    """Значення з кешу, якщо свіже; інакше з сайту. Якщо сайт не відповідає — старе значення з кешу."""
    stale = None
    if CACHE_DB is not None:
        row = CACHE_DB.one("SELECT ts, data FROM address_cache WHERE key=?", (key,))
        hit = (row["ts"], json.loads(row["data"])) if row else None
    else:
        hit = _memory.get(key)
    if hit:
        if time.time() - hit[0] < CACHE_TTL:
            return hit[1]
        stale = hit[1]
    try:
        value = fn()
    except Exception:
        if stale is not None:
            return stale
        raise
    if CACHE_TTL > 0:
        if CACHE_DB is not None:
            CACHE_DB.run("INSERT OR REPLACE INTO address_cache(key, ts, data) VALUES(?, ?, ?)",
                         (key, time.time(), json.dumps(value, ensure_ascii=False)))
        else:
            _memory[key] = (time.time(), value)
    return value


def clear_cache() -> int:
    """Очищає кеш адрес; повертає кількість видалених записів."""
    n = len(_memory)
    _memory.clear()
    if CACHE_DB is not None:
        n += CACHE_DB.run("DELETE FROM address_cache").rowcount
    return n


def cache_info() -> dict:
    if CACHE_DB is not None:
        row = CACHE_DB.one("SELECT COUNT(*) AS n, MIN(ts) AS oldest FROM address_cache")
        n, oldest = row["n"], row["oldest"]
    else:
        n, oldest = len(_memory), min((t for t, _ in _memory.values()), default=None)
    return {"entries": n, "ttl_hours": CACHE_TTL // 3600,
            "oldest": time.strftime("%d.%m %H:%M", time.localtime(oldest)) if oldest else None}


def settlements(rem_id: int) -> list:
    """[(назва, naspunkt_id)] для РЕМ."""
    return _cached(f"np:{rem_id}", lambda: options(query(rem_id=rem_id), "naspunkt_id"))


def streets(rem_id: int, np_id: int) -> dict:
    """{"streets": [(назва, vulica_id)], "rows": рядки будинків, якщо вулиць немає (село)}."""
    def load():
        page = query(rem_id=rem_id, naspunkt_id=np_id, vulica_id=0)
        found = options(page, "vulica_id")
        return {"streets": found, "rows": [] if found else address_rows(page)}
    return _cached(f"st:{rem_id}:{np_id}", load)


def houses(rem_id: int, np_id: int, st_id: int) -> list[dict]:
    """Рядки таблиці «будинки | черга | підчерга» для вулиці."""
    return _cached(f"h:{rem_id}:{np_id}:{st_id}",
                   lambda: address_rows(query(rem_id=rem_id, naspunkt_id=np_id, vulica_id=st_id)))


def options(page: str, name: str) -> list[tuple[str, int]]:
    m = re.search(rf'<select[^>]*name="{name}"[^>]*>(.*?)</select>', page, re.S | re.I)
    if not m:
        return []
    out = []
    for val, text in re.findall(r'<option[^>]*value="(\d+)"[^>]*>(.*?)</option>', m.group(1), re.S | re.I):
        if val != "0":
            out.append((_clean(text), int(val)))
    return out


def address_rows(page: str) -> list[dict]:
    """Рядки таблиці результатів: будинки, підчерга ("4.1" або None), примітка."""
    rows = []
    for m in _ROW_RE.finditer(page):
        cells = [_clean(c) for c in _TD_RE.findall(m.group(1))]
        if len(cells) != 7 or cells[0] == "РЕМ":
            continue
        _, _, street, houses, q, s, note = cells
        sub = f"{q}.{s}" if q.isdigit() and s.isdigit() else None
        rows.append({"street": street, "houses": houses, "sub": sub,
                     "queue_text": "" if sub else q, "note": note})
    return rows


class _Filtered(ttk.Combobox):
    """Combobox, у якому можна друкувати частину назви для фільтрації."""

    def __init__(self, master, on_pick, **kw):
        super().__init__(master, **kw)
        self.items: list[tuple[str, object]] = []
        self.on_pick = on_pick
        self.bind("<KeyRelease>", self._filter)
        self.bind("<<ComboboxSelected>>", self._picked)
        self.bind("<Return>", self._picked)

    def set_items(self, items):
        self.items = items
        self["values"] = [t for t, _ in items]
        self.set("")
        self.state(["!disabled"] if items else ["disabled"])

    def _filter(self, event):
        if event.keysym in ("Up", "Down", "Return", "Escape", "Tab"):
            return
        typed = self.get().lower()
        self["values"] = [t for t, _ in self.items if typed in t.lower()] or [t for t, _ in self.items]

    def _picked(self, _event=None):
        text = self.get()
        for t, v in self.items:
            if t == text:
                self.on_pick(t, v)
                return
        vals = self["values"]
        if len(vals) == 1:  # введено частину назви, що збігається з одним пунктом
            self.set(vals[0])
            self._picked()

    def select_value(self, value) -> bool:
        for t, v in self.items:
            if v == value:
                self.set(t)
                return True
        return False


def build(app, parent) -> None:
    saved = dict(app.cfg.get("address") or {})
    box = ttk.LabelFrame(parent, text="Пошук підчерги за адресою", padding=8)
    box.pack(fill="x")
    status = ttk.Label(box, text="", foreground="#555")

    def row(r, label, widget):
        ttk.Label(box, text=label).grid(row=r, column=0, sticky="w", padx=(0, 8), pady=2)
        widget.grid(row=r, column=1, sticky="we", pady=2)

    def busy(text):
        status.config(text=text)

    def run(fn, done):
        def work():
            try:
                res, err = fn(), None
            except Exception as e:  # noqa: BLE001
                res, err = None, str(e)
            app.after(0, lambda: busy(f"Помилка: {err}") if err else (busy(""), done(res)))
        threading.Thread(target=work, daemon=True).start()

    def remember(**kw):
        saved.update(kw)
        app.cfg["address"] = dict(saved)

    def pick_rem(name, rem_id, restore=False):
        if not restore:
            remember(rem=name, rem_id=rem_id, naspunkt=None, naspunkt_id=None, vulica=None, vulica_id=None, house=None)
        np_box.set_items([]); st_box.set_items([]); house_box.set_items([])
        busy("Завантажую населені пункти…")

        def done(items):
            np_box.set_items([tuple(x) for x in items])
            if restore and saved.get("naspunkt_id") and np_box.select_value(saved["naspunkt_id"]):
                pick_np(np_box.get(), saved["naspunkt_id"], restore=True)
        run(lambda: settlements(rem_id), done)

    def pick_np(name, np_id, restore=False):
        if not restore:
            remember(naspunkt=name, naspunkt_id=np_id, vulica=None, vulica_id=None, house=None)
        st_box.set_items([]); house_box.set_items([])
        busy("Завантажую вулиці…")

        def done(res):
            st_box.set_items([tuple(x) for x in res["streets"]])
            if not res["streets"] and res["rows"]:  # село без вулиць: одразу будинки
                show_houses(res["rows"], restore)
            elif restore and saved.get("vulica_id") and st_box.select_value(saved["vulica_id"]):
                pick_street(st_box.get(), saved["vulica_id"], restore=True)
        run(lambda: streets(saved["rem_id"], np_id), done)

    def pick_street(name, st_id, restore=False):
        if not restore:
            remember(vulica=name, vulica_id=st_id, house=None)
        house_box.set_items([])
        busy("Завантажую будинки…")

        run(lambda: houses(saved["rem_id"], saved["naspunkt_id"], st_id), lambda rows: show_houses(rows, restore))

    def show_houses(rows, restore=False):
        items = []
        for r in rows:
            where = r["sub"] and f"підчерга {r['sub']}" or "не вимикається"
            for h in [h.strip() for h in r["houses"].split(",") if h.strip()] or ["вся вулиця"]:
                items.append((f"{h} — {where} ({r['note']})", {"house": h, **r}))
        # побутові споживачі першими
        items.sort(key=lambda it: (not it[1]["note"].startswith("побут"), _house_key(it[1]["house"])))
        house_box.set_items(items)
        if restore and saved.get("house"):
            house_box.select_value(next((v for t, v in items if t == saved["house"]), None))
        if not items:
            busy("Для цієї вулиці немає даних")

    def pick_house(text, info):
        remember(house=text)
        if info["sub"]:
            app.var_sub.set(info["sub"])
            app.redraw()
            busy(f"Ваша підчерга {info['sub']}. Натисніть «Зберегти».")
        else:
            busy(info["queue_text"] or "Цей будинок не вимикається за графіком")

    rem_box = _Filtered(box, lambda t, v: pick_rem(t, v), width=48)
    rem_box.set_items(list(REMS.items()))
    np_box = _Filtered(box, lambda t, v: pick_np(t, v), width=48)
    st_box = _Filtered(box, lambda t, v: pick_street(t, v), width=48)
    house_box = _Filtered(box, pick_house, width=48)
    for w in (np_box, st_box, house_box):
        w.state(["disabled"])
    row(0, "РЕМ:", rem_box)
    row(1, "Населений пункт:", np_box)
    row(2, "Вулиця:", st_box)
    row(3, "Будинок:", house_box)
    status.grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))

    if saved.get("rem_id") and rem_box.select_value(saved["rem_id"]):
        pick_rem(rem_box.get(), saved["rem_id"], restore=True)
