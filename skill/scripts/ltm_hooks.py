#!/usr/bin/env python3
"""Установка хуков долговременной памяти в Claude Code.

Хуки это то, что превращает память из папки с файлами в рабочий механизм:
без них агент каждую сессию начинает с нуля, а всё сказанное исчезает
при сжатии контекста.

Ставится ЧЕТЫРЕ события:

  SessionStart  поднимает память в контекст; после сжатия возвращает дамп
  PreCompact    сохраняет разговор до того, как контекст сожмут
  SessionEnd    страховка: ловит `/clear` и выход, если человек не сохранил сам
  Stop          коммитит и пушит память в конце сессии

Чего хуки НЕ делают. Они не решают, что достойно попасть в knowledge, и не
пишут туда страницы. Это работа агента вместе с человеком: скрипт не понимает
содержания разговора и выдал бы мусор. Хуки лишь сохраняют сырьё и напоминают
о нём.

Важно про SessionEnd. Он срабатывает только при явном завершении: `/clear`,
`/resume`, выход из приложения. Если человек просто перестал писать и оставил
окно открытым, событие не наступит НИКОГДА. Поэтому основной путь сохранения
сессии - просьба человека «сохрани сессию», а этот хук лишь страховка.

Запуск:
    python3 ltm_hooks.py --status      что стоит сейчас
    python3 ltm_hooks.py --dry-run     что будет сделано, без изменений
    python3 ltm_hooks.py --install     поставить
    python3 ltm_hooks.py --uninstall   убрать только наши хуки
"""

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

try:
    from ltm_paths import find_vault, log_dir, IS_WIN
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from ltm_paths import find_vault, log_dir, IS_WIN

SETTINGS = Path.home() / ".claude" / "settings.json"

# Наши скрипты опознаются по имени файла. Так мы отличаем свои записи от
# чужих хуков на том же событии и никогда не трогаем чужое.
OURS = {
    "SessionStart": ("ltm_session_start.py", 25, "Поднимаю долговременную память..."),
    "PreCompact":   ("ltm_precompact.py",    30, "Сохраняю сессию до сжатия контекста..."),
}

# SessionEnd намеренно НЕ ставится.
#
# Причина первая, практическая: он срабатывает только при явном завершении
# (`/clear`, `/resume`, выход из приложения). Если человек просто перестал
# писать и оставил окно открытым - а так бывает чаще всего - событие не
# наступит никогда. Хук, который молчит в основном сценарии, создаёт ложное
# чувство, что сырьё сохраняется само.
#
# Причина вторая, дубль: он читает тот же транскрипт и пишет тот же дамп, что
# и PreCompact. Проверка совпадением хешей показала побайтово одинаковые тела
# файлов под разными именами.
#
# Причина третья, каноническая: по методу Карпати сессию сохраняет человек
# командой «сохрани сессию». Что достойно попасть в knowledge, решает человек,
# а не таймер. Подробности: knowledge/decisions/hooks-are-not-karpathy-canon.
SYNC_HOOK = ("Stop", "ltm_sync.py", 60, "Синхронизирую память с GitHub...")


def scripts_dir():
    """Каталог, откуда запускаются хуки.

    Это каталог самого скрипта: хуки ставятся рядом с ним, независимо от
    того, куда пользователь развернул скил.
    """
    return Path(__file__).resolve().parent


def cmd_for(script):
    """Команда запуска.

    Путь подставляется абсолютный и в кавычках: в нём почти всегда есть
    пробелы (`Application Support`, `Mobile Documents`, `Program Files`),
    и без кавычек хук молча не запустится.
    """
    p = scripts_dir() / script
    if IS_WIN:
        # На Windows `python3` обычно отсутствует, есть `python` или лаунчер `py`.
        exe = "py -3" if shutil.which("py") else "python"
    else:
        exe = "python3"
    return '%s "%s"' % (exe, p)


def load():
    if not SETTINGS.is_file():
        return {}
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except ValueError as e:
        print("settings.json повреждён: %s" % e)
        print("Почини файл вручную, автоматически трогать его опасно.")
        raise SystemExit(1)


