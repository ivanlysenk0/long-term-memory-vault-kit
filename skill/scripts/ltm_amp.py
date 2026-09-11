#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Установка плагина долговременной памяти в Amp.

Плагин Amp это не то же самое, что хуки Claude Code, поэтому и установщик
отдельный. Ключевые отличия, из-за которых нельзя было обойтись общим кодом:

- плагин лежит каталогом, а не строкой в настройках
- ему нужны скрипты памяти рядом с собой, в подкаталоге `scripts/`
- событий сжатия контекста в Amp нет, и `PreCompact` сюда не ставится

Куда ставим (по документации Amp):
    $XDG_CONFIG_HOME/amp/plugins/   если переменная задана
    ~/.config/amp/plugins/          macOS и Linux
    %USERPROFILE%\\.config\\amp\\plugins\\  Windows

Режимы: --status, --dry-run, --install, --uninstall.
"""
import os
import shutil
import sys
from pathlib import Path

NAME = "ltm-vault"
# Скрипты, без которых плагин бесполезен. ltm_precompact.py не нужен: в Amp
# нет события сжатия контекста.
SCRIPTS = ["ltm_paths.py", "ltm_session_start.py", "ltm_sync.py", "ltm_doctor.py"]


def plugins_dir():
    """Каталог системных плагинов Amp."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "amp" / "plugins"
    if os.name == "nt":
        return Path(os.environ.get("USERPROFILE", Path.home())) / ".config" / "amp" / "plugins"
    return Path.home() / ".config" / "amp" / "plugins"


def source_dir():
    """Каталог скилла: отсюда берём и плагин, и скрипты."""
    return Path(__file__).resolve().parent.parent


def status():
    target = plugins_dir() / NAME
    print("Каталог плагинов Amp: %s" % plugins_dir())
    if not target.is_dir():
        print("Плагин памяти: не установлен")
        return 0
    print("Плагин памяти: установлен в %s" % target)
    missing = [s for s in SCRIPTS if not (target / "scripts" / s).is_file()]
    if missing:
        print("  НЕ ХВАТАЕТ скриптов: %s" % ", ".join(missing))
    else:
        print("  скрипты на месте: %d" % len(SCRIPTS))
    return 0


def install(dry):
    src = source_dir()
    plugin_src = src / "amp-plugin" / "index.ts"
    if not plugin_src.is_file():
        print("Не найден файл плагина: %s" % plugin_src)
        return 1

    missing = [s for s in SCRIPTS if not (src / "scripts" / s).is_file()]
    if missing:
        print("Не найдены скрипты: %s" % ", ".join(missing))
        return 1

    target = plugins_dir() / NAME
    print("Откуда: %s" % (src / "amp-plugin"))
    print("Куда:   %s" % target)
    print("")
    print("Будет сделано:")
    print("  %s index.ts" % ("обновить" if (target / "index.ts").is_file() else "добавить"))
    for s in SCRIPTS:
        exists = (target / "scripts" / s).is_file()
        print("  %s scripts/%s" % ("обновить" if exists else "добавить", s))

    if dry:
        print("")
        print("Это предпросмотр. Ничего не изменено.")
        print("Для установки повтори с --install.")
        return 0

    (target / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy2(plugin_src, target / "index.ts")
    for s in SCRIPTS:
        shutil.copy2(src / "scripts" / s, target / "scripts" / s)

    print("")
    print("Готово. Плагин загружается при старте Amp: перезапусти его")
    print("либо выполни `plugins: reload` из палитры команд.")
    print("Проверить: `amp plugins list`")
    return 0


def uninstall(dry):
    target = plugins_dir() / NAME
    if not target.is_dir():
        print("Плагин не установлен, удалять нечего.")
        return 0
    print("Будет удалён каталог: %s" % target)
    if dry:
        print("Это предпросмотр. Ничего не изменено.")
        return 0
    shutil.rmtree(target)
    print("Удалено. Сама память не тронута: это данные, а не файлы установщика.")
    return 0


def main():
    args = set(sys.argv[1:])
    if "--install" in args:
        return install(False)
    if "--uninstall" in args:
        return uninstall("--dry-run" in args)
    if "--dry-run" in args:
        return install(True)
    if "--status" in args or not args:
        return status()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
