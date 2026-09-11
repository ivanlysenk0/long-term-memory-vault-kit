#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Синхронизация памяти с удалённым репозиторием. Хук события Stop.

Запускается после каждого ответа агента. Делает три вещи по порядку:

1. помечает свежие session-логи как кандидатов в knowledge
2. коммитит изменения памяти
3. отправляет их в удалённый репозиторий

Почему после КАЖДОГО ответа, а не по расписанию: правки в памяти появляются
непредсказуемо, а окно агента могут закрыть в любой момент. Хук дежурит и
срабатывает только когда есть что сохранять: при чистом дереве он молча
выходит, не трогая сеть.

Почему кандидаты помечаются ДО коммита: запись очереди должна уехать тем же
коммитом. Если пометить после, очередь отправится только со следующим ответом
агента, а при закрытии окна не отправится вовсе.

Почему это Python, а не shell: на Windows нет bash, а хук нужен всем.
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ltm_paths import find_vault, log_dir, state_dir  # noqa: E402

# Сколько ждать освобождения блокировки, секунд.
LOCK_WAIT = 30
# Замок старше этого срока считаем протухшим: процесс убили, снять некому.
LOCK_STALE = 120
# Сколько совпадений ключевых слов нужно, чтобы пометить лог кандидатом.
THRESHOLD = 3
# Признаки того, что в логе есть разбор, а не просто переписка.
TRIGGERS = [
    r"глибок\w+ аналіз", r"глубок\w+ анализ", r"deep dive",
    r"детальн\w+ розбір", r"детальн\w+ разбор",
    r"архітектур\w+", r"архитектур\w+", r"architecture",
    r"патерн", r"паттерн", r"pattern",
    r"root cause", r"корінь причини", r"корень причины",
    r"ухвалено рішення", r"принят\w+ решение", r"decision made",
    r"порівняння", r"сравнение", r"comparison",
]


def log(msg):
    try:
        d = log_dir()
        d.mkdir(parents=True, exist_ok=True)
        p = d / "ltm-sync.log"
        with p.open("a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def git(vault, *args, timeout=90):
    """Запуск git. Возвращает (код, вывод). Ошибки не бросает."""
    try:
        r = subprocess.run(["git", *args], cwd=str(vault), capture_output=True,
                           text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return 1, str(e)


def acquire_lock(vault):
    """Блокировка через mkdir: он атомарен на всех файловых системах.

    Проверка «существует ли файл» с последующим созданием таким свойством не
    обладает и сама содержит гонку.
    """
    lock = vault / ".git" / "ltm-sync.lock"
    for _ in range(LOCK_WAIT):
        try:
            lock.mkdir(parents=True)
            return lock
        except FileExistsError:
            time.sleep(1)
        except Exception:
            return None
    try:
        if time.time() - lock.stat().st_mtime > LOCK_STALE:
            lock.rmdir()
            lock.mkdir()
            return lock
    except Exception:
        pass
    return None


def scout(vault):
    """Пометить свежие session-логи кандидатами в knowledge.

    Скрипт только помечает. Страницы пишет агент, а решение принимает человек:
    автоматика не отличает разбор от случайного упоминания слова, и очередь из
    мусора перестают читать.
    """
    marked = 0
    cutoff = time.time() - 86400
    for proj in sorted(p for p in vault.iterdir() if p.is_dir()):
        sessions = proj / "sessions"
        home = proj / "00-home"
        if not sessions.is_dir() or not home.is_dir():
            continue
        queue = home / "pending-concepts.md"
        seen = queue.read_text(encoding="utf-8", errors="ignore") if queue.is_file() else ""
        for f in sorted(sessions.glob("*.md")):
            try:
                if f.stat().st_mtime < cutoff or f.stem in seen:
                    continue
                body = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            hits = [t for t in TRIGGERS if re.search(t, body, re.I)]
            if len(hits) < THRESHOLD:
                continue
            entry = (
                "\n### %s\n"
                "- Session: [[%s/sessions/%s|%s]]\n"
                "- Збігів ключових слів: %d\n"
                "- Status: pending\n"
                "- Дія: витягти концепт(и) у knowledge/<category>/, "
                "оновити index.md і log.md\n"
                % (datetime.now().strftime("%Y-%m-%d %H:%M"),
                   proj.name, f.name, f.stem, len(hits))
            )
            try:
                if not queue.is_file():
                    queue.write_text(
                        "---\ntitle: \"Кандидати в knowledge\"\n"
                        "type: pending-queue\nstatus: active\n---\n\n"
                        "# Кандидати в knowledge\n\n"
                        "Записи зі статусом `pending` чекають на компіляцію.\n",
                        encoding="utf-8")
                with queue.open("a", encoding="utf-8") as fh:
                    fh.write(entry)
                seen += f.stem
                marked += 1
            except Exception:
                continue
    return marked


def main():
    try:
        json.load(sys.stdin)
    except Exception:
        pass

    vault = find_vault()
    if vault is None or not (vault / ".git").is_dir():
        return 0

    lock = acquire_lock(vault)
    if lock is None:
        log("пропуск: синхронізація вже триває в іншому процесі")
        return 0

    try:
        marked = scout(vault)
        if marked:
            log("concept_scout: позначено кандидатів: %d" % marked)

        code, out = git(vault, "status", "--porcelain")
        if code != 0 or not out:
            return 0
        files = len(out.splitlines())

        git(vault, "add", "-A")
        rc, _ = git(vault, "commit", "-m", "auto vault sync")
        if rc != 0:
            return 0

        # Развёрнутый pull: цель rebase задана ИМЕНЕМ ветки, а не файлом
        # .git/FETCH_HEAD. Иначе параллельный fetch другого процесса
        # переписывает FETCH_HEAD списком всех веток, и rebase падает с
        # «Cannot rebase onto multiple branches».
        branch = git(vault, "rev-parse", "--abbrev-ref", "HEAD")[1] or "main"
        fail = None
        rc, out = git(vault, "fetch", "--quiet", "origin", branch)
        if rc == 0:
            rc, out = git(vault, "rebase", "--autostash", "origin/%s" % branch)
            if rc != 0:
                fail = "rebase: %s" % out.splitlines()[0][:90] if out else "rebase"
        else:
            fail = "fetch: %s" % out.splitlines()[0][:90] if out else "fetch"

        if fail is None:
            rc, out = git(vault, "push", "origin", branch)
            if rc != 0:
                fail = "push: %s" % out.splitlines()[0][:90] if out else "push"

        alert = state_dir() / "sync-alert.txt"
        if fail:
            log("ЗБІЙ синхронізації (%s), файлів %d" % (fail, files))
            try:
                alert.parent.mkdir(parents=True, exist_ok=True)
                with alert.open("a", encoding="utf-8") as f:
                    f.write("%s | %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M"), fail))
            except Exception:
                pass
        else:
            log("ok: закомічено і відправлено, файлів %d" % files)
            try:
                alert.unlink()
            except Exception:
                pass
    finally:
        try:
            lock.rmdir()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log("ПОМИЛКА: %s" % e)
        sys.exit(0)