def describe(data):
    """Показать, что стоит сейчас, разделяя свои хуки и чужие."""
    hooks = data.get("hooks", {})
    if not hooks:
        print("  хуков нет")
        return
    known = {v[0] for v in OURS.values()} | {SYNC_HOOK[1]}
    for event in sorted(hooks):
        for g in hooks[event]:
            for h in g.get("hooks", []):
                cmd = str(h.get("command", ""))
                mine = any(k in cmd for k in known)
                tail = cmd.rsplit("/", 1)[-1].rstrip('"')[:42]
                print("  %-13s %s %s" % (event, "наш   " if mine else "чужой ", tail))


def install(dry):
    vault = find_vault()
    print("Память: %s" % (vault or "НЕ НАЙДЕНА"))
    if vault is None:
        print("")
        print("Хуки без памяти бесполезны. Сначала разверни хранилище,")
        print("либо укажи путь переменной LTM_VAULT.")
        return 1

    missing = [s for s, *_ in OURS.values() if not (scripts_dir() / s).is_file()]
    if missing:
        print("Не найдены скрипты рядом с установщиком: %s" % ", ".join(missing))
        return 1

    data = load()
    print("\nБыло:")
    describe(data)

    hooks = data.setdefault("hooks", {})
    changes = []

    for event, (script, timeout, msg) in OURS.items():
        spec = {"type": "command", "command": cmd_for(script),
                "timeout": timeout, "statusMessage": msg}
        groups = hooks.setdefault(event, [])
        found = False
        for g in groups:
            for h in g.get("hooks", []):
                if script in str(h.get("command", "")):
                    if h != {**h, **spec}:
                        changes.append("%s: обновить" % event)
                    h.update(spec)
                    found = True
        if not found:
            groups.append({"hooks": [spec]})
            changes.append("%s: добавить" % event)

    if not changes:
        print("\nВсё уже установлено и совпадает, менять нечего.")
        return 0

    print("\nБудет сделано:")
    for c in changes:
        print("  " + c)

    if dry:
        print("\nЭто предпросмотр. Ничего не изменено.")
        print("Для установки повтори с --install.")
        return 0

    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    if SETTINGS.is_file():
        bak = SETTINGS.with_name("settings.json.bak-%s"
                                 % datetime.now().strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(SETTINGS, bak)
        print("\nБэкап: %s" % bak.name)

    SETTINGS.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")

    print("\nСтало:")
    describe(load())
    print("\nХуки читаются при старте сессии: открой НОВУЮ сессию, текущая")
    print("их не подхватит. Лог: %s" % (log_dir() / "claude-ltm-hooks.log"))
    return 0


def uninstall(dry):
    data = load()
    hooks = data.get("hooks", {})
    known = {v[0] for v in OURS.values()}
    removed = []

    for event in list(hooks):
        groups = []
        for g in hooks[event]:
            kept = [h for h in g.get("hooks", [])
                    if not any(k in str(h.get("command", "")) for k in known)]
            if len(kept) != len(g.get("hooks", [])):
                removed.append(event)
            if kept:
                groups.append({**g, "hooks": kept})
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]

    if not removed:
        print("Наших хуков в настройках нет.")
        return 0

    print("Будут удалены: %s" % ", ".join(sorted(set(removed))))
    print("Чужие хуки на этих событиях остаются нетронутыми.")
    if dry:
        print("\nЭто предпросмотр. Ничего не изменено.")
        return 0

    bak = SETTINGS.with_name("settings.json.bak-%s"
                             % datetime.now().strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(SETTINGS, bak)
    SETTINGS.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    print("Готово. Бэкап: %s" % bak.name)
    return 0


def main():
    ap = argparse.ArgumentParser(description="Хуки долговременной памяти")
    ap.add_argument("--status", action="store_true", help="показать текущее состояние")
    ap.add_argument("--dry-run", action="store_true", help="показать план без изменений")
    ap.add_argument("--install", action="store_true", help="установить хуки")
    ap.add_argument("--uninstall", action="store_true", help="убрать наши хуки")
    a = ap.parse_args()

    if a.status:
        print("Файл: %s" % SETTINGS)
        print("Память: %s" % (find_vault() or "не найдена"))
        print("")
        describe(load())
        return 0
    if a.uninstall:
        return uninstall(a.dry_run)
    if a.install or a.dry_run:
        return install(dry=not a.install)

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
