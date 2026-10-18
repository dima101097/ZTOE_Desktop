"""Оновлення програми з релізів GitHub і перенесення її у власну папку.

У релізі (тег vX.Y.Z) лежать ZTOE_Power_x64.exe і ZTOE_Power_x86.exe. Програма порівнює тег
із своєю версією, завантажує .exe своєї розрядності поруч із запущеним і після виходу підміняє
його невеликим .cmd-скриптом, а потім запускає нову версію.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = "dima101097/ZTOE_Desktop"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{REPO}/releases/latest"
ARCH = "x64" if struct.calcsize("P") == 8 else "x86"
ASSET_NAMES = (f"ZTOE_Power_{ARCH}.exe",) + (("ZTOE_Power.exe",) if ARCH == "x64" else ())
FROZEN = getattr(sys, "frozen", False)


def parse_version(text: str) -> tuple[int, ...]:
    """«v1.10.0» -> (1, 10, 0)."""
    return tuple(int(n) for n in re.findall(r"\d+", text)[:3])


def _request(url: str, accept: str = "application/vnd.github+json") -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": "ZTOE_Desktop-updater", "Accept": accept})


def check(current: str, timeout: int = 15) -> dict | None:
    """Новий реліз або None: {"version", "notes", "url", "asset": {"name", "url", "size", "sha256"} | None}."""
    try:
        with urllib.request.urlopen(_request(API_URL), timeout=timeout) as r:
            rel = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:  # релізів ще немає
            return None
        raise
    tag = rel.get("tag_name") or ""
    if rel.get("draft") or rel.get("prerelease") or parse_version(tag) <= parse_version(current):
        return None
    assets = {a["name"]: a for a in rel.get("assets", [])}
    a = next((assets[n] for n in ASSET_NAMES if n in assets), None)
    return {
        "version": tag.lstrip("vV"),
        "notes": (rel.get("body") or "").strip(),
        "url": rel.get("html_url") or RELEASES_URL,
        "asset": a and {
            "name": a["name"], "url": a["browser_download_url"], "size": a.get("size") or 0,
            "sha256": (a.get("digest") or "").removeprefix("sha256:") or None,
        },
    }


def download(asset: dict, progress=None) -> Path:
    """Завантажує .exe поруч із запущеним (<ім'я>.new) і перевіряє розмір та SHA-256."""
    target = Path(sys.executable).with_suffix(".new")
    h, done = hashlib.sha256(), 0
    try:
        with urllib.request.urlopen(_request(asset["url"], "application/octet-stream"), timeout=60) as r, \
                open(target, "wb") as f:
            total = asset["size"] or int(r.headers.get("Content-Length") or 0)
            while chunk := r.read(256 * 1024):
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        if asset["size"] and done != asset["size"]:
            raise OSError(f"завантажено {done} з {asset['size']} байт")
        if asset["sha256"] and h.hexdigest() != asset["sha256"].lower():
            raise OSError("контрольна сума не збігається")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def _run_script(script: Path, body: str) -> None:
    """Записує й запускає у фоні .cmd-скрипт (він видаляє себе сам після роботи)."""
    script.write_text("@echo off\r\nchcp 65001 >nul\r\nset n=0\r\n"   # chcp: у шляху може бути кирилиця
                      + body + '(goto) 2>nul & del "%~f0"\r\n', encoding="utf-8", newline="")
    # нова копія onefile-програми не повинна успадкувати тимчасову теку PyInstaller цієї
    env = {k: v for k, v in os.environ.items() if not k.startswith(("_PYI", "_MEI"))}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(["cmd", "/c", str(script)], cwd=str(script.parent), env=env, creationflags=flags, close_fds=True)


WAIT = "ping -n 2 127.0.0.1 >nul\r\n"   # пауза ~1 с (timeout без консолі не працює)


def install(new_exe: Path) -> None:
    """Запускає скрипт, який дочекається виходу програми, підмінить .exe і запустить нову версію.
    Після виклику програму треба закрити."""
    exe = Path(sys.executable)
    _run_script(exe.with_name("ztoe_update.cmd"),
                ":wait\r\n" + WAIT
                + f'move /y "{new_exe}" "{exe}" >nul 2>&1 && goto run\r\n'
                "set /a n+=1\r\n"
                "if %n% lss 60 goto wait\r\n"
                ":run\r\n"
                f'start "" "{exe}"\r\n')


# ------------------------------------------------------- власна папка ---

APP_DIR = "ZTOE_Power"   # програма має лежати в папці з такою назвою (поруч — її налаштування й журнал)
DEFAULT_BASE = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Programs"
DATA_FILES = ("config.json", "state.json", "address_cache.db", "ztoe.log", "ztoe.log.1", "ztoe.log.2", "ztoe.log.3")


def in_own_folder() -> bool:
    return Path(sys.executable).parent.name.lower() == APP_DIR.lower()


def relocate(base: Path) -> Path:
    """Копіює .exe і його дані в base\\ZTOE_Power (або в base, якщо вона вже так називається) і запускає скрипт,
    який після виходу програми видалить старі файли й запустить її з нового місця.
    Файли, які вже є в новій папці, не перезаписуються. Після виклику програму треба закрити."""
    exe = Path(sys.executable)
    target = base if base.name.lower() == APP_DIR.lower() else base / APP_DIR
    target.mkdir(parents=True, exist_ok=True)
    new_exe = target / exe.name
    shutil.copy2(exe, new_exe)
    moved = []
    for name in DATA_FILES:
        src, dst = exe.parent / name, target / name
        if src.is_file() and not dst.exists():
            shutil.copy2(src, dst)
            moved.append(src)
    _run_script(target / "ztoe_move.cmd",
                ":wait\r\n" + WAIT
                + f'del /f /q "{exe}" >nul 2>&1\r\n'
                f'if not exist "{exe}" goto done\r\n'
                "set /a n+=1\r\n"
                "if %n% lss 60 goto wait\r\n"
                ":done\r\n"
                + "".join(f'del /f /q "{p}" >nul 2>&1\r\n' for p in moved)
                + f'start "" "{new_exe}"\r\n')
    return target
