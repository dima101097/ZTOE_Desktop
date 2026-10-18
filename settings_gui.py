#!/usr/bin/env python3
"""Відключення світла: вікно налаштувань і програма в треї.

Три рівні попереджень перед відключенням (кожен вмикається окремо):
  1. Telegram і сповіщення Windows;
  2. вікно поверх усього зі звуком;
  3. примусове вимкнення ПК, якщо вікно з п. 2 проігноровано.
Запуск: start.bat (одразу в трей) або `python settings_gui.py`.
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import ztoe_notifier as zn

try:  # трей потребує pystray і Pillow; без них вікно працює як звичайне
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    pystray = None

try:
    import winsound
except ImportError:  # не Windows
    winsound = None

TASK_NAME = "ZTOE power notifier"
TICK_MS = 15 * 1000          # як часто перевіряти, чи час попереджати
OFFLINE_RETRY_S = 30         # без інтернету — перевіряти зв'язок так часто, с
SITE_RETRY_S = 60            # сайт не відповідає — пробувати знову так часто, с
NET_TEXT = {  # стан зв'язку -> (заголовок, сповіщення Windows про втрату, про відновлення)
    "offline": ("Немає інтернету",
                "Немає інтернету. Графік не перевіряється, сповіщення й вимкнення ПК призупинено до відновлення зв'язку.",
                "Інтернет відновлено — перевірку графіка й сповіщення відновлено."),
    "site_down": ("Сайт ztoe.com.ua не відповідає",
                  "Сайт ztoe.com.ua не відповідає. Сповіщення й вимкнення ПК призупинено до відновлення роботи сайту.",
                  "Роботу сайту ztoe.com.ua відновлено — перевірку графіка й сповіщення відновлено."),
}
CELL_W, CELL_H = 16, 28
ALL_CELL_W, ALL_ROW_H, ALL_LABEL_W = 15, 16, 44   # графік усіх черг
SUBQUEUES = [f"{q}.{s}" for q in range(1, 7) for s in (1, 2)]

# палітра
BG, CARD, BORDER = "#f3f5f9", "#ffffff", "#e3e7ef"
TEXT, MUTED = "#1f2937", "#6b7280"
ACCENT, ACCENT_DARK = "#2563eb", "#1d4ed8"
RED, RED_SOFT, GREEN, GREEN_SOFT = "#dc2626", "#fde2e2", "#16a34a", "#dcfce7"
ACCENT_SOFT = "#e0e7ff"
FONT = "Segoe UI"

DEFAULTS = {
    "notify_schedule": True,
    "remind_off_enabled": True, "remind_off_minutes": 15,
    "remind_on_enabled": True, "remind_on_minutes": 15,
    "windows_notifications": True,
    "popup_enabled": True, "popup_minutes": 5,
    "shutdown_enabled": False, "shutdown_minutes": 1, "shutdown_countdown": 60,
    "check_interval_minutes": zn.DEFAULT_CHECK_MINUTES, "address_cache_hours": zn.DEFAULT_ADDRESS_CACHE_HOURS,
    "check_updates": True,
    "telegram_site_down": True,
}

def _mci(cmd: str) -> int:
    import ctypes
    return ctypes.windll.winmm.mciSendStringW(cmd, None, 0, None)


def play_file(path: str, loop: bool = False) -> bool:
    """Грає свій звук (MP3, WAV, WMA…) через Windows MCI. False, якщо не вдалося."""
    if sys.platform != "win32" or not path or not Path(path).is_file():
        return False
    stop_sound()
    if _mci(f'open "{path}" type mpegvideo alias ztoe_snd') != 0:
        return False
    return _mci("play ztoe_snd" + (" repeat" if loop else "")) == 0


def stop_sound() -> None:
    if sys.platform == "win32":
        _mci("close ztoe_snd")
    if winsound:
        winsound.PlaySound(None, winsound.SND_PURGE)


STARTUP_FILE = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup/ZTOE power.cmd"


FROZEN = getattr(sys, "frozen", False)  # запущено як зібраний .exe


def launch_cmd(*args: str) -> str:
    """Команда запуску цієї програми з аргументами (для Планувальника й автозапуску)."""
    if FROZEN:
        return " ".join([f'"{sys.executable}"', *args])
    return " ".join([f'"{pythonw()}"', f'"{zn.HERE / "settings_gui.py"}"', *args])


def pythonw() -> Path:
    pyw = Path(sys.executable).with_name("pythonw.exe")
    return pyw if pyw.exists() else Path(sys.executable)


def setup_style(root: tk.Tk) -> None:
    st = ttk.Style(root)
    st.theme_use("clam")
    root.configure(bg=BG)
    st.configure(".", background=BG, foreground=TEXT, font=(FONT, 10), bordercolor=BORDER)
    st.configure("TFrame", background=BG)
    st.configure("Card.TFrame", background=CARD, relief="solid", borderwidth=1)
    st.configure("TLabel", background=CARD, foreground=TEXT)
    st.configure("Status.TLabel", background=BG, foreground=MUTED)
    st.configure("Card.TLabel", background=CARD)
    st.configure("Muted.TLabel", background=CARD, foreground=MUTED, font=(FONT, 9))
    st.configure("Title.TLabel", background=CARD, font=(FONT, 11, "bold"))
    st.configure("Big.TLabel", background=CARD, font=(FONT, 15, "bold"))
    st.configure("TLabelframe", background=CARD, bordercolor=BORDER, relief="solid")
    st.configure("TLabelframe.Label", background=CARD, foreground=TEXT, font=(FONT, 10, "bold"))
    st.configure("TCheckbutton", background=CARD, font=(FONT, 10, "bold"))
    st.map("TCheckbutton", background=[("active", CARD)])
    st.configure("TRadiobutton", background=CARD)
    st.map("TRadiobutton", background=[("active", CARD)])
    st.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 4, 0, 0))
    st.configure("TNotebook.Tab", padding=(14, 7), background="#e8ecf4", foreground=MUTED, borderwidth=0)
    st.map("TNotebook.Tab", background=[("selected", CARD)], foreground=[("selected", ACCENT)])
    st.configure("TButton", padding=(12, 6), background="#eef1f7", borderwidth=1, focuscolor=BG)
    st.map("TButton", background=[("active", "#e2e7f1")])
    st.configure("Accent.TButton", background=ACCENT, foreground="white", bordercolor=ACCENT, focuscolor=ACCENT)
    st.map("Accent.TButton", background=[("active", ACCENT_DARK), ("pressed", ACCENT_DARK)])
    st.configure("Danger.TButton", background=RED, foreground="white", bordercolor=RED, focuscolor=RED)
    st.map("Danger.TButton", background=[("active", "#b91c1c")])
    st.configure("TEntry", fieldbackground="white", padding=4)
    st.configure("TSpinbox", fieldbackground="white", padding=3, arrowsize=12)
    st.configure("TCombobox", fieldbackground="white", padding=3)


def card(parent, **pack) -> ttk.Frame:
    f = ttk.Frame(parent, style="Card.TFrame", padding=14)
    f.pack(fill="x", **({"pady": (0, 10)} | pack))
    return f


def plain(parent, **pack) -> ttk.Frame:
    """Рамка без обведення на білому тлі картки."""
    f = ttk.Frame(parent, style="Card.TFrame", padding=0)
    f.configure(borderwidth=0)
    f.pack(**({"fill": "x"} | pack))
    return f


fmt_left, slot_end = zn.fmt_left, zn.slot_end
DAY_H = CELL_H + 70   # висота одного дня на шкалі: підписи періодів, клітинки, години
SILENT_LABELS = {"none": "🔔 усе зі звуком", "schedule": "🔕 графік беззвучно", "all": "🔇 усе беззвучно"}
QUIET_WHEN = {"hours": "у тихі години", "always": "завжди"}


class Scrolled(ttk.Frame):
    """Вкладка з вертикальною прокруткою: вміст кладеться в self.inner, висоту видимої частини задає App."""

    def __init__(self, master) -> None:
        super().__init__(master)
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0, borderwidth=0)
        # смуга прокрутки — лише коли вміст вищий за вкладку (ширину всіх вкладок тримає Notebook)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = ttk.Frame(self.canvas, padding=(0, 10, 0, 0))
        self._win = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._sync)
        self.canvas.bind("<Configure>", self._sync)

    def _sync(self, _event=None) -> None:
        c = self.canvas
        c.itemconfigure(self._win, width=c.winfo_width())
        c.configure(scrollregion=(0, 0, c.winfo_width(), max(self.inner.winfo_reqheight(), c.winfo_height())))
        need = self.inner.winfo_reqheight() > c.winfo_height() + 1
        if need and not self.bar.winfo_manager():
            self.bar.pack(side="right", fill="y", padx=(6, 0), before=c)
        elif not need and self.bar.winfo_manager():
            self.bar.pack_forget()
            c.yview_moveto(0)

    def scroll(self, units: int) -> None:
        if self.inner.winfo_reqheight() > self.canvas.winfo_height():
            self.canvas.yview_scroll(units, "units")


class Tip:
    """Маленька підказка біля курсора (час клітинки чи шкали)."""

    def __init__(self, master) -> None:
        self.master, self.win, self.text = master, None, ""

    def show(self, text: str, x: int, y: int) -> None:
        if self.win is None:
            self.win = tk.Toplevel(self.master)
            self.win.overrideredirect(True)
            self.win.attributes("-topmost", True)
            self.lbl = tk.Label(self.win, bg="#111827", fg="white", font=(FONT, 9), padx=8, pady=3)
            self.lbl.pack()
        if text != self.text:
            self.text = text
            self.lbl.config(text=text)
        self.win.geometry(f"+{x + 14}+{y + 16}")
        self.win.deiconify()

    def hide(self, _event=None) -> None:
        if self.win is not None:
            self.win.withdraw()
        self.text = ""


def draw_scale(c: tk.Canvas, slots: list[bool], y: int, cw: float, ch: int, x0: float = 0,
               now_x: float | None = None, now_text: str = "", labels: bool = True) -> None:
    """Шкала доби: підписи періодів над червоними блоками, клітинки по годинах, поділки й час під ними.

    y — верх смуги підписів періодів; клітинки починаються на 16 px нижче (або одразу, якщо labels=False).
    """
    width = 48 * cw
    top = y + 16 if labels else y
    if labels:  # «14:30–16:00» над кожним відключенням
        for a, b in zn.intervals(slots):
            cx = x0 + (a + b) / 2 * cw
            text = f"{zn.slot_time(a)}–{slot_end(b)}"
            half = len(text) * 3
            cx = min(max(cx, x0 + half), x0 + width - half)
            past = now_x is not None and x0 + b * cw <= now_x
            c.create_text(cx, y + 6, text=text, font=(FONT, 8, "bold"), fill=MUTED if past else RED)
    for i, off in enumerate(slots):
        x = x0 + i * cw
        # пара півгодин = година: всередині години проміжок 1 px, між годинами — 3 px
        c.create_rectangle(x + (3 if i % 2 == 0 else 1), top, x + cw, top + ch,
                           fill=RED if off else GREEN_SOFT, width=0)
    base = top + ch + 2
    c.create_line(x0, base, x0 + width, base, fill=BORDER)
    for h in range(25):  # поділки щогодини, довші й підписані — кожні 2 години
        x = x0 + h * 2 * cw
        major = h % 2 == 0
        c.create_line(x, base, x, base + (6 if major else 3), fill=MUTED if major else BORDER)
        if major and labels or h % 3 == 0 and not labels:
            if now_x is not None and abs(x - now_x) < 24:
                continue  # не перекривати мітку «зараз»
            anchor = "nw" if h == 0 else "ne" if h == 24 else "n"
            c.create_text(x, base + 7, anchor=anchor, text=f"{h:02d}:00", font=(FONT, 7), fill=MUTED)
    if now_x is not None:
        c.create_line(now_x, top - 4, now_x, base + 6, fill=ACCENT, width=3)
        if now_text:
            c.create_rectangle(now_x - 17, base + 6, now_x + 17, base + 19, fill=ACCENT, width=0)
            c.create_text(now_x, base + 12, text=now_text, font=(FONT, 7, "bold"), fill="white")


class Alarm(tk.Toplevel):
    """Вікно поверх усього зі звуком, що повторюється, поки його не закриють."""

    def __init__(self, master, title: str, text: str, button: str, on_close=None, countdown: int = 0,
                 on_timeout=None, colour: str = RED, sound: str = ""):
        super().__init__(master)
        self.on_close, self.on_timeout, self.left = on_close, on_timeout, countdown
        self.sound = sound
        self.overrideredirect(False)
        self.title("Відключення світла")
        self.configure(bg=colour)
        self.attributes("-topmost", True)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.close)
        tk.Label(self, text="⚡", font=(FONT, 44), bg=colour, fg="white").pack(pady=(18, 0))
        tk.Label(self, text=title, font=(FONT, 18, "bold"), bg=colour, fg="white").pack(padx=40)
        tk.Label(self, text=text, font=(FONT, 12), bg=colour, fg="white", justify="center",
                 wraplength=460).pack(padx=40, pady=(8, 6))
        self.lbl_count = tk.Label(self, text="", font=(FONT, 34, "bold"), bg=colour, fg="white")
        if countdown:
            self.lbl_count.pack()
        tk.Button(self, text=button, font=(FONT, 12, "bold"), bg="white", fg=colour, relief="flat",
                  activebackground="#f3f4f6", padx=24, pady=8, command=self.close).pack(pady=(10, 22))
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        self.geometry(f"+{(self.winfo_screenwidth() - w) // 2}+{(self.winfo_screenheight() - h) // 3}")
        self.lift()
        self.focus_force()
        self._alive = True
        # свій звук грає по колу, поки вікно відкрите; якщо не вийшло — стандартний
        self.custom = play_file(self.sound, loop=True)
        self._beep()
        if countdown:
            self._count()

    def _beep(self):
        if not self._alive:
            return
        if not winsound:
            self.bell()
        elif not self.custom:
            winsound.PlaySound("SystemHand", winsound.SND_ALIAS | winsound.SND_ASYNC)
        self.attributes("-topmost", True)
        self.lift()
        self.after(2500, self._beep)

    def _count(self):
        if not self._alive:
            return
        self.lbl_count.config(text=f"{self.left} с")
        if self.left <= 0:
            self._alive = False
            stop_sound()
            self.destroy()
            if self.on_timeout:
                self.on_timeout()
            return
        self.left -= 1
        self.after(1000, self._count)

    def close(self):
        if not self._alive:
            return
        self._alive = False
        stop_sound()
        self.destroy()
        if self.on_close:
            self.on_close()


class App(tk.Tk):
    def __init__(self, start_hidden: bool = False) -> None:
        super().__init__()
        setup_style(self)
        self.title("Відключення світла")
        if pystray is not None:  # своя іконка у вікні й на панелі задач
            from PIL import ImageTk
            self._app_icon = ImageTk.PhotoImage(self._icon(False).resize((32, 32)))
            self.iconphoto(True, self._app_icon)
        self.resizable(False, False)
        self.cfg = DEFAULTS | zn.migrate_config(zn.load_json(zn.CONFIG_PATH, {}))
        self._setup_address_cache()
        self.schedule: dict[str, dict[str, list[bool]]] = {}
        self.toasted: set[str] = set()
        self.popups: dict[str, bool] = {}     # ключ події -> чи натиснули «Зрозуміло»
        self.popup_shown_at: dict[str, dt.datetime] = {}
        self.shutdowns: set[str] = set()
        self.tray = None
        self.net = "ok"   # "ok" | "offline" — немає інтернету | "site_down" — сайт не відповідає

        self.var_sub = tk.StringVar(value=str(self.cfg.get("subqueue", "")))
        self.var_token = tk.StringVar(value=self.cfg.get("telegram_token", ""))
        self.var_chat = tk.StringVar(value=str(self.cfg.get("telegram_chat_id", "")))
        self.var_thread_on = tk.BooleanVar(value=bool(self.cfg.get("telegram_thread_enabled", False)))
        self.var_thread = tk.StringVar(value=str(self.cfg.get("telegram_thread_id", "") or ""))
        self.var_silent = tk.StringVar(value=SILENT_LABELS.get(self.cfg.get("telegram_silent", "none"),
                                                               SILENT_LABELS["none"]))
        self.var_q_when = tk.StringVar(value=QUIET_WHEN[self.cfg.get("telegram_silent_when", "hours")])
        self.var_q_from = tk.StringVar(value=self.cfg.get("telegram_silent_from", zn.QUIET_DEFAULT[0]))
        self.var_q_to = tk.StringVar(value=self.cfg.get("telegram_silent_to", zn.QUIET_DEFAULT[1]))
        self.tip = Tip(self)
        self._hits: dict = {}   # canvas -> [(x0, y1, y2, ширина клітинки, слоти, дата, підчерга)]
        self._last_minute = None
        self.var_status = tk.StringVar(value="")
        self.var_sound = tk.StringVar(value=self.cfg.get("popup_sound", ""))
        self.v = {k: (tk.BooleanVar if isinstance(d, bool) else tk.IntVar)(value=self.cfg.get(k, d))
                  for k, d in DEFAULTS.items()}
        self.var_autostart = tk.BooleanVar(value=STARTUP_FILE.exists())

        outer = ttk.Frame(self, padding=14)
        outer.pack(fill="both", expand=True)
        self._build_header(outer)
        nb = self.nb = ttk.Notebook(outer)
        nb.pack(fill="both", expand=True)
        self.tabs: list[Scrolled] = []
        self._build_schedule_tab(nb)
        self._build_alerts_tab(nb)
        self._build_settings_tab(nb)
        # ширина — за найширшою вкладкою, висота — за головною («Графік»); решта прокручується
        self.update_idletasks()
        width = max(t.inner.winfo_reqwidth() for t in self.tabs)
        for t in self.tabs:
            t.canvas.configure(width=width)
        self._fitted = False
        self._fit_height()
        self.bind_all("<MouseWheel>", self._wheel)

        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", pady=(10, 0))
        ttk.Button(bottom, text="💾  Зберегти", style="Accent.TButton", command=self.save).pack(side="left")
        ttk.Label(bottom, textvariable=self.var_status, style="Status.TLabel").pack(side="left", padx=12)

        self.last_check: dt.datetime | None = None
        self.next_check: dt.datetime | None = None
        self._auto_id = None
        self.refresh()
        self.after(TICK_MS, self._tick)
        self._setup_tray(start_hidden)
        threading.Thread(target=self.ensure_task, daemon=True).start()
        if STARTUP_FILE.exists():  # програму могли перенести — автозапуск має вести на поточне місце
            self.toggle_autostart()
        self._updating = False
        if self.cfg.get("check_updates", True):
            self.after(3000, self.check_updates)

    # ================================================================ UI
    def _build_header(self, parent) -> None:
        f = card(parent)
        self.dot = tk.Canvas(f, width=54, height=54, bg=CARD, highlightthickness=0)
        self.dot.pack(side="left", padx=(0, 14))
        txt = ttk.Frame(f, style="Card.TFrame", padding=0)
        txt.configure(borderwidth=0)
        txt.pack(side="left", fill="x", expand=True)
        self.lbl_state = ttk.Label(txt, text="Завантажую графік…", style="Big.TLabel")
        self.lbl_state.pack(anchor="w")
        self.lbl_next = ttk.Label(txt, text="", style="Muted.TLabel")
        self.lbl_next.pack(anchor="w")
        self.lbl_checks = ttk.Label(txt, text="", style="Muted.TLabel")
        self.lbl_checks.pack(anchor="w")
        self.lbl_badge = tk.Label(f, text="", bg="#e0e7ff", fg=ACCENT, font=(FONT, 11, "bold"), padx=12, pady=4)
        self.lbl_badge.pack(side="right")

    def _tab(self, nb, title) -> ttk.Frame:
        t = Scrolled(nb)
        nb.add(t, text=title)
        self.tabs.append(t)
        return t.inner

    def _fit_height(self) -> None:
        """Висота вкладок — як у головної сторінки (без розгорнутого графіка всіх черг під своєю підчергою)."""
        self.update_idletasks()
        h = self.tabs[0].inner.winfo_reqheight()
        if self.var_sub.get() and self.all_open:
            h -= self.all_box.winfo_reqheight() + 6
        chrome = self.winfo_reqheight() - self.tabs[0].canvas.winfo_reqheight()  # шапка, ярлики вкладок, кнопки
        h = max(240, min(int(h * 1.1), self.winfo_screenheight() - chrome - 80))  # +10% запасу
        for t in self.tabs:
            t.canvas.configure(height=h)

    def _wheel(self, e) -> None:
        """Колесо миші прокручує відкриту вкладку (крім полів, які самі реагують на колесо)."""
        w = e.widget
        if isinstance(w, str) or isinstance(w, (ttk.Spinbox, ttk.Combobox)) or w.winfo_toplevel() is not self:
            return
        self.nametowidget(self.nb.select()).scroll(-3 if e.delta > 0 else 3)

    def _build_schedule_tab(self, nb) -> None:
        f = card(self._tab(nb, "📅  Графік"))
        top = plain(f)
        self.lbl_title = ttk.Label(top, text="Графік на сьогодні й завтра", style="Title.TLabel")
        self.lbl_title.pack(side="left")
        ttk.Button(top, text="⟳  Оновити з сайту", command=self.refresh).pack(side="right")

        # адреса й черга
        info = plain(f, pady=(8, 0))
        self.lbl_address = ttk.Label(info, text="", style="Card.TLabel", wraplength=520, justify="left")
        self.lbl_address.pack(side="left")
        self.btn_del_addr = ttk.Button(info, text="🗑", width=3, command=self.delete_address)
        self.btn_add_addr = ttk.Button(info, text="➕  Додати адресу", style="Accent.TButton", command=self.add_address)
        self.lbl_sub_badge = tk.Label(info, text="", bg=ACCENT_SOFT, fg=ACCENT, font=(FONT, 9, "bold"), padx=8, pady=2)
        self.lbl_queue_badge = tk.Label(info, text="", bg=ACCENT_SOFT, fg=ACCENT, font=(FONT, 9, "bold"), padx=8, pady=2)
        self.info_row = info
        self._mode = None  # чи вибрано підчергу — для перемикання вигляду

        # власна підчерга; без адреси замість неї одразу графік усіх черг
        p = self.personal = plain(f)
        # скільки лишилося до вимкнення / включення
        self.countdown = tk.Frame(p, bg=GREEN_SOFT, highlightthickness=1, highlightbackground=GREEN)
        self.countdown.pack(fill="x", pady=(10, 0))
        self.lbl_count_title = tk.Label(self.countdown, text="", bg=GREEN_SOFT, fg=GREEN, font=(FONT, 10))
        self.lbl_count_title.pack(side="left", padx=(12, 6), pady=8)
        self.lbl_count_value = tk.Label(self.countdown, text="", bg=GREEN_SOFT, fg=GREEN, font=(FONT, 15, "bold"))
        self.lbl_count_value.pack(side="left", pady=4)
        self.lbl_count_at = tk.Label(self.countdown, text="", bg=GREEN_SOFT, fg=MUTED, font=(FONT, 10))
        self.lbl_count_at.pack(side="right", padx=12)

        self.canvas = tk.Canvas(p, width=48 * CELL_W + 2, height=2 * (CELL_H + 40), bg=CARD, highlightthickness=0)
        self.canvas.pack(pady=(10, 4))
        self.canvas.bind("<Motion>", self._hover)
        self.canvas.bind("<Leave>", self.tip.hide)
        legend = plain(p, fill="none", anchor="w")
        for colour, label in ((RED, "можливе відключення"), (GREEN_SOFT, "світло є"), (ACCENT, "зараз")):
            tk.Canvas(legend, width=12, height=12, bg=colour, highlightthickness=0).pack(side="left", padx=(0, 4))
            ttk.Label(legend, text=label, style="Muted.TLabel").pack(side="left", padx=(0, 14))
        self.summary = plain(p, pady=(10, 0))

        # спойлер з графіком усіх черг
        self.var_all_date = tk.StringVar()
        self.all_open = False
        self.btn_all = tk.Label(p, text="", bg=CARD, fg=ACCENT, font=(FONT, 10, "bold"), cursor="hand2")
        self.btn_all.pack(anchor="w", pady=(8, 0))
        self.btn_all.bind("<Button-1>", lambda _e: self.toggle_all())
        self.all_box = plain(f, pady=(6, 0))
        self.all_box.pack_forget()
        self.all_dates = plain(self.all_box)
        self.all_canvas = tk.Canvas(self.all_box, width=ALL_LABEL_W + 48 * ALL_CELL_W + 2,
                                    height=len(SUBQUEUES) * ALL_ROW_H + 24, bg=CARD, highlightthickness=0)
        self.all_canvas.pack(pady=(6, 0), anchor="w")
        self.all_canvas.bind("<Motion>", self._hover)
        self.all_canvas.bind("<Leave>", self.tip.hide)
        self._update_all_button()

    def _hover(self, e) -> None:
        """Підказка з часом: над клітинкою — її півгодина і стан, над лінійкою — точний час під курсором."""
        for x0, y1, y2, cw, slots, date, sub in self._hits.get(e.widget, []):
            if not (y1 <= e.y < y2 and x0 <= e.x < x0 + 48 * cw):
                continue
            if slots is None:
                mins = min(24 * 60, max(0, round((e.x - x0) / cw * 30 / 5) * 5))
                text = f"{mins // 60:02d}:{mins % 60:02d}"
            else:
                i = int((e.x - x0) // cw)
                state = "можливе відключення" if slots[i] else "світло є"
                text = (f"{sub}  " if sub else "") + f"{zn.slot_time(i)}–{slot_end(i + 1)}  ·  {state}"
            return self.tip.show(f"{self._day_label(date) or date}, {text}", e.x_root, e.y_root)
        self.tip.hide()

    def toggle_all(self) -> None:
        self.all_open = not self.all_open
        if self.all_open:
            self.all_box.pack(fill="x", pady=(6, 0))
            self.draw_all()
        else:
            self.all_box.pack_forget()
        self._update_all_button()

    def _update_all_button(self) -> None:
        self.btn_all.config(text="▾  Сховати графік усіх черг" if self.all_open else "▸  Показати графік усіх черг")

    def add_address(self) -> None:
        """Вікно вибору адреси (або підчерги вручну); «Зберегти» одразу записує її в config.json."""
        old_sub, old_addr = self.var_sub.get(), dict(self.cfg.get("address") or {})
        win = tk.Toplevel(self)
        win.title("Додати адресу")
        win.configure(bg=BG)
        win.resizable(False, False)
        win.transient(self)
        body = ttk.Frame(win, padding=14)
        body.pack(fill="both", expand=True)
        f = card(body)
        try:
            import address_picker
            address_picker.build(self, f)
        except ImportError:
            ttk.Label(f, text="Файл address_picker.py не знайдено", style="Muted.TLabel").pack()
        g = card(body)
        ttk.Label(g, text="Або оберіть підчергу вручну", style="Title.TLabel").pack(anchor="w", pady=(0, 6))
        grid = plain(g, fill="none", anchor="w")

        def manual():
            self.cfg["address"] = {}  # підчерга без адреси
            self.redraw()

        for q in range(1, 7):
            for s in (1, 2):
                name = f"{q}.{s}"
                ttk.Radiobutton(grid, text=name, value=name, variable=self.var_sub,
                                command=manual).grid(row=s - 1, column=q - 1, padx=8, pady=3)

        def cancel():
            self.var_sub.set(old_sub)
            self.cfg["address"] = old_addr
            win.destroy()
            self.redraw()

        def save():
            sub = self.var_sub.get()
            if not sub:
                return messagebox.showwarning("Додати адресу", "Виберіть будинок або підчергу.", parent=win)
            cfg = zn.load_json(zn.CONFIG_PATH, {})
            cfg["subqueue"], cfg["address"] = sub, dict(self.cfg.get("address") or {})
            zn.save_json(zn.CONFIG_PATH, cfg)
            if sub != old_sub and zn.STATE_PATH.exists():
                zn.STATE_PATH.unlink()  # нова підчерга: надіслати її графік заново
            zn.log.info("адреса: %s, підчерга %s", zn.address_text(cfg) or "—", sub)
            self.cfg["subqueue"] = sub
            win.destroy()
            self.var_status.set("Адресу збережено ✓")
            self.redraw()

        bottom = ttk.Frame(body)
        bottom.pack(fill="x", pady=(4, 0))
        ttk.Button(bottom, text="💾  Зберегти", style="Accent.TButton", command=save).pack(side="left")
        ttk.Button(bottom, text="Скасувати", command=cancel).pack(side="left", padx=8)
        win.protocol("WM_DELETE_WINDOW", cancel)
        win.update_idletasks()
        win.geometry(f"+{self.winfo_rootx() + 40}+{self.winfo_rooty() + 60}")
        win.grab_set()

    def _alert_card(self, parent, key, title, desc, minutes_key=None, minutes_label="за, хв:",
                    test=None, extra=None):
        f = card(parent, pady=(0, 8))
        row = ttk.Frame(f, style="Card.TFrame", padding=0)
        row.configure(borderwidth=0)
        row.pack(fill="x")
        ttk.Checkbutton(row, text=title, variable=self.v[key]).pack(side="left")
        if test:
            ttk.Button(row, text="▶  Тест", command=test).pack(side="right")
        if minutes_key:
            ttk.Spinbox(row, from_=1, to=240, increment=1, textvariable=self.v[minutes_key], width=5).pack(side="right", padx=8)
            ttk.Label(row, text=minutes_label, style="Card.TLabel").pack(side="right")
        ttk.Label(f, text=desc, style="Muted.TLabel", wraplength=640, justify="left").pack(anchor="w", pady=(4, 0))
        if extra:
            extra(f)

    def _build_alerts_tab(self, nb) -> None:
        tab = self._tab(nb, "🔔  Попередження")
        self._alert_card(tab, "notify_schedule", "Telegram: новий або змінений графік",
                         "Повідомлення в Telegram, коли на сайті з'являється графік або він змінюється.",
                         test=lambda: self.test_telegram("schedule"))
        self._alert_card(tab, "remind_off_enabled", "1. Telegram: попередження про вимкнення",
                         "Перше попередження. Приходить у Telegram навіть коли ноутбук вимкнено (працює через Планувальник).",
                         "remind_off_minutes", test=lambda: self.test_telegram("off"))
        self._alert_card(tab, "remind_on_enabled", "Telegram: попередження про включення",
                         "Повідомлення, коли світло має повернутися.",
                         "remind_on_minutes", test=lambda: self.test_telegram("on"))
        self._alert_card(tab, "windows_notifications", "Сповіщення Windows",
                         "Спливаюче сповіщення біля годинника одночасно з попередженнями Telegram (поки програма в треї).",
                         test=self.test_toast)
        self._alert_card(tab, "popup_enabled", "2. Вікно поверх усього зі звуком",
                         "Друге попередження перед вимкненням: червоне вікно і звук, поки не натиснете «Зрозуміло». "
                         "Має бути раніше за примусове вимкнення.",
                         "popup_minutes", test=self.test_popup, extra=self._sound_row)

        def shutdown_extra(f):
            row = ttk.Frame(f, style="Card.TFrame", padding=0)
            row.configure(borderwidth=0)
            row.pack(anchor="w", pady=(6, 0))
            ttk.Label(row, text="Відлік перед вимкненням, с:", style="Card.TLabel").pack(side="left")
            ttk.Spinbox(row, from_=10, to=600, increment=10, textvariable=self.v["shutdown_countdown"],
                        width=5).pack(side="left", padx=8)

        self._alert_card(tab, "shutdown_enabled", "3. Примусове вимкнення ПК",
                         "Якщо вікно з п. 2 проігноровано (не натиснуто «Зрозуміло»), за вказаний час до відключення з'явиться відлік, "
                         "а потім усі програми закриються без збереження і ПК вимкнеться. "
                         "Натисніть «Скасувати» у відліку, щоб зупинити. Тест показує відлік, але ПК не вимикає.",
                         "shutdown_minutes", test=self.test_shutdown, extra=shutdown_extra)

    def _sound_row(self, f) -> None:
        row = ttk.Frame(f, style="Card.TFrame", padding=0)
        row.configure(borderwidth=0)
        row.pack(fill="x", pady=(6, 0))
        ttk.Label(row, text="Звук:", style="Card.TLabel").pack(side="left")
        self.lbl_sound = ttk.Label(row, text="", style="Card.TLabel", width=34)
        self.lbl_sound.pack(side="left", padx=(6, 8))
        ttk.Button(row, text="📂  Обрати файл…", command=self.pick_sound).pack(side="left")
        ttk.Button(row, text="🔊  Прослухати", command=self.preview_sound).pack(side="left", padx=6)
        ttk.Button(row, text="Стандартний", command=lambda: self.set_sound("")).pack(side="left")
        self.set_sound(self.var_sound.get())

    def set_sound(self, path: str) -> None:
        stop_sound()
        self.var_sound.set(path)
        self.lbl_sound.config(text=Path(path).name if path else "стандартний звук Windows")

    def pick_sound(self) -> None:
        path = filedialog.askopenfilename(title="Оберіть звук", filetypes=[("Звук", "*.mp3 *.wav *.wma *.m4a"), ("Усі файли", "*.*")])
        if path:
            self.set_sound(path)

    def preview_sound(self) -> None:
        path = self.var_sound.get()
        if path and play_file(path):
            return
        if path:
            messagebox.showwarning("Звук", "Не вдалося відтворити файл, буде стандартний звук.")
        if winsound:
            winsound.PlaySound("SystemHand", winsound.SND_ALIAS | winsound.SND_ASYNC)
        else:
            self.bell()

    def _build_settings_tab(self, nb) -> None:
        tab = self._tab(nb, "⚙  Налаштування")
        self._build_telegram_card(tab)
        self._build_system_cards(tab)

    def _build_telegram_card(self, tab) -> None:
        f = card(tab)
        ttk.Label(f, text="✈  Telegram", style="Title.TLabel").pack(anchor="w", pady=(0, 6))
        ttk.Label(f, text="Токен бота (від @BotFather)", style="Card.TLabel").pack(anchor="w")
        ttk.Entry(f, textvariable=self.var_token, width=60, show="•").pack(anchor="w", pady=(4, 10))
        ttk.Label(f, text="Chat ID", style="Card.TLabel").pack(anchor="w")
        row = plain(f, pady=(4, 2))
        ttk.Entry(row, textvariable=self.var_chat, width=24).pack(side="left")
        ttk.Button(row, text="🔍  Визначити автоматично", command=self.detect_chat).pack(side="left", padx=8)
        ttk.Label(f, text="Напишіть боту будь-яке повідомлення (у групі — команду, наприклад /start@імʼя_бота, "
                          "у потрібній гілці), потім натисніть «Визначити»: Chat ID і гілка підставляться "
                          "з останнього повідомлення, яке отримав бот.",
                  style="Muted.TLabel", wraplength=640, justify="left").pack(anchor="w", pady=(0, 10))

        row = plain(f, pady=(0, 10))
        ttk.Checkbutton(row, text="Надсилати в гілку (тему) групи, ID гілки:", variable=self.var_thread_on,
                        command=self._thread_state).pack(side="left")
        self.ent_thread = ttk.Entry(row, textvariable=self.var_thread, width=12)
        self.ent_thread.pack(side="left", padx=8)
        self._thread_state()
        row = plain(f, pady=(0, 2))
        ttk.Label(row, text="Звук повідомлень:", style="Card.TLabel").pack(side="left")
        ttk.Combobox(row, textvariable=self.var_silent, values=list(SILENT_LABELS.values()), state="readonly",
                     width=22).pack(side="left", padx=8)
        ttk.Label(row, text="коли:", style="Card.TLabel").pack(side="left")
        ttk.Combobox(row, textvariable=self.var_q_when, values=list(QUIET_WHEN.values()), state="readonly",
                     width=14).pack(side="left", padx=(6, 8))
        ttk.Label(row, text="з", style="Card.TLabel").pack(side="left")
        ttk.Entry(row, textvariable=self.var_q_from, width=6).pack(side="left", padx=4)
        ttk.Label(row, text="до", style="Card.TLabel").pack(side="left")
        ttk.Entry(row, textvariable=self.var_q_to, width=6).pack(side="left", padx=4)
        ttk.Button(row, text="🌙 Ніч", command=lambda: (self.var_q_from.set(zn.QUIET_DEFAULT[0]),
                                                       self.var_q_to.set(zn.QUIET_DEFAULT[1]))).pack(side="left", padx=4)
        ttk.Label(f, text="«Графік беззвучно» — новий і змінений графік приходять без звуку, попередження — зі звуком. "
                          "«У тихі години» — лише в цей проміжок (типово вночі 22:00–07:00), решту часу — зі звуком.",
                  style="Muted.TLabel", wraplength=640).pack(anchor="w", pady=(0, 10))
        ttk.Checkbutton(f, text="Повідомляти в Telegram, коли сайт ztoe.com.ua не працює і коли відновиться",
                        variable=self.v["telegram_site_down"]).pack(anchor="w", pady=(0, 10))
        ttk.Button(f, text="▶  Надіслати тестове повідомлення", style="Accent.TButton",
                   command=lambda: self.test_telegram("hello")).pack(anchor="w")

    def _thread_state(self) -> None:
        self.ent_thread.state(["!disabled"] if self.var_thread_on.get() else ["disabled"])

    def detect_chat(self) -> None:
        token = self.var_token.get().strip().removeprefix("bot")
        if not token:
            messagebox.showwarning("Telegram", "Спершу вкажіть токен бота.")
            return
        self.var_status.set("Шукаю останнє повідомлення боту…")

        def work():
            try:
                res, err = zn.detect_chat(token), None
            except Exception as e:  # noqa: BLE001
                res, err = None, str(e)
            self.after(0, lambda: self._detected(res, err))

        threading.Thread(target=work, daemon=True).start()

    def _detected(self, res, err) -> None:
        if err:
            self.var_status.set("")
            messagebox.showerror("Telegram", f"Не вдалося визначити: {err}")
            return
        if not res:
            self.var_status.set("")
            messagebox.showinfo("Telegram", "Бот ще не отримав жодного повідомлення.\n"
                                            "Напишіть йому (або в групі/гілці, де він є) і спробуйте ще раз.")
            return
        self.var_chat.set(str(res["chat_id"]))
        self.var_thread_on.set(res["thread_id"] is not None)
        self.var_thread.set(str(res["thread_id"] or ""))
        self._thread_state()
        where = f"«{res['title']}», Chat ID {res['chat_id']}"
        if res["thread_id"] is not None:
            where += f", гілка {res['thread_id']}"
        self.var_status.set(f"Знайдено {where}. Натисніть «Зберегти».")

    def _build_system_cards(self, tab) -> None:
        f = card(tab)
        ttk.Label(f, text="🖥  Перевірка у фоні", style="Title.TLabel").pack(anchor="w")
        ttk.Label(f, text="Планувальник Windows перевіряє сайт і надсилає Telegram, якщо графік змінився, "
                          "навіть коли це вікно закрито. Вмикається сам під час запуску програми. Без інтернету "
                          "або коли сайт не відповідає, перевірки й сповіщення призупиняються до відновлення.",
                  style="Muted.TLabel", wraplength=640).pack(anchor="w", pady=(2, 8))
        row = plain(f, pady=(0, 8))
        ttk.Label(row, text="Перевіряти сайт на зміни кожні, хв:", style="Card.TLabel").pack(side="left")
        ttk.Spinbox(row, from_=1, to=240, textvariable=self.v["check_interval_minutes"], width=5).pack(side="left", padx=8)
        ttk.Label(row, text="Оновлювати довідник адрес кожні, год:", style="Card.TLabel").pack(side="left", padx=(16, 0))
        ttk.Spinbox(row, from_=1, to=240, textvariable=self.v["address_cache_hours"], width=5).pack(side="left", padx=8)
        ttk.Button(row, text="🗑  Очистити кеш адрес", command=self.clear_address_cache).pack(side="left")
        ttk.Checkbutton(f, text="Запускати програму в треї разом з Windows", variable=self.var_autostart,
                        command=self.toggle_autostart).pack(anchor="w")
        ttk.Label(f, text="Потрібно для вікна зі звуком і примусового вимкнення: вони працюють, лише коли програма в треї.",
                  style="Muted.TLabel", wraplength=640).pack(anchor="w", pady=(4, 0))

        a = card(tab)
        head = plain(a)
        ttk.Label(head, text="Про програму", style="Title.TLabel").pack(side="left")
        tk.Label(head, text=f"версія {zn.__version__}", bg=ACCENT_SOFT, fg=ACCENT, font=(FONT, 9, "bold"),
                 padx=8, pady=2).pack(side="right")
        ttk.Label(a, text=zn.ABOUT, style="Muted.TLabel", wraplength=640, justify="left").pack(anchor="w", pady=(6, 0))
        ttk.Label(a, text=f"Налаштування: {zn.CONFIG_PATH}", style="Muted.TLabel",
                  wraplength=640).pack(anchor="w", pady=(6, 0))
        row = plain(a, pady=(8, 0))
        ttk.Checkbutton(row, text="Перевіряти оновлення під час запуску",
                        variable=self.v["check_updates"]).pack(side="left")
        ttk.Button(row, text="🔄  Перевірити оновлення",
                   command=lambda: self.check_updates(manual=True)).pack(side="left", padx=(16, 0))

    # ================================================================ графік
    def refresh(self) -> None:
        self.var_status.set("Завантажую графік…")

        def work():
            if not zn.internet_ok():  # без інтернету сайт навіть не пробуємо
                return self.after(0, lambda: self._loaded({}, None, offline=True))
            try:
                sched = zn.parse_schedule(zn.fetch(zn.BASE_URL))
                err = None if sched else "таблицю на сторінці не знайдено"
            except Exception as e:  # noqa: BLE001
                sched, err = {}, str(e)
            self.after(0, lambda: self._loaded(sched, err))

        threading.Thread(target=work, daemon=True).start()

    def _loaded(self, sched, err, offline: bool = False) -> None:
        if offline:
            self.var_status.set("⚠ Немає інтернету. Очікую відновлення зв'язку…")
            self._set_net("offline")
            secs = OFFLINE_RETRY_S
        else:
            zn.site_result(not err, err or "")
            self._set_net("site_down" if zn.site_down() else "ok")
            self.var_status.set(f"⚠ Сайт не відповідає ({err}). {zn.SITE_WAIT_TEXT}…" if err
                                else f"Графік оновлено о {dt.datetime.now():%H:%M}")
            self.schedule = sched or self.schedule
            # наступна автоматична перевірка — через період від цієї (і після ручного «Оновити» теж);
            # після помилки — швидше, щоб помітити відновлення
            secs = SITE_RETRY_S if err else zn.check_seconds(self.cfg)
        self.last_check = dt.datetime.now()
        self.next_check = self.last_check + dt.timedelta(seconds=secs)
        if self._auto_id is not None:
            self.after_cancel(self._auto_id)
        self._auto_id = self.after(secs * 1000, self._auto_refresh)
        self.redraw()
        self._tick(reschedule=False)
        if self.schedule and not self._fitted:  # перший графік: підігнати висоту під головну сторінку
            self._fitted = True
            self.after_idle(self._fit_height)

    def redraw(self) -> None:
        self.draw_schedule()
        self._update_header()

    def _set_net(self, state: str) -> None:
        """Запам'ятовує стан зв'язку; про втрату й відновлення повідомляє сповіщенням Windows."""
        old, self.net = self.net, state
        if state == old:
            return
        zn.log.info("зв'язок: %s -> %s", old, state)
        if state != "ok":
            self._toast(NET_TEXT[state][1])
        else:
            self._toast(NET_TEXT[old][2])

    def _dates(self) -> list[str]:
        return sorted(self.schedule, key=lambda d: dt.datetime.strptime(d, "%d.%m.%Y"))[:2]

    @staticmethod
    def _day_label(date: str) -> str:
        today = dt.date.today()
        d = dt.datetime.strptime(date, "%d.%m.%Y").date()
        return {today: "Сьогодні", today + dt.timedelta(days=1): "Завтра"}.get(d, "")

    def _show_mode(self, sub: str) -> None:
        """З підчергою — власний графік і кнопка «Видалити»; без неї — одразу графік усіх черг."""
        if self._mode == bool(sub):
            return
        if self._mode is not None:  # адресу додано чи видалено — інша висота головної сторінки
            self.after_idle(self._fit_height)
        self._mode = bool(sub)
        for w in (self.btn_del_addr, self.btn_add_addr, self.lbl_sub_badge, self.lbl_queue_badge):
            w.pack_forget()
        if sub:
            self.btn_del_addr.pack(side="left", padx=(8, 0))
            self.lbl_sub_badge.pack(side="right")
            self.lbl_queue_badge.pack(side="right", padx=(0, 6))
            self.personal.pack(fill="x", after=self.info_row)
            if not self.all_open:
                self.all_box.pack_forget()
        else:
            self.btn_add_addr.pack(side="left", padx=(12, 0))
            self.personal.pack_forget()
            self.all_box.pack(fill="x", pady=(10, 0))
        self.lbl_title.config(text="Графік на сьогодні й завтра" if sub else "Графік усіх черг")

    def delete_address(self) -> None:
        if not messagebox.askyesno(
                "Видалити адресу", "Видалити адресу й підчергу?\n\nНа головній сторінці показуватиметься графік "
                "усіх черг, а сповіщень у Telegram не буде, доки ви знову не додасте адресу.",
                icon="warning", parent=self):
            return
        cfg = zn.load_json(zn.CONFIG_PATH, {})
        cfg["subqueue"], cfg["address"] = "", {}
        zn.save_json(zn.CONFIG_PATH, cfg)
        if zn.STATE_PATH.exists():
            zn.STATE_PATH.unlink()  # нова підчерга потім отримає свій графік заново
        zn.log.info("адресу й підчергу видалено")
        self.cfg["subqueue"], self.cfg["address"] = "", {}
        self.var_sub.set("")
        self.var_status.set("Адресу видалено — показую графік усіх черг")
        self.redraw()

    def draw_schedule(self) -> None:
        sub = self.var_sub.get()
        self._show_mode(sub)
        if not sub:
            return self.draw_all()
        c = self.canvas
        c.delete("all")
        dates = self._dates()
        now = dt.datetime.now()
        c.config(height=max(len(dates), 1) * DAY_H - 8)
        self._hits[c] = []
        for w in self.summary.winfo_children():
            w.destroy()
        for row, date in enumerate(dates):
            slots = self.schedule[date].get(sub)
            y = row * DAY_H
            label = self._day_label(date)
            c.create_text(1, y, anchor="nw", text=f"{label}  {date}".strip(), font=(FONT, 10, "bold"), fill=TEXT)
            if slots is None:
                c.create_text(1, y + 24, anchor="nw", text="немає даних", fill=MUTED)
                continue
            now_x = (now.hour * 60 + now.minute) / 30 * CELL_W + 1 if date == now.strftime("%d.%m.%Y") else None
            draw_scale(c, slots, y + 18, CELL_W, CELL_H, x0=1, now_x=now_x, now_text=f"{now:%H:%M}")
            top = y + 18 + 16
            self._hits[c].append((1, top, top + CELL_H, CELL_W, slots, date, ""))       # клітинки
            self._hits[c].append((1, top + CELL_H, top + CELL_H + 22, CELL_W, None, date, ""))  # лінійка
            self._summary_row(date, slots, now)
        if not dates:
            c.create_text(4, 20, anchor="nw", text="Графік ще не завантажено", fill=MUTED)
        if self.all_open:
            self.draw_all()

    def _summary_row(self, date: str, slots: list[bool], now: dt.datetime) -> None:
        """Рядок «дата: [14:30 – 16:00] [20:00 – 22:00]», кожен період в окремій рамці."""
        row = plain(self.summary, pady=(0, 6))
        ttk.Label(row, text=f"{self._day_label(date)} {date}:".strip(), style="Card.TLabel",
                  width=19).pack(side="left")
        iv = zn.intervals(slots)
        if not iv:
            tk.Label(row, text="відключень не заплановано ✅", bg=GREEN_SOFT, fg=GREEN, font=(FONT, 10, "bold"),
                     padx=10, pady=3, highlightthickness=1, highlightbackground=GREEN).pack(side="left")
            return
        day = dt.datetime.strptime(date, "%d.%m.%Y")
        for a, b in iv:
            start, end = day + dt.timedelta(minutes=30 * a), day + dt.timedelta(minutes=30 * b)
            if end <= now:        # минуло
                bg, fg, border = CARD, MUTED, BORDER
            elif start <= now:    # триває зараз
                bg, fg, border = RED, "white", RED
            else:                 # попереду
                bg, fg, border = RED_SOFT, RED, RED
            tk.Label(row, text=f"{zn.slot_time(a)} – {slot_end(b)}", bg=bg, fg=fg, font=(FONT, 10, "bold"),
                     padx=10, pady=3, highlightthickness=1, highlightbackground=border,
                     highlightcolor=border).pack(side="left", padx=(0, 6))

    def draw_all(self) -> None:
        """Графік усіх 12 підчерг на обраний день; поточна підчерга виділена."""
        dates = self._dates()
        for w in self.all_dates.winfo_children():
            w.destroy()
        if dates and self.var_all_date.get() not in dates:
            self.var_all_date.set(dates[0])
        for date in dates:
            ttk.Radiobutton(self.all_dates, text=f"{self._day_label(date)} {date}".strip(), value=date,
                            variable=self.var_all_date, command=self.draw_all).pack(side="left", padx=(0, 12))
        c = self.all_canvas
        c.delete("all")
        date = self.var_all_date.get()
        day = self.schedule.get(date)
        if not day:
            c.create_text(4, 4, anchor="nw", text="Графік ще не завантажено", fill=MUTED)
            return
        mine, x0 = self.var_sub.get(), ALL_LABEL_W
        self._hits[c] = [(x0, len(SUBQUEUES) * ALL_ROW_H, len(SUBQUEUES) * ALL_ROW_H + 20, ALL_CELL_W, None, date, "")]
        for r, sub in enumerate(SUBQUEUES):
            if day.get(sub) is not None:
                self._hits[c].append((x0, r * ALL_ROW_H, (r + 1) * ALL_ROW_H, ALL_CELL_W, day[sub], date, sub))
            y = r * ALL_ROW_H
            current = sub == mine
            if current:
                c.create_rectangle(1, y, x0 + 48 * ALL_CELL_W, y + ALL_ROW_H, fill=ACCENT_SOFT,
                                   outline=ACCENT, width=2)
            c.create_text(6, y + ALL_ROW_H / 2, anchor="w", text=("▶ " if current else "") + sub,
                          font=(FONT, 9, "bold" if current else "normal"), fill=ACCENT if current else TEXT)
            slots = day.get(sub)
            if slots is None:
                c.create_text(x0 + 4, y + ALL_ROW_H / 2, anchor="w", text="немає даних", fill=MUTED, font=(FONT, 8))
                continue
            for i, off in enumerate(slots):
                x = x0 + i * ALL_CELL_W
                c.create_rectangle(x + 1, y + 2, x + ALL_CELL_W - 1, y + ALL_ROW_H - 2,
                                   fill=RED if off else GREEN_SOFT, width=0)
        bottom = len(SUBQUEUES) * ALL_ROW_H
        now = dt.datetime.now()
        now_x = x0 + (now.hour * 60 + now.minute) / 30 * ALL_CELL_W if date == now.strftime("%d.%m.%Y") else None
        for h in range(0, 25, 3):  # пунктир кожні 3 години через усі черги — легше звіряти час
            c.create_line(x0 + h * 2 * ALL_CELL_W, 0, x0 + h * 2 * ALL_CELL_W, bottom, fill=MUTED, dash=(1, 3))
        for h in range(25):
            x = x0 + h * 2 * ALL_CELL_W
            major = h % 3 == 0
            c.create_line(x, bottom + 1, x, bottom + (6 if major else 3), fill=MUTED if major else BORDER)
            if major and (now_x is None or abs(x - now_x) > 22):
                c.create_text(x, bottom + 7, anchor="nw" if h == 0 else "ne" if h == 24 else "n",
                              text=f"{h:02d}:00", font=(FONT, 7), fill=MUTED)
        if now_x is not None:
            c.create_line(now_x, 0, now_x, bottom + 6, fill=ACCENT, width=2)
            c.create_rectangle(now_x - 17, bottom + 6, now_x + 17, bottom + 19, fill=ACCENT, width=0)
            c.create_text(now_x, bottom + 12, text=f"{now:%H:%M}", font=(FONT, 7, "bold"), fill="white")

    def _state_now(self):
        sub = self.var_sub.get()
        now = dt.datetime.now()
        today = self.schedule.get(now.strftime("%d.%m.%Y"), {}).get(sub)
        off_now = bool(today and today[(now.hour * 60 + now.minute) // 30])
        nxt = next((t for kind, t in zn.transitions(self.schedule, sub) if t > now), None)
        return sub, off_now, nxt, today is not None

    def _update_header(self) -> None:
        sub, off_now, nxt, known = self._state_now()
        if self.net != "ok":
            return self._update_header_paused(sub)
        colour = RED if off_now else GREEN if sub else MUTED
        self.dot.delete("all")
        self.dot.create_oval(3, 3, 51, 51, fill=RED_SOFT if off_now else GREEN_SOFT if sub else BG, width=0)
        self.dot.create_oval(13, 13, 41, 41, fill=colour, width=0)
        self.dot.create_text(27, 27, text="⚡", fill="white", font=(FONT, 12, "bold"))
        if not sub:
            self.lbl_state.config(text="Графік усіх черг")
        elif not known:
            self.lbl_state.config(text="Немає графіка на сьогодні")
        else:
            self.lbl_state.config(text="Можливе відключення зараз" if off_now else "Світло за графіком є")
        when = (f"{nxt:%H:%M}" + ("" if nxt.date() == dt.date.today() else " завтра")) if nxt else ""
        left = fmt_left(nxt - dt.datetime.now()) if nxt else ""
        self.lbl_next.config(text=f"{'Включення' if off_now else 'Наступне вимкнення'} о {when}, через {left}"
                             if nxt else "")
        self.lbl_badge.config(text=f"Підчерга {sub}" if sub else "Усі черги")
        self.lbl_checks.config(
            text=f"🔄 Графік перевірено о {zn.fmt_check_time(self.last_check)} · наступна перевірка о "
                 f"{zn.fmt_check_time(self.next_check)}" if self.last_check else "🔄 Графік ще не перевіряли")
        self._update_info(sub, off_now, nxt, known, when, left)
        self._update_tray(off_now, nxt)

    def _update_header_paused(self, sub: str) -> None:
        """Немає інтернету / сайт не відповідає: сірий значок і причина замість стану світла."""
        self.dot.delete("all")
        self.dot.create_oval(3, 3, 51, 51, fill=BG, width=0)
        self.dot.create_oval(13, 13, 41, 41, fill=MUTED, width=0)
        self.dot.create_text(27, 27, text="!", fill="white", font=(FONT, 14, "bold"))
        self.lbl_state.config(text=NET_TEXT[self.net][0])
        self.lbl_next.config(text="Очікую відновлення · сповіщення й вимкнення ПК призупинено")
        self.lbl_badge.config(text=f"Підчерга {sub}" if sub else "Усі черги")
        self.lbl_checks.config(text=f"🔄 Наступна спроба о {zn.fmt_check_time(self.next_check)}"
                               if self.next_check else "")
        self.lbl_address.config(text=self._address_text())
        if sub:
            self.lbl_queue_badge.config(text=f"Черга {sub.split('.')[0]}")
            self.lbl_sub_badge.config(text=f"Підчерга {sub}")
        if self.tray:
            self.tray.icon = self._icon(None)
            self.tray.title = f"{NET_TEXT[self.net][0]} — сповіщення призупинено"[:127]

    def _address_text(self) -> str:
        if not self.var_sub.get():
            return "📍 Адресу не вибрано — без неї немає сповіщень"
        text = zn.address_text(self.cfg)
        return f"📍 {text}" if text else "📍 Підчергу вибрано вручну, без адреси"

    def _update_info(self, sub, off_now, nxt, known, when, left) -> None:
        self.lbl_address.config(text=self._address_text())
        if not sub:
            return
        self.lbl_queue_badge.config(text=f"Черга {sub.split('.')[0]}")
        self.lbl_sub_badge.config(text=f"Підчерга {sub}")
        if nxt is None:
            title = "Відключень найближчим часом не заплановано" if known else "Графіка на сьогодні немає"
            value = at = ""
            bg, fg = (RED_SOFT, RED) if off_now else (GREEN_SOFT, GREEN) if known else (BG, MUTED)
        elif off_now:
            title, value, at, bg, fg = "💡 До включення світла:", left, f"о {when}", RED_SOFT, RED
        else:
            title, value, at, bg, fg = "⚠ До відключення світла:", left, f"о {when}", GREEN_SOFT, GREEN
        self.countdown.config(bg=bg, highlightbackground=fg, highlightcolor=fg)
        self.lbl_count_title.config(text=title, bg=bg, fg=fg)
        self.lbl_count_value.config(text=value, bg=bg, fg=fg)
        self.lbl_count_at.config(text=at, bg=bg)

    # ================================================================ попередження
    def _auto_refresh(self) -> None:
        self._auto_id = None
        self.refresh()  # наступну перевірку планує _loaded

    def _tick(self, reschedule: bool = True) -> None:
        try:
            self._check_alerts()
            minute = dt.datetime.now().replace(second=0, microsecond=0)
            if minute != self._last_minute:  # раз на хвилину: позначка «зараз» і стан періодів
                self._last_minute = minute
                self.draw_schedule()
            self._update_header()
        finally:
            if reschedule:
                self.after(TICK_MS, self._tick)

    def _check_alerts(self) -> None:
        cfg = self.collect()
        sub, now = cfg["subqueue"], dt.datetime.now()
        if not sub or self.net != "ok":  # без адреси чи без зв'язку — без попереджень і вимкнення ПК
            return
        if cfg["windows_notifications"] and self.tray:
            lead_off, lead_on = zn.lead_times(cfg)
            for key, text in zn.reminders(self.schedule, sub, now, lead_off, lead_on):
                if key not in self.toasted:
                    self.toasted.add(key)
                    self._toast(text)
        for kind, t in zn.transitions(self.schedule, sub):
            if kind != "off":
                continue
            key, mins = t.isoformat(), (t - now).total_seconds() / 60
            if cfg["popup_enabled"] and 0 < mins <= cfg["popup_minutes"] and key not in self.popups:
                self.popups[key] = False
                self.popup_shown_at[key] = now
                self.show_popup(t, key)
            if (cfg["shutdown_enabled"] and -2 < mins <= cfg["shutdown_minutes"]
                    and self.popups.get(key) is False and key not in self.shutdowns
                    and now - self.popup_shown_at[key] >= dt.timedelta(minutes=1)):
                self.shutdowns.add(key)
                self.start_shutdown(t, cfg["shutdown_countdown"], real=True)

    def show_popup(self, t: dt.datetime, key: str | None = None) -> None:
        def ack():
            if key:
                self.popups[key] = True
        Alarm(self, "Скоро відключення світла!",
              f"О {t:%H:%M} можливе відключення (підчерга {self.var_sub.get()}).\nЗбережіть роботу.",
              "Зрозуміло", on_close=ack, sound=self.var_sound.get())

    def start_shutdown(self, t: dt.datetime, seconds: int, real: bool) -> None:
        def go():
            if real:
                threading.Thread(target=self._tg_quiet, args=("🔌 ПК вимикається перед відключенням світла",),
                                 daemon=True).start()
                self.after(1500, lambda: subprocess.run(["shutdown", "/s", "/f", "/t", "0"],
                                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))
            else:
                messagebox.showinfo("Тест", "Тут ПК вимкнувся б, а всі програми закрилися б.")

        Alarm(self, "ПК буде вимкнено" + ("" if real else " (тест)"),
              f"Попередження про відключення о {t:%H:%M} проігноровано.\n"
              "Усі програми закриються без збереження.", "Скасувати вимкнення",
              countdown=int(seconds), on_timeout=go, colour="#7f1d1d", sound=self.var_sound.get())

    def _toast(self, text: str) -> None:
        try:
            self.tray.notify(text, "Відключення світла")
        except Exception:  # noqa: BLE001
            pass

    def _tg_quiet(self, text: str) -> None:
        try:
            zn.send_telegram(self.collect(), text, False)
        except Exception:  # noqa: BLE001
            pass

    # ================================================================ тести
    def _next_off(self) -> dt.datetime:
        now = dt.datetime.now()
        return next((t for k, t in zn.transitions(self.schedule, self.var_sub.get()) if k == "off" and t > now),
                    (now + dt.timedelta(minutes=15)).replace(second=0, microsecond=0))

    def test_telegram(self, what: str) -> None:
        cfg = self.collect()
        if not cfg["telegram_token"] or not cfg["telegram_chat_id"]:
            messagebox.showwarning("Telegram", "Вкажіть токен і Chat ID на вкладці «Налаштування».")
            return
        sub = cfg["subqueue"]
        if not sub:
            messagebox.showwarning("Telegram", zn.NO_ADDRESS_TEXT[0].upper() + zn.NO_ADDRESS_TEXT[1:] + ".")
            return
        if what == "schedule":
            date = next(iter(sorted(self.schedule, key=lambda d: dt.datetime.strptime(d, "%d.%m.%Y"))), None)
            slots = self.schedule.get(date, {}).get(sub) if date else None
            text = "🧪 Тест\n📢 Графік\n" + (zn.format_day(date, sub, slots) if slots else "графік ще не завантажено")
        elif what == "off":
            text = "🧪 Тест\n" + zn.event_text("off", self._next_off(), sub)
        elif what == "on":
            now = dt.datetime.now()
            t = next((t for k, t in zn.transitions(self.schedule, sub) if k == "on" and t > now), now)
            text = "🧪 Тест\n" + zn.event_text("on", t, sub)
        else:
            text = f"✅ Бот працює. Стежу за підчергою {sub}."
        try:
            zn.send_telegram(cfg, text, False, "schedule" if what == "schedule" else "alert")
            self.var_status.set("Тестове повідомлення надіслано ✓")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Telegram", f"Помилка: {e}")

    def test_toast(self) -> None:
        if not self.tray:
            messagebox.showinfo("Сповіщення Windows", "Потрібен трей (pystray і Pillow).")
            return
        if not self.var_sub.get():
            return messagebox.showwarning("Сповіщення Windows", zn.NO_ADDRESS_TEXT[0].upper() + zn.NO_ADDRESS_TEXT[1:] + ".")
        self._toast("🧪 Тест: " + zn.event_text("off", self._next_off(), self.var_sub.get()))

    def test_popup(self) -> None:
        if not self.var_sub.get():
            return messagebox.showwarning("Вікно попередження", zn.NO_ADDRESS_TEXT[0].upper() + zn.NO_ADDRESS_TEXT[1:] + ".")
        self.show_popup(self._next_off())

    def test_shutdown(self) -> None:
        self.start_shutdown(self._next_off(), min(int(self.v["shutdown_countdown"].get() or 10), 10), real=False)

    # ================================================================ збереження
    def collect(self) -> dict:
        cfg = dict(self.cfg)
        for k, var in self.v.items():
            try:
                cfg[k] = var.get()
            except tk.TclError:  # порожнє поле
                cfg[k] = DEFAULTS[k]
        cfg.pop("remind_minutes_before", None)
        cfg.pop("deleted_days", None), cfg.pop("hidden_days", None)  # від проміжних варіантів (приховування днів)
        cfg.update({
            "subqueue": self.var_sub.get(),
            "telegram_token": self.var_token.get().strip().removeprefix("bot"),
            "telegram_chat_id": self.var_chat.get().strip(),
            "telegram_thread_enabled": self.var_thread_on.get(),
            "telegram_thread_id": self.var_thread.get().strip(),
            "telegram_silent": next((k for k, v in SILENT_LABELS.items() if v == self.var_silent.get()), "none"),
            "telegram_silent_when": next((k for k, v in QUIET_WHEN.items() if v == self.var_q_when.get()), "hours"),
            "telegram_silent_from": zn.parse_hhmm(self.var_q_from.get()) or zn.QUIET_DEFAULT[0],
            "telegram_silent_to": zn.parse_hhmm(self.var_q_to.get()) or zn.QUIET_DEFAULT[1],
            "popup_sound": self.var_sound.get(),
        })
        return cfg

    def save(self) -> None:
        old_sub = self.cfg.get("subqueue")
        self.cfg = self.collect()
        zn.save_json(zn.CONFIG_PATH, self.cfg)
        import address_picker
        address_picker.CACHE_TTL = zn.address_cache_seconds(self.cfg)
        if self.task_exists():  # новий період перевірки для Планувальника
            subprocess.run(["schtasks", "/Change", "/TN", TASK_NAME, "/RI", str(zn.check_seconds(self.cfg) // 60)],
                           capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if old_sub != self.cfg["subqueue"] and zn.STATE_PATH.exists():
            zn.STATE_PATH.unlink()  # нова підчерга: надіслати її графік заново
        self.var_status.set("Збережено ✓")
        self.redraw()

    # ================================================================ система
    def _setup_address_cache(self) -> None:
        """Кеш адрес у address_cache.db поруч з config.json."""
        import address_picker
        try:
            db = address_picker.CacheDB(zn.DATA_DIR / "address_cache.db")
        except Exception:  # noqa: BLE001  без бази кеш лише в пам'яті
            db = None
        address_picker.configure_cache(db, zn.address_cache_seconds(self.cfg))

    def clear_address_cache(self) -> None:
        import address_picker
        self.var_status.set(f"Кеш адрес очищено (записів: {address_picker.clear_cache()})")
    def task_exists(self) -> bool:
        if sys.platform != "win32":
            return False
        r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode == 0

    def ensure_task(self) -> None:
        """Перевірка у фоні працює завжди: під час запуску (пере)створює завдання Планувальника,
        щоб воно запускало саме цю програму з поточним періодом перевірки."""
        if sys.platform != "win32":
            return
        mins = str(zn.check_seconds(self.cfg) // 60)
        r = subprocess.run(["schtasks", "/Create", "/F", "/SC", "MINUTE", "/MO", mins, "/TN", TASK_NAME,
                            "/TR", launch_cmd("--check")],
                           capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0:
            zn.log.warning("Планувальник: %s", (r.stderr or r.stdout).strip())

    def toggle_autostart(self) -> None:
        try:
            if self.var_autostart.get():
                STARTUP_FILE.write_text(
                    f'@echo off\r\nchcp 65001 >nul\r\nstart "" {launch_cmd("--tray")}\r\n',
                    encoding="utf-8", newline="")  # chcp: у шляху кирилиця
            elif STARTUP_FILE.exists():
                STARTUP_FILE.unlink()
        except OSError as e:
            messagebox.showerror("Автозапуск", str(e))
        self.var_autostart.set(STARTUP_FILE.exists())

    # ================================================================ оновлення
    def check_updates(self, manual: bool = False) -> None:
        """Шукає новий реліз на GitHub у фоні; manual=True повідомляє й тоді, коли оновлень немає."""
        import updater
        if self._updating:
            return
        if manual:
            self.var_status.set("Перевіряю оновлення…")

        def work():
            try:
                rel, err = updater.check(zn.__version__), None
            except Exception as e:  # noqa: BLE001
                rel, err = None, str(e)
            self.after(0, lambda: self._update_checked(rel, err, manual))

        threading.Thread(target=work, daemon=True).start()

    def _update_checked(self, rel, err, manual: bool) -> None:
        import updater
        if not rel:
            if manual:
                self.var_status.set("")
                if err:
                    messagebox.showerror("Оновлення", f"Не вдалося перевірити оновлення: {err}", parent=self)
                else:
                    messagebox.showinfo("Оновлення", f"У вас остання версія {zn.__version__}.", parent=self)
            return
        notes = rel["notes"] if len(rel["notes"]) <= 700 else rel["notes"][:700].rstrip() + "…"
        text = f"Знайдено нову версію {rel['version']} (у вас {zn.__version__})."
        if notes:
            text += f"\n\nЩо нового:\n{notes}"
        auto = updater.FROZEN and rel["asset"]
        text += "\n\nОновити зараз?" if auto else "\n\nВідкрити сторінку завантаження?"
        self.show_window()
        if not messagebox.askyesno("Оновлення", text, parent=self):
            return
        if not auto:  # запуск з .py або немає .exe потрібної розрядності
            webbrowser.open(rel["url"])
            return
        self._updating = True
        self.var_status.set(f"Завантажую версію {rel['version']}…")

        def progress(done, total):
            pct = f"{done * 100 // total}%" if total else f"{done // 1024} КБ"
            self.after(0, lambda: self.var_status.set(f"Завантажую версію {rel['version']}… {pct}"))

        def work():
            try:
                path, err = updater.download(rel["asset"], progress), None
            except Exception as e:  # noqa: BLE001
                path, err = None, str(e)
            self.after(0, lambda: self._update_downloaded(path, err, rel))

        threading.Thread(target=work, daemon=True).start()

    def _update_downloaded(self, path, err, rel) -> None:
        import updater
        if err:
            self._updating = False
            self.var_status.set("")
            if messagebox.askyesno("Оновлення", f"Не вдалося завантажити оновлення: {err}\n\n"
                                                "Відкрити сторінку завантаження?", parent=self):
                webbrowser.open(rel["url"])
            return
        try:
            updater.install(path)
        except Exception as e:  # noqa: BLE001
            self._updating = False
            messagebox.showerror("Оновлення", f"Не вдалося встановити оновлення: {e}", parent=self)
            return
        self.quit_app()  # скрипт дочекається виходу, підмінить .exe і запустить нову версію

    # ================================================================ трей
    def _setup_tray(self, start_hidden: bool) -> None:
        if pystray is None:
            return
        menu = pystray.Menu(
            pystray.MenuItem("Відкрити", lambda: self.after(0, self.show_window), default=True),
            pystray.MenuItem("Оновити графік", lambda: self.after(0, self.refresh)),
            pystray.MenuItem("Тест Telegram", lambda: self.after(0, lambda: self.test_telegram("hello"))),
            pystray.MenuItem("Перевірити оновлення", lambda: self.after(0, lambda: self.check_updates(manual=True))),
            pystray.MenuItem("Вийти", lambda: self.after(0, self.quit_app)),
        )
        self.tray = pystray.Icon("ztoe", self._icon(False), "Відключення світла", menu)
        self.tray.run_detached()
        self.protocol("WM_DELETE_WINDOW", self.withdraw)  # хрестик ховає в трей
        if start_hidden:
            self.withdraw()

    @staticmethod
    def _icon(off: bool | None):
        """Значок: червоний — відключення, зелений — світло є, сірий (None) — немає зв'язку."""
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((4, 4, 60, 60), fill=MUTED if off is None else RED if off else GREEN)
        d.polygon([(36, 10), (18, 36), (31, 36), (26, 54), (46, 26), (33, 26)], fill="white")
        return img

    def _update_tray(self, off_now: bool, nxt) -> None:
        if not self.tray:
            return
        sub = self.var_sub.get()
        tip = (f"Підчерга {sub}: " + ("можливо без світла" if off_now else "світло за графіком є")) if sub \
            else "Адресу не вибрано — графік усіх черг"
        if nxt:
            tip += f"\n{'включення' if off_now else 'вимкнення'} о {nxt:%H:%M}"
        self.tray.icon = self._icon(off_now)
        self.tray.title = tip[:127]

    def show_window(self) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()

    def quit_app(self) -> None:
        if self.tray:
            self.tray.stop()
        self.destroy()


_instance_lock = None


def single_instance() -> bool:
    """False, якщо вікно програми вже запущено (іменований м'ютекс Windows тримається до виходу)."""
    global _instance_lock
    if sys.platform != "win32":
        return True
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    _instance_lock = k32.CreateMutexW(None, False, "Local\\ZTOE_Power_window")
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def offer_relocation() -> bool:
    """Якщо .exe лежить не в папці ZTOE_Power, один раз пропонує вибрати місце й перенести його туди.
    True — програму перенесено, і ця копія має завершитися (нову запустить скрипт)."""
    import updater
    cfg = zn.load_json(zn.CONFIG_PATH, {})
    if not FROZEN or updater.in_own_folder() or cfg.get("placement_asked"):
        return False
    root = tk.Tk()
    root.withdraw()
    try:
        here = Path(sys.executable).parent
        if not messagebox.askyesno(
                "Відключення світла",
                f"Програма лежить у папці «{here}», а не у власній папці {updater.APP_DIR}.\n\n"
                "Поруч з нею зберігаються налаштування, журнал і кеш адрес, тому краще тримати її окремо.\n\n"
                f"Вибрати місце, де створити папку {updater.APP_DIR}, і перенести програму туди?", parent=root):
            cfg["placement_asked"] = True  # більше не питати
            zn.save_json(zn.CONFIG_PATH, cfg)
            return False
        while True:
            base = filedialog.askdirectory(parent=root, mustexist=True, initialdir=str(updater.DEFAULT_BASE),
                                           title=f"Де створити папку {updater.APP_DIR}?")
            if not base:  # скасовано — запитаємо наступного разу
                return False
            try:
                updater.relocate(Path(base))
                return True
            except OSError as e:
                messagebox.showerror("Відключення світла", f"Не вдалося перенести програму в «{base}»: {e}\n\n"
                                     "Виберіть інше місце, наприклад «Документи».", parent=root)
    finally:
        root.destroy()


if __name__ == "__main__":
    if "--check" in sys.argv:  # одна перевірка сайту (для Планувальника), без вікна
        sys.argv.remove("--check")
        sys.exit(zn.main())
    if not single_instance():
        _root = tk.Tk()
        _root.withdraw()
        messagebox.showinfo("Відключення світла", "Програму вже запущено — її значок у треї біля годинника.\n"
                                                  "Подвійний клік по ньому відкриває вікно.", parent=_root)
        sys.exit(0)
    if offer_relocation():
        sys.exit(0)
    App(start_hidden="--tray" in sys.argv).mainloop()
